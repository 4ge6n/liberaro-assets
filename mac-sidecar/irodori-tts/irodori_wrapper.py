from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel


app = FastAPI(title="Irodori Wrapper")


class SynthesizeRequest(BaseModel):
    text: str
    model_name: Optional[str] = None
    style: Optional[str] = None
    style_weight: float = 1.0
    speed: float = 1.0


IRODORI_CLI = os.environ.get("IRODORI_CLI", "python infer.py")
IRODORI_TEXT_FLAG = os.environ.get("IRODORI_TEXT_FLAG", "--text-file")
IRODORI_OUTPUT_FLAG = os.environ.get("IRODORI_OUTPUT_FLAG", "--output")
IRODORI_MODEL_FLAG = os.environ.get("IRODORI_MODEL_FLAG", "--model")
IRODORI_STYLE_FLAG = os.environ.get("IRODORI_STYLE_FLAG", "--style")
IRODORI_STYLE_WEIGHT_FLAG = os.environ.get("IRODORI_STYLE_WEIGHT_FLAG", "--style-weight")
IRODORI_SPEED_FLAG = os.environ.get("IRODORI_SPEED_FLAG", "--speed")


def synthesize_with_irodori(
    text: str,
    model_name: Optional[str],
    style: Optional[str],
    style_weight: float,
    speed: float,
) -> bytes:
    with tempfile.TemporaryDirectory() as temp_dir:
        working_dir = Path(temp_dir)
        input_txt = working_dir / "input.txt"
        output_wav = working_dir / "output.wav"

        input_txt.write_text(text, encoding="utf-8")

        command = shlex.split(IRODORI_CLI)
        command += [IRODORI_TEXT_FLAG, str(input_txt), IRODORI_OUTPUT_FLAG, str(output_wav)]

        if model_name:
            command += [IRODORI_MODEL_FLAG, model_name]
        if style:
            command += [IRODORI_STYLE_FLAG, style]
        if IRODORI_STYLE_WEIGHT_FLAG:
            command += [IRODORI_STYLE_WEIGHT_FLAG, str(style_weight)]
        if IRODORI_SPEED_FLAG:
            command += [IRODORI_SPEED_FLAG, str(speed)]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=os.environ.get("IRODORI_WORKDIR"),
            check=False,
        )

        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "Irodori 推論失敗"
            raise RuntimeError(message)

        if not output_wav.exists():
            raise RuntimeError("Irodori が output.wav を生成しませんでした。")

        return output_wav.read_bytes()


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest) -> Response:
    try:
        wav_bytes = synthesize_with_irodori(
            text=req.text,
            model_name=req.model_name,
            style=req.style,
            style_weight=req.style_weight,
            speed=req.speed,
        )
        return Response(content=wav_bytes, media_type="audio/wav")
    except Exception as exc:  # pragma: no cover - surfaced to HTTP client
        raise HTTPException(status_code=500, detail=str(exc)) from exc
