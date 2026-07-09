#!/bin/bash
# Liberaro Upscale Server をフォアグラウンドで起動する。
#
# 使い方:
#   - Finder からダブルクリック (`.command` 拡張子なので Terminal で開く)
#   - もしくは `bash "mac-sidecar/upscale/start server upscale.command"` で実行
#
# 環境変数で上書き可:
#   PORT            (既定 8088)
#   HOST            (既定 0.0.0.0。iPhone から使うため LAN に bind)
#   LIBERARO_UPSCALE_AUTH_TOKEN / LIBERARO_UPSCALE_AUTH_TOKEN_FILE
#       未指定なら ~/Library/Caches/LiberaroUpscaleJobs/server_token.txt を自動生成する。
#   LIBERARO_WAIFU2X_BIN / LIBERARO_REALCUGAN_BIN / LIBERARO_REALESRGAN_BIN
#       未指定なら `which` で自動検出する。検出に失敗したエンジンは
#       /health で false として返るだけでサーバ自体は起動する。

set -e

# このスクリプトの場所 (mac-sidecar/upscale/) を起点に作業ディレクトリを決める。
# Finder からダブルクリックされても、ターミナルからどこで叩かれても動く。
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"
cd "$SCRIPT_DIR"

# install_ncnn_vulkan.command が配置する `~/.liberaro/<engine>/` を最優先で見る。
# 旧構成 (~/.liberaro/bin/) も後方互換で見る。次に PATH 上の `which` を fallback。
LIBERARO_ROOT="$HOME/.liberaro"

resolve_bin() {
  local name="$1" engine_dir="$2"
  if [[ -x "$LIBERARO_ROOT/$engine_dir/$name" ]]; then
    echo "$LIBERARO_ROOT/$engine_dir/$name"
  elif [[ -x "$LIBERARO_ROOT/bin/$name" ]]; then
    echo "$LIBERARO_ROOT/bin/$name"
  else
    command -v "$name" || true
  fi
}

# 自動検出（明示指定があればそちら優先）
: "${LIBERARO_WAIFU2X_BIN:=$(resolve_bin waifu2x-ncnn-vulkan waifu2x)}"
: "${LIBERARO_REALCUGAN_BIN:=$(resolve_bin realcugan-ncnn-vulkan realcugan)}"
: "${LIBERARO_REALESRGAN_BIN:=$(resolve_bin realesrgan-ncnn-vulkan realesrgan)}"

# モデルディレクトリも明示的に渡す。バイナリと同階層を見るのが既定だが、
# モデル混在を避けるため明示する。
[[ -d "$LIBERARO_ROOT/waifu2x" ]]    && : "${LIBERARO_WAIFU2X_MODELS_DIR:=$LIBERARO_ROOT/waifu2x}"
[[ -d "$LIBERARO_ROOT/realcugan" ]]  && : "${LIBERARO_REALCUGAN_MODELS_DIR:=$LIBERARO_ROOT/realcugan}"
[[ -d "$LIBERARO_ROOT/realesrgan" ]] && : "${LIBERARO_REALESRGAN_MODELS_DIR:=$LIBERARO_ROOT/realesrgan}"
export LIBERARO_WAIFU2X_MODELS_DIR LIBERARO_REALCUGAN_MODELS_DIR LIBERARO_REALESRGAN_MODELS_DIR
: "${PORT:=8088}"
: "${HOST:=0.0.0.0}"

export LIBERARO_WAIFU2X_BIN LIBERARO_REALCUGAN_BIN LIBERARO_REALESRGAN_BIN

# 情報表示
echo "==================== Liberaro Upscale Server ===================="
echo "project root: $PROJECT_ROOT"
echo "listen:       $HOST:$PORT"
echo "auth token:   起動後に表示される token を iOS 設定へ入力してください"
echo "engines (自動検出):"
echo "  waifu2x:    ${LIBERARO_WAIFU2X_BIN:-(not found)}"
echo "  realCUGAN:  ${LIBERARO_REALCUGAN_BIN:-(not found)}"
echo "  realESRGAN: ${LIBERARO_REALESRGAN_BIN:-(not found)}"

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
# 前回の Terminal を Ctrl-C せずに閉じた場合や、私 (Claude) のテストで
# 残骸プロセスが居る場合の自動復旧。
EXISTING_PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
if [[ -n "$EXISTING_PID" ]]; then
  EXISTING_CMD="$(ps -p "$EXISTING_PID" -o command= 2>/dev/null || true)"
  if [[ "$EXISTING_CMD" == *liberaro_upscale_server* ]]; then
    echo "[既存サーバを検出 (PID $EXISTING_PID) → 停止して再起動します]"
    kill "$EXISTING_PID" 2>/dev/null || true
    # 最大 3 秒待ってポート開放を確認
    for _ in 1 2 3 4 5 6; do
      sleep 0.5
      if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
        break
      fi
    done
    # それでも開かなければ強制終了
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
      echo "[15 秒待っても開放されないので SIGKILL]"
      kill -9 "$EXISTING_PID" 2>/dev/null || true
      sleep 1
    fi
  else
    echo "[!] ポート $PORT は別プロセス (PID $EXISTING_PID) が使用中:"
    echo "    $EXISTING_CMD"
    echo "    別ポートを使うには PORT=8089 でこのスクリプトを起動してください。"
    exit 1
  fi
fi

# SCRIPT_DIR にいるので relative で OK
exec python3 ./liberaro_upscale_server.py --host "$HOST" --port "$PORT"
