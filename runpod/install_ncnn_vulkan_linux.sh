#!/bin/bash
# Colab / Kaggle 等の Linux 環境で ncnn-vulkan バイナリ + モデルをセットアップする。
# Mac 用 install_ncnn_vulkan.command の Linux 版。sudo 不要、ユーザーホーム配下で完結。
#
# 使い方:
#     bash cloudflare/scripts/install_ncnn_vulkan_linux.sh realesrgan
#
# あるいは Colab/Kaggle セルから:
#     !bash <(curl -sL https://raw.githubusercontent.com/4ge6n/Liberaro-iOS/main/cloudflare/scripts/install_ncnn_vulkan_linux.sh)
#
# 引数なしなら全 engine、引数ありなら指定 engine だけ入れる。
# colab_worker.py はジョブを拾った時に必要な engine だけ遅延インストールする。
#
# インストール後の各 engine 用パスは環境変数経由で colab_worker.py に渡される。
#     export LIBERARO_WAIFU2X_BIN=~/.liberaro/waifu2x/waifu2x-ncnn-vulkan
#     etc.

set -eo pipefail

ROOT_DIR="$HOME/.liberaro"
W2X_DIR="$ROOT_DIR/waifu2x"
RCUGAN_DIR="$ROOT_DIR/realcugan"
RESRGAN_DIR="$ROOT_DIR/realesrgan"

TMP_DIR="$(mktemp -d -t liberaro-install-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$W2X_DIR" "$RCUGAN_DIR" "$RESRGAN_DIR"

if [[ "$#" -gt 0 ]]; then
  INSTALL_ENGINES="$*"
else
  INSTALL_ENGINES="${LIBERARO_INSTALL_ENGINES:-all}"
fi

normalize_engine() {
  case "$1" in
    waifu2x|w2x) echo "waifu2x" ;;
    realcugan|realCUGAN|cugan) echo "realcugan" ;;
    realesrgan|realESRGAN|esrgan) echo "realesrgan" ;;
    all) echo "all" ;;
    *) echo "$1" ;;
  esac
}

should_install() {
  local wanted
  for wanted in $INSTALL_ENGINES; do
    wanted="$(normalize_engine "$wanted")"
    [[ "$wanted" == "all" || "$wanted" == "$1" ]] && return 0
  done
  return 1
}

# 必要な前提パッケージ（Vulkan loader と unzip）が揃っているか確認し、
# 足りないものだけ apt で補う。
# - Kaggle / 一部の Colab は NVIDIA GPU は見えていても libvulkan.so.1 が抜けている。
# - runpod/pytorch などの slim 系イメージは unzip が入っておらず、ここを抜くと
#   後段の `unzip` で set -e により即死する。
ensure_prereqs() {
  local missing=()
  if ! ldconfig -p 2>/dev/null | grep -q 'libvulkan.so.1'; then
    missing+=(libvulkan1 vulkan-tools)
  fi
  if ! command -v unzip >/dev/null 2>&1; then
    missing+=(unzip)
  fi
  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi
  echo "==> 前提パッケージの確認"
  echo "    不足: ${missing[*]}"
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "    [WARN] apt-get が無いため自動インストールできません"
    return 0
  fi
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  else
    echo "    [WARN] root/sudo ではないため自動インストールできません"
  fi
}

# ---- 共通ダウンロード関数 ----
fetch_unzip() {
  local label="$1" url="$2" zipname="$3" target_dir="$4"
  echo
  echo "==> $label"
  if [[ -f "$target_dir/.installed" ]]; then
    echo "    既にインストール済み ($target_dir/.installed)"
    return 0
  fi
  echo "    download: $url"
  curl -L --fail --show-error --progress-bar -o "$TMP_DIR/$zipname" "$url"
  echo "    unzip..."
  unzip -q -o "$TMP_DIR/$zipname" -d "$TMP_DIR"
}

# ---- Vulkan ICD / unzip の有無確認（ない場合は apt で補う） ----
ensure_prereqs

echo "==> Vulkan サポートの確認"
if command -v vulkaninfo >/dev/null 2>&1; then
  vulkaninfo --summary 2>/dev/null | grep -E "GPU|Driver" | head -5 || true
else
  echo "    [WARN] vulkaninfo コマンドなし。Colab/Kaggle のデフォルトは NVIDIA GPU 環境"
  echo "    [WARN] で Vulkan ICD が同梱されている想定。NVIDIA ドライバが入っていれば動きます。"
fi
echo "    nvidia-smi:" && (nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "    (なし)")

# ---- 1. waifu2x-ncnn-vulkan (Linux) ----
if should_install "waifu2x"; then
  fetch_unzip "waifu2x" \
    "https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/20220728/waifu2x-ncnn-vulkan-20220728-ubuntu.zip" \
    "waifu2x.zip" "$W2X_DIR"
  W2X_SRC="$TMP_DIR/waifu2x-ncnn-vulkan-20220728-ubuntu"
  if [[ -d "$W2X_SRC" ]]; then
    cp    "$W2X_SRC/waifu2x-ncnn-vulkan" "$W2X_DIR/"
    cp -R "$W2X_SRC/"models-* "$W2X_DIR/" 2>/dev/null || true
    touch "$W2X_DIR/.installed"
  fi
