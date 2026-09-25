#!/usr/bin/env python3
"""期待波形 (WaveDrom JSON) の node / edge を機械チェックする。

    py .github/skills/rtl-spec-auto/scripts/check_edges.py doc/spec/img     # ディレクトリ内の *_exp_*.json をすべて
    py tools/wavedrom/check_edges.py doc/spec/img                            # 元リポジトリのシム (同じ引数)。個別ファイルも可

チェック内容 (すべて描画せずに JSON から計算する。座標は wavedrom 3.x の描画式に合わせた):
  - node 文字列の長さが wave と一致し、`|` (省略/区切り) の位置に node がない
  - node 文字がチャート内で重複しない、edge が参照する node がすべて存在する
  - edge のラベル (白背景の矩形) が、矢印の中間レーンにある波形の遷移 (立ち上がり / 立ち下がり) を隠さない
  - ラベル同士、ラベルと node マーカーが重ならない
  - signal に period を使っていない (2026-09-21: period < 1 は wavedrom 3.7 で無視され、行が途中で切れる)
違反があれば 1 行ずつ出力し exit 1。
"""
import json
import re
import sys
from pathlib import Path

FONT = 11                 # renderLabel の既定フォントサイズ (px)
LANE_PITCH = 30           # レーン間隔 (yo)
LANE_Y0 = 15              # レーン 0 の中心 y
COL_W = 40                # hscale=1 の 1 列幅 (px)
NODE_DX = 6               # node マーカー / 遷移中心の列先頭からのオフセット
SLOPE = (3, 9)            # 遷移 (0m1 / 1m0 ブリック) が占める列先頭からの x 範囲
SPACE_REAL = 0.30         # wavedrom は空白幅を 0 と見積もるため、実描画幅の推定に使う (em)

_CW = None


def char_widths():
    """wavedrom 同梱の文字幅表 (% of font size)。無ければ近似値。"""
    global _CW
    if _CW is None:
        here = Path(__file__).resolve().parent
        repo = next((q for q in here.parents if (q / "rtl").is_dir() or (q / ".git").exists()), here)
        p = next((c for c in (here / "node_modules", here / "wavedrom" / "node_modules", repo / "tools" / "wavedrom" / "node_modules")
                  if (c / "wavedrom" / "lib" / "char-width.json").exists()), None)
        if p:
            _CW = json.loads((p / "wavedrom" / "lib" / "char-width.json").read_text(encoding="utf-8"))
        else:
            _CW = {"chars": [], "other": 114}
    return _CW


def text_width(s, real=False):
    cw = char_widths()
    w = 0.0
    for ch in s:
        c = ord(ch)
        if real and ch == " ":
            w += SPACE_REAL * 100
            continue
        v = cw["chars"][c] if c < len(cw["chars"]) else None
        if v is None:
            v = cw["other"]
        w += v
    return w * FONT / 100


def transitions(wave):
    """値が変わる列番号の集合 (その列の先頭で遷移が描かれる)。"""
    out = set()
    cur = None
    for i, ch in enumerate(wave):
        if ch in ".|":
            continue
        if cur is not None and (ch != cur or ch in "=23456789"):
            out.add(i)
        cur = ch
    return out


