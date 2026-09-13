# rtl-spec-auto 配布パッケージ — 会社 PC への展開手順

既存の Verilog RTL から仕様書を無人生成する GitHub Copilot CLI 用のスキルと、モデルを固定したカスタムエージェント一式。
このパッケージ 1 つで動く (Python 3 と Copilot CLI 以外の追加インストールは不要)。

## 入手 (会社 PC)

公開リポジトリ https://github.com/ytyt1513-cell/rtl-spec-auto から取る。サインインは不要。

```
git clone https://github.com/ytyt1513-cell/rtl-spec-auto.git
```

git を使わない場合は Releases の zip: https://github.com/ytyt1513-cell/rtl-spec-auto/releases/latest/download/rtl-spec-auto.zip
(GitHub の Web で Releases → Assets からも落とせる)。更新は `git pull`、zip の場合は再ダウンロードして `install.py` を再実行する。
このリポジトリには汎用ツールだけが入っており、設計プロジェクトの情報は含まない。

## パッケージの中身

```
rtl-spec-auto/
├── README.md                          ← この文書
├── install.py / install.sh            ← 対象リポジトリ (または個人環境) へコピーするスクリプト (Windows は install.py)
├── VERSION                            ← 元リポジトリのコミットと生成日
└── .github/
    ├── skills/rtl-spec-auto/
    │   ├── SKILL.md                   ← Copilot が読む手順書 (自動発動 / /rtl-spec-auto)
    │   ├── rules.md                   ← 記述ルール (check_spec --strict の検査項目と 1 対 1)
    │   ├── README.md                  ← スキルの説明 (使い方、費用、設計)
    │   ├── run.sh                     ← シェル版の無人実行
    │   ├── scripts/                   ← rtlparse.py / rtl_extract.py / check_io.py / check_spec.py
    │   └── deploy/                    ← この配布物を作るスクリプト (pack.py) と本文書の原本
    └── agents/
        ├── rtl-spec-orchestrator.agent.md   ← 司令塔 (Luna)。writer / reviewer に委譲
        ├── rtl-spec-writer.agent.md         ← 執筆 (Opus 5、拡張モード)
        ├── rtl-spec-writer-lite.agent.md    ← 執筆 (Luna、基本モード)
        └── rtl-spec-reviewer.agent.md       ← 見直し (Sonnet 5、別コンテキスト)
```

## 前提

- Windows 10/11 または Linux。Python 3.10 以上 (`py` または `python3` で起動できること)。
- GitHub Copilot CLI (`copilot`) がインストール済みで、Business ライセンスの GitHub アカウントでサインイン済み。
- `run.sh` を使うなら Git Bash (Windows は Git for Windows に同梱)。カスタムエージェント版だけならシェルは不要。
- PowerShell スクリプト (`.ps1`) は同梱しない。Gmail などのメールが `.ps1` を含む zip を遮断するため、展開は Python スクリプト
  (`install.py`) で行う。`.py` / `.sh` / `.md` は遮断対象ではない。
- 対象の RTL リポジトリは、RTL が 1 ディレクトリ (既定 `rtl/`)、仕様書の置き場が 1 ディレクトリ (既定 `doc/spec/`)。
  違う配置でも `--rtl-dir` / `--spec-dir` で指定できる。

## 展開先 (2 通り)

### A. 対象リポジトリに入れる (推奨。チームで共有され、git で追跡される)

対象リポジトリのルートに `.github/` をコピーする。Copilot CLI は作業ディレクトリのリポジトリ直下の
`.github/skills/*/SKILL.md` と `.github/agents/*.agent.md` を読む。

```
# Windows (PowerShell / コマンドプロンプト)
py install.py C:\work\your-rtl-repo

# Git Bash / Linux
python3 install.py /path/to/your-rtl-repo      # または ./install.sh /path/to/your-rtl-repo
```

スクリプトがやること: `.github/skills/rtl-spec-auto/` と `.github/agents/rtl-spec-*.agent.md` をコピーし、
`.gitignore` に `doc/spec/.rtl-spec-auto/` (実行ログ) と `doc/spec/*.new.md` (更新モードの一時骨格) を追記する。
コピー後は対象リポジトリで `git add .github .gitignore` してコミットする。

