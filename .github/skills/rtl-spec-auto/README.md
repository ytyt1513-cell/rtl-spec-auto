# rtl-spec-auto — Copilot CLI 向け「既存 RTL → 仕様書」スキル (人向けの説明)

既存の Verilog RTL からブラックボックス機能仕様書 (`doc/spec/<module>.md`) を無人生成するための自己完結パッケージ。
考え方は「RTL から決まるものはスクリプトが作り、モデルは文章だけを書き、機械検査と別コンテキストのレビューが判定する」。
ヘッダやポートにコメントが無い RTL でも動くように、親モジュールの接続・FSM・使用箇所を機械抽出して材料にする。
Claude Code 用の `.claude/skills/rtl-spec` (上位モデル前提、対話型) とは別物。

## 中身

| ファイル | 役割 |
|---|---|
| `SKILL.md` | Copilot が読む手順書 (自動発動 / `/rtl-spec-auto <module>`、モード、review) |
| `rules.md` | 記述ルール。`check_spec.py --strict` の検査項目と 1 対 1 |
| `scripts/rtl_extract.py` | RTL → 骨格。I/O 表、パラメータ表、親との接続 (上流 / 下流 / 未接続)、周辺接続図、動作モード表、リセット値、検証項目と境界値の候補 (根拠行付き)、各ポートの使用箇所 |
| `scripts/check_io.py` | I/O 表と RTL ポート宣言の突合 (MISSING / EXTRA / DIR / WIDTH) |
| `scripts/check_spec.py` | 章立て・表・図・文体・内部名の漏れ・幻覚識別子・インデックス。`--strict` で根拠行の照合、境界値表の書式、判定点の網羅 |
| `scripts/rtlparse.py` | 共通の Verilog パーサ (ANSI / 非 ANSI 宣言、パラメータ、インスタンスと接続、同期段、判定点、FSM の分岐、親の探索) |
| `run.sh` | 無人実行 (骨格 → `copilot -p` 執筆 → 検査 → 任意で review → 検査) |
| `deploy/` | 配布用: `DEPLOY.md` (会社 PC への展開手順、パッケージでは README.md)、`install.py` / `install.sh` (対象リポジトリまたは `~/.copilot` へコピー。`.ps1` はメールで遮断されるので同梱しない)、`pack.py` (`dist/rtl-spec-auto/` と zip を再生成) |
| `../../agents/rtl-spec-*.agent.md` | モデルを固定したカスタムエージェント (orchestrator = Luna 司令塔、writer = Opus 5 拡張、writer-lite = Luna 基本、reviewer = Sonnet 5) |

## 使い方 (Copilot CLI)

カスタムエージェント版 (司令塔 Luna が writer Opus 5 → 検査 → reviewer Sonnet 5 の順に委譲する。1 コマンドで 3 段):

```
copilot --agent rtl-spec-orchestrator -p "rec_ctrl の仕様書を作る" --allow-tool='shell(py:*),read,write' --no-ask-user
```

対話なら `copilot` を開き `/agent` で rtl-spec-orchestrator を選んで「rec_ctrl の仕様書を作って」。初回は報告に含まれる
「使用モデル」を見て、writer が Opus 5、reviewer が Sonnet 5 で動いたか (エージェント定義の `model` が効いたか) を確認する。

シェル版で 1 本 (既定: 拡張モード Opus 5、review なし):

```
.github/skills/rtl-spec-auto/run.sh rec_ctrl --review                 # 執筆 Opus 5 + review Sonnet 5 (推奨)
.github/skills/rtl-spec-auto/run.sh rec_ctrl --writer claude-sonnet-5 --review
.github/skills/rtl-spec-auto/run.sh rec_ctrl --mode basic             # Luna だけ (コメントが豊富な小モジュール向け)
```

対話で:

```
copilot
/model                                   # 使うモデルを選ぶ
/rtl-spec-auto rec_ctrl の仕様書を作って (拡張モード)
/rtl-spec-auto review rec_ctrl           # 別セッションで (執筆側の文脈を渡さない)
```

スクリプト単体 (モデル不要):

