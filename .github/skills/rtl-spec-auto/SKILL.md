---
name: rtl-spec-auto
description: 既存の Verilog / SystemVerilog RTL (rtl/<module>.v) からブラックボックス機能仕様書 doc/spec/<module>.md を無人で生成・更新する。「<module> の仕様書を作って」「rtl/x.v をドキュメント化」「spec を生成 / 更新 / レビュー」で使う。骨格・表・境界値候補はスクリプトが RTL から作り、モデルは文章を書き、機械検査 (check_io / check_spec --strict) と別コンテキストのレビューで品質を担保する。コメントの無い RTL でも動く。
allowed-tools: shell
---

# rtl-spec-auto — 既存 RTL から仕様書を作る (無人・根拠行付き)

## 役割分担

- スクリプト (`scripts/`) が RTL から決まるものをすべて作る: 章立て、I/O 表 (名前 / 方向 / 幅 / 節分け)、パラメータ表、
  親モジュールとの接続 (上流 / 下流、未接続ポート、周辺接続図)、動作モード表の骨格 (FSM の状態、出力値、遷移と優先順)、
  リセット値、検証項目と境界値の候補 (根拠行付き)、各ポートの使用箇所。
- モデル (あなた) は骨格の `<!-- TODO -->` と候補表を文章で確定する。表の Signal / Dir / Bits と根拠行は変更しない。
- 判定は `scripts/check_io.py` と `scripts/check_spec.py --strict` が行い、review モードが別コンテキストで反証する。
  人の確認は入らない前提で仕上げる。

## 入出力

- 入力: `rtl/<module>.v` (別の場所なら各スクリプトに `--rtl-dir` / `--spec-dir`)
- 出力: `doc/spec/<module>.md`、`doc/spec/README.md` のインデックス 1 行
- 規則: [rules.md](rules.md)。書いてあることはすべて `check_spec.py --strict` が検査する。

## モード

| モード | 読むもの | 既定モデル | 用途 |
|---|---|---|---|
| 基本 | RTL、骨格、rules.md | GPT-5.6 Luna | コメントが豊富な小さいモジュール。表と骨格の確定が主 |
| 拡張 (既定) | 基本 + 親モジュールの RTL + 隣接モジュールの spec の「概要・用途」章 + `doc/architecture/*.md` (あれば) | Claude Opus 5 (費用を抑えるなら Sonnet 5) | コメントの無い RTL、ラッパ、外部契約が多いモジュール |
| review | RTL と仕様書だけ (執筆側の文脈は渡さない) | Claude Sonnet 5 | 執筆後に必ず 1 回 |

モデルは Copilot CLI の `/model`、`--model`、またはカスタムエージェント (`.github/agents/rtl-spec-*.agent.md`) で指定する。
無人実行は 2 通り: `run.sh <module> [--mode basic|extended] [--review]` (シェルが別々の `copilot -p` に `--model` を渡す)、
または `copilot --agent rtl-spec-orchestrator -p "<module> の仕様書を作る"` (司令塔がサブエージェントに委譲する。下記)。

## 手順 (1 モジュールを 1 回の依頼で完走する)

1. **骨格を作る**
   ```
   py .github/skills/rtl-spec-auto/scripts/rtl_extract.py rtl/<module>.v -o doc/spec/<module>.md
   ```
   既に `doc/spec/<module>.md` がある場合は「更新」: `-o doc/spec/<module>.new.md --force` に出し、既存の本文を保ったまま
   I/O 表・パラメータ表・動作モード表・境界値候補の差分だけを既存へ反映し、`.new.md` は削除する。
2. **読む** (モードの表にあるもの以外は読まない。読む量が費用になる)
3. **埋める** — 骨格の TODO と候補表を上から順に。詳細は rules.md。
   - 概要 / 用途: 骨格のヒント (親、上流 / 下流、状態機械、出力の代入箇所) と RTL 本文から書く。コメントが無い RTL では
     名前・接続・使用箇所から読み取り、RTL から確定できないこと (親の外の発生元、設計意図) は `(推測)` を付けて書く。
   - I/O の Description: 極性、パルス / レベル、有効条件、単位、相手モジュール。ヒントの使用箇所 (L 番号) を読んで書く。
   - 動作モード表: `[状態 X]` を日本語のモード名に、`[内部:x]` をポート名や「残り回数」などの言葉に言い換える。
   - 機能詳細の箇条書き: 「入力 → 何サイクル後に → 出力」。数値を含む箇条書きには根拠行 `<file>.v:L<start>-<end>` を付ける。
     サイクル数の主張には `(要シム確認)` を残す。
   - 検証項目と境界値: 候補の「条件」を読みやすく直し、「期待動作」を RTL から確定する。意味のない候補は削除してよいが、
     削除した候補の判定点 (根拠行) が本文か別の行で参照されていること。根拠列は変更しない。
   - 埋め終えたら HTML コメント、TODO、`[状態 X]` / `[内部:x]` をすべて消す。
