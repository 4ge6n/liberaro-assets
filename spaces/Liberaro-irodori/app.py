from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import gradio as gr
from huggingface_hub import hf_hub_download

class _SpacesShim:
    @staticmethod
    def GPU(*_args, **_kwargs):
        def decorator(function):
            return function

        return decorator

hf_spaces = _SpacesShim()

from irodori_tts.inference_runtime import (
    RuntimeKey,
    SamplingRequest,
    clear_cached_runtime,
    default_runtime_device,
    get_cached_runtime,
    list_available_runtime_devices,
    list_available_runtime_precisions,
    save_wav,
)


FIXED_SECONDS = float(os.environ.get("IRODORI_FIXED_SECONDS", "30.0"))
MAX_GRADIO_CANDIDATES = int(os.environ.get("MAX_GRADIO_CANDIDATES", "32"))
GRADIO_AUDIO_COLS_PER_ROW = int(os.environ.get("IRODORI_AUDIO_COLS", "4"))
OUTPUT_DIR = Path(os.environ.get("IRODORI_OUTPUT_DIR", "gradio_outputs"))
SPACE_TITLE = os.environ.get("IRODORI_SPACE_TITLE", "Liberaro Irodori TTS")
DEFAULT_CHECKPOINT = os.environ.get(
    "IRODORI_DEFAULT_CHECKPOINT",
    "Aratako/Irodori-TTS-600M-v3-VoiceDesign",
)
CODEC_REPO = os.environ.get(
    "IRODORI_CODEC_REPO",
    "Aratako/Semantic-DACVAE-Japanese-32dim",
)


def _checkpoint_choices() -> list[str]:
    choices = [
        DEFAULT_CHECKPOINT,
        # v3 系（最新）。VoiceDesign は caption + 参照音声 + テキストの多modal制御。
        "Aratako/Irodori-TTS-600M-v3-VoiceDesign",
        "Aratako/Irodori-TTS-500M-v3",
        # v2 系（後方互換。v3 コードでもロード可能）。
        "Aratako/Irodori-TTS-500M-v2-VoiceDesign",
        "Aratako/Irodori-TTS-500M-v2",
    ]
    unique: list[str] = []
    for candidate in choices:
        value = candidate.strip()
        if value and value not in unique:
            unique.append(value)
    return unique


def _default_model_device() -> str:
    return default_runtime_device()


def _default_codec_device() -> str:
    return default_runtime_device()


def _precision_choices_for_device(device: str) -> list[str]:
    return list_available_runtime_precisions(device)


def _on_model_device_change(device: str) -> gr.Dropdown:
    choices = _precision_choices_for_device(device)
    return gr.Dropdown(choices=choices, value=choices[0], allow_custom_value=True)


def _on_codec_device_change(device: str) -> gr.Dropdown:
    choices = _precision_choices_for_device(device)
    return gr.Dropdown(choices=choices, value=choices[0], allow_custom_value=True)


def _parse_optional_float(raw: str | None, label: str) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "" or text.lower() == "none":
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a float or blank.") from exc


def _parse_optional_int(raw: str | None, label: str) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "" or text.lower() == "none":
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an int or blank.") from exc


def _format_timings(stage_timings: list[tuple[str, float]], total_to_decode: float) -> str:
    lines = [
        "[timing] ---- request ----",
        *[f"[timing] {name}: {sec * 1000.0:.1f} ms" for name, sec in stage_timings],
        f"[timing] total_to_decode: {total_to_decode:.3f} s",
    ]
    return "\n".join(lines)


def _resolve_ref_wav(uploaded_audio: str | None) -> str | None:
    if uploaded_audio is None:
        return None
    text = str(uploaded_audio).strip()
    if text == "":
        return None
    return text


def _prepare_reference_audio_path(raw_path: str | None) -> tuple[str | None, str | None]:
    if raw_path is None:
        return None, None

    source = Path(raw_path)
    if not source.exists():
        raise FileNotFoundError(f"Reference audio file was not found: {source}")

    suffix = source.suffix or ".wav"
    with tempfile.NamedTemporaryFile(prefix="irodori_ref_", suffix=suffix, delete=False) as temp_file:
        temp_path = Path(temp_file.name)
    shutil.copyfile(source, temp_path)
    return str(temp_path), str(temp_path)


