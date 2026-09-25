---
name: rtl-spec-orchestrator
description: rtl-spec-auto の仕様生成・独立レビュー・費用計測・品質比較・原因解析を案内する対話用窓口。
---

rtl-spec-auto/SKILL.md の「依頼に応じた入口」から対象工程を選ぶ。仕様生成の工程順序と完了判定の正本は run.py。
一括実行は run.py run、他のモデル環境では prepare / check / review-prompt を使う。
自分で本文を書いて自分で承認しない。独立コンテキストを使えない場合は draft_valid までにとどめる。
仕様生成では result.json の complete 以外を完了と報告しない。入力診断、未解決事項、成果物の場所を簡潔に報告する。
費用計測・品質比較・原因解析は対応する references/ の手順に従う。比較や机上解析の依頼から仕様の再生成・修正・有料試行を開始しない。
