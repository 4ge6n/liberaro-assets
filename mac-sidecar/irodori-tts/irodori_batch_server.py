#!/usr/bin/env python3
"""
irodori_batch_server.py
Mac sidecar: receives batch TTS jobs from iPhone, synthesizes each chunk
via the local Irodori Gradio server, converts WAV -> M4A via afconvert,
and serves the results for download.

Usage: python3 irodori_batch_server.py [--port 9988] [--host 127.0.0.1]
"""

import argparse
import base64
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------

JOB_ROOT = Path(
    os.environ.get(
        "IRODORI_BATCH_JOB_ROOT",
        str(Path.home() / "Library" / "Caches" / "LiberaroIrodoriBatchJobs"),
    )
).expanduser()
JOB_RETENTION_SECONDS = int(
    os.environ.get("IRODORI_BATCH_RETENTION_SECONDS", str(24 * 60 * 60))
)

JOB_ROOT.mkdir(parents=True, exist_ok=True)
DEFAULT_TOKEN_FILE = JOB_ROOT / "server_token.txt"
AUTH_TOKEN = ""
AUTH_TOKEN_FILE = DEFAULT_TOKEN_FILE
ALLOWED_GRADIO_PORTS = {
    int(port.strip())
    for port in os.environ.get("IRODORI_ALLOWED_GRADIO_PORTS", "7860,7861").split(",")
    if port.strip()
}
ALLOWED_INFO_PATHS = {"/gradio_api/info", "/config"}
ALLOWED_RUNTIME_PATHS = {"/gradio_api/run/_describe_runtime"}
ALLOWED_RUN_PATHS = {"/gradio_api/run/_run_generation"}
ALLOWED_UPLOAD_PATHS = {"/gradio_api/upload", "/upload"}
ALLOWED_FILE_PROXY_PREFIXES = {"/gradio_api/file=", "/file="}

_jobs: dict = {}
_jobs_lock = threading.Lock()


def _now_ts():
    return time.time()


def _chunk_key(chapter_index, chunk_index):
    return f"{int(chapter_index)}_{int(chunk_index)}"


def _configure_auth(token=None, token_file=None):
    global AUTH_TOKEN, AUTH_TOKEN_FILE

    if token_file:
        AUTH_TOKEN_FILE = Path(token_file).expanduser()

    configured = (token or os.environ.get("IRODORI_BATCH_AUTH_TOKEN") or "").strip()
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


