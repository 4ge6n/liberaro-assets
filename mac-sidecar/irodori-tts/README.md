# mac-sidecar/irodori-tts

iPhone から TTS チャンクをまとめて受け取り、ローカル Irodori (Gradio) で音声合成して
M4A に変換、結果を iPhone に返すバッチサーバ。

## ファイル

| ファイル | 役割 |
| --- | --- |
| `irodori_batch_server.py` | HTTP サーバ本体 (stdlib のみ、`ThreadingHTTPServer`) |
| `irodori_wrapper.py` | Gradio API の細かい違いを吸収するラッパー |
| `start server irodori-tts.command` | サーバ起動 (Finder ダブルクリック対応、`.command` 拡張子) |

## クイックスタート

ローカルに Irodori (Gradio) を起動した状態で、`start server irodori-tts.command` を Finder からダブルクリックする（既定で LAN 公開 bind + token 自動生成）。

CLI から直接叩く場合:

```bash
python3 mac-sidecar/irodori-tts/irodori_batch_server.py --port 9988
```

上記は `127.0.0.1` にだけ bind する。iPhone から LAN 経由で使う場合だけ、Mac 側で明示的に公開する（`.command` ランチャーは既定でこちら）:

```bash
python3 mac-sidecar/irodori-tts/irodori_batch_server.py --host 0.0.0.0 --port 9988
```

起動時に `auth token` が表示され、同じ値が `server_token.txt` に保存される。iOS 設定 → TTS で Mac サーバ URL と認証トークンを入れる。

バッチサーバーが代理接続できる Gradio は Mac 上の loopback (`127.0.0.1` / `localhost`) に限定される。既定の許可ポートは `7860,7861`。変更が必要な場合は `IRODORI_ALLOWED_GRADIO_PORTS` を設定する。

送信された本文と参照音声 base64 は処理中メモリ上だけで扱い、`payload.json` として保存しない。生成結果と `job.json` は retention 経過後に削除され、サーバー再起動後に残っていた完了済みジョブも prune 対象になる。

## 環境変数

| 変数 | 用途 |
| --- | --- |
| `IRODORI_BATCH_JOB_ROOT` | ジョブ保管ディレクトリ (既定 `~/Library/Caches/LiberaroIrodoriBatchJobs`) |
| `IRODORI_BATCH_RETENTION_SECONDS` | 終端ジョブの保持秒数 (既定 86400) |
| `IRODORI_BATCH_AUTH_TOKEN` | 明示的に使う認証トークン。未指定なら token file から読み込み、なければ自動生成 |
| `IRODORI_ALLOWED_GRADIO_PORTS` | sidecar が接続できる loopback Gradio ポートのカンマ区切り allowlist (既定 `7860,7861`) |

詳細はソース冒頭の docstring を参照。
