#!/usr/bin/env python3
r"""
mac-sidecar/upscale/liberaro_upscale_server.py
Mac sidecar: receives upscale jobs from iPhone, dispatches to a local
upscaler binary (waifu2x-ncnn-vulkan / realcugan-ncnn-vulkan /
realesrgan-ncnn-vulkan), and serves the result for download.

Job lifecycle (matches iOS docs/upscale-routing-policy.md §5):
    queued -> processing -> done
                         \-> failed
                         \-> cancelled

Endpoints:
    GET    /health
    GET    /models              available models per engine (0.2.0+)
    GET    /jobs                all known jobs (0.3.0+, diagnostics/recovery)
    GET    /progress            aggregate queue progress + ETA (0.4.0+)
    POST   /jobs                multipart: image=<file>, meta=<json>
    GET    /jobs/{id}           status JSON
    GET    /jobs/{id}/result    binary image (only when status == done)
    DELETE /jobs/{id}           cancel or delete

Memory model (0.3.0+):
    - POST /jobs はジョブをキューに積むだけ。実行は固定数のワーカースレッド
      (LIBERARO_UPSCALE_MAX_WORKERS, 既定 1) が順に行うので、何ページ投入されても
      ncnn プロセスは同時に MAX_WORKERS 本しか走らない。
    - アップロードボディの同時バッファリングは LIBERARO_UPSCALE_MAX_CONCURRENT_UPLOADS
      (既定 2) 本に制限し、RAM 使用量を bound する。
    - サーバ再起動時、queued / processing だった job は input が残っていれば
      queued に戻して再実行する（旧動作: 一律 failed）。

Usage:
    python3 mac-sidecar/upscale/liberaro_upscale_server.py [--port 8088] [--host 127.0.0.1]

Environment:
    LIBERARO_UPSCALE_JOB_ROOT      Job storage dir (default ~/Library/Caches/LiberaroUpscaleJobs)
    LIBERARO_UPSCALE_RETENTION_SEC Retention after finish (default 86400)
    LIBERARO_UPSCALE_HTTP_LOG      Set to 1/true/yes to show raw HTTP request logs
    LIBERARO_UPSCALE_AUTH_TOKEN    Bearer token required for every endpoint
    LIBERARO_UPSCALE_AUTH_TOKEN_FILE
                                     Token file (default <JOB_ROOT>/server_token.txt)
    LIBERARO_UPSCALE_MAX_MULTIPART_BYTES
                                     Max POST /jobs body bytes (default 80MiB)
    LIBERARO_UPSCALE_MAX_IMAGE_BYTES
                                     Max uploaded image bytes (default 60MiB)
    LIBERARO_UPSCALE_MAX_WORKERS   Concurrent upscale processes (default 1)
    LIBERARO_UPSCALE_MAX_CONCURRENT_UPLOADS
                                     Concurrent request-body buffers (default 2)
    LIBERARO_UPSCALE_TILE_SIZE     ncnn tile size (-t). 0 = engine auto (default).
                                     GPU メモリ不足で落ちる場合は 200〜400 を推奨
    LIBERARO_WAIFU2X_BIN           Path to waifu2x-ncnn-vulkan binary
    LIBERARO_REALCUGAN_BIN         Path to realcugan-ncnn-vulkan binary
    LIBERARO_REALESRGAN_BIN        Path to realesrgan-ncnn-vulkan binary
    LIBERARO_*_MODELS_DIR          Override the models-* lookup directory per engine
"""

