# 合成ログ

`writer-r1.jsonl` と `reviewer-r1.jsonl` は計算例のために作ったOTel形式のデータ。
Copilotを動かした記録ではなく、使用量・時刻・モデル名・AIUは全て試験値。
AIUは料金表換算との一致を表す値として作成していない。

```powershell
python <skill>/tools/rtl_spec_cost/copilot_cost.py report --input writer 1 <skill>/tools/rtl_spec_cost/examples/writer-r1.jsonl --input reviewer 1 <skill>/tools/rtl_spec_cost/examples/reviewer-r1.jsonl --rates <skill>/tools/rtl_spec_cost/copilot-rates-2026-09-24.json --label synthetic-example --out work/copilot-cost/example
```

このコマンドはネットワークもCopilotも使用しない。指定料金表での手計算値は執筆0.0101 USD、レビュー0.0066 USD、計0.0167 USD / 1.67 AI credits。