else
  echo
  echo "==> waifu2x は今回スキップ"
fi

# ---- 2. realcugan-ncnn-vulkan (Linux) ----
if should_install "realcugan"; then
  fetch_unzip "realcugan" \
    "https://github.com/nihui/realcugan-ncnn-vulkan/releases/download/20220728/realcugan-ncnn-vulkan-20220728-ubuntu.zip" \
    "realcugan.zip" "$RCUGAN_DIR"
  RCUGAN_SRC="$TMP_DIR/realcugan-ncnn-vulkan-20220728-ubuntu"
  if [[ -d "$RCUGAN_SRC" ]]; then
    cp    "$RCUGAN_SRC/realcugan-ncnn-vulkan" "$RCUGAN_DIR/"
    cp -R "$RCUGAN_SRC/"models-* "$RCUGAN_DIR/" 2>/dev/null || true
    touch "$RCUGAN_DIR/.installed"
  fi
else
  echo
  echo "==> realcugan は今回スキップ"
fi

# ---- 3. realesrgan-ncnn-vulkan (Linux) + モデル ----
if should_install "realesrgan"; then
  fetch_unzip "realesrgan" \
    "https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases/download/v0.2.0/realesrgan-ncnn-vulkan-v0.2.0-ubuntu.zip" \
    "realesrgan.zip" "$RESRGAN_DIR"
  RE_SRC="$TMP_DIR/realesrgan-ncnn-vulkan-v0.2.0-ubuntu"
  if [[ -d "$RE_SRC" ]]; then
    cp "$RE_SRC/realesrgan-ncnn-vulkan" "$RESRGAN_DIR/"
    cp -R "$RE_SRC/models" "$RESRGAN_DIR/" 2>/dev/null || true
  fi

  RE_MODELS_DIR="$RESRGAN_DIR/models"
  if [[ ! -f "$RESRGAN_DIR/.models_installed" ]]; then
    echo "    download realesrgan models (x4plus / x4plus-anime / animevideov3)..."
    mkdir -p "$RE_MODELS_DIR"
    # ローカル CoreML 同等の x4plus / x4plus-anime(6B) を含める。配布元は ncnn 版同梱の Tohrusky。
    RE_MODEL_BASE="https://github.com/Tohrusky/realesrgan-ncnn-py/raw/main/src/realesrgan_ncnn_py/models"
    for variant in \
      realesrgan-x4plus realesrgan-x4plus-anime \
      realesr-animevideov3-x2 realesr-animevideov3-x3 realesr-animevideov3-x4; do
      for ext in param bin; do
        curl -sL --fail --show-error -o "$RE_MODELS_DIR/$variant.$ext" \
          "$RE_MODEL_BASE/$variant.$ext" \
          || echo "      [WARN] $variant.$ext 取得失敗"
      done
    done
    touch "$RESRGAN_DIR/.models_installed"
  fi

  [[ -x "$RESRGAN_DIR/realesrgan-ncnn-vulkan" ]] && touch "$RESRGAN_DIR/.installed"
else
  echo
  echo "==> realesrgan は今回スキップ"
fi

# ---- 4. 実行権限 + 動作確認 ----
echo
echo "==> 実行権限と動作確認"
for triple in \
  "$W2X_DIR:waifu2x-ncnn-vulkan" \
  "$RCUGAN_DIR:realcugan-ncnn-vulkan" \
  "$RESRGAN_DIR:realesrgan-ncnn-vulkan"
do
  dir="${triple%%:*}"; bin="${triple##*:}"
  if [[ -f "$dir/$bin" ]]; then
    chmod +x "$dir/$bin"
    "$dir/$bin" -h >/dev/null 2>&1 || true
    echo "    [OK] $dir/$bin"
  else
    echo "    [NG] $bin (見つからない)"
  fi
done

# ---- 5. 環境変数を書き出してくれる sourceable な行を表示 ----
echo
echo "==> colab_worker.py を起動する前に以下の環境変数を設定してください:"
cat <<EOF
export LIBERARO_WAIFU2X_BIN=$W2X_DIR/waifu2x-ncnn-vulkan
export LIBERARO_REALCUGAN_BIN=$RCUGAN_DIR/realcugan-ncnn-vulkan
export LIBERARO_REALESRGAN_BIN=$RESRGAN_DIR/realesrgan-ncnn-vulkan
export LIBERARO_WAIFU2X_MODELS_DIR=$W2X_DIR
export LIBERARO_REALCUGAN_MODELS_DIR=$RCUGAN_DIR
export LIBERARO_REALESRGAN_MODELS_DIR=$RESRGAN_DIR
# それから:
export LIBERARO_WORKER_BASE=https://<your>.workers.dev
export LIBERARO_WORKER_TOKEN=<WORKER_AUTH_TOKEN>
python3 cloudflare/scripts/colab_worker.py
EOF