def _resolve_checkpoint_path(raw_checkpoint: str) -> str:
    checkpoint = str(raw_checkpoint).strip()
    if checkpoint == "":
        raise ValueError("checkpoint is required.")

    suffix = Path(checkpoint).suffix.lower()
    if suffix in {".pt", ".safetensors"}:
        return checkpoint

    resolved = hf_hub_download(repo_id=checkpoint, filename="model.safetensors")
    print(f"[irodori-space] checkpoint: hf://{checkpoint} -> {resolved}", flush=True)
    return str(resolved)


def _normalize_device(raw_device: str, role: str) -> tuple[str, str | None]:
    requested = str(raw_device).strip().lower() or default_runtime_device()
    available = list_available_runtime_devices()
    if requested in available:
        return requested, None

    fallback = default_runtime_device()
    if fallback not in available:
        fallback = available[0]
    return fallback, (
        f"warning: requested {role}_device={requested} is unavailable here; using {fallback} instead."
    )


def _normalize_precision(raw_precision: str, device: str, role: str) -> tuple[str, str | None]:
    requested = str(raw_precision).strip().lower()
    available = list_available_runtime_precisions(device)
    if requested in available:
        return requested, None

    fallback = available[0]
    requested_label = requested or "default"
    return fallback, (
        f"warning: requested {role}_precision={requested_label} is unavailable for {device}; using {fallback} instead."
    )


def _resolve_runtime_key_and_notes(
    checkpoint: str,
    model_device: str,
    model_precision: str,
    codec_device: str,
    codec_precision: str,
    enable_watermark: bool,
) -> tuple[RuntimeKey, list[str]]:
    checkpoint_path = _resolve_checkpoint_path(checkpoint)
    resolved_model_device, model_device_note = _normalize_device(model_device, "model")
    resolved_codec_device, codec_device_note = _normalize_device(codec_device, "codec")
    resolved_model_precision, model_precision_note = _normalize_precision(
        model_precision,
        resolved_model_device,
        "model",
    )
    resolved_codec_precision, codec_precision_note = _normalize_precision(
        codec_precision,
        resolved_codec_device,
        "codec",
    )
    notes = [
        note
        for note in [
            model_device_note,
            codec_device_note,
            model_precision_note,
            codec_precision_note,
        ]
        if note is not None
    ]
    # v3 では watermark は RuntimeKey フラグではなく SilentCipherWatermarker が
    # 使えるとき (silentcipher 導入時) だけ自動適用される。requirements に
    # silentcipher を含めていないため watermarker.ready=False となり無効。
    # UI の enable_watermark トグルは現状 no-op（引数は互換のため受けるだけ）。
    _ = enable_watermark
    return RuntimeKey(
        checkpoint=checkpoint_path,
        model_device=resolved_model_device,
        codec_repo=CODEC_REPO,
        model_precision=resolved_model_precision,
        codec_device=resolved_codec_device,
        codec_precision=resolved_codec_precision,
        compile_model=False,
        compile_dynamic=False,
    ), notes


