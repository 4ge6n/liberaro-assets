#!/bin/bash
# Liberaro Upscale Server 用の ncnn-vulkan バイナリ + モデル一式を
# ~/.liberaro/bin/ にインストールするスクリプト。sudo 不要。
#
# 使い方: Finder からダブルクリック、または `bash mac-sidecar/upscale/install_ncnn_vulkan.command`。
# 再実行しても安全（既にインストール済みのものはスキップ）。

set -eo pipefail

ROOT_DIR="$HOME/.liberaro"
# エンジンごとにサブディレクトリを分ける。models-* / models が他エンジンと混ざらないように。
W2X_DIR="$ROOT_DIR/waifu2x"
RCUGAN_DIR="$ROOT_DIR/realcugan"
RESRGAN_DIR="$ROOT_DIR/realesrgan"
# 互換: 旧構成 (~/.liberaro/bin) があれば作り直すため一旦パージする
LEGACY_DIR="$ROOT_DIR/bin"

TMP_DIR="$(mktemp -d -t liberaro-install)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$W2X_DIR" "$RCUGAN_DIR" "$RESRGAN_DIR"

# 旧構成があれば一度退避（再実行時に古いファイルが残らないように）
if [[ -d "$LEGACY_DIR" ]]; then
  echo "[!] 旧レイアウト ($LEGACY_DIR) を検出 → 退避して再構築します"
  mv "$LEGACY_DIR" "$LEGACY_DIR.legacy.$(date +%s)"
fi

# ---- 共通ダウンロード関数 ----
fetch_unzip() {
  local label="$1" url="$2" zipname="$3" target_dir="$4"
  echo
  echo "==> $label"
  if [[ -f "$target_dir/.installed" ]]; then
    echo "    既にインストール済み（再インストールは $target_dir/.installed を消して下さい）"
    return 0
  fi
  echo "    download: $url"
  curl -L --fail --show-error --progress-bar -o "$TMP_DIR/$zipname" "$url"
  echo "    unzip..."
  unzip -q -o "$TMP_DIR/$zipname" -d "$TMP_DIR"
}

# ---- 1. waifu2x-ncnn-vulkan ----
fetch_unzip "waifu2x" \
  "https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/20220728/waifu2x-ncnn-vulkan-20220728-macos.zip" \
  "waifu2x.zip" "$W2X_DIR"
W2X_SRC="$TMP_DIR/waifu2x-ncnn-vulkan-20220728-macos"
if [[ -d "$W2X_SRC" ]]; then
  cp    "$W2X_SRC/waifu2x-ncnn-vulkan" "$W2X_DIR/"
  cp -R "$W2X_SRC/"models-* "$W2X_DIR/" 2>/dev/null || true
  touch "$W2X_DIR/.installed"
fi

# ---- 2. realcugan-ncnn-vulkan ----
fetch_unzip "realcugan" \
  "https://github.com/nihui/realcugan-ncnn-vulkan/releases/download/20220728/realcugan-ncnn-vulkan-20220728-macos.zip" \
  "realcugan.zip" "$RCUGAN_DIR"
RCUGAN_SRC="$TMP_DIR/realcugan-ncnn-vulkan-20220728-macos"
if [[ -d "$RCUGAN_SRC" ]]; then
  cp    "$RCUGAN_SRC/realcugan-ncnn-vulkan" "$RCUGAN_DIR/"
  cp -R "$RCUGAN_SRC/"models-* "$RCUGAN_DIR/" 2>/dev/null || true
  touch "$RCUGAN_DIR/.installed"
fi

# ---- 3. realesrgan-ncnn-vulkan ----
# xinntao の release zip はモデルを同梱していないため、バイナリと models を別々に取る。
# モデルは upscayl が公開している realesr-animevideov3 シリーズを採用
# （アニメ・漫画向けで本プロジェクトの主要ユースケース）。
fetch_unzip "realesrgan" \
  "https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases/download/v0.2.0/realesrgan-ncnn-vulkan-v0.2.0-macos.zip" \
  "realesrgan.zip" "$RESRGAN_DIR"
