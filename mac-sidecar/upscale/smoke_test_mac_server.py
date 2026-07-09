#!/usr/bin/env python3
"""
Mac サイドカー (`mac-sidecar/upscale/liberaro_upscale_server.py`) を実環境で動作確認するスモークテスト。
このスクリプトはサーバを起動しない。別ターミナルでサーバ起動済み前提で使う。

使い方:
    python3 mac-sidecar/upscale/smoke_test_mac_server.py \\
        --base http://127.0.0.1:8088 \\
        --token <server-token> \\
        --engine waifu2x --model cunet --scale 2 --noise 2

オプションで `--image <path>` を渡すと指定 PNG をテスト画像に使う。
未指定なら 64x64 のグラデーション PNG を自前で作る（stdlib のみ・依存追加なし）。

成功すると "OK <ms>ms" と出て exit 0。失敗時は stderr に詳細を吐いて exit 1。
"""

import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path


def _make_gradient_png(width=64, height=64):
    """stdlib のみで小さな PNG を作る (RGB, グラデーション)。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type
        for x in range(width):
            raw.append(int(255 * x / max(1, width - 1)))   # R
            raw.append(int(255 * y / max(1, height - 1)))  # G
            raw.append(128)                                # B
    compressed = zlib.compress(bytes(raw), 9)

    def chunk(tag, body):
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def _build_multipart(image_bytes, meta_dict):
    boundary = f"----LiberaroBoundary{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts = []
    parts.append(f"--{boundary}".encode())
    parts.append(b'Content-Disposition: form-data; name="image"; filename="input.png"')
    parts.append(b"Content-Type: image/png")
    parts.append(b"")
    parts.append(image_bytes)
    parts.append(f"--{boundary}".encode())
    parts.append(b'Content-Disposition: form-data; name="meta"')
    parts.append(b"Content-Type: application/json")
    parts.append(b"")
    parts.append(json.dumps(meta_dict).encode())
    parts.append(f"--{boundary}--".encode())
    body = crlf.join(parts) + crlf
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def _request(method, url, *, token, body=None, content_type=None, accept=("application/json",), timeout=30):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Liberaro-Token", token)
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), resp.headers.get("Content-Type", "")


def run(base, token, engine, model, scale, noise, image_path):
    # 1. /health
    sys.stderr.write(f"[1/4] GET {base}/health\n")
    status, body, _ = _request("GET", f"{base}/health", token=token)
    if status != 200:
        raise RuntimeError(f"/health returned {status}: {body!r}")
    health = json.loads(body)
    if not health.get("engines", {}).get(engine):
        raise RuntimeError(f"server reports engine {engine!r} unavailable: {health}")
    sys.stderr.write(f"      ok, version={health.get('version')}\n")

    # 2. /models (新エンドポイント, 0.2.0+)
    sys.stderr.write(f"[2/4] GET {base}/models\n")
    try:
        status, body, _ = _request("GET", f"{base}/models", token=token)
        if status == 200:
            models = json.loads(body).get("models", {}).get(engine, [])
            sys.stderr.write(f"      {len(models)} models: {models}\n")
        else:
            sys.stderr.write(f"      skipped (status {status})\n")
    except urllib.error.HTTPError as exc:
        sys.stderr.write(f"      skipped (HTTPError {exc.code}, server may be pre-0.2.0)\n")

    # 3. POST /jobs
    sys.stderr.write(f"[3/4] POST {base}/jobs\n")
    if image_path:
        image_bytes = Path(image_path).read_bytes()
    else:
        image_bytes = _make_gradient_png(64, 64)
    meta = {
        "engine": engine,
        "modelID": model,
        "scale": scale,
        "noise": noise,
        "noUpscale": False,
        "skipMinPixel": 0,
    }
    body, ct = _build_multipart(image_bytes, meta)
    started = time.time()
    status, resp, _ = _request("POST", f"{base}/jobs", token=token, body=body, content_type=ct, timeout=60)
    if status != 201:
        raise RuntimeError(f"POST /jobs returned {status}: {resp!r}")
    job = json.loads(resp)
    job_id = job["id"]
    sys.stderr.write(f"      job id={job_id} status={job['status']}\n")

    # 4. Poll → done / failed
    sys.stderr.write(f"[4/4] poll {base}/jobs/{job_id}\n")
    deadline = time.time() + 120
    last_status = job["status"]
    while time.time() < deadline:
        time.sleep(0.5)
        status, resp, _ = _request("GET", f"{base}/jobs/{job_id}", token=token, timeout=10)
        state = json.loads(resp)
        if state["status"] != last_status:
            sys.stderr.write(f"      status -> {state['status']}\n")
            last_status = state["status"]
        if state["status"] == "done":
            elapsed_ms = int((time.time() - started) * 1000)
            # 結果も取得して非空を確認
            status, result_bytes, ct = _request("GET", f"{base}/jobs/{job_id}/result", token=token, timeout=60)
            if status != 200 or not result_bytes:
                raise RuntimeError(f"result fetch returned {status} len={len(result_bytes)}")
            status, refetched_bytes, _ = _request("GET", f"{base}/jobs/{job_id}/result", token=token, timeout=60)
            if status != 200 or refetched_bytes != result_bytes:
                raise RuntimeError("result refetch did not return the same pooled bytes")
            sys.stderr.write(f"      result size={len(result_bytes)} bytes content-type={ct} refetch=ok\n")
            print(f"OK {elapsed_ms}ms")
            return
        if state["status"] == "failed":
            raise RuntimeError(f"job failed: {state.get('error')}")
        if state["status"] == "cancelled":
            raise RuntimeError("job cancelled by server")
    raise RuntimeError("job did not finish within 120s")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8088",
                   help="server base URL (default: http://127.0.0.1:8088)")
    p.add_argument("--token", default=os.environ.get("LIBERARO_UPSCALE_AUTH_TOKEN", ""),
                   help="server auth token (or LIBERARO_UPSCALE_AUTH_TOKEN)")
    p.add_argument("--engine", default="waifu2x", choices=["waifu2x", "realCUGAN", "realESRGAN"])
    p.add_argument("--model", default="cunet",
                   help="model ID (engine 依存。waifu2x: cunet / realCUGAN: pro / realESRGAN: realesr-animevideov3-x4 など)")
    p.add_argument("--scale", type=int, default=2)
    p.add_argument("--noise", type=int, default=2)
    p.add_argument("--image", default=None, help="optional PNG to use as input")
    args = p.parse_args()
    if not args.token.strip():
        p.error("--token is required (or set LIBERARO_UPSCALE_AUTH_TOKEN)")

    try:
        run(args.base, args.token.strip(), args.engine, args.model, args.scale, args.noise, args.image)
    except Exception as exc:
        sys.stderr.write(f"FAIL: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