def check_file(path):
    errs = []
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(d, dict) or not isinstance(d.get("signal"), list):
        return ["WaveDrom must contain a signal array"], 0, 0
    # Flatten WaveDrom named groups so grouped lanes receive the same checks.
    def flatten(items):
        result = []
        for item in items:
            if isinstance(item, list):
                result.extend(flatten(item[1:] if item and isinstance(item[0], str) else item))
            elif isinstance(item, dict):
                result.append(item)
            else:
                errs.append("signal must contain lane objects or named groups")
        return result
    signals = flatten(d["signal"])
    if not any(isinstance(s.get("wave"), str) and s["wave"] for s in signals):
        errs.append("waveform has no nonempty lanes")
    for s in signals:
        if "wave" in s and (not isinstance(s["wave"], str) or not s["wave"]):
            errs.append("wave must be a nonempty string")
        if "node" in s and not isinstance(s["node"], str):
            errs.append("node must be a string")
    if errs:
        return errs, 0, 0
    hscale = (d.get("config") or {}).get("hscale", 1)
    colw = COL_W * hscale
    lanes = []           # (k, name, wave)
    nodes = {}           # ch -> dict(x, y, k, idx, name)
    k = -1
    for s in signals:
        k += 1
        if not isinstance(s, dict) or "wave" not in s:
            continue
        name, wave, node = s.get("name", f"lane{k}"), s["wave"], s.get("node")
        if s.get("period", 1) != 1:
            errs.append(f"{name}: period={s['period']} は使わない (period < 1 は描画されず行が途中で切れる。2 クロックは同じ周期で描く)")
        lanes.append((k, name, wave))
        if node is None:
            continue
        if len(node) != len(wave):
            errs.append(f"{name}: node 長 {len(node)} != wave 長 {len(wave)}")
        for i, ch in enumerate(node):
            if ch == ".":
                continue
            if i < len(wave) and wave[i] == "|":
                errs.append(f"{name}: node '{ch}' が '|' の列 {i} にある")
            if ch in nodes:
                errs.append(f"node '{ch}' が重複 ({nodes[ch]['name']} / {name})")
            nodes[ch] = dict(x=colw * i + NODE_DX, y=LANE_Y0 + LANE_PITCH * k, k=k, idx=i, name=name)

    labels = []          # (text, x0, x1, y0, y1, desc)
    for e in d.get("edge", []):
        tok, _, label = e.partition(" ")
        label = label.strip()
        m = re.match(r"^([A-Za-z])(.*?)([A-Za-z])$", tok)
        if not m:
            errs.append(f"edge を解釈できない: {e}")
            continue
        a, shape, b = m.group(1), m.group(2), m.group(3)
        missing = [c for c in (a, b) if c not in nodes]
        if missing:
            errs.append(f"edge '{e}' が参照する node {missing} がない")
            continue
        if not label:
            continue
        fr, to = nodes[a], nodes[b]
        core = shape.strip("<>")
        if core == "-~":
            lx = fr["x"] + (to["x"] - fr["x"]) * 0.75
        elif core == "~-":
            lx = fr["x"] + (to["x"] - fr["x"]) * 0.25
        elif core == "-|":
            lx = to["x"]
        elif core == "|-":
            lx = fr["x"]
        else:
            lx = (fr["x"] + to["x"]) / 2
        ly = (fr["y"] + to["y"]) / 2
        half = max(text_width(label) + 2, text_width(label, real=True)) / 2
        x0, x1, y0, y1 = lx - half, lx + half, ly - FONT / 2, ly + FONT / 2
        labels.append((label, x0, x1, y0, y1, e))
        # 中間レーンの遷移を隠さないか (レーン帯 ±10 と矩形が 1 px 以上重なるレーンが対象)
        for lk, lname, lwave in lanes:
            if lwave[:1] in "pnPN":
                continue
            yk = LANE_Y0 + LANE_PITCH * lk
            if abs(yk - ly) >= 14.5:
                continue
            for i in sorted(transitions(lwave)):
                sx0, sx1 = colw * i + SLOPE[0], colw * i + SLOPE[1]
                if sx0 < x1 and x0 < sx1:
                    errs.append(f"edge '{e}': ラベルが {lname} の列 {i} の遷移を隠す "
                                f"(ラベル x {x0:.0f}〜{x1:.0f}, 遷移 x {sx0}〜{sx1})")
        # node マーカー (白矩形 ≈ 10 px) との重なり
        for ch, n in nodes.items():
            if ch in (a, b):
                continue
            nx0, nx1 = n["x"] - 5, n["x"] + 5
            if abs(n["y"] - ly) < FONT and nx0 < x1 and x0 < nx1:
                errs.append(f"edge '{e}': ラベルが node '{ch}' ({n['name']} 列 {n['idx']}) と重なる")
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            p, q = labels[i], labels[j]
            if p[1] < q[2] and q[1] < p[2] and p[3] < q[4] and q[3] < p[4]:
                errs.append(f"ラベル同士が重なる: '{p[5]}' / '{q[5]}'")
    return errs, len(nodes), len(d.get("edge", []))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    files = []
    for a in argv[1:]:
        p = Path(a)
        files += sorted(p.glob("*_exp_*.json")) if p.is_dir() else [p]
    if not files:
        print("NG no expected waveform JSON files")
        return 2
    bad = 0
    for f in files:
        try:
            errs, nn, ne = check_file(f)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as ex:
            errs, nn, ne = [f"invalid WaveDrom: {ex}"], 0, 0
        status = "OK" if not errs else "NG"
        print(f"{status} {f} (node {nn}, edge {ne})")
        for e in errs:
            print(f"   - {e}")
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv))