RE_SRC="$TMP_DIR/realesrgan-ncnn-vulkan-v0.2.0-macos"
if [[ -d "$RE_SRC" ]]; then
  cp "$RE_SRC/realesrgan-ncnn-vulkan" "$RESRGAN_DIR/"
fi

# モデルを個別ダウンロード。realesrgan のデフォルトモデル探索パスはバイナリと同階層の `models/`。
# ローカル CoreML と同等の x4plus / x4plus-anime(6B) を含める（アニメ・写真ともローカル相当の画質）。
# animevideov3 も override 用に同梱。配布元は ncnn 版を同梱している Tohrusky/realesrgan-ncnn-py。
RE_MODELS_DIR="$RESRGAN_DIR/models"
if [[ ! -f "$RESRGAN_DIR/.models_installed" ]]; then
  echo "    download realesrgan models (x4plus / x4plus-anime / animevideov3)..."
  mkdir -p "$RE_MODELS_DIR"
  RE_MODEL_BASE="https://github.com/Tohrusky/realesrgan-ncnn-py/raw/main/src/realesrgan_ncnn_py/models"
  for variant in \
    realesrgan-x4plus realesrgan-x4plus-anime \
    realesr-animevideov3-x2 realesr-animevideov3-x3 realesr-animevideov3-x4; do
    for ext in param bin; do
      curl -sL --fail --show-error -o "$RE_MODELS_DIR/$variant.$ext" \
        "$RE_MODEL_BASE/$variant.$ext" \
        || echo "      [WARN] $variant.$ext を取得できませんでした"
    done
  done
  touch "$RESRGAN_DIR/.models_installed"
fi

[[ -x "$RESRGAN_DIR/realesrgan-ncnn-vulkan" ]] && touch "$RESRGAN_DIR/.installed"

# ---- 4. 実行権限 + Gatekeeper の quarantine 解除 ----
echo
echo "==> 実行権限と quarantine 解除"
for triple in \
  "$W2X_DIR:waifu2x-ncnn-vulkan" \
  "$RCUGAN_DIR:realcugan-ncnn-vulkan" \
  "$RESRGAN_DIR:realesrgan-ncnn-vulkan"
do
  dir="${triple%%:*}"; bin="${triple##*:}"
  if [[ -f "$dir/$bin" ]]; then
    chmod +x "$dir/$bin"
    xattr -d com.apple.quarantine "$dir/$bin" 2>/dev/null || true
    echo "    $dir/$bin"
  fi
done

# ---- 5. 動作確認（簡易） ----
echo
echo "==> 動作確認"
for triple in \
  "$W2X_DIR:waifu2x-ncnn-vulkan" \
  "$RCUGAN_DIR:realcugan-ncnn-vulkan" \
  "$RESRGAN_DIR:realesrgan-ncnn-vulkan"
do
  dir="${triple%%:*}"; bin="${triple##*:}"
  if [[ -x "$dir/$bin" ]]; then
    "$dir/$bin" -h >/dev/null 2>&1 || true
    echo "    [OK] $bin"
  else
    echo "    [NG] $bin (見つからない)"
  fi
done

# ---- 6. モデル一覧 ----
echo
echo "==> インストールされたモデル"
for triple in \
  "waifu2x:$W2X_DIR" \
  "realCUGAN:$RCUGAN_DIR" \
  "realESRGAN:$RESRGAN_DIR"
do
  name="${triple%%:*}"; dir="${triple##*:}"
  echo "    [$name]"
  find "$dir" -maxdepth 1 \( -type d -name "models-*" -o -type d -name "models" \) 2>/dev/null \
    | sed "s|^$dir/|      |"
done

echo
echo "================================================================"
echo "完了。次に Finder から 'start server upscale.command' をダブルクリック"
echo "すると 3 つのエンジンが (not found) ではなくパス表示に変わります。"
echo "インストール先:"
echo "  $W2X_DIR"
echo "  $RCUGAN_DIR"
echo "  $RESRGAN_DIR"
echo "================================================================"

# Terminal をすぐ閉じずに確認できるよう少し止める
read -n 1 -s -r -p "Press any key to close..."
echo