```
py .github/skills/rtl-spec-auto/scripts/rtl_extract.py rtl/rec_ctrl.v          # 骨格を標準出力へ
py .github/skills/rtl-spec-auto/scripts/check_io.py rec_ctrl
py .github/skills/rtl-spec-auto/scripts/check_spec.py --strict rec_ctrl        # 生成物向け。--all で既存全 spec (緩い)
```

`rtl/` と `doc/spec/` 以外の配置なら各スクリプトに `--rtl-dir` / `--spec-dir`、`run.sh` にも同名オプション。

## 骨格が確定するもの / モデルが書くもの

- **確定 (スクリプト)**: 章立て、I/O 表の Signal / Dir / Bits と節分け、パラメータ表、親モジュール名、上流 / 下流と信号名、
  親の外へ出る信号、未接続の出力、周辺接続図の辺、FSM の状態と遷移条件 (if の順 = 優先順)、状態ごとの出力値、リセット値、
  検証項目と境界値の候補 (基本フロー / 値の境界 / 同期・捕捉 / 回数・連続確認 / 同一サイクル競合 / パルス / リセット)、
  それぞれの根拠行。
- **モデルが書く**: 概要と用途の文、I/O の Description、モード名と `[内部:x]` の言い換え、機能詳細の箇条書き、境界値表の
  「期待動作」の確定、シーケンス図、非対応事項、まとめ。RTL から決まらないこと (親の外の発生元、設計意図) は `(推測)`、
  サイクル精度の主張は `(要シム確認)` を付けて残す。
- **rec_ctrl での照合**: 人が Claude Code で書いた 32 項目の境界値に対し、候補は 26 項目相当を機械生成できた。出ないのは
  結合 (I-01)、送信側契約 (H-02) など RTL 単体に無い情報と、「遷移を起こす入力と次状態で意味を持つ入力の同時」(C-04) の 1 件。

## 他リポジトリへの導入

1. `.github/skills/rtl-spec-auto/` と `.github/agents/rtl-spec-*.agent.md` をコピーする (Python 3 だけが必要)。
2. `rules.md` の章立て・表形式を会社の書式に合わせて直す。`check_spec.py` の `CHAPTERS` と検査項目も同じ内容に合わせる
   (ルールと検査は 1 対 1 に保つ)。
3. `run.sh` と `.agent.md` のモデル名を `copilot` の `/model` 一覧の表記に合わせる (`claude-opus-5` / `claude-sonnet-5` / `gpt-5.6-luna` は要確認)。
4. 権限プロンプトで無人実行が止まる場合は `COPILOT_EXTRA=--allow-all-tools` (サンドボックス内でのみ)。
5. 最初の 1 本は `--review` 付きで作り、review の指摘件数を見て以後の要否を決める。

## 費用の目安 (Copilot の単価表、2026-09。キャッシュが効く前提)

| 構成 | リーフ (200 行級) | 13 インスタンスのラッパ (700〜800 行) |
|---|---|---|
| 基本 (Luna) | 5〜15 クレジット | 20〜60 |
| 拡張 (Sonnet 5) + review (Sonnet 5) | 150〜200 | 200〜300 |
| 拡張 (Opus 5) + review (Sonnet 5) | 300〜380 | 350〜600 |

読む量が費用を決める。拡張モードは architecture 3 本 (約 21K トークン) が固定費として乗るため、モジュールの大小の差は 1.2〜1.6 倍。
キャッシュが効かない場合は Opus 5 の執筆だけで 1,000 を超えるので、Sonnet 5 同士に切り替える。

## 検査を既存の仕様書に掛けた結果 (2026-09-12、このリポジトリ)

`check_spec.py --all` (緩いモード) は 32 本中 14 本 OK。NG の大半は「内部信号名 / FSM 状態名を書いている」で、ルール上は違反だが
過去に人が書いた仕様書では慣習的に許してきたもの。`--strict` は根拠行の無い既存文書には使わない (生成物専用)。

## 未検証

- Copilot CLI での実行 (作成環境に Copilot CLI が無い)。`.agent.md` の `model` キー名、`--allow-tool` のフィルタ表記、モデル名は
  公開ドキュメントに基づく推定を含む。初回実行で `copilot help` と `/model` で確認する。
- 非 ANSI 宣言、always @(*) の組み合わせ FSM (blocking 代入) は骨格の材料が薄くなる (状態と遷移は出るが出力値は出ない)。
