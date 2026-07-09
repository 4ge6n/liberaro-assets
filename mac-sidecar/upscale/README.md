# mac-sidecar/upscale

iOS アプリの「Home Mac Backend」経路を提供するアップスケールサーバ。
`waifu2x-ncnn-vulkan` / `realcugan-ncnn-vulkan` / `realesrgan-ncnn-vulkan` を
subprocess で呼び、結果を iOS に返す。

## ファイル

| ファイル | 役割 |
| --- | --- |
| `liberaro_upscale_server.py` | HTTP サーバ本体 (stdlib のみ) |
| `start server upscale.command` | サーバ起動 (Finder ダブルクリック対応、`.command` 拡張子) |
| `install_ncnn_vulkan.command` | ncnn-vulkan バイナリとモデルを `~/.liberaro/<engine>/` に配置 |
| `smoke_test_mac_server.py` | サーバ動作確認スクリプト (PNG 生成含む) |
| `com.liberaro.upscale.plist` | 常駐させるための launchd 設定 |

## クイックスタート

```bash
# 1. ncnn-vulkan + モデルをインストール (sudo 不要、~/.liberaro/ 配下)
open mac-sidecar/upscale/install_ncnn_vulkan.command

# 2. サーバ起動
open "mac-sidecar/upscale/start server upscale.command"
```

サーバが起動すると `http://<LAN-IP>:8088` と Tailscale IP が表示されます。
これを iOS 設定 → アップスケール実行先 → LAN URL に入れ、同じログに表示される
`auth token` も認証トークン欄に入れて疎通テスト。

token は既定で `~/Library/Caches/LiberaroUpscaleJobs/server_token.txt` に保存され、
次回起動でも同じ値を再利用します。明示したい場合は `LIBERARO_UPSCALE_AUTH_TOKEN`
または `--token` を指定できます。

大きすぎるリクエストはサーバ側で拒否します。既定は multipart body 80MiB、入力画像 60MiB、
入力画像 120MP、scale 最大 4 です。必要な場合は `LIBERARO_UPSCALE_MAX_*` 環境変数で調整できます。

## メモリ管理とジョブキュー (0.3.0+)

- `POST /jobs` はジョブを**キューに積むだけ**。実行は固定数のワーカースレッドが順に行うので、
  iOS から何百ページ一括投入されても ncnn プロセスは同時に `LIBERARO_UPSCALE_MAX_WORKERS`
  本（既定 **1**）しか走らない。メモリに余裕がある Mac だけ 2 以上に上げる。
- アップロードボディの同時 RAM バッファは `LIBERARO_UPSCALE_MAX_CONCURRENT_UPLOADS`
  本（既定 2）に制限される。
- それでも GPU メモリ不足で ncnn が落ちる場合は `LIBERARO_UPSCALE_TILE_SIZE=200`〜`400` を
  指定するとタイル分割サイズが固定され、1 プロセスの GPU 使用量が抑えられる（既定は自動）。
- **サーバ再起動時、queued / processing だった job は入力が残っていれば自動で再実行**される。
  Mac がスリープ・クラッシュしても、サーバを再起動すれば残りのバッチが流れ、iOS 側は同じ
  jobID で結果を回収できる（結果は完了後 24h 保持）。
- `GET /jobs` で全ジョブの状態一覧が取れる（診断用）。
- `GET /progress` (0.4.0+) でキュー全体の集計が取れる。`total / queued / processing /
  done / failed / cancelled / remaining` と、完了実績から推定した `avgSeconds / etaSeconds`
  を返す。iOS 側はこれを軽くポーリングして「Mac 全体: 残り N枚 / 約M分」を表示する。

端末ログは通常、ジョブ状態だけを表示します。HTTP の `200` ログも見たい場合は
`LIBERARO_UPSCALE_HTTP_LOG=1` を付けて起動してください。

## 動作確認

```bash
# サーバが動いている状態で
python3 mac-sidecar/upscale/smoke_test_mac_server.py \
  --base http://127.0.0.1:8088 \
  --token "$(cat ~/Library/Caches/LiberaroUpscaleJobs/server_token.txt)" \
  --engine waifu2x --model cunet --scale 2 --noise 2
# OK <ms>ms
```

## 常駐させる (launchd)

```bash
cp mac-sidecar/upscale/com.liberaro.upscale.plist ~/Library/LaunchAgents/
$EDITOR ~/Library/LaunchAgents/com.liberaro.upscale.plist  # パスを書き換え
launchctl load -w ~/Library/LaunchAgents/com.liberaro.upscale.plist
```

## 詳細ドキュメント

- 運用全般: [`../../docs/mac-backend-setup.md`](../../docs/mac-backend-setup.md)
- 経路全体: [`../../docs/upscale-system.md`](../../docs/upscale-system.md)
- API: `liberaro_upscale_server.py` 冒頭の docstring
