# Real-CUGAN CoreML Models (tile256)

[bilibili Real-CUGAN](https://github.com/bilibili/ailab/tree/main/Real-CUGAN) を CoreML mlpackage に変換したもの。
[Liberaro-iOS](https://github.com/4ge6n/Liberaro-iOS) の超解像エンジンで使う、256×256 タイル推論前提のモデル群。

## モデル一覧

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

## ライセンス

元モデルは bilibili 公式 Real-CUGAN（[ailab リポジトリ](https://github.com/bilibili/ailab) の MIT ライセンス）に従います。CoreML 変換物もそれに準じます。

## 使い方（Liberaro-iOS から）

アプリの「設定 → 画像超解像 → エンジン: Real-CUGAN」でモデルを選んでダウンロードすると、各 zip を取得して展開し、デバイス上で `MLModel.compileModel` を実行して `.mlmodelc` 化します。
