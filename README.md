# Liberaro-iOS Upscaler CoreML Models

[Liberaro-iOS](https://github.com/4ge6n/Liberaro-iOS) の画像超解像エンジンが初回起動時に取得する CoreML モデル群。

## Real-CUGAN（直下、tile256）

[bilibili Real-CUGAN](https://github.com/bilibili/ailab/tree/main/Real-CUGAN) を 256×256 タイル推論用に CoreML mlpackage 化したもの。

| ファイル | スケール | ノイズ除去 | サイズ |
| --- | --- | --- | --- |
| `up2x_no_denoise_tile256.mlpackage.zip` | 2x | なし | ~2.4 MB |
| `up2x_conservative_tile256.mlpackage.zip` | 2x | conservative | ~2.4 MB |
| `up2x_denoise1x_tile256.mlpackage.zip` | 2x | 弱 | ~2.4 MB |
| `up2x_denoise2x_tile256.mlpackage.zip` | 2x | 中 | ~2.4 MB |
| `up2x_denoise3x_tile256.mlpackage.zip` | 2x | 強 | ~2.4 MB |
| `up3x_no_denoise_tile256.mlpackage.zip` | 3x | なし | ~2.4 MB |
| `up3x_conservative_tile256.mlpackage.zip` | 3x | conservative | ~2.4 MB |
| `up3x_denoise3x_tile256.mlpackage.zip` | 3x | 強 | ~2.4 MB |
| `up4x_no_denoise_tile256.mlpackage.zip` | 4x | なし | ~2.6 MB |
| `up4x_conservative_tile256.mlpackage.zip` | 4x | conservative | ~2.6 MB |
| `up4x_denoise3x_tile256.mlpackage.zip` | 4x | 強 | ~2.6 MB |

ライセンス: 元モデルは bilibili 公式 Real-CUGAN（MIT）に従う。

## Real-ESRGAN（`realesrgan/`、tile512+pad10）

[hanxiao/real-esrgan-coreml](https://github.com/hanxiao/real-esrgan-coreml) v1.0.0 のミラー。`xinntao/Real-ESRGAN` の公式重みを 512×512 タイル + 5px 上下左右パディング(計 522×522 入力)で CoreML 化したもの。batch=1 固定形状版のみ収録(ANE 利用の flexbatch 版は省略)。

| ファイル | スケール | 用途 | サイズ |
| --- | --- | --- | --- |
| `realesrgan/RealESRGAN_x4plus_522_fp16.zip` | 4x | 汎用（写真・カラーイラスト） | ~30 MB |
| `realesrgan/RealESRGAN_anime_6B_522_fp16.zip`（= x4plus_anime_6B） | 4x | アニメ／カラー漫画 | ~7.9 MB |
| `realesrgan/RealESRGAN_x2plus_522_fp16.zip` | 2x | 汎用 2 倍 | ~30 MB |
| `realesrgan/RealESRGAN_animevideo_522_fp16.zip` | 4x | アニメ動画フレーム | ~1.1 MB |
| `realesrgan/RealESRGAN_general_522_fp16.zip` | 4x | 汎用一般 | ~2.2 MB |

ライセンス: 元モデルは BSD 3-Clause（Real-ESRGAN）。CoreML 変換物の権利は hanxiao 氏に帰属。

## 使い方

アプリの「設定 → 画像超解像 → インストール済モデルを管理」で各エンジンのモデルを選んでダウンロードすると、zip を取得して展開し、デバイス上で `MLModel.compileModel` を実行して `.mlmodelc` 化します。