@hf_spaces.GPU(duration=120)
def _describe_runtime(
    checkpoint: str,
    model_device: str,
    model_precision: str,
    codec_device: str,
    codec_precision: str,
    enable_watermark: bool,
) -> str:
    runtime_key, normalization_notes = _resolve_runtime_key_and_notes(
        checkpoint=checkpoint,
        model_device=model_device,
        model_precision=model_precision,
        codec_device=codec_device,
        codec_precision=codec_precision,
        enable_watermark=enable_watermark,
    )
    runtime, reloaded = get_cached_runtime(runtime_key)
    status = "loaded model into memory" if reloaded else "model already loaded; reused existing runtime"

    notes: list[str] = []
    if runtime.model_cfg.use_caption_condition:
        notes.append("info: caption conditioning is available.")
    else:
        notes.append("info: caption conditioning is disabled for this checkpoint.")

    if runtime.model_cfg.use_speaker_condition:
        notes.append("info: reference audio conditioning is available.")
    else:
        notes.append("info: reference audio conditioning is disabled for this checkpoint.")

    return "\n".join(
        [
            status,
            f"checkpoint: {runtime_key.checkpoint}",
            f"model_device: {runtime_key.model_device}",
            f"model_precision: {runtime_key.model_precision}",
            f"codec_device: {runtime_key.codec_device}",
            f"codec_precision: {runtime_key.codec_precision}",
            f"use_caption_condition: {runtime.model_cfg.use_caption_condition}",
            f"use_speaker_condition: {runtime.model_cfg.use_speaker_condition}",
            *normalization_notes,
            *notes,
        ]
    )


