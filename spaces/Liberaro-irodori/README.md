---
title: Liberaro Irodori TTS
emoji: 🚀
colorFrom: blue
colorTo: green
sdk: gradio
sdk_version: 6.12.0
python_version: "3.12"
app_file: app.py
license: mit
short_description: Irodori-TTS server Space for Liberaro
models:
  - Aratako/Irodori-TTS-600M-v3-VoiceDesign
  - Aratako/Irodori-TTS-500M-v3
  - Aratako/Irodori-TTS-500M-v2
  - Aratako/Irodori-TTS-500M-v2-VoiceDesign
  - Aratako/Semantic-DACVAE-Japanese-32dim
---

Gradio-based Irodori-TTS server for Liberaro.

This Space exposes Gradio API endpoints compatible with Liberaro iOS:

- `/_describe_runtime`
- `/_run_generation`

Default checkpoint: **`Aratako/Irodori-TTS-600M-v3-VoiceDesign`** (v3). The v2 checkpoints
remain selectable and load on the v3 codebase for backward compatibility.

Usage notes:

- Use a VoiceDesign checkpoint (`600M-v3-VoiceDesign`) to drive voice identity + style via
  any combination of `Caption / Style Prompt`, reference audio, and text (v3 multi-modal voice design).
- Use the base checkpoint (`500M-v3`) to drive voice with uploaded reference audio, with
  emoji-based style control in the input text.
- v3 base / VoiceDesign predict output length automatically; the Space still passes a fixed
  `seconds` for a stable iOS contract.
- A GPU Space is strongly recommended for practical inference time.

This Space vendors the upstream `irodori_tts` Python package (v3 / `main`) from the official
MIT-licensed repository: [Aratako/Irodori-TTS](https://github.com/Aratako/Irodori-TTS)
(pinned at commit `eaf74d6`). SilentCipher watermarking is left disabled (dependency not
installed), so generated audio is unwatermarked.
