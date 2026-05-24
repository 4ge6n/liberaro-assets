#!/usr/bin/env python3
"""
RunPod Serverless handler for Liberaro upscale jobs.

iOS sends:
{
  "input": {
    "image": "<base64 png>",
    "imageMimeType": "image/png",
    "settings": {
      "engine": "realESRGAN",
      "modelID": "realesrgan-x4plus",
      "scale": 4,
      "noise": -1
    }
  }
}

The handler returns:
{
  "imageBase64": "<base64 png>",
  "imageMimeType": "image/png"
}
"""

from __future__ import annotations

import base64
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import runpod

try:
    from PIL import Image
except Exception:  # pragma: no cover - startup environments may install Pillow late.
    Image = None


ENGINE_BIN_ENV = {
    "waifu2x": "LIBERARO_WAIFU2X_BIN",
    "realCUGAN": "LIBERARO_REALCUGAN_BIN",
    "realESRGAN": "LIBERARO_REALESRGAN_BIN",
}

ENGINE_BIN_NAME = {
    "waifu2x": "waifu2x-ncnn-vulkan",
    "realCUGAN": "realcugan-ncnn-vulkan",
    "realESRGAN": "realesrgan-ncnn-vulkan",
}

ENGINE_MODELS_ENV = {
    "waifu2x": "LIBERARO_WAIFU2X_MODELS_DIR",
    "realCUGAN": "LIBERARO_REALCUGAN_MODELS_DIR",
    "realESRGAN": "LIBERARO_REALESRGAN_MODELS_DIR",
}


def _strip_models_prefix(model_id: str) -> str:
    if model_id.startswith("models-"):
        return model_id[len("models-") :]
    return model_id


def _binary_for(engine: str) -> str:
    override = os.environ.get(ENGINE_BIN_ENV[engine], "")
    if override and Path(override).expanduser().is_file():
        return str(Path(override).expanduser())
    resolved = shutil.which(ENGINE_BIN_NAME[engine])
    if resolved:
        return resolved
    raise RuntimeError(f"{ENGINE_BIN_NAME[engine]} is not available in this RunPod image")


def _models_root(engine: str, bin_path: str) -> Path:
    override = os.environ.get(ENGINE_MODELS_ENV[engine], "")
    if override:
        return Path(override).expanduser().resolve()
    return Path(bin_path).resolve().parent


def _int_setting(settings: dict[str, Any], key: str, fallback: int) -> int:
    try:
        return int(settings.get(key, fallback))
    except Exception:
        return fallback


def _command(input_path: Path, output_path: Path, settings: dict[str, Any]) -> list[str]:
    engine = str(settings.get("engine", ""))
    if engine not in ENGINE_BIN_ENV:
        raise RuntimeError(f"unsupported engine: {engine}")

    bin_path = _binary_for(engine)
    model = _strip_models_prefix(str(settings.get("modelID", "")))
    scale = _int_setting(settings, "scale", 2)
    noise = _int_setting(settings, "noise", -1)
    models_root = _models_root(engine, bin_path)

    if engine == "waifu2x":
        argv = [
            bin_path,
            "-i",
            str(input_path),
            "-o",
            str(output_path),
            "-s",
            str(max(1, scale)),
        ]
        if -1 <= noise <= 3:
            argv += ["-n", str(noise)]
        if model:
            argv += ["-m", str(models_root / f"models-{model}")]
        return argv

    if engine == "realCUGAN":
        argv = [
            bin_path,
            "-i",
            str(input_path),
            "-o",
            str(output_path),
            "-s",
            str(max(2, scale)),
        ]
        if noise in (-1, 0, 3):
            argv += ["-n", str(noise)]
        if model:
            argv += ["-m", str(models_root / f"models-{model}")]
        return argv

    argv = [
        bin_path,
        "-i",
        str(input_path),
        "-o",
        str(output_path),
        "-s",
        str(max(2, scale)),
    ]
    models_dir = models_root / "models"
    if models_dir.exists():
        argv += ["-m", str(models_dir)]
    if model:
        argv += ["-n", model]
    return argv


def _decode_image(value: str) -> bytes:
    text = value.strip()
    if "," in text and "base64" in text.split(",", 1)[0].lower():
        text = text.split(",", 1)[1]
    return base64.b64decode(text, validate=False)


def _png_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()[:24]
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    return None


def _image_size(path: Path) -> tuple[int, int] | None:
    if Image is not None:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    return _png_size(path)


def _resize_to_input_size(input_path: Path, output_path: Path) -> None:
    if Image is None:
        raise RuntimeError("Pillow is required for noUpscale output resizing")
    input_size = _image_size(input_path)
    if not input_size:
        return
    with Image.open(output_path) as image:
        if (image.width, image.height) == input_size:
            return
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        resized = image.resize(input_size, resampling)
        resized.save(output_path, format="PNG")


def _should_skip(input_path: Path, settings: dict[str, Any]) -> bool:
    skip_min_pixel = _int_setting(settings, "skipMinPixel", 0)
    if skip_min_pixel <= 0:
        return False
    size = _image_size(input_path)
    if not size:
        return False
    return max(size) >= skip_min_pixel


def handler(job: dict[str, Any]) -> dict[str, str]:
    payload = job.get("input") or {}
    image_base64 = payload.get("image")
    settings = payload.get("settings") or {}
    if not isinstance(image_base64, str) or not image_base64.strip():
        raise RuntimeError("input.image is required")
    if not isinstance(settings, dict):
        raise RuntimeError("input.settings must be an object")

    runpod.serverless.progress_update(job, "decoding")
    with tempfile.TemporaryDirectory(prefix="liberaro-runpod-") as tmp:
        input_path = Path(tmp) / "input.png"
        output_path = Path(tmp) / "output.png"
        input_path.write_bytes(_decode_image(image_base64))

        if _should_skip(input_path, settings):
            runpod.serverless.progress_update(job, "skipped")
            result = base64.b64encode(input_path.read_bytes()).decode("ascii")
            return {"imageBase64": result, "imageMimeType": "image/png"}

        argv = _command(input_path, output_path, settings)
        runpod.serverless.progress_update(job, "processing")
        completed = subprocess.run(argv, text=True, capture_output=True, timeout=900)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:1000]
            raise RuntimeError(f"upscale failed: {detail}")
        if not output_path.exists():
            raise RuntimeError("upscale finished without output image")

        if bool(settings.get("noUpscale", False)):
            runpod.serverless.progress_update(job, "resizing")
            _resize_to_input_size(input_path, output_path)

        runpod.serverless.progress_update(job, "encoding")
        result = base64.b64encode(output_path.read_bytes()).decode("ascii")
        return {"imageBase64": result, "imageMimeType": "image/png"}


runpod.serverless.start({"handler": handler})