@hf_spaces.GPU(duration=240)
def _run_generation(
    checkpoint: str,
    model_device: str,
    model_precision: str,
    codec_device: str,
    codec_precision: str,
    enable_watermark: bool,
    text: str,
    caption: str,
    uploaded_audio: str | None,
    num_steps: int,
    num_candidates: int,
    seed_raw: str,
    cfg_guidance_mode: str,
    cfg_scale_text: float,
    cfg_scale_caption: float,
    cfg_scale_speaker: float,
    cfg_scale_raw: str,
    cfg_min_t: float,
    cfg_max_t: float,
    context_kv_cache: bool,
    max_text_len_raw: str,
    max_caption_len_raw: str,
    truncation_factor_raw: str,
    rescale_k_raw: str,
    rescale_sigma_raw: str,
    speaker_kv_scale_raw: str,
    speaker_kv_min_t_raw: str,
    speaker_kv_max_layers_raw: str,
) -> tuple[object, ...]:
    def stdout_log(message: str) -> None:
        print(message, flush=True)

    runtime_key, normalization_notes = _resolve_runtime_key_and_notes(
        checkpoint=checkpoint,
        model_device=model_device,
        model_precision=model_precision,
        codec_device=codec_device,
        codec_precision=codec_precision,
        enable_watermark=enable_watermark,
    )

    text_value = str(text).strip()
    caption_value = str(caption).strip()
    if text_value == "":
        raise ValueError("text is required.")

    requested_candidates = int(num_candidates)
    if requested_candidates <= 0:
        raise ValueError("num_candidates must be >= 1.")
    if requested_candidates > MAX_GRADIO_CANDIDATES:
        raise ValueError(f"num_candidates must be <= {MAX_GRADIO_CANDIDATES}.")

    cfg_scale = _parse_optional_float(cfg_scale_raw, "cfg_scale")
    max_text_len = _parse_optional_int(max_text_len_raw, "max_text_len")
    max_caption_len = _parse_optional_int(max_caption_len_raw, "max_caption_len")
    truncation_factor = _parse_optional_float(truncation_factor_raw, "truncation_factor")
    rescale_k = _parse_optional_float(rescale_k_raw, "rescale_k")
    rescale_sigma = _parse_optional_float(rescale_sigma_raw, "rescale_sigma")
    speaker_kv_scale = _parse_optional_float(speaker_kv_scale_raw, "speaker_kv_scale")
    speaker_kv_min_t = _parse_optional_float(speaker_kv_min_t_raw, "speaker_kv_min_t")
    speaker_kv_max_layers = _parse_optional_int(
        speaker_kv_max_layers_raw,
        "speaker_kv_max_layers",
    )
    seed = _parse_optional_int(seed_raw, "seed")
    ref_wav = _resolve_ref_wav(uploaded_audio=uploaded_audio)
    prepared_ref_wav, temp_ref_wav = _prepare_reference_audio_path(ref_wav)

    runtime, reloaded = get_cached_runtime(runtime_key)
    try:
        if caption_value and not runtime.model_cfg.use_caption_condition:
            raise ValueError(
                "Loaded checkpoint does not enable caption conditioning. Clear caption or switch to a VoiceDesign checkpoint."
            )
        if prepared_ref_wav is not None and not runtime.model_cfg.use_speaker_condition:
            raise ValueError(
                "Loaded checkpoint does not enable reference audio conditioning. Remove the audio or switch checkpoints."
            )

        for note in normalization_notes:
            stdout_log(f"[irodori-space] {note}")

        stdout_log(f"[irodori-space] runtime: {'reloaded' if reloaded else 'reused'}")
        stdout_log(
            (
                "[irodori-space] request: model_device={} model_precision={} codec_device={} codec_precision={} "
                "watermark={} mode={} seconds={} steps={} seed={} candidates={}"
            ).format(
                model_device,
                model_precision,
                codec_device,
                codec_precision,
                enable_watermark,
                cfg_guidance_mode,
                FIXED_SECONDS,
                num_steps,
                "random" if seed is None else seed,
                requested_candidates,
            )
        )
        stdout_log(
            "[irodori-space] conditioning: caption={} reference_audio={}".format(
                "on" if caption_value else "off",
                "on" if prepared_ref_wav is not None else "off",
            )
        )

        result = runtime.synthesize(
            SamplingRequest(
                text=text_value,
                caption=caption_value or None,
                ref_wav=prepared_ref_wav,
                ref_latent=None,
                no_ref=prepared_ref_wav is None,
                ref_normalize_db=-16.0,
                ref_ensure_max=True,
                num_candidates=requested_candidates,
                decode_mode="sequential",
                seconds=FIXED_SECONDS,
                max_ref_seconds=30.0,
                max_text_len=max_text_len,
                max_caption_len=max_caption_len,
                num_steps=int(num_steps),
                seed=None if seed is None else int(seed),
                cfg_guidance_mode=str(cfg_guidance_mode),
                cfg_scale_text=float(cfg_scale_text),
                cfg_scale_caption=float(cfg_scale_caption),
                cfg_scale_speaker=float(cfg_scale_speaker),
                cfg_scale=cfg_scale,
                cfg_min_t=float(cfg_min_t),
                cfg_max_t=float(cfg_max_t),
                truncation_factor=truncation_factor,
                rescale_k=rescale_k,
                rescale_sigma=rescale_sigma,
                context_kv_cache=bool(context_kv_cache),
                speaker_kv_scale=speaker_kv_scale,
                speaker_kv_min_t=speaker_kv_min_t,
                speaker_kv_max_layers=speaker_kv_max_layers,
                trim_tail=True,
            ),
            log_fn=stdout_log,
        )
    finally:
        if temp_ref_wav is not None:
            try:
                os.remove(temp_ref_wav)
            except OSError:
                pass

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_paths: list[str] = []
    for index, audio in enumerate(result.audios, start=1):
        out_path = save_wav(
            OUTPUT_DIR / f"sample_{stamp}_{index:03d}.wav",
            audio.float(),
            result.sample_rate,
        )
        out_paths.append(str(out_path))

    runtime_msg = "runtime: reloaded" if reloaded else "runtime: reused"
    detail_lines = [
        runtime_msg,
        f"checkpoint: {runtime_key.checkpoint}",
        f"use_caption_condition: {runtime.model_cfg.use_caption_condition}",
        f"use_speaker_condition: {runtime.model_cfg.use_speaker_condition}",
        f"seed_used: {result.used_seed}",
        f"candidates: {len(result.audios)}",
        *normalization_notes,
        *[f"saved[{index}]: {path}" for index, path in enumerate(out_paths, start=1)],
        *result.messages,
    ]
    detail_text = "\n".join(detail_lines)
    timing_text = _format_timings(result.stage_timings, result.total_to_decode)

    audio_updates: list[object] = []
    for index in range(MAX_GRADIO_CANDIDATES):
        if index < len(out_paths):
            audio_updates.append(gr.update(value=out_paths[index], visible=True))
        else:
            audio_updates.append(gr.update(value=None, visible=False))

    return (*audio_updates, detail_text, timing_text)


def _clear_runtime_cache() -> str:
    clear_cached_runtime()
    return "cleared loaded model from memory"


