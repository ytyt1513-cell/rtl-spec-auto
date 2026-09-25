# rtl-spec-auto v4 配布パッケージ（v0.2.0）

既存 RTL から機能仕様書 (テンプレート v7) と期待波形を作る。
公開リポジトリと Releases の ZIP から導入できる。
[リポジトリ](https://github.com/ytyt1513-cell/rtl-spec-auto) / [リリース](https://github.com/ytyt1513-cell/rtl-spec-auto/releases)。

## 導入

Python 3.10 以上、Node.js、Copilot 自動実行を使う場合は Copilot CLI と認証済みアカウントが必要。
CLI以外のモデルでも prepare / check / review-prompt の手動工程は共通で使える。

```text
python install.py <target-repository>
# または
python install.py --user
```

リポジトリ配置は .github/skills/rtl-spec-auto/ と .github/agents/。
個人配置は ~/.copilot/skills/rtl-spec-auto/ と ~/.copilot/agents/。
同名ファイルは更新し、利用者が追加したファイルは削除しない。配布物の VERSION は内容ハッシュを含む。

WaveDrom の依存は配置先の scripts/wavedrom/ で `npm ci` を実行する。
package-lock.json を同梱し、描画依存を固定する。Pythonは標準ライブラリのみ。

## 実行

<skill> は配置したスキルの絶対パス。空の出力先を使う。

```text
python <skill>/run.py prepare <module> --rtl-dir <source-directory> --out <output-directory>
python <skill>/run.py check <output-directory> --draft
python <skill>/run.py review-prompt <output-directory>
python <skill>/run.py check <output-directory>
```

prepare と check の間に TASK.md に従ってモデルが執筆する。
review-prompt の出力は執筆と別のコンテキストに渡し、review.json を得てから最終 check を行う。
Copilot でまとめて実行する場合:

```text
python <skill>/run.py run <module> --rtl-dir <source-directory> --model <available-model> --rounds 3
```

モデルは利用環境の一覧から指定。未指定なら環境設定を使う。既定の反復回数は1、上限3。
run.sh は Python 入口への互換ラッパ。Windows PowerShell では run.py を直接使う。

## 入力と成果物

サブディレクトリ、.v/.sv、ファイル名と異なる module 名、複数 module、位置接続を扱う。
別の文字コードは --encoding cp932 等。ビルド対象を絞る場合は --file-list <list>。
未対応の構文、マクロ、重複 module 等は診断して停止する。generate/IPの内部・位相などは原本と独立レビューで確認する。
詳しくは配置先の references/input.md。

結果の正本は result.json。complete だけが文書作成・レビュー完了。
draft_valid は機械検査だけの合格。失敗・未実施・上限到達を完了と扱わない。
本文と参照する図を相対配置を保って納める。仕様書変更後の承認は取り直す。
作業フォルダには RTL 原本の写しがあるため、配布する文書と作業フォルダを区別する。

## 利用環境での確認

Python/Nodeと描画依存、CLIのバージョン・モデル・ツール権限を確認する。
`python -m unittest discover -s <skill>/tests -v` はネットワーク・Copilotを使わず回帰試験を実行する。
代表的な小回路、CDC、ラッパ、大きな制御回路で手直し量と時間・使用量を測る。
スキルのテンプレートと検査器を更新したら同じ入力で再評価する。

CLIの参考: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference

## v0.2.0 の追加機能

配置先の `SKILL.md` から目的別の手順を選ぶ。

| 目的 | 配置先からの相対パス | 方式 |
|---|---|---|
| Copilot使用量・費用 | references/cost.md、tools/rtl_spec_cost/copilot_cost.py | ローカルOTel取得、保存ログ集計、固定料金表による推計 |
| 文書品質比較 | references/quality.md | 基準を原本照合して凍結、独立採点と反証、4軸＋重大欠陥の判定 |
| 採点後の原因解析 | references/cause-analysis.md | 根拠を保存した引き継ぎと5階層の解析 |

品質比較・原因解析はモデル用の手順であり、自動採点CLIではない。生成ループへの自動挿入もしない。
コストのテストは `<skill>` に移動して `python -m unittest discover -s tools/rtl_spec_cost/tests -v`。
`tools/rtl_spec_cost/examples/` の合成ログなら、Copilotやネットワークなしで計算例を試せる。
料金表は2026-09-24の固定スナップショット。実CLIログへの適合性、請求一致、モデル間の同品質は未保証。
