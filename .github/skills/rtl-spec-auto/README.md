# rtl-spec-auto v4

既存 RTL から機能仕様書と期待波形を作る。テンプレートは v7 を継続し、入力の診断と工程管理を強化した。
Python が材料生成・検査・完了判定を担当し、モデルが執筆と独立レビューを担当する。

## 手動工程 (モデル環境を選ばない)

Python 3.10 以上と、SVG生成用の Node.js が必要。Python ランチャーが無ければ Python 実体を使う。

```text
python <skill>/run.py prepare <module> --rtl-dir <rtl-dir> --out <empty-directory>
# TASK.md の指示でモデルが執筆
python <skill>/run.py check <output> --draft
python <skill>/run.py review-prompt <output>
# 出力した指示を独立コンテキストに渡し、review.json を得る
python <skill>/run.py check <output>
```

<skill> はこのディレクトリの絶対パス。個人インストールでもリポジトリ配置でも同じ入口を使う。
原本・解析用コピー・診断を保存する。親 RTL は全文が標準。仕様書は出力フォルダ内だけに作成する。
詳細は [SKILL.md](SKILL.md)、[入力の範囲](references/input.md)、[レビュー](references/review.md)。

## Copilot CLI

```text
python <skill>/run.py run <module> --rtl-dir <rtl-dir> --model <available-model> --rounds 3
```

--model / --reviewer は任意。指定しなければ環境のモデル設定を使う。
既定は1ラウンド。最大3ラウンドまで執筆・検査・独立レビューを反復できる。
新しい出力フォルダを work/<module>/<run-id>/ に作る。完了判定の正本は result.json。
Copilot のCLI互換性と権限は利用環境で確認が必要。自動的に包括権限へ切り替えない。

| 状態 | 意味 |
|---|---|
| input_blocked | 入力の解釈・構成を解決する必要あり |
| draft_valid | 機械検査合格。独立レビュー前 |
| complete | 最新成果物の機械検査と独立承認が揃った |
| needs_revision | 反復上限までに承認できなかった |
| failed / writer_failed / reviewer_failed | 検査・外部プロセス失敗。ログを参照 |

意味レビューはモデルによる評価であり、RTLの形式検証・シミュレーションの代替ではない。
レビュー未実施、古い承認、波形NG、画像欠落、描画失敗を complete としない。

## 検証と配布

`python -m unittest discover -s <skill>/tests -v` で入力・検査・工程の回帰試験。
テストのバックエンド代役は工程制御の検証用で、モデル品質の検証とは区別する。
インストールは [deploy/DEPLOY.md](deploy/DEPLOY.md)。配布物には利用側に不要なパッケージ作成・公開スクリプトを含めない。

旧 v1 の9章形式ツールは互換用に残す。新規生成には run.py と v7 テンプレートを使う。
費用の推測値は置かない。実行環境のモデル・時間・使用量と、指摘/修正回数を実測記録する。

## 比較と原因解析（任意の別工程）

- [Copilotコスト計測](references/cost.md)：OTelの保存・標準価格による推計・欠測診断。料金表は日付付き固定版。
- [品質比較](references/quality.md)：固定した基準に対する独立採点と反証、4軸評価、重大欠陥の別判定。
- [原因解析](references/cause-analysis.md)：採点根拠を引き継ぎ、事象・課題・原因・施策・効果を区別する。

品質比較・原因解析はモデルが実施する手順であり、専用の自動採点CLIは含まない。
仕様生成の通常ループには自動挿入せず、依頼された工程だけを実行する。