def _is_loopback_host(host):
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize_gradio_base_url(raw_url):
    parsed = urllib.parse.urlparse(str(raw_url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("gradio_url must use http or https")
    if not _is_loopback_host(parsed.hostname):
        raise ValueError("gradio_url must point to localhost/loopback")
    if parsed.username or parsed.password:
        raise ValueError("gradio_url must not include credentials")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("gradio_url must be an origin without path/query")
    if parsed.port not in ALLOWED_GRADIO_PORTS:
        allowed = ", ".join(str(port) for port in sorted(ALLOWED_GRADIO_PORTS))
        raise ValueError(f"gradio_url port must be one of: {allowed}")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def _validate_gradio_path(name, raw_path, allowed_paths):
    path = str(raw_path or "").strip()
    parsed = urllib.parse.urlparse(path)
    if parsed.scheme or parsed.netloc or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must be a relative path from the allowlist")
    if path not in allowed_paths:
        allowed = ", ".join(sorted(allowed_paths))
        raise ValueError(f"{name} must be one of: {allowed}")
    return path


def _join_gradio_url(gradio_url, path):
    return gradio_url.rstrip("/") + path


def _same_origin(url, base_url):
    parsed = urllib.parse.urlparse(url)
    base = urllib.parse.urlparse(base_url)
    return (
        parsed.scheme == base.scheme
        and parsed.hostname == base.hostname
        and parsed.port == base.port
    )


def _persist_job_unlocked(job):
    job_dir = Path(job["tmp_dir"])
    job_dir.mkdir(parents=True, exist_ok=True)
    state_path = job_dir / "job.json"
    payload = {
        "id": job["id"],
        "status": job["status"],
        "total": job["total"],
        "completed_chunks": job["completed_chunks"],
        "failed_chunks": job["failed_chunks"],
        "error": job.get("error"),
        "tmp_dir": job["tmp_dir"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "finished_at": job.get("finished_at"),
        "cancel_requested": bool(job.get("cancel_requested", False)),
        "delete_requested": bool(job.get("delete_requested", False)),
    }
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(state_path)


def _discard_sensitive_payload(job):
    try:
        (Path(job["tmp_dir"]) / "payload.json").unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _remove_chunk_result_unlocked(bucket, chapter_index, chunk_index):
    key = _chunk_key(chapter_index, chunk_index)
    bucket[:] = [
        item
        for item in bucket
        if _chunk_key(item["chapter_index"], item["chunk_index"]) != key
    ]


def _mark_chunk_completed(job, chapter_index, chunk_index):
    with _jobs_lock:
        _remove_chunk_result_unlocked(job["failed_chunks"], chapter_index, chunk_index)
        _remove_chunk_result_unlocked(job["completed_chunks"], chapter_index, chunk_index)
        job["completed_chunks"].append(
            {"chapter_index": int(chapter_index), "chunk_index": int(chunk_index)}
        )
        job["updated_at"] = _now_ts()
        _persist_job_unlocked(job)


def _mark_chunk_failed(job, chapter_index, chunk_index, error_message):
    with _jobs_lock:
        _remove_chunk_result_unlocked(job["completed_chunks"], chapter_index, chunk_index)
        _remove_chunk_result_unlocked(job["failed_chunks"], chapter_index, chunk_index)
        job["failed_chunks"].append(
            {
                "chapter_index": int(chapter_index),
                "chunk_index": int(chunk_index),
                "error": str(error_message),
            }
        )
        job["updated_at"] = _now_ts()
        _persist_job_unlocked(job)


def _set_job_status(job, status, error=None, finished=False):
    with _jobs_lock:
        job["status"] = status
        job["error"] = None if error is None else str(error)
        job["updated_at"] = _now_ts()
        job["finished_at"] = job["updated_at"] if finished else None
        if finished:
            _discard_sensitive_payload(job)
        _persist_job_unlocked(job)


def _new_job(payload):
    job_id = str(uuid.uuid4())
    job_dir = JOB_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    now = _now_ts()
    job = {
        "id": job_id,
        "status": "pending",
        "total": 0,
        "completed_chunks": [],
        "failed_chunks": [],
        "error": None,
        "tmp_dir": str(job_dir),
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "cancel_requested": False,
        "delete_requested": False,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        _persist_job_unlocked(job)
    return job


def _get_job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def _delete_job(job_id):
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["tmp_dir"], ignore_errors=True)


def _request_job_cancel(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, "not_found"

        if job["status"] in ("completed", "failed", "cancelled"):
            pass
        else:
            job["cancel_requested"] = True
            job["delete_requested"] = True
            job["updated_at"] = _now_ts()
            _persist_job_unlocked(job)
            return True, "cancelling"

    _delete_job(job_id)
    return True, "deleted"


def _job_cancel_requested(job):
    with _jobs_lock:
        return bool(job.get("cancel_requested", False))


def _prune_finished_jobs():
    now = _now_ts()
    stale_ids = []
    with _jobs_lock:
        for job_id, job in _jobs.items():
            if job["status"] not in ("completed", "failed", "cancelled"):
                continue
            finished_at = job.get("finished_at") or job.get("updated_at") or now
            if now - float(finished_at) >= JOB_RETENTION_SECONDS:
                stale_ids.append(job_id)
    for job_id in stale_ids:
        _delete_job(job_id)
    _prune_finished_job_dirs_on_disk(now=now)


def _prune_finished_job_dirs_on_disk(now=None):
    now = _now_ts() if now is None else now
    if not JOB_ROOT.exists():
        return
    with _jobs_lock:
        live_ids = set(_jobs.keys())

    for job_dir in JOB_ROOT.iterdir():
        if not job_dir.is_dir() or job_dir.name in live_ids:
            continue
        state_path = job_dir / "job.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if state.get("status") not in ("completed", "failed", "cancelled"):
            continue
        finished_at = state.get("finished_at") or state.get("updated_at") or state.get("created_at")
        try:
            age = now - float(finished_at)
        except (TypeError, ValueError):
            continue
        if age >= JOB_RETENTION_SECONDS:
            shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Gradio API helpers
# ---------------------------------------------------------------------------

def _http_get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _http_post_json(url, payload, timeout=900):
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _http_post_multipart(url, field_name, file_data, filename, mime_type, timeout=120):
    boundary = "Boundary-" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fetch_api_info(gradio_url, info_path):
    url = _join_gradio_url(gradio_url, info_path)
    return json.loads(_http_get(url, timeout=30))


def _parse_parameters(api_info_json, endpoint_name):
    """endpoint_name は Gradio named_endpoints のキー（例: '/_run_generation'）。"""
    endpoints = api_info_json.get("named_endpoints", {})
    raw_params = endpoints.get(endpoint_name, {}).get("parameters", [])
    params = []
    for p in raw_params:
        component = p.get("component")
        name = p.get("parameter_name")
        if name is None and component == "State":
            name = "__gradio_state__"
        if name is None:
            continue
        params.append(
            {
                "name": name,
                "component": component,
                "label": p.get("label", ""),
                "has_default": bool(p.get("parameter_has_default", False)),
                "default_value": p.get("parameter_default"),
            }
        )
    return params


def _is_reference_audio_param(param):
    component = param.get("component") or ""
    name = (param.get("name") or "").lower()
    label = (param.get("label") or "").lower()
    if component == "Audio":
        return True
    return any(kw in name or kw in label for kw in ["reference", "speaker", "voice", "audio"])


def _build_input_value(param, text, cfg, runtime_caps, uploaded_ref):
    name = param["name"]

    if name == "checkpoint":
        v = (cfg.get("checkpoint") or "").strip()
        return v if v else (param["default_value"] or "")
    if name == "model_device":
        return cfg.get("model_device", "mps")
    if name == "model_precision":
        return cfg.get("model_precision", "fp32")
    if name == "codec_device":
        return cfg.get("codec_device", "mps")
    if name == "codec_precision":
        return cfg.get("codec_precision", "fp32")
    if name == "text":
        return text if text is not None else ""
    if name == "caption":
        if runtime_caps and runtime_caps.get("use_caption_condition") is False:
            return ""
        return cfg.get("style", "")
    if name == "num_steps":
        return cfg.get("num_steps", 40)
    if name in ("speed", "length", "speaking_rate", "speed_modifier"):
        return cfg.get("speed", 1.0)
    if name == "num_candidates":
        return cfg.get("num_candidates", 1)
    if name == "seed_raw":
        v = (cfg.get("seed_raw") or "").strip()
        return v if v else "42"
    if name == "cfg_guidance_mode":
        return cfg.get("cfg_guidance_mode", "independent")
    if name == "cfg_scale_text":
        return cfg.get("cfg_scale_text", 2.0)
    if name == "cfg_scale_caption":
        return cfg.get("cfg_scale_caption", 4.0)
    if name == "cfg_scale_speaker":
        return cfg.get("cfg_scale_speaker", 5.0)
    if name in (
        "cfg_scale_raw",
        "max_text_len_raw",
        "max_caption_len_raw",
        "truncation_factor_raw",
        "rescale_k_raw",
        "rescale_sigma_raw",
        "speaker_kv_scale_raw",
        "speaker_kv_min_t_raw",
        "speaker_kv_max_layers_raw",
    ):
        v = cfg.get(name)
        if isinstance(v, str):
            v = v.strip()
            return v if v else (param["default_value"] if param["has_default"] else None)
        return v if v is not None else (param["default_value"] if param["has_default"] else None)
    if name == "cfg_min_t":
        return cfg.get("cfg_min_t", 0.5)
    if name == "cfg_max_t":
        return cfg.get("cfg_max_t", 1.0)
    if name == "context_kv_cache":
        return bool(cfg.get("context_kv_cache", True))

    if _is_reference_audio_param(param):
        if runtime_caps and runtime_caps.get("use_speaker_condition") is False:
            return None
        return uploaded_ref

    if param.get("component") == "State":
        return param["default_value"]

    if param["has_default"]:
        return param["default_value"]

    raise ValueError(f"未対応のパラメータ: {name}")


def _find_audio_reference(obj):
    if isinstance(obj, dict):
        path = obj.get("path")
        url = obj.get("url")
        mime = obj.get("mime_type") or ""
        if path is not None or url is not None:
            is_audio = (
                "audio" in mime
                or (isinstance(path, str) and path.lower().endswith(".wav"))
                or (isinstance(url, str) and url.lower().endswith(".wav"))
            )
            if is_audio or not mime:
                return {"path": path, "url": url}
        for v in obj.values():
            result = _find_audio_reference(v)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_audio_reference(item)
            if result:
                return result
    return None


def _resolve_audio_url(gradio_url, file_proxy_prefix, reference):
    if reference.get("url"):
        raw_url = str(reference["url"])
        if raw_url.startswith("http"):
            if not _same_origin(raw_url, gradio_url):
                raise ValueError("Gradio audio URL must stay on the configured origin")
            url = raw_url
        else:
            url = gradio_url.rstrip("/") + "/" + raw_url.lstrip("/")
    elif reference.get("path"):
        encoded = urllib.parse.quote(reference["path"], safe="/")
        url = _join_gradio_url(gradio_url, file_proxy_prefix + encoded)
    else:
        raise ValueError("音声ファイルの URL が見つかりませんでした")
    return url


def _download_audio(gradio_url, file_proxy_prefix, reference):
    url = _resolve_audio_url(gradio_url, file_proxy_prefix, reference)
    data = _http_get(url, timeout=300)

    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        snippet = data[:200].decode("utf-8", errors="replace")
        raise ValueError(f"サーバー応答が WAV データではありません: {snippet}")
    return data


def _parse_runtime_caps(response):
    data = (response or {}).get("data") or []
    summary = data[0] if data and isinstance(data[0], str) else ""

    def get_flag(name):
        prefix = f"{name}:"
        for line in summary.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                val = stripped[len(prefix):].strip().lower()
                if val == "true":
                    return True
                if val == "false":
                    return False
        return None

    return {
        "use_caption_condition": get_flag("use_caption_condition"),
        "use_speaker_condition": get_flag("use_speaker_condition"),
    }


# ---------------------------------------------------------------------------
# Job runner (executed in background thread)
# ---------------------------------------------------------------------------

def _run_job(job, payload):
    tmp_dir = job["tmp_dir"]

    gradio_url = _normalize_gradio_base_url(payload["gradio_url"])
    info_path = _validate_gradio_path(
        "info_path",
        payload.get("info_path", "/gradio_api/info"),
        ALLOWED_INFO_PATHS,
    )
    runtime_path = _validate_gradio_path(
        "runtime_path",
        payload.get("runtime_path", "/gradio_api/run/_describe_runtime"),
        ALLOWED_RUNTIME_PATHS,
    )
    run_path = _validate_gradio_path(
        "run_path",
        payload.get("run_path", "/gradio_api/run/_run_generation"),
        ALLOWED_RUN_PATHS,
    )
    upload_path = _validate_gradio_path(
        "upload_path",
        payload.get("upload_path", "/gradio_api/upload"),
        ALLOWED_UPLOAD_PATHS,
    )
    file_proxy_prefix = _validate_gradio_path(
        "file_proxy_prefix",
        payload.get("file_proxy_prefix", "/gradio_api/file="),
        ALLOWED_FILE_PROXY_PREFIXES,
    )
    chunks = payload["chunks"]
    cfg = payload["config"]

    with _jobs_lock:
        job["status"] = "running"
        job["total"] = len(chunks)
        job["error"] = None
        job["finished_at"] = None
        job["updated_at"] = _now_ts()
        _persist_job_unlocked(job)

    try:
        # 1. Fetch Gradio API info
        api_info = _fetch_api_info(gradio_url, info_path)

        # Gradio named_endpoints keys use short names like /_run_generation
        gen_endpoint = "/_run_generation"
        rt_endpoint = "/_describe_runtime"
        gen_params = _parse_parameters(api_info, gen_endpoint)
        rt_params = _parse_parameters(api_info, rt_endpoint)

        # 2. Call _describe_runtime to detect caption/speaker condition flags
        runtime_caps = None
        if rt_params:
            rt_inputs = [
                _build_input_value(p, "", cfg, None, None) for p in rt_params
            ]
            has_watermark_rt = any(p["name"] == "enable_watermark" for p in rt_params)
            if not has_watermark_rt and len(rt_inputs) == 5:
                rt_inputs.append(False)
            try:
                rt_resp = _http_post_json(_join_gradio_url(gradio_url, runtime_path), {"data": rt_inputs})
                runtime_caps = _parse_runtime_caps(rt_resp)
            except Exception as e:
                print(f"[runtime] _describe_runtime 失敗（続行）: {e}", file=sys.stderr)

        # 3. Upload reference audio once if voice-clone mode
        uploaded_ref = None
        ref_b64 = cfg.get("reference_audio_base64")
        ref_name = cfg.get("reference_audio_filename")
        ref_mime = cfg.get("reference_audio_mime_type") or "audio/wav"
        if ref_b64 and ref_name:
            try:
                ref_data = base64.b64decode(ref_b64)
                upload_url = _join_gradio_url(gradio_url, upload_path)
                up_resp = _http_post_multipart(upload_url, "files", ref_data, ref_name, ref_mime)
                uploaded_path = None
                if isinstance(up_resp, list) and up_resp:
                    uploaded_path = up_resp[0]
                elif isinstance(up_resp, dict):
                    files = up_resp.get("files") or []
                    uploaded_path = files[0] if files else None
                if uploaded_path:
                    uploaded_ref = {
                        "path": uploaded_path,
                        "orig_name": ref_name,
                        "mime_type": ref_mime,
                        "meta": {"_type": "gradio.FileData"},
                    }
            except Exception as e:
                raise RuntimeError(f"参照音声のアップロードに失敗しました: {e}")

        # 4. Synthesize each chunk sequentially
        for chunk in chunks:
            if _job_cancel_requested(job):
                _set_job_status(job, "cancelled", error="client requested cancellation", finished=True)
                if job.get("delete_requested"):
                    _delete_job(job["id"])
                return

            ch = int(chunk["chapter_index"])
            idx = int(chunk["chunk_index"])
            text = (chunk.get("text") or "").strip()
            m4a_path = os.path.join(tmp_dir, f"ch{ch:04d}_chunk{idx:04d}.m4a")

            completed_keys = {
                _chunk_key(item["chapter_index"], item["chunk_index"])
                for item in job["completed_chunks"]
            }
            if _chunk_key(ch, idx) in completed_keys and os.path.exists(m4a_path):
                continue

            if not text:
                _mark_chunk_completed(job, ch, idx)
                continue

            try:
                inputs = [
                    _build_input_value(p, text, cfg, runtime_caps, uploaded_ref)
                    for p in gen_params
                ]
                # enable_watermark (gr.State) は /info に含まれないことがある。
                # text が index 5 にある場合は index 5 に false を挿入する。
                has_watermark = any(p["name"] == "enable_watermark" for p in gen_params)
                if not has_watermark:
                    text_idx = next(
                        (i for i, p in enumerate(gen_params) if p["name"] == "text"), None
                    )
                    if text_idx == 5:
                        inputs.insert(5, False)

                resp = _http_post_json(_join_gradio_url(gradio_url, run_path), {"data": inputs})
                ref = _find_audio_reference(resp)
                if not ref:
                    raise ValueError("Gradio 応答から音声ファイルを見つけられませんでした")

                wav_data = _download_audio(gradio_url, file_proxy_prefix, ref)

                wav_path = os.path.join(tmp_dir, f"ch{ch:04d}_chunk{idx:04d}.wav")

                with open(wav_path, "wb") as f:
                    f.write(wav_data)

                result = subprocess.run(
                    ["afconvert", "-f", "m4af", "-d", "aac", wav_path, m4a_path],
                    capture_output=True,
                    timeout=120,
                )
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

                if result.returncode != 0:
                    err = result.stderr.decode("utf-8", errors="replace")
                    raise RuntimeError(f"afconvert 失敗 (code {result.returncode}): {err}")

                _mark_chunk_completed(job, ch, idx)

            except Exception as e:
                print(
                    f"[chunk] ch{ch:04d}_chunk{idx:04d} 失敗: {e}", file=sys.stderr
                )
                _mark_chunk_failed(job, ch, idx, e)

        if _job_cancel_requested(job):
            _set_job_status(job, "cancelled", error="client requested cancellation", finished=True)
            if job.get("delete_requested"):
                _delete_job(job["id"])
            return

        _set_job_status(job, "completed", finished=True)

    except Exception as e:
        print(f"[job] 失敗: {e}", file=sys.stderr)
        _set_job_status(job, "failed", error=e, finished=True)


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}", file=sys.stderr)

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_binary(self, status, data, content_type="application/octet-stream"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _require_auth(self):
        if _is_authorized_headers(self.headers):
            return True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def do_GET(self):
        if not self._require_auth():
            return

        path = urllib.parse.urlparse(self.path).path
        _prune_finished_jobs()

        if path == "/health":
            self._send_json(200, {"status": "ok", "job_root": str(JOB_ROOT)})

        elif path.startswith("/job_status/"):
            job_id = path[len("/job_status/"):]
            job = _get_job(job_id)
            if not job:
                self._send_json(404, {"error": "job not found"})
                return
            with _jobs_lock:
                resp = {
                    "job_id": job["id"],
                    "status": job["status"],
                    "total": job["total"],
                    "completed_chunks": list(job["completed_chunks"]),
                    "failed_chunks": list(job["failed_chunks"]),
                    "error": job.get("error"),
                    "created_at": job.get("created_at"),
                    "updated_at": job.get("updated_at"),
                    "finished_at": job.get("finished_at"),
                    "cancel_requested": bool(job.get("cancel_requested", False)),
                }
            self._send_json(200, resp)

        elif path.startswith("/batch_chunk/"):
            # /batch_chunk/{job_id}/{chapter_index}/{chunk_index}
            parts = path[len("/batch_chunk/"):].split("/")
            if len(parts) != 3:
                self._send_json(400, {"error": "invalid path"})
                return
            job_id, ch_str, idx_str = parts
            try:
                ch = int(ch_str)
                idx = int(idx_str)
            except ValueError:
                self._send_json(400, {"error": "invalid indices"})
                return
            job = _get_job(job_id)
            if not job:
                self._send_json(404, {"error": "job not found"})
                return
            m4a_path = os.path.join(job["tmp_dir"], f"ch{ch:04d}_chunk{idx:04d}.m4a")
            if not os.path.exists(m4a_path):
                self._send_json(404, {"error": "chunk not ready"})
                return
            with open(m4a_path, "rb") as f:
                data = f.read()
            self._send_binary(200, data, "audio/mp4")

        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._require_auth():
            return

        if self.path == "/batch_synthesize":
            body = self._read_body()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"invalid JSON: {e}"})
                return
            job = _new_job(payload)
            threading.Thread(target=_run_job, args=(job, payload), daemon=True).start()
            self._send_json(200, {"job_id": job["id"]})
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._require_auth():
            return

        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/batch_job/"):
            job_id = path[len("/batch_job/"):]
            ok, state = _request_job_cancel(job_id)
            if not ok:
                self._send_json(404, {"error": "job not found"})
                return
            self._send_json(200, {"ok": True, "status": state})
        else:
            self._send_json(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Irodori Mac Batch TTS Server")
    parser.add_argument("--port", type=int, default=9988, help="Listen port (default: 9988)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--token", type=str, default=None, help="Auth token. Defaults to env/file/generated token")
    parser.add_argument(
        "--token-file",
        type=str,
        default=str(DEFAULT_TOKEN_FILE),
        help=f"Auth token file (default: {DEFAULT_TOKEN_FILE})",
    )
    args = parser.parse_args()

    token = _configure_auth(token=args.token, token_file=args.token_file)
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"Irodori batch server listening on {args.host}:{args.port}", file=sys.stderr)
    print(f"job root: {JOB_ROOT}", file=sys.stderr)
    print(f"auth token file: {AUTH_TOKEN_FILE}", file=sys.stderr)
    print(f"auth token: {token}", file=sys.stderr)
    print("Stop with Ctrl-C", file=sys.stderr)
    _prune_finished_job_dirs_on_disk()

    def _reaper():
        while True:
            time.sleep(300)
            _prune_finished_jobs()

    threading.Thread(target=_reaper, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
