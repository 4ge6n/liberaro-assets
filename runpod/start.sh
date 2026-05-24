#!/usr/bin/env bash
# RunPod Serverless 用ブートストラップ（公開リポ 4ge6n/realcugan-coreml-models に配置）。
#
# 目的: Docker イメージを自前ビルドせず、RunPod の汎用 GPU ベースイメージの
# 「Container Start Command」からこれ1本を叩くだけでワーカーを立ち上げる。
# 本体アプリのリポはプライベートなので、ここ（公開リポ）の raw から取得する。
#
# RunPod エンドポイントの Container Start Command 例:
#   bash -c "curl -sL https://raw.githubusercontent.com/4ge6n/realcugan-coreml-models/main/runpod/start.sh | bash"
#
# エンドポイントの Environment Variables に NVIDIA_DRIVER_CAPABILITIES=all を設定すること
# （Vulkan を含めないと ncnn-vulkan が GPU を掴めない）。
#
# ~/.liberaro 配下に入れるので、RunPod の Network Volume を /root にマウントすれば
# 2 回目以降のコールドスタートで再ダウンロードを省ける。

set -euo pipefail

RAW="${LIBERARO_RAW_BASE:-https://raw.githubusercontent.com/4ge6n/realcugan-coreml-models/main/runpod}"
WORKDIR="${LIBERARO_WORKDIR:-/opt/liberaro-runpod}"
mkdir -p "$WORKDIR"

echo "==> runpod SDK を導入"
pip install --no-cache-dir runpod >/dev/null

echo "==> ncnn-vulkan バイナリ + モデルを導入"
curl -sL --fail "$RAW/install_ncnn_vulkan_linux.sh" -o "$WORKDIR/install.sh"
bash "$WORKDIR/install.sh"

# handler.py が参照するパス（install スクリプトの配置先に合わせる）。
export LIBERARO_WAIFU2X_BIN="$HOME/.liberaro/waifu2x/waifu2x-ncnn-vulkan"
export LIBERARO_REALCUGAN_BIN="$HOME/.liberaro/realcugan/realcugan-ncnn-vulkan"
export LIBERARO_REALESRGAN_BIN="$HOME/.liberaro/realesrgan/realesrgan-ncnn-vulkan"
export LIBERARO_WAIFU2X_MODELS_DIR="$HOME/.liberaro/waifu2x"
export LIBERARO_REALCUGAN_MODELS_DIR="$HOME/.liberaro/realcugan"
export LIBERARO_REALESRGAN_MODELS_DIR="$HOME/.liberaro/realesrgan"

echo "==> handler.py を取得"
curl -sL --fail "$RAW/handler.py" -o "$WORKDIR/handler.py"

echo "==> Serverless ハンドラ起動"
exec python3 -u "$WORKDIR/handler.py"