### B. 個人環境に入れる (リポジトリを汚さない。自分だけ、全リポジトリで有効)

```
py install.py --user         # → ~/.copilot/skills/rtl-spec-auto/ と ~/.copilot/agents/  (Windows は %USERPROFILE%\.copilot\)
./install.sh --user
```

Copilot CLI はユーザーレベルの `~/.copilot/skills/` と `~/.copilot/agents/` も読む。同名がリポジトリ側にもあると
ユーザー側が優先される点に注意 (agents はホーム側が優先、と公式ドキュメントに記載)。

## 初回の手順

1. 対象リポジトリのルートで `copilot` を起動し、`/model` の一覧を見る。モデル名の表記が `gpt-5.6-luna` /
   `claude-opus-5` / `claude-sonnet-5` と違えば、`.github/agents/*.agent.md` の `model:` と `run.sh` の既定値を書き換える。
2. 1 本目を作る (既存の spec が無いモジュールが分かりやすい)。カスタムエージェント版:
   ```
   copilot --agent rtl-spec-orchestrator -p "<module> の仕様書を作る" --allow-tool='shell(py:*),read,write' --no-ask-user
   ```
   シェル版 (モデルを `--model` で固定するので確実):
   ```
   .github/skills/rtl-spec-auto/run.sh <module> --review
   ```
   RTL が `rtl/` 以外なら `-p` の文に「RTL は src/hdl/<module>.v、spec は docs/spec」と書く (run.sh は `--rtl-dir` / `--spec-dir`)。
3. 報告の 4 点を見る: (1) 司令塔が writer / reviewer に委譲したか、(2) 報告の使用モデルが Opus 5 / Sonnet 5 か
   (違えばエージェント定義の `model` が効いていないので、以後はシェル版を使う)、(3) 権限プロンプトで止まらなかったか
   (止まるなら `--allow-all-tools`、サンドボックス内のみ)、(4) 使用クレジット (目安: リーフ 300〜380、大きなラッパ 350〜600)。
4. 生成物を `py .github/skills/rtl-spec-auto/scripts/check_spec.py --strict <module>` で確認する (NG 0 が完成条件)。
   `(推測)` と `(要シム確認)` の印は読み手向けに残る。

## 会社の書式に合わせる

- 章立て・表形式が本パッケージの 9 章と違う場合は、`rules.md` と `scripts/check_spec.py` の `CHAPTERS` を同時に直す
  (ルールと検査は 1 対 1 に保つ)。骨格の章立ては `scripts/rtl_extract.py` の `skeleton()` にある。
- 境界値の分類 (N/V/S/K/C/E/G/I) を変えるなら `rules.md` と `rtl_extract.py` の `cats`。

## 更新

- 元リポジトリで `py .github/skills/rtl-spec-auto/deploy/publish.py --tag vX.Y.Z` を実行すると、`pack.py` で `dist/` を再生成した上で
  公開リポジトリ ytyt1513-cell/rtl-spec-auto に push し、Releases に zip を添付する。会社側は `git pull` するか zip を展開して
  `install.py` (または `install.sh`) を再実行すれば上書き更新される (ローカルで直した
  `rules.md` や `CHAPTERS` は上書きされるので、差分を取ってから)。

## 参考: 何が自動で決まり、何をモデルが書くか

- スクリプトが確定: 章立て、I/O 表 (名前 / 方向 / 幅 / 節分け)、パラメータ表、親モジュールと上流 / 下流、未接続の出力、
  周辺接続図の辺、FSM の状態・遷移・優先順、状態ごとの出力値、リセット値、検証項目と境界値の候補 (根拠行付き)。
- モデルが書く: 概要・用途の文、I/O の Description、モード名、機能詳細の箇条書き、境界値の期待動作、シーケンス図、非対応事項、まとめ。
- 機械検査が判定: I/O 表の一致、章立て、文体、内部名の漏れ、根拠行の照合、判定点の網羅、骨格の残存。
- 別コンテキストの review が反証: 機能詳細・タイミング・非対応事項・境界値表。
