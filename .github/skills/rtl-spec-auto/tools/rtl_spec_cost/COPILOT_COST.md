# GitHub Copilot CLIのコスト計測

Python標準ライブラリだけで、取得→計算→工程別一覧を作成する。既存仕様生成スキルとは独立したコマンドなので、執筆・レビューの呼出しを包んで利用できる。文書の品質評価はこのツールの対象外。

## 分かるもの

| 出力 | 根拠 | 意味 |
|---|---|---|
| 工程・回・モデル別のトークンと料金 | CopilotのOTelログ＋指定料金表 | 提供ログに対する標準単価推計 |
| nano AIU / AIU | 最上位invoke_agent | CLIが報告した使用量。親子は重複加算しない |
| gross / discount / net | 任意で取り込むGitHub billing API JSON | 所有者・期間全体のGitHub報告値。個々の実行へ配賦しない |

`github.copilot.cost` は料金倍率であってドルではない。AIUとAI creditの対応は請求側で未照合のため、AIUからドルへ直接換算しない。料金表推計は別に計算する。他サービスのクレジットとは別の単位。

## 1. 料金表を選ぶ

同梱 `copilot-rates-2026-09-24.json` は2026-09-24に確認した公開価格の固定スナップショット。Astra/Sol/Luna等8モデルを登録済み。実行時は `--rates` で明示する。新価格に更新するときは新しい日付のJSONを作り、過去のカードを上書きしない。

単価の並びは通常入力／cache read／cache write／出力、単位はUSD / 100万tokens。非適用cache writeは `null`。モデル名はログのresponse modelと完全一致が必要で、曖昧な別名やAutoへ単価を推測して割り当てない。追加モデルも同じschemaで登録する。将来の価格が自動取得される機能はない。

料金式：

```text
通常入力 = 総入力 − cache read − cache write
USD = (通常入力×単価 + cache read×単価 + cache write×単価 + 出力×単価) / 1,000,000
AI credits = USD / 0.01
```

長文閾値は各chatの総入力（cacheを含む）で判定する。複数呼出しの合計で閾値を判定しない。推論トークンは出力に重複加算しない。公開標準価格の換算であり、含有枠・契約割引・Auto割引・高速モード等の個別条件、税・為替・固定月額は含めない。別単価のモードでは対応するカード・モデル識別が必要。

## 2. 工程を実行して計測する

Copilot CLIを導入・認証済みの環境で、実際の執筆コマンドを `--` の後に渡す。次の実行例はCopilotを起動するため、通常の契約・課金条件が適用される。

```powershell
python <skill>/tools/rtl_spec_cost/copilot_cost.py run --stage writer --round 1 --label example-module --rates <skill>/tools/rtl_spec_cost/copilot-rates-2026-09-24.json --out work/copilot-cost/example-module/writer-r1 -- copilot --agent rtl-spec-writer --model <available-model> -p "対象フォルダのTASK.mdに従って仕様を作成する"
```

プロンプトの対象フォルダは実際の準備済み材料に合わせる。レビューは `--stage reviewer`、修正は `--round 2` 等にする。出力先は毎回新しくし、再実行で原本を上書きしない。Windowsでは必要に応じCopilot実行ファイルのフルパスを指定する。

出力：

```text
writer-r1/
  telemetry.jsonl         OTel原本
  command-output.txt      子プロセスのstdout/stderr
  execution.json          開始/終了UTC・終了コード・計測状態
  usage.json              使用量の解析結果
  cost/
    REPORT.md             閲覧用一覧
    report.json           1リクエストごとの内訳、適用モデル・tier・単価
    usage.json            root/chatの別集計、欠測診断
    sources.json          原本のパス・SHA256
    sources/              使用ログ、料金表、実行記録のコピー
```

OTelはローカルファイル出力を要求し、本文収集を無効にする。既存の管理ポリシーを変更する機能ではない。`command-output.txt` と既存ログには本文が含まれ得るため、共有対象は選別する。CLI設定・版によりログ形式が変わる可能性がある。対象環境で `copilot --version` の結果も保存することを推奨する。

実行が失敗しても観測できた消費量を保存する。終了コード0でもログが欠測なら計測成功にしない。実行の成功、仕様の完成、費用の計算可否は別の状態。

## 3. 執筆・レビュー・修正をまとめる

```powershell
python <skill>/tools/rtl_spec_cost/copilot_cost.py report --label example-module --rates <skill>/tools/rtl_spec_cost/copilot-rates-2026-09-24.json --capture work/copilot-cost/example-module/writer-r1 --capture work/copilot-cost/example-module/reviewer-r1 --capture work/copilot-cost/example-module/writer-r2 --capture work/copilot-cost/example-module/reviewer-r2 --out work/copilot-cost/example-module/summary
```

存在する工程だけ指定する。終了済みCLIのエクスポート全体を工程ごとに渡す。ファイルを途中で分割するとrootが欠ける等の問題がある。保存済みログだけなら、何度でも別の出力先・料金表で再集計できる。

```powershell
python <skill>/tools/rtl_spec_cost/copilot_cost.py report --input writer 1 path/to/writer.jsonl --input reviewer 1 path/to/reviewer.jsonl --rates <skill>/tools/rtl_spec_cost/copilot-rates-2026-09-24.json --out work/copilot-cost/recalculated
```

状態は `estimated`（提供ログを計算可能）、`incomplete`（欠測等）、`invalid`（破損・重複競合等）。終了コードは順に0/1/2。`run` の子プロセスが失敗した場合はその終了コードを優先する。

欠測は `null` / 「未確定」。計算済み小計と全体合計は分ける。rootとchatのトークン一致も確認するが、ログに丸ごと含まれない呼出しは検出できないため、エクスポート全体の完全性は常に未検証。これは実請求保証ではない。

## 4. GitHub課金側の期間集計を添付する（任意）

GitHub billing APIのAI credit使用量レスポンスをJSON保存し、`report` に `--billing path/to/billing.json` を追加する。受け付けるのは1つの期間スナップショット。複数の日次/月次スナップショットを足す機能はない。

APIパスは個人契約なら `/users/{username}/settings/billing/ai_credit/usage`、組織契約なら `/organizations/{org}/settings/billing/ai_credit/usage`。個人APIは組織が課金する使用量を返さない。課金主体に合うAPIと読取権限が必要。[GitHub Billing REST API](https://docs.github.com/en/rest/billing/usage)

APIの集計値には他作業の使用量も含まれ得る。期間・所有者・製品・総額・割引額・正味額を別欄に表示し、今回の実行費と自動照合済みとは扱わない。確定請求書ではない。APIを自動実行する処理・認証情報の保存は実装していない。

## 検証と制限

```powershell
cd <skill>
python -m unittest discover -s tools/rtl_spec_cost/tests -v
```

テストは合成ログと模擬プロセスのみ。Copilotは起動しない。手計算例、閾値境界、cache書込、欠測、重複、未知モデル、失敗実行、原本保存、請求集計の分離を検証する。この配布版では実ログ適合性・請求との一致は未検証。VS Codeの独自ログ形式、SDKセッション累計、旧premium request課金への換算は未対応。

## 根拠

- [GitHub Copilot CLI OTel](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#opentelemetry-monitoring)：ログ取得と親子の集計規則。
- [GitHubモデル価格](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)：公開単価とAI credit換算。
- [GitHub SDKの使用量](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing)：nano AIUと課金側確認の区別。
- [OpenTelemetry GenAI属性](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)：総入力にcache read/writeを含む定義。

公式資料確認日：2026-09-24。価格表とログ形式は将来変わり得るため、カードと実ログを保存して再計算可能にする。