def build_ui() -> gr.Blocks:
    default_model_device = _default_model_device()
    default_codec_device = _default_codec_device()
    device_choices = list_available_runtime_devices()
    model_precision_choices = _precision_choices_for_device(default_model_device)
    codec_precision_choices = _precision_choices_for_device(default_codec_device)

    with gr.Blocks(title=SPACE_TITLE) as demo:
        gr.Markdown(f"# {SPACE_TITLE}")
        gr.Markdown(
            "Liberaro 向けの Irodori-TTS Space です。"
            " `/_describe_runtime` と `/_run_generation` を公開するので、"
            " iOS アプリからもそのまま接続できます。"
        )
        gr.Markdown(
            "- VoiceDesign を使うときは `Caption / Style Prompt` を入れて参照音声は空にします。\n"
            "- Voice Clone を使うときは参照音声を入れて、必要なら caption は空のままにします。\n"
            "- GPU Space を強く推奨します。"
        )

        with gr.Row():
            checkpoint = gr.Dropdown(
                label="Checkpoint (.pt/.safetensors or HF repo id)",
                choices=_checkpoint_choices(),
                value=DEFAULT_CHECKPOINT,
                allow_custom_value=True,
                scale=4,
            )
            model_device = gr.Dropdown(
                label="Model Device",
                choices=device_choices,
                value=default_model_device,
                allow_custom_value=True,
                scale=1,
            )
            model_precision = gr.Dropdown(
                label="Model Precision",
                choices=model_precision_choices,
                value=model_precision_choices[0],
                allow_custom_value=True,
                scale=1,
            )
            codec_device = gr.Dropdown(
                label="Codec Device",
                choices=device_choices,
                value=default_codec_device,
                allow_custom_value=True,
                scale=1,
            )
            codec_precision = gr.Dropdown(
                label="Codec Precision",
                choices=codec_precision_choices,
                value=codec_precision_choices[0],
                allow_custom_value=True,
                scale=1,
            )
            enable_watermark = gr.State(False)

        with gr.Row():
            load_model_btn = gr.Button("Load / Describe Runtime")
            clear_cache_btn = gr.Button("Unload Model")
            runtime_summary = gr.Textbox(label="Runtime Summary", lines=8, interactive=False)

        text = gr.Textbox(label="Text", lines=4)
        caption = gr.Textbox(label="Caption / Style Prompt (optional)", lines=4)
        uploaded_audio = gr.Audio(
            label="Reference Audio Upload (optional)",
            type="filepath",
        )

        with gr.Accordion("Sampling", open=True):
            with gr.Row():
                num_steps = gr.Slider(label="Num Steps", minimum=1, maximum=120, value=40, step=1)
                num_candidates = gr.Slider(
                    label="Num Candidates",
                    minimum=1,
                    maximum=MAX_GRADIO_CANDIDATES,
                    value=1,
                    step=1,
                )
                seed_raw = gr.Textbox(label="Seed (blank=random)", value="")

            with gr.Row():
                cfg_guidance_mode = gr.Dropdown(
                    label="CFG Guidance Mode",
                    choices=["independent", "joint", "alternating"],
                    value="independent",
                )
                cfg_scale_text = gr.Slider(
                    label="CFG Scale Text",
                    minimum=0.0,
                    maximum=10.0,
                    value=2.0,
                    step=0.1,
                )
                cfg_scale_caption = gr.Slider(
                    label="CFG Scale Caption",
                    minimum=0.0,
                    maximum=10.0,
                    value=4.0,
                    step=0.1,
                )
                cfg_scale_speaker = gr.Slider(
                    label="CFG Scale Speaker",
                    minimum=0.0,
                    maximum=10.0,
                    value=5.0,
                    step=0.1,
                )

        with gr.Accordion("Advanced (Optional)", open=False):
            cfg_scale_raw = gr.Textbox(label="CFG Scale Override (optional)", value="")
            with gr.Row():
                cfg_min_t = gr.Number(label="CFG Min t", value=0.5)
                cfg_max_t = gr.Number(label="CFG Max t", value=1.0)
                context_kv_cache = gr.Checkbox(label="Context KV Cache", value=True)
            with gr.Row():
                max_text_len_raw = gr.Textbox(label="Max Text Len (optional)", value="")
                max_caption_len_raw = gr.Textbox(label="Max Caption Len (optional)", value="")
            with gr.Row():
                truncation_factor_raw = gr.Textbox(label="Truncation Factor (optional)", value="")
                rescale_k_raw = gr.Textbox(label="Rescale k (optional)", value="")
                rescale_sigma_raw = gr.Textbox(label="Rescale sigma (optional)", value="")
            with gr.Row():
                speaker_kv_scale_raw = gr.Textbox(label="Speaker KV Scale (optional)", value="")
                speaker_kv_min_t_raw = gr.Textbox(label="Speaker KV Min t (optional)", value="0.9")
                speaker_kv_max_layers_raw = gr.Textbox(
                    label="Speaker KV Max Layers (optional)",
                    value="",
                )

        generate_btn = gr.Button("Generate", variant="primary")

        out_audios: list[gr.Audio] = []
        num_rows = (
            MAX_GRADIO_CANDIDATES + GRADIO_AUDIO_COLS_PER_ROW - 1
        ) // GRADIO_AUDIO_COLS_PER_ROW
        with gr.Column():
            for row_index in range(num_rows):
                with gr.Row():
                    for col_index in range(GRADIO_AUDIO_COLS_PER_ROW):
                        output_index = row_index * GRADIO_AUDIO_COLS_PER_ROW + col_index
                        if output_index >= MAX_GRADIO_CANDIDATES:
                            break
                        out_audios.append(
                            gr.Audio(
                                label=f"Generated Audio {output_index + 1}",
                                type="filepath",
                                interactive=False,
                                visible=(output_index == 0),
                                min_width=180,
                            )
                        )

        out_log = gr.Textbox(label="Run Log", lines=10)
        out_timing = gr.Textbox(label="Timing", lines=8)

        generate_btn.click(
            _run_generation,
            inputs=[
                checkpoint,
                model_device,
                model_precision,
                codec_device,
                codec_precision,
                enable_watermark,
                text,
                caption,
                uploaded_audio,
                num_steps,
                num_candidates,
                seed_raw,
                cfg_guidance_mode,
                cfg_scale_text,
                cfg_scale_caption,
                cfg_scale_speaker,
                cfg_scale_raw,
                cfg_min_t,
                cfg_max_t,
                context_kv_cache,
                max_text_len_raw,
                max_caption_len_raw,
                truncation_factor_raw,
                rescale_k_raw,
                rescale_sigma_raw,
                speaker_kv_scale_raw,
                speaker_kv_min_t_raw,
                speaker_kv_max_layers_raw,
            ],
            outputs=[*out_audios, out_log, out_timing],
            api_name="_run_generation",
        )

        model_device.change(
            _on_model_device_change,
            inputs=[model_device],
            outputs=[model_precision],
        )
        codec_device.change(
            _on_codec_device_change,
            inputs=[codec_device],
            outputs=[codec_precision],
        )

        load_model_btn.click(
            _describe_runtime,
            inputs=[
                checkpoint,
                model_device,
                model_precision,
                codec_device,
                codec_precision,
                enable_watermark,
            ],
            outputs=[runtime_summary],
            api_name="_describe_runtime",
        )
        clear_cache_btn.click(
            _clear_runtime_cache,
            outputs=[runtime_summary],
            api_name="_clear_runtime_cache",
        )

    return demo


demo = build_ui()
demo.queue(default_concurrency_limit=1)


if __name__ == "__main__":
    if os.environ.get("SPACE_ID"):
        demo.launch(ssr_mode=False)
    else:
        demo.launch(
            server_name=os.environ.get("IRODORI_SERVER_NAME", "0.0.0.0"),
            server_port=int(os.environ.get("PORT", os.environ.get("IRODORI_SERVER_PORT", "7860"))),
            ssr_mode=False,
        )
