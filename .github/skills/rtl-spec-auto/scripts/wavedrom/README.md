# wavedrom — 仕様の期待波形 (WaveDrom JSON) を SVG に描く

タイミングチャートは **仕様側で手書きする** (WaveDrom JSON、`doc/spec/img/<module>_exp_<ID>.json`)。
シミュレーション結果から図を起こすことはしない (TB が見ていない境界が仕様から抜けるため)。
判定は図ではなく、シミュレーション内のアサーション (`sim/<module>_checker.sv`、bind した SVA チェッカ) で行う。
ここにあるのは図を GitHub でも表示できる SVG にする描画ツールだけ。

## 使い方

```
cd tools/wavedrom && npm install          # 初回のみ (wavedrom 3.x + onml、純 JS。node_modules は git 管理外)
node tools/wavedrom/render.js --all doc/spec/img            # *_exp_*.json をすべて同名 .svg に
node tools/wavedrom/render.js <in.json> [<out.svg>]         # 1 枚
py tools/wavedrom/check_edges.py doc/spec/img               # node / edge の整合とラベルの重なりを検査 (描画不要)
```

- 描画エンジンは WaveDrom 本体 (`wavedrom` パッケージの `renderAny` + `onml`)。wavedrom-cli と同じ経路だが、
  PNG 用の native 依存 (svg2img / canvas) を持たないので Windows でも `npm install` だけで入る。
- 出力は決定論的 (同じ JSON から同じ SVG)。JSON を直したら再描画して SVG も更新する。

## ファイル

| ファイル | 役割 |
|---|---|
| `render.js` | JSON → SVG (1 枚 / `--all <dir>`) |
| `check_edges.py` | node / edge の機械チェック (長さ・重複・参照、ラベルが遷移 / 他のラベル / node マーカーを隠さないか) |
| `package.json` / `package-lock.json` | 依存 (`wavedrom`, `onml`) の固定 |

## 期待波形の書き方 (列規約)

- 列 i は「クロック立ち上がり i で DUT が取り込む値」(エッジ直前の値)。列 i の入力に対するレジスタ出力の応答は列 i+1。
  TB が negedge で駆動する入力は 1 列ちょうどのパルスになる。
- `x` は「規定しない」。
- 内部信号 (FSM 状態、カウンタ) は載せない。ポートだけで挙動が読める区間を選ぶ。
- 見出し (`head.text`) は検証項目 ID だけにする (例: `C-02 / C-03 / C-05`)。条件・列番号・列規約などの文章は JSON に
  書かず、spec の Markdown で図の直下に書く。SVG は幅が列数で決まるので、長文は画像化で切れる。`foot` は使わない。
  長い区間は `|` で分ける。
- 注目点は矢印で示す: 原因の列と結果の列に `node` (1 文字、wave と同じ長さの文字列。`|` の位置は `.`) を置き、
  `edge` に `"a~>b ラベル"` (曲線) で結ぶ。ラベルは矢印の中点に白背景で描かれ、その位置にある中間レーンの波形
  (特に遷移) を隠すので短くする。目安は全角換算で「矢印が横に跨ぐ列数 × 3 字」(hscale 1、半角は 0.5 字)。
  中間レーンに遷移がなければ長くてもよく、最終判断は `check_edges.py` に任せる。
  - 1 列しか跨がない矢印 (例: N 回目の frame_done → 翌列の freeze_req) に 3 字以上付けるときは `"c|->d ラベル"`
    (縦→横。ラベルは起点の列に付く) にし、起点の列に中間レーンの遷移がないことを確認する。
  - 同一列の上下関係は `"c-|>d ラベル"` (縦線)。ラベルは列の線上に載るので 2 字まで (例: `同時`)。
  - 置いた node はすべて edge から参照する (参照のない小文字 node は英字だけが図に残る)。
  - `py tools/wavedrom/check_edges.py doc/spec/img` が長さ・重複・参照と、ラベルが遷移 / 他のラベル / node マーカーを
    隠さないことを検査する (wavedrom 3.x の座標式で計算する。描画は不要)。
  例: `"a~>b 翌列に計数開始"` (1 列だが中間レーンに遷移なし)、`"c|->d 3 回で凍結"`、`"e~>f 4 cycle 連続で解除"`。
- 同一サイクル競合を描くときは、同期段を通る入力 (CTRL_REC のビット) は FSM に見えるのが 2 cycle 後である点を
  刺激側で吸収する (例: bit1 を列 1 に立て、トリガを列 3 に立てると FSM では同一エッジになる)。
- TB (`sim/tb_<module>.v`) は図と同じ刺激を同じ間隔で駆動する。図と実装の一致はチェッカのアサーションが毎サイクル判定する。

## 経緯

- 2026-09-11: TB の VCD から図を生成する方式で開始。
- 2026-09-12: 順序が逆 (仕様が先) との指摘で、期待波形は手書きに反転。VCD と期待波形を Python で突合する経路
  (check_<module>.py / vcd2wavedrom.py / ブラウザ描画) を作ったが、同日「判定はアサーションで」との指示により撤去。
  描画は本ディレクトリの純 JS ツールに一本化した。