4. **検査して直す** (最大 3 往復)
   ```
   py .github/skills/rtl-spec-auto/scripts/check_io.py <module>
   py .github/skills/rtl-spec-auto/scripts/check_spec.py --strict <module>
   ```
   NG を 0 にする。WARN は内容を確認し、正当なら残す。3 往復で消えない NG は報告に残す。
5. **インデックス**: 新規なら `doc/spec/README.md` の該当カテゴリ表に `| <module> | [<module>.md](<module>.md) |` を追加する。
6. **報告** (3 行以内): 作成 / 更新したファイル、検査結果 (NG / WARN の数)、`(推測)` と `(要シム確認)` の件数。

## カスタムエージェント版 (司令塔が委譲する)

`copilot --agent rtl-spec-orchestrator -p "<module> の仕様書を作る"`、または対話で `/agent` から rtl-spec-orchestrator を選んで頼むと、
司令塔 (安価なモデル) が次の順に進める。司令塔は文章を書かず、スクリプトの実行と委譲と検査だけを行う。

1. 手順 1 (骨格生成) を自分で実行する。
2. **rtl-spec-writer** サブエージェント (拡張モード、Opus 5) に執筆を委譲する。委譲文の雛形:
   「rtl-spec-auto の手順 2〜3 を拡張モードで行う。モジュール <module>、RTL <rtl path>、骨格 <spec path> (TODO 入り)、
   規則 .github/skills/rtl-spec-auto/rules.md。終わったら手順 4 の検査を自分でも実行し、結果を 3 行で返す。」
   前回の検査で NG が残っていれば、その NG の全文を委譲文に含める (サブエージェントは前回の文脈を持たない)。
3. 戻ってきたら手順 4 の検査を自分で再実行する。NG があれば 2 に戻る (最大 3 往復)。
4. **rtl-spec-reviewer** サブエージェント (Sonnet 5) に見直しを委譲する。委譲文には RTL と仕様書のパスだけを入れ、
   執筆側の説明・経過・自分の会話は渡さない (反証の独立性のため)。
5. 戻ってきたら手順 4 の検査を再実行する。NG があれば writer に 1 回だけ差し戻す。
6. 手順 5 (インデックス) を確認し、手順 6 の形式で報告する (review の指摘件数を加える)。

サブエージェントは別のコンテキストで、定義ファイルの `model` で動く (CLI の優先順位はエージェント定義 > `--model` > 環境変数)。
`model` が効いていない場合は writer と reviewer が司令塔と同じモデルで動くので、報告に「使用モデル」を含めさせて確認する。
無人化を確実にしたいときは `run.sh` (別々の `copilot -p` に `--model` を渡す) を使う。

## review モード

依頼: `/rtl-spec-auto review <module>`。読むのは `rtl/<module>.v` と `doc/spec/<module>.md` だけで、執筆側の説明は受け取らない。

1. 反証から入る。「機能詳細仕様」(動作モード表、箇条書き、検証項目と境界値) と「タイミング仕様」「非対応事項・注意点」について、
   RTL と矛盾する記述、根拠行が主張を裏付けていない箇所、抜けている境界 (リセット直後の値、ハンドシェイクの成立条件、
   同一サイクル競合時の優先、上下限、遅延サイクル数) を列挙する。severity は blocking (誤り) / recommend (改善) / question。
2. blocking を直接修正する (表の Signal / Dir / Bits、根拠列、図は触らない)。recommend は妥当なら反映する。
3. `check_spec.py --strict` を再実行して NG 0 を確認し、直した点と残した question を 3 行以内で報告する。

## 費用の目安 (キャッシュが効く前提、1 モジュール)

| 構成 | リーフ | 13 インスタンスのラッパ |
|---|---|---|
| 基本 (Luna) | 5〜15 クレジット | 20〜60 |
| 拡張 (Sonnet 5) + review (Sonnet 5) | 150〜200 | 200〜300 |
| 拡張 (Opus 5) + review (Sonnet 5) | 300〜380 | 350〜600 |

Fable / GPT-6 級は使わない (1 本で 750 を超える)。費用が増える原因は読む量なので、モードの表以外を読まない。

## 禁止事項

- モードの表にないファイルを読み回らない。`doc/` 全体を読まない。
- RTL に無い信号名・パラメータ名を書かない。内部 FSM 状態名・内部 reg / wire 名を書かない。
- 表の Signal / Dir / Bits と根拠列を変えない。章を減らさない、章番号を付けない。
- ですます調、主観語 (便利 / シンプル / 簡単) を使わない。
- 骨格の HTML コメント、TODO、`[状態 X]` / `[内部:x]` を残さない。
- 検査が NG のまま「完了」と報告しない。RTL から確定できないことを断定しない (`(推測)` を付ける)。
