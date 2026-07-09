#!/bin/bash
# Liberaro Irodori TTS バッチサーバをフォアグラウンドで起動する。
#
# 使い方:
#   - Finder からダブルクリック (`.command` 拡張子なので Terminal で開く)
#   - もしくは `bash "mac-sidecar/irodori-tts/start server irodori-tts.command"` で実行
#
# 前提: ローカルに Irodori (Gradio) を起動しておくこと。
#   既定では 127.0.0.1:7860 / :7861 の loopback Gradio にだけ代理接続する。
#
# 環境変数で上書き可:
#   PORT            (既定 9988)
#   HOST            (既定 0.0.0.0。iPhone から LAN 経由で使うため公開 bind)
#                    ローカルだけで使うなら HOST=127.0.0.1 を指定する。
#   IRODORI_BATCH_AUTH_TOKEN / IRODORI_BATCH_JOB_ROOT / IRODORI_BATCH_RETENTION_SECONDS
#   IRODORI_ALLOWED_GRADIO_PORTS  (既定 7860,7861)
#       未指定なら token file から読み込み、なければ自動生成する。

set -e

# このスクリプトの場所 (mac-sidecar/irodori-tts/) を起点に作業ディレクトリを決める。
# Finder からダブルクリックされても、ターミナルからどこで叩かれても動く。
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"
cd "$SCRIPT_DIR"

: "${PORT:=9988}"
: "${HOST:=0.0.0.0}"

# 情報表示
echo "==================== Liberaro Irodori TTS Server ================"
echo "project root: $PROJECT_ROOT"
echo "listen:       $HOST:$PORT"
echo "gradio ports: ${IRODORI_ALLOWED_GRADIO_PORTS:-7860,7861} (loopback のみ代理接続)"
echo "auth token:   起動後に表示される token を iOS 設定 → TTS へ入力してください"

# LAN IP (主に Wi-Fi en0) を表示
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
if [[ -n "$LAN_IP" ]]; then
  echo "iOS から接続:  http://$LAN_IP:$PORT"
fi

# Tailscale IP も拾えれば
if command -v tailscale >/dev/null 2>&1; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  if [[ -n "$TS_IP" ]]; then
    echo "Tailscale 経由: http://$TS_IP:$PORT"
  fi
fi
echo "停止するには Ctrl-C （または Terminal ウィンドウを閉じる）"
echo "================================================================="
echo

# 既に同ポートでサーバが動いていれば停止してから起動する。
# 前回の Terminal を Ctrl-C せずに閉じた場合の自動復旧。
EXISTING_PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
if [[ -n "$EXISTING_PID" ]]; then
  EXISTING_CMD="$(ps -p "$EXISTING_PID" -o command= 2>/dev/null || true)"
  if [[ "$EXISTING_CMD" == *irodori_batch_server* ]]; then
    echo "[既存サーバを検出 (PID $EXISTING_PID) → 停止して再起動します]"
    kill "$EXISTING_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6; do
      sleep 0.5
      if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
        break
      fi
    done
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
      echo "[開放されないので SIGKILL]"
      kill -9 "$EXISTING_PID" 2>/dev/null || true
      sleep 1
    fi
  else
    echo "[!] ポート $PORT は別プロセス (PID $EXISTING_PID) が使用中:"
    echo "    $EXISTING_CMD"
    echo "    別ポートを使うには PORT=9989 でこのスクリプトを起動してください。"
    exit 1
  fi
fi

# SCRIPT_DIR にいるので relative で OK
exec python3 ./irodori_batch_server.py --host "$HOST" --port "$PORT"