import argparse
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JOB_ROOT = Path(
    os.environ.get(
        "LIBERARO_UPSCALE_JOB_ROOT",
        str(Path.home() / "Library" / "Caches" / "LiberaroUpscaleJobs"),
    )
).expanduser()
JOB_RETENTION_SECONDS = int(
    os.environ.get("LIBERARO_UPSCALE_RETENTION_SEC", str(24 * 60 * 60))
)
JOB_ROOT.mkdir(parents=True, exist_ok=True)
DEFAULT_TOKEN_FILE = JOB_ROOT / "server_token.txt"
AUTH_TOKEN = ""
AUTH_TOKEN_FILE = Path(
    os.environ.get("LIBERARO_UPSCALE_AUTH_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))
).expanduser()
MAX_MULTIPART_BYTES = int(
    os.environ.get("LIBERARO_UPSCALE_MAX_MULTIPART_BYTES", str(80 * 1024 * 1024))
)
MAX_IMAGE_BYTES = int(
    os.environ.get("LIBERARO_UPSCALE_MAX_IMAGE_BYTES", str(60 * 1024 * 1024))
)
MAX_IMAGE_PIXELS = int(
    os.environ.get("LIBERARO_UPSCALE_MAX_IMAGE_PIXELS", str(120_000_000))
)
MAX_SCALE = int(os.environ.get("LIBERARO_UPSCALE_MAX_SCALE", "4"))
MAX_SKIP_MIN_PIXEL = int(os.environ.get("LIBERARO_UPSCALE_MAX_SKIP_MIN_PIXEL", "20000"))
# 同時に走らせる ncnn プロセス数。ncnn-vulkan は 1 プロセスで GPU ヒープを大きく確保する
# ため、既定 1。メモリに余裕がある Mac だけ 2 以上にする。
MAX_WORKERS = max(1, int(os.environ.get("LIBERARO_UPSCALE_MAX_WORKERS", "1")))
# アップロードボディを同時に RAM に持つ本数の上限（1 本あたり最大 MAX_MULTIPART_BYTES）。
MAX_CONCURRENT_UPLOADS = max(
    1, int(os.environ.get("LIBERARO_UPSCALE_MAX_CONCURRENT_UPLOADS", "2"))
)
# ncnn の -t (tile size)。0 なら指定せずエンジンの自動判定に任せる。
# 大きい画像で GPU メモリ不足になる場合は 200〜400 程度に下げると使用量が bound される。
TILE_SIZE = int(os.environ.get("LIBERARO_UPSCALE_TILE_SIZE", "0"))

SERVER_VERSION = "0.4.0"
VERBOSE_HTTP_LOG = os.environ.get("LIBERARO_UPSCALE_HTTP_LOG", "").lower() in (
    "1",
    "true",
    "yes",
    "debug",
)

# Engine -> binary path. None means "not installed"; jobs for that engine fail fast.
ENGINE_BIN = {
    "waifu2x":    os.environ.get("LIBERARO_WAIFU2X_BIN"),
    "realCUGAN":  os.environ.get("LIBERARO_REALCUGAN_BIN"),
    "realESRGAN": os.environ.get("LIBERARO_REALESRGAN_BIN"),
}

# Engine -> 探索したいモデルディレクトリの親。未指定なら binary の隣を見る。
ENGINE_MODELS_DIR = {
    "waifu2x":    os.environ.get("LIBERARO_WAIFU2X_MODELS_DIR"),
    "realCUGAN":  os.environ.get("LIBERARO_REALCUGAN_MODELS_DIR"),
    "realESRGAN": os.environ.get("LIBERARO_REALESRGAN_MODELS_DIR"),
}


def _strip_models_prefix(model_id):
    """`models-` で始まる model_id を綺麗にする。
    iOS 設定画面のリモート専用 picker で `models-pro` のように貼られても動くようにする。"""
    if not model_id:
        return ""
    if model_id.startswith("models-"):
        return model_id[len("models-"):]
    return model_id


def _models_search_root(engine):
    """指定 engine の models-* を探す親ディレクトリを返す。"""
    override = ENGINE_MODELS_DIR.get(engine)
    if override:
        return Path(override).expanduser()
    bin_path = ENGINE_BIN.get(engine)
    if not bin_path:
        return None
    return Path(bin_path).resolve().parent


def _enumerate_models(engine):
    """`models-*` ディレクトリの一覧を返す。realESRGAN は -n <name> 方式のため
    モデルファイル単位（.param/.bin の prefix）も拾う。"""
    root = _models_search_root(engine)
    if not root or not root.exists():
        return []
    if engine in ("waifu2x", "realCUGAN"):
        names = []
        for child in root.iterdir():
            if child.is_dir() and child.name.startswith("models-"):
                names.append(child.name[len("models-"):])
        return sorted(names)
    if engine == "realESRGAN":
        # realesrgan-ncnn-vulkan は <prefix>.param と <prefix>.bin の対が models-* 内にある。
        names = set()
        for child in root.rglob("*.param"):
            names.add(child.stem)
        return sorted(names)
    return []

# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------

_jobs: dict = {}
_jobs_lock = threading.Lock()
# queued ジョブの実行待ち行列。ワーカースレッド (MAX_WORKERS 本) だけが消費する。
# job オブジェクトではなく id を入れる: DELETE で registry から消えたジョブを
# ワーカーが拾って走らせてしまわないようにするため。
_job_queue: "queue.Queue[str]" = queue.Queue()
# アップロードボディの同時バッファリング制限（RAM 使用量の bound）。
_upload_slots = threading.BoundedSemaphore(MAX_CONCURRENT_UPLOADS)


def _enqueue_job(job):
    _job_queue.put(job["id"])


def _worker_loop():
    while True:
        job_id = _job_queue.get()
        job = _get_job(job_id)
        if job is None:
            continue  # deleted while queued
        with _jobs_lock:
            if job.get("status") != "queued":
                continue  # cancelled while queued
        try:
            _run_job(job)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            _log(f"worker error on job {job_id[:8]}: {exc}")


def _now_ts():
    return time.time()


def _log(message):
    sys.stderr.write(f"[{time.strftime('%d/%b/%Y %H:%M:%S')}] {message}\n")


def _configure_auth(token=None, token_file=None):
    global AUTH_TOKEN, AUTH_TOKEN_FILE

    if token_file:
        AUTH_TOKEN_FILE = Path(token_file).expanduser()

    configured = (token or os.environ.get("LIBERARO_UPSCALE_AUTH_TOKEN") or "").strip()
    if configured:
        AUTH_TOKEN = configured
        return AUTH_TOKEN

    if AUTH_TOKEN_FILE.exists():
        existing = AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if existing:
            AUTH_TOKEN = existing
            return AUTH_TOKEN

    AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_TOKEN = secrets.token_urlsafe(32)
    AUTH_TOKEN_FILE.write_text(AUTH_TOKEN + "\n", encoding="utf-8")
    try:
        os.chmod(AUTH_TOKEN_FILE, 0o600)
    except OSError:
        pass
    return AUTH_TOKEN


def _extract_request_token(headers):
    auth = (headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("X-Liberaro-Token") or "").strip()


def _is_authorized_headers(headers):
    if not AUTH_TOKEN:
        return False
    supplied = _extract_request_token(headers)
    return bool(supplied) and hmac.compare_digest(supplied, AUTH_TOKEN)


def _short_job_id(job):
    return str(job.get("id", ""))[:8]


def _job_label(job):
    return (
        f"job {_short_job_id(job)} "
        f"engine={job.get('engine', '')} "
        f"model={job.get('model_id', '') or '(default)'} "
        f"scale={job.get('scale', '')} "
        f"noise={job.get('noise', '')}"
    )


def _persist_job_unlocked(job):
    """Persist job metadata under <JOB_ROOT>/<id>/job.json atomically."""
    job_dir = Path(job["tmp_dir"])
    job_dir.mkdir(parents=True, exist_ok=True)
    state_path = job_dir / "job.json"
    payload = {k: job.get(k) for k in (
        "id", "status", "error",
        "engine", "model_id", "scale", "noise", "no_upscale", "skip_min_pixel",
        "tmp_dir", "created_at", "updated_at", "finished_at",
        "cancel_requested",
    )}
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(state_path)


def _set_job_status(job, status, error=None, finished=False):
    old_status = job.get("status")
    job["status"] = status
    job["updated_at"] = _now_ts()
    if error is not None:
        job["error"] = error
    if finished:
        job["finished_at"] = _now_ts()
    _persist_job_unlocked(job)
    if old_status != status:
        elapsed = ""
        if job.get("created_at"):
            elapsed = f" elapsed={job['updated_at'] - job['created_at']:.1f}s"
        if status == "failed" and job.get("error"):
            _log(f"{_job_label(job)} -> failed{elapsed}: {str(job['error'])[:240]}")
        else:
            _log(f"{_job_label(job)} -> {status}{elapsed}")


def _new_job(meta, input_path):
    """Create a job from parsed meta and a path to the uploaded source image."""
    job_id = uuid.uuid4().hex
    tmp_dir = JOB_ROOT / job_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    now = _now_ts()
    job = {
        "id": job_id,
        "status": "queued",
        "error": None,
        "engine":         str(meta.get("engine", "")),
        "model_id":       str(meta.get("modelID", "")),
        "scale":          int(meta.get("scale", 1)),
        "noise":          int(meta.get("noise", -1)),
        "no_upscale":     bool(meta.get("noUpscale", False)),
        "skip_min_pixel": int(meta.get("skipMinPixel", 0)),
        "tmp_dir": str(tmp_dir),
        "input_path": str(input_path),
        "output_path": str(tmp_dir / "result.png"),
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "cancel_requested": False,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        _persist_job_unlocked(job)
    _log(f"{_job_label(job)} -> queued")
    return job


def _get_job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def _delete_job(job_id):
    """Remove a job from registry and disk."""
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["tmp_dir"], ignore_errors=True)
    return job is not None


def _request_job_cancel(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return False
        if job["status"] in ("done", "failed", "cancelled"):
            return False
        job["cancel_requested"] = True
        if job["status"] == "queued":
            _set_job_status(job, "cancelled", finished=True)
        else:
            _persist_job_unlocked(job)
        return True


def _prune_finished_jobs():
    """Remove jobs that finished more than JOB_RETENTION_SECONDS ago."""
    now = _now_ts()
    stale = []
    with _jobs_lock:
        for job_id, job in list(_jobs.items()):
            finished_at = job.get("finished_at")
            if finished_at and (now - finished_at) > JOB_RETENTION_SECONDS:
                stale.append(job_id)
    for job_id in stale:
        _delete_job(job_id)


def _queue_progress_snapshot():
    """全ジョブを集計して「全体の進捗」を返す。

    iOS 側は個別ジョブの `/jobs/{id}` を回収しつつ、この `/progress` を軽くポーリングして
    「全体であと何枚 / だいたい何秒で終わるか」をユーザーに提示する。

    ETA は完了実績（`finished_at - created_at`）の直近サンプル平均 × 残り件数の直列近似。
    workers=1 前提。完了実績が無ければ avg/eta は None（不明）。
    """
    counts = {
        "queued": 0,
        "processing": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
    }
    # (finished_at, duration) を集めて直近サンプルの平均を取る。
    durations = []
    with _jobs_lock:
        for job in _jobs.values():
            status = job.get("status")
            if status in counts:
                counts[status] += 1
            if status == "done":
                created = job.get("created_at")
                finished = job.get("finished_at")
                if created and finished and finished >= created:
                    durations.append((finished, finished - created))
    total = sum(counts.values())
    remaining = counts["queued"] + counts["processing"]

    avg_seconds = None
    eta_seconds = None
    if durations:
        durations.sort(key=lambda pair: pair[0])
        recent = [d for _, d in durations[-20:]]
        if recent:
            avg_seconds = sum(recent) / len(recent)
            eta_seconds = avg_seconds * remaining

    return {
        "total": total,
        "queued": counts["queued"],
        "processing": counts["processing"],
        "done": counts["done"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "remaining": remaining,
        "avgSeconds": avg_seconds,
        "etaSeconds": eta_seconds,
        "updatedAt": _now_ts(),
    }


def _restore_jobs_from_disk():
    """サーバ起動時にディスクに残った job.json を再読込する。

    - queued / processing は input.bin が残っていれば queued に戻して再実行する
      （入力・パラメータは全て永続化済みなので再開可能）。
      input が消えている場合のみ failed に倒す。
    - done / failed / cancelled は復元し、reaper が retention で順次掃除する。

    これにより、長時間のバッチ中にサーバ (や Mac 自体) が落ちても、
    再起動すれば残りのジョブが自動で流れ、iOS 側は同じ jobID で結果を回収できる。

    戻り値: 再実行のために queued へ戻したジョブのリスト（main が enqueue する）。
    """
    if not JOB_ROOT.exists():
        return []
    restored = 0
    requeued = []
    abandoned = 0
    for job_dir in sorted(JOB_ROOT.iterdir()):
        if not job_dir.is_dir() or job_dir.name == "_staging":
            continue
        state_path = job_dir / "job.json"
        if not state_path.exists():
            continue
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        job_id = data.get("id")
        if not job_id:
            continue
        # 入出力パスを今のディスクレイアウトに合わせ直す（移動された場合の安全策）。
        data.setdefault("tmp_dir", str(job_dir))
        data.setdefault("input_path", str(job_dir / "input.bin"))
        data.setdefault("output_path", str(job_dir / "result.png"))
        if data.get("status") in ("queued", "processing"):
            if data.get("cancel_requested"):
                data["status"] = "cancelled"
                data["finished_at"] = _now_ts()
                data["updated_at"] = _now_ts()
                abandoned += 1
            elif Path(data["input_path"]).exists():
                data["status"] = "queued"
                data["error"] = None
                data["finished_at"] = None
                data["updated_at"] = _now_ts()
                requeued.append(data)
            else:
                data["status"] = "failed"
                data["error"] = "server restarted and job input was lost"
                data["finished_at"] = _now_ts()
                data["updated_at"] = _now_ts()
                abandoned += 1
        else:
            restored += 1
        with _jobs_lock:
            _jobs[job_id] = data
            _persist_job_unlocked(data)
    sys.stderr.write(
        f"[restore] {restored} terminal jobs restored, "
        f"{len(requeued)} in-flight jobs requeued, {abandoned} abandoned\n"
    )
    # 再実行順は作成時刻順（≒ ページ順）に揃える。
    requeued.sort(key=lambda j: j.get("created_at") or 0)
    return requeued


# ---------------------------------------------------------------------------
# Upscale execution
# ---------------------------------------------------------------------------

def _png_size(path):
    try:
        with open(path, "rb") as f:
            data = f.read(24)
    except OSError:
        return None
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return None


def _image_size(path):
    png = _png_size(path)
    if png:
        return png
    try:
        completed = subprocess.run(
            ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    width = height = None
    for line in completed.stdout.splitlines():
        if "pixelWidth:" in line:
            width = int(line.rsplit(":", 1)[1].strip())
        elif "pixelHeight:" in line:
            height = int(line.rsplit(":", 1)[1].strip())
    if width and height:
        return width, height
    return None


def _bounded_int(value, key, default, minimum, maximum):
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return number


def _validate_job_meta(meta):
    if not isinstance(meta, dict):
        raise ValueError("meta must be a JSON object")
    _bounded_int(meta.get("scale"), "scale", 1, 1, MAX_SCALE)
    _bounded_int(meta.get("skipMinPixel"), "skipMinPixel", 0, 0, MAX_SKIP_MIN_PIXEL)


def _validate_uploaded_image(path, byte_count):
    if byte_count <= 0:
        raise ValueError("image is empty")
    if byte_count > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds MAX_IMAGE_BYTES ({byte_count} > {MAX_IMAGE_BYTES})")
    size = _image_size(path)
    if not size:
        raise ValueError("image is not a readable image")
    width, height = size
    pixels = int(width) * int(height)
    if pixels <= 0:
        raise ValueError("image has invalid dimensions")
    if pixels > MAX_IMAGE_PIXELS:
        raise ValueError(f"image exceeds MAX_IMAGE_PIXELS ({pixels} > {MAX_IMAGE_PIXELS})")


def _should_skip_job(job):
    skip = int(job.get("skip_min_pixel") or 0)
    if skip <= 0:
        return False
    size = _image_size(job["input_path"])
    if not size:
        return False
    return max(size) >= skip


def _resize_output_to_input_size(job):
    size = _image_size(job["input_path"])
    if not size:
        return
    width, height = size
    output_path = Path(job["output_path"])
    if _image_size(output_path) == size:
        return
    resized_path = output_path.with_name(output_path.stem + "-resized.png")
    completed = subprocess.run(
        ["/usr/bin/sips", "-z", str(height), str(width), str(output_path), "--out", str(resized_path)],
        text=True,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0 or not resized_path.exists():
        detail = (completed.stderr or completed.stdout or "").strip()[:500]
        raise RuntimeError(f"noUpscale resize failed: {detail}")
    resized_path.replace(output_path)


def _build_command(job):
    """Return argv for the engine, or raise RuntimeError if engine is unsupported."""
    engine = job["engine"]
    bin_path = ENGINE_BIN.get(engine)
    if not bin_path:
        raise RuntimeError(f"engine '{engine}' is not configured on this Mac")
    if not Path(bin_path).exists():
        raise RuntimeError(f"engine binary not found: {bin_path}")

    builder = _ENGINE_BUILDERS.get(engine)
    if not builder:
        raise RuntimeError(f"unsupported engine: {engine}")
    return builder(bin_path, job)


def _resolve_models_root(engine, bin_path):
    """エンジンのモデル探索ルート (絶対パス) を決定する。
    LIBERARO_<ENGINE>_MODELS_DIR 環境変数を最優先、無ければバイナリ隣を見る。
    CWD 依存だと subprocess 起動時に models が見つからず segfault する事故を防ぐ目的。"""
    override = ENGINE_MODELS_DIR.get(engine)
    if override:
        return Path(override).expanduser().resolve()
    return Path(bin_path).resolve().parent


def _tile_args():
    """ncnn 系バイナリ共通の -t (tile size) 引数。

    0 (既定) はエンジンの自動判定（GPU ヒープから逆算）に任せる。
    自動判定が大きすぎて GPU メモリ不足で落ちる Mac では
    LIBERARO_UPSCALE_TILE_SIZE=200〜400 を指定すると使用量が bound される。"""
    if TILE_SIZE > 0:
        return ["-t", str(TILE_SIZE)]
    return []


def _build_waifu2x(bin_path, job):
    """waifu2x-ncnn-vulkan の引数組み立て.

    CLI 概要:
        -i <input> -o <output>
        -n <-1|0|1|2|3>         ノイズ除去レベル (-1: off)
        -s <1|2>                スケール (1 ならノイズ除去のみ)
        -m <absolute>/models-cunet | ... (絶対パスで渡す)
    """
    argv = [bin_path, "-i", job["input_path"], "-o", job["output_path"]]
    argv += ["-s", str(max(1, job["scale"]))]
    argv += _tile_args()
    noise = job["noise"]
    if -1 <= noise <= 3:
        argv += ["-n", str(noise)]
    model = _strip_models_prefix(job["model_id"])
    if model:
        models_root = _resolve_models_root("waifu2x", bin_path)
        argv += ["-m", str(models_root / f"models-{model}")]
    return argv


def _build_real_cugan(bin_path, job):
    """realcugan-ncnn-vulkan の引数組み立て.

    CLI 概要:
        -i <input> -o <output>
        -n <-1|0|3>             denoise レベル（-1: off, 0: 軽, 3: 強）
        -s <2|3|4>              倍率
        -m <absolute>/models-se | ... (絶対パスで渡す)
    """
    argv = [bin_path, "-i", job["input_path"], "-o", job["output_path"]]
    argv += ["-s", str(max(2, job["scale"]))]
    argv += _tile_args()
    noise = job["noise"]
    if noise in (-1, 0, 3):
        argv += ["-n", str(noise)]
    model = _strip_models_prefix(job["model_id"])
    if model:
        models_root = _resolve_models_root("realCUGAN", bin_path)
        argv += ["-m", str(models_root / f"models-{model}")]
    return argv


def _build_real_esrgan(bin_path, job):
    """realesrgan-ncnn-vulkan の引数組み立て.

    CLI 概要:
        -i <input> -o <output>
        -s <scale>
        -m <models_dir> (絶対パス、内部に <name>.param/.bin が並ぶ)
        -n <name>       models 内の .param/.bin の prefix
    realESRGAN は -m がモデル**親ディレクトリ**、-n が prefix。CWD 依存で
    models を見失うと segfault するので -m は必ず絶対パスで渡す。
    """
    argv = [bin_path, "-i", job["input_path"], "-o", job["output_path"]]
    argv += ["-s", str(max(2, job["scale"]))]
    argv += _tile_args()
    models_root = _resolve_models_root("realESRGAN", bin_path)
    models_dir = models_root / "models"
    if models_dir.exists():
        argv += ["-m", str(models_dir)]
    model = _strip_models_prefix(job["model_id"])
    if model:
        argv += ["-n", model]
    return argv


_ENGINE_BUILDERS = {
    "waifu2x":    _build_waifu2x,
    "realCUGAN":  _build_real_cugan,
    "realESRGAN": _build_real_esrgan,
}


def _run_job(job):
    """Run the upscale; called in a worker thread."""
    try:
        with _jobs_lock:
            if job.get("cancel_requested"):
                _set_job_status(job, "cancelled", finished=True)
                return
            _set_job_status(job, "processing")

        if _should_skip_job(job):
            shutil.copyfile(job["input_path"], job["output_path"])
            with _jobs_lock:
                _set_job_status(job, "done", finished=True)
            return

        argv = _build_command(job)
        # No shell=True; we want predictable arg passing.
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Cooperative cancel: poll proc + cancel flag.
        while True:
            try:
                ret = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if job.get("cancel_requested"):
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    with _jobs_lock:
                        _set_job_status(job, "cancelled", finished=True)
                    return

        if ret != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            with _jobs_lock:
                _set_job_status(job, "failed",
                                error=f"exit={ret}: {stderr.strip()[:500]}",
                                finished=True)
            return

        if not Path(job["output_path"]).exists():
            with _jobs_lock:
                _set_job_status(job, "failed",
                                error="binary returned 0 but output file is missing",
                                finished=True)
            return

        if job.get("no_upscale"):
            _resize_output_to_input_size(job)

        with _jobs_lock:
            _set_job_status(job, "done", finished=True)

    except Exception as exc:  # noqa: BLE001 - surface any failure to the iOS client
        with _jobs_lock:
            _set_job_status(job, "failed", error=str(exc), finished=True)


# ---------------------------------------------------------------------------
# Multipart parser (stdlib only)
# ---------------------------------------------------------------------------
# Python 3.13 で `cgi` モジュールが削除されたため、自前で multipart/form-data
# を最小限解釈する。RFC 7578 のうち本サーバが扱う 2 パート (`image` ファイル +
# `meta` JSON テキスト) を取り出せれば足りる前提。chunked encoding や Base64
# 等の transfer-encoding はサポートしない（iOS 側クライアントが使わない）。


def _extract_boundary(content_type):
    """`multipart/form-data; boundary=xxx` から boundary を取り出す。
    `boundary="xxx"` のクォート付きにも対応。"""
    m = re.search(r'boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))', content_type)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _read_exact(fp, n):
    """fp から n バイト確実に読む。"""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = fp.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_multipart(body, boundary):
    """multipart body をパースして `{name: {"filename":..., "content_type":..., "body": bytes}}` を返す。

    エラーパターン (ValueError):
      - boundary が body に見つからない
      - パートに Content-Disposition: form-data; name=... が無い
    """
    boundary_bytes = ("--" + boundary).encode("ascii")
    # 末尾区切り `--boundary--` を取り除く。RFC では末尾に CRLF が付くこともある。
    end_marker = boundary_bytes + b"--"
    end_pos = body.rfind(end_marker)
    if end_pos == -1:
        raise ValueError("closing boundary not found")
    body = body[:end_pos]

    parts = {}
    # 個々のパートを境界で分割
    raw_parts = body.split(boundary_bytes)
    for raw in raw_parts:
        # 先頭 CRLF と末尾 CRLF を剥がす
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        if not raw:
            continue
        # ヘッダブロックと本文を CRLFCRLF で分割
        try:
            header_blob, payload = raw.split(b"\r\n\r\n", 1)
        except ValueError:
            # ヘッダだけで本文無しのパートはスキップ
            continue
        # ヘッダを 1 行ずつパース
        headers = {}
        for line in header_blob.split(b"\r\n"):
            if not line:
                continue
            if b":" not in line:
                continue
            k, v = line.split(b":", 1)
            headers[k.strip().lower().decode("ascii", errors="replace")] = \
                v.strip().decode("ascii", errors="replace")
        disposition = headers.get("content-disposition", "")
        # name="image" を取り出す
        name_match = re.search(r'name\s*=\s*"([^"]+)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename\s*=\s*"([^"]*)"', disposition)
        filename = filename_match.group(1) if filename_match else None
        parts[name] = {
            "filename": filename,
            "content_type": headers.get("content-type", "application/octet-stream"),
            "body": payload,
        }
    return parts


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = f"LiberaroUpscale/{SERVER_VERSION}"

    def log_message(self, fmt, *args):
        if VERBOSE_HTTP_LOG:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- helpers ----

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_binary(self, status, data, content_type="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _require_auth(self):
        if _is_authorized_headers(self.headers):
            return True
        self.close_connection = True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _send_pooled_file(self, status, path, content_type="application/octet-stream"):
        """Send a pooled result file without consuming it.

        If the iPhone disconnects mid-download, the file remains under the job
        directory and the same `/jobs/{id}/result` URL can be fetched again
        until retention cleanup or explicit DELETE.
        """
        size = path.stat().st_size
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Liberaro-Result-Retained", "true")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except (BrokenPipeError, ConnectionResetError) as exc:
            _log(f"result send interrupted path={path.name} bytes={size}: {exc}; retained for refetch")

    def _job_status_payload(self, job):
        result_available = False
        result_bytes = None
        if job.get("status") == "done":
            out = Path(job.get("output_path", ""))
            if out.exists():
                result_available = True
                result_bytes = out.stat().st_size
        return {
            "id":           job["id"],
            "status":       job["status"],
            "error":        job.get("error"),
            "engine":       job["engine"],
            "modelID":      job["model_id"],
            "scale":        job["scale"],
            "noise":        job["noise"],
            "noUpscale":    job["no_upscale"],
            "skipMinPixel": job["skip_min_pixel"],
            "createdAt":    job["created_at"],
            "updatedAt":    job["updated_at"],
            "finishedAt":   job.get("finished_at"),
            "resultAvailable": result_available,
            "resultBytes": result_bytes,
            "retentionSeconds": JOB_RETENTION_SECONDS,
        }

    # ---- routing ----

    def do_GET(self):
        if not self._require_auth():
            return

        if self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "version": SERVER_VERSION,
                "engines": {k: bool(v) for k, v in ENGINE_BIN.items()},
            })
            return

        if self.path in ("/jobs", "/jobs/"):
            # 全ジョブの状態一覧。iOS 側の再接続時リカバリ・診断用。
            with _jobs_lock:
                jobs = sorted(
                    _jobs.values(),
                    key=lambda j: j.get("created_at") or 0,
                    reverse=True,
                )
                payloads = [self._job_status_payload(j) for j in jobs]
            self._send_json(200, {"jobs": payloads})
            return

        if self.path in ("/progress", "/progress/"):
            # キュー全体の集計（0.4.0+）。iOS 側で「Mac 全体: 残り N枚 / 約M分」を出すために使う。
            self._send_json(200, _queue_progress_snapshot())
            return

        if self.path == "/models":
            # 各 engine のバイナリ隣（または LIBERARO_*_MODELS_DIR）を走査して
            # 利用可能なモデル名一覧を返す。iOS 設定画面の「リモート専用モデル」入力欄や
            # トラブルシュート用。
            models = {}
            for engine in ENGINE_BIN.keys():
                if not ENGINE_BIN.get(engine):
                    models[engine] = []
                    continue
                models[engine] = _enumerate_models(engine)
            self._send_json(200, {"models": models})
            return

        if self.path.startswith("/jobs/"):
            tail = self.path[len("/jobs/"):]
            if "/" in tail:
                job_id, action = tail.split("/", 1)
            else:
                job_id, action = tail, ""

            job = _get_job(job_id)
            if not job:
                self._send_json(404, {"error": "job not found"})
                return

            if action == "":
                self._send_json(200, self._job_status_payload(job))
                return
            if action == "result":
                if job["status"] != "done":
                    self._send_json(409, {"error": f"job is {job['status']}, not done"})
                    return
                out = Path(job["output_path"])
                if not out.exists():
                    self._send_json(500, {"error": "result file missing"})
                    return
                self._send_pooled_file(200, out, content_type="image/png")
                return

            self._send_json(404, {"error": "unknown action"})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._require_auth():
            return

        if self.path != "/jobs":
            self._send_json(404, {"error": "not found"})
            return

        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._send_json(400, {"error": "expected multipart/form-data with 'image' and 'meta' fields"})
            return

        # boundary を Content-Type から取り出す
        boundary = _extract_boundary(ctype)
        if not boundary:
            self._send_json(400, {"error": "multipart boundary not found in Content-Type"})
            return

        # Content-Length 分のボディを読む。
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._send_json(400, {"error": "missing or zero Content-Length"})
            return
        if content_length > MAX_MULTIPART_BYTES:
            self.close_connection = True
            self._send_json(413, {
                "error": f"multipart body exceeds limit ({content_length} > {MAX_MULTIPART_BYTES})"
            })
            return

        # ボディの RAM バッファリングを MAX_CONCURRENT_UPLOADS 本に制限する。
        # 大量ページの一括投入時にアップロードが多重化しても、ボディ×コピーで
        # メモリが積み上がらないようにバックプレッシャをかける。
        if not _upload_slots.acquire(timeout=120):
            self.close_connection = True
            self._send_json(503, {"error": "server busy receiving uploads; retry"})
            return
        try:
            body = _read_exact(self.rfile, content_length)

            try:
                parts = _parse_multipart(body, boundary)
            except ValueError as exc:
                self._send_json(400, {"error": f"failed to parse multipart: {exc}"})
                return
            finally:
                del body

            if "image" not in parts or "meta" not in parts:
                self._send_json(400, {"error": "missing 'image' or 'meta' field"})
                return

            try:
                meta = json.loads(parts["meta"]["body"])
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": f"meta is not valid JSON: {exc}"})
                return
            try:
                _validate_job_meta(meta)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return

            image_bytes = parts["image"]["body"]
            if not image_bytes:
                self._send_json(400, {"error": "'image' part is empty"})
                return
            if len(image_bytes) > MAX_IMAGE_BYTES:
                self._send_json(413, {
                    "error": f"image exceeds limit ({len(image_bytes)} > {MAX_IMAGE_BYTES})"
                })
                return

            # Stage the input file first, then create the job pointing at it.
            staging_dir = JOB_ROOT / "_staging"
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged_path = staging_dir / f"{uuid.uuid4().hex}.bin"
            with open(staged_path, "wb") as out:
                out.write(image_bytes)
            image_byte_count = len(image_bytes)
            del parts, image_bytes
        finally:
            _upload_slots.release()

        try:
            _validate_uploaded_image(staged_path, image_byte_count)
        except ValueError as exc:
            staged_path.unlink(missing_ok=True)
            self._send_json(400, {"error": str(exc)})
            return

        job = _new_job(meta, staged_path)
        # Move staged file into job dir so retention cleanup gets it too.
        final_input = Path(job["tmp_dir"]) / "input.bin"
        shutil.move(str(staged_path), str(final_input))
        with _jobs_lock:
            job["input_path"] = str(final_input)
            _persist_job_unlocked(job)

        _enqueue_job(job)
        self._send_json(201, self._job_status_payload(job))

    def do_DELETE(self):
        if not self._require_auth():
            return

        if not self.path.startswith("/jobs/"):
            self._send_json(404, {"error": "not found"})
            return
        job_id = self.path[len("/jobs/"):].split("/", 1)[0]
        job = _get_job(job_id)
        if not job:
            self._send_json(404, {"error": "job not found"})
            return
        if job["status"] in ("done", "failed", "cancelled"):
            _delete_job(job_id)
            self._send_json(200, {"deleted": True})
        else:
            _request_job_cancel(job_id)
            self._send_json(202, {"cancelling": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--token", type=str, default=None, help="Auth token. Defaults to env/file/generated token")
    parser.add_argument(
        "--token-file",
        type=str,
        default=str(DEFAULT_TOKEN_FILE),
        help=f"Auth token file (default: {DEFAULT_TOKEN_FILE})",
    )
    args = parser.parse_args()
    token = _configure_auth(token=args.token, token_file=args.token_file)

    def _reaper():
        while True:
            try:
                _prune_finished_jobs()
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"reaper error: {exc}\n")
            time.sleep(300)

    requeued = _restore_jobs_from_disk()
    for job in requeued:
        _enqueue_job(job)
    threading.Thread(target=_reaper, daemon=True).start()
    for i in range(MAX_WORKERS):
        threading.Thread(target=_worker_loop, daemon=True, name=f"upscale-worker-{i}").start()

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    sys.stderr.write(
        f"liberaro_upscale_server {SERVER_VERSION} listening on "
        f"{args.host}:{args.port} (jobs in {JOB_ROOT})\n"
    )
    sys.stderr.write(
        f"workers: {MAX_WORKERS}, tile size: {TILE_SIZE or 'auto'}, "
        f"max concurrent uploads: {MAX_CONCURRENT_UPLOADS}\n"
    )
    sys.stderr.write(f"auth token file: {AUTH_TOKEN_FILE}\n")
    sys.stderr.write(f"auth token: {token}\n")
    sys.stderr.write(
        "engines: "
        + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in ENGINE_BIN.items())
        + "\n"
    )
    # 起動時にモデル一覧をログに出しておくと、CLI から `curl /models` で確認する手間が省ける。
    for engine in ENGINE_BIN.keys():
        if not ENGINE_BIN.get(engine):
            continue
        models = _enumerate_models(engine)
        if models:
            sys.stderr.write(f"  models[{engine}]: {', '.join(models)}\n")
        else:
            sys.stderr.write(f"  models[{engine}]: (none detected)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("shutting down\n")


if __name__ == "__main__":
    main()
