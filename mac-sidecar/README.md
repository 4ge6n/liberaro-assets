# mac-sidecar

iOS アプリから利用する Mac 上の常駐サービス群。

```
mac-sidecar/
├── upscale/        ncnn-vulkan ベースの upscale サーバ
│                   (waifu2x / Real-CUGAN / Real-ESRGAN)
└── irodori-tts/    Irodori TTS をブリッジするバッチサーバ
                    (iPhone から WAV/M4A をまとめて生成)
```

両サービスとも:

- macOS 標準 Python のみ (`/usr/bin/python3`) で動く
- 外部ライブラリ追加なし
- launchd で常駐させる場合の plist 同梱
- iOS と HTTP + JSON で通信

詳細は各サブディレクトリの README を参照:

- [`upscale/README.md`](upscale/README.md) — アップスケールサーバ
- [`irodori-tts/README.md`](irodori-tts/README.md) — TTS バッチサーバ
