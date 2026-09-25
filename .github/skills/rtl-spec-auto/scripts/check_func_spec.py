# -*- coding: utf-8 -*-
"""機能仕様書 (テンプレート v7 形式) の機械検査 (S3)。

    py .github/skills/rtl-spec-auto/scripts/check_func_spec.py <spec.md> --rtl rtl/<module>.v [--rtl-dir <dir>] [--graph <graph.json>]
    py tools/speccheck/check_func_spec.py <spec.md> --rtl rtl/<module>.v      # 元リポジトリのシム (同じ引数)

検査 (NG = exit 1、WARN = 報告のみ):
  NG   章立て (1 / 1.1 / 1.2 / 2 / 3 / 3.1 / 4 / 4.1 / 4.2 / 4.3 / 5 / 6 / 7 / 7.1 / 7.2 / 7.3 が揃う)
  NG   信号表 (2 章の Signal / Dir / Bits) が RTL のポートと一致する (欠落・余分・方向・幅)
  NG   規則 ID R-nn が 01 からの連番で重複しない。規則は 3 章の節の中にだけある
  NG   禁止表現: 「列 n」「RTL からは決まらない」「RTL コメントより」「思われる」「おそらく」「エッジ E」
  NG   3.1 の表は ID / 機能 / 概要 の 3 列
  WARN 規則の本数 15〜25、1 本の文数 (「。」) 2 以下、例示の括弧 (「なら」を含む括弧)
  WARN 「入力の駆動条件」「出力の扱い」の箇条書き 3 つ以下、6 章「機能をまたぐ制約」5 つ以下
  WARN 参照している画像 / JSON が存在する
  WARN RTL の内部名 (reg / wire / localparam) が本文に出ていない
  WARN バッククォートで参照したモジュール名が、対象 / 親 / 隣接 (graph.json) 以外
"""
import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = next((p for p in HERE.parents if (p / "rtl").is_dir() or (p / ".git").exists()), HERE.parents[1])
sys.path.insert(0, str(HERE))
from rtlparse import parse  # noqa: E402

REQUIRED = ["## 1.", "### 1.1", "### 1.2", "## 2.", "## 3.", "### 3.1", "## 4.", "### 4.1", "### 4.2", "### 4.3",
            "## 5.", "## 6.", "## 7.", "### 7.1", "### 7.2", "### 7.3"]
FORBIDDEN = [(r"列 \d", "「列 n」(cycle n と書く)"), (r"RTL からは決まらない", "「RTL からは決まらない」(未規定と書く)"),
             (r"RTL コメントより", "「RTL コメントより」"), (r"思われる", "「思われる」"), (r"おそらく", "「おそらく」"),
             (r"エッジ E\b", "「エッジ E」記法")]
ROW = re.compile(r"^\|\s*([A-Za-z_][\w\[\]:]*)\s*\|\s*(in|out|inout)\s*\|\s*([^|]+?)\s*\|")  # Bits は数値またはパラメータ式 (BANK_BITS-1:0)
RULE = re.compile(r"^\|\s*(R-\d+)\s*\|\s*(.*?)\s*\|\s*$")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("md")
    ap.add_argument("--rtl", required=True, help="対象モジュールの RTL")
    ap.add_argument("--rtl-dir", default=None, help="親 / 隣接の RTL の場所 (既定は --rtl と同じ)")
    ap.add_argument("--graph", default=None, help="隣接判定に使う graph.json (既定: md と同じ場所、無ければ <repo>/tools/blockdiag/graph.json。無ければ省略)")
    a = ap.parse_args(argv)
    md = Path(a.md)
    text = md.read_text(encoding="utf-8")
    lines = text.splitlines()
    ng, warn = [], []

    # ---- 章立て
    for h in REQUIRED:
        if not any(l.startswith(h) for l in lines):
            ng.append(f"章立て: {h} が無い")

    # ---- 信号表 vs RTL
    info = parse(Path(a.rtl))
    ports = {p["name"]: p for p in info["ports"]}
    rows = {}
    in_io = False
    for l in lines:
        if l.startswith("## "):
            in_io = l.startswith("## 2.")
        if not in_io:
            continue
        m = ROW.match(l)
        if m and m.group(1) in ports or (m and m.group(2) in ("in", "out", "inout")):
            b = m.group(3).strip().strip("`")
            if m.group(1) in rows:
                ng.append(f"信号表: {m.group(1)} が重複している")
            rows[m.group(1)] = (m.group(2), int(b) if b.isdigit() else b.replace(" ", ""))
    for n, p in ports.items():
        if n not in rows:
            ng.append(f"信号表: ポート {n} ({p['dir']}, {p['width']}) が 2 章に無い")
        else:
            d, w = rows[n]
            if d != p["dir"]:
                ng.append(f"信号表: {n} の Dir が {d} (RTL は {p['dir']})")
            # RTL の幅がパラメータ式 (rtlparse が文字列で返す) なら、空白を除いた式の文字列で比較する
            rw = p["width"] if isinstance(p["width"], int) else str(p["width"]).replace(" ", "")
            if w != rw:
                ng.append(f"信号表: {n} の Bits が {w} (RTL は {p['width']})")
    for n in rows:
        if n not in ports:
            ng.append(f"信号表: {n} は RTL のポートに無い")

    # ---- 規則
    rules, in_ch3, sec = [], False, ""
    for i, l in enumerate(lines, 1):
        if l.startswith("## "):
            in_ch3 = l.startswith("## 3.")
        if l.startswith("### "):
            sec = l
        m = RULE.match(l)
        if m:
            rules.append((m.group(1), m.group(2), i, in_ch3, sec))
    ids = [r[0] for r in rules]
    if not ids:
        ng.append("機能規則が無い")
    exp = [f"R-{k:02d}" for k in range(1, len(ids) + 1)]
    if ids != exp:
        missing = [e for e in exp if e not in ids]
        dup = sorted({x for x in ids if ids.count(x) > 1})
        extra = [x for x in ids if x not in exp]
        ng.append(f"規則 ID: 01 からの連番でない (欠番 {missing}, 重複 {dup}, 範囲外 {extra})" if (dup or extra) else
                  f"規則 ID: 順序が連番でない (欠番 {missing})")
    for rid, body, ln, ch3, s in rules:
        if not ch3:
            ng.append(f"規則 {rid} (L{ln}) が 3 章の外にある ({s})")
        if body.count("。") > 2:
            warn.append(f"規則 {rid} (L{ln}): 文が {body.count('。')} つ (2 つまで)")
        if re.search(r"\([^)]*なら[^)]*\)", body):
            warn.append(f"規則 {rid} (L{ln}): 例示の括弧 (「なら」)")
    lo = 8 if len(ports) <= 14 else 15          # 小さいモジュール (ポート 14 本以下) は 8 本から
    if not lo <= len(rules) <= 25:
        warn.append(f"規則の本数 {len(rules)} (ポート {len(ports)} 本なので {lo}〜25 が目安)")

    # ---- 禁止表現
    for pat, name in FORBIDDEN:
        hits = [i for i, l in enumerate(lines, 1) if re.search(pat, l) and not l.startswith("#")]
        if hits:
            ng.append(f"禁止表現 {name}: L{', L'.join(map(str, hits[:5]))}{' …' if len(hits) > 5 else ''}")

    # ---- 3.1 の表
    for i, l in enumerate(lines, 1):
        if l.startswith("### 3.1"):
            hdr = next((x for x in lines[i:i + 6] if x.startswith("| ID")), "")
            cols = [c.strip() for c in hdr.strip("|").split("|")]
            if cols != ["ID", "機能", "概要"]:
                ng.append(f"3.1 の表の列が {cols} (ID / 機能 / 概要 の 3 列)")

    # ---- 箇条書きの数
    def bullets_after(start, limit, label):
        k = start
        while k < len(lines) and lines[k].strip() == "":
            k += 1
        n = 0
        while k < len(lines) and lines[k].startswith("- "):
            n += 1
            k += 1
        if n > limit:
            warn.append(f"{label} (L{start}) の箇条書きが {n} (上限 {limit})")
    for i, l in enumerate(lines):
        if l.startswith("入力の駆動条件") and l.rstrip().endswith(":"):
            bullets_after(i + 1, 3, "入力の駆動条件")
        if l.startswith("出力の扱い") and l.rstrip().endswith(":"):
            bullets_after(i + 1, 3, "出力の扱い")
        if l.startswith("機能をまたぐ制約"):
            bullets_after(i + 1, 5, "機能をまたぐ制約")

    # ---- 画像 / JSON
    for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
        p = (md.parent / m.group(1)).resolve()
        if not p.exists():
            warn.append(f"画像が無い: {m.group(1)}")
        elif p.name.endswith(".svg") and "_exp_" in p.name and not p.with_suffix(".json").exists():
            warn.append(f"WaveDrom JSON が無い: {p.with_suffix('.json').name}")

    # ---- 内部名
    names = {x["name"] if isinstance(x, dict) else str(x) for x in (info.get("internals") or [])}
    names |= {x["name"] if isinstance(x, dict) else str(x) for x in (info.get("localparams") or [])}
    names -= set(ports)
    body_text = "\n".join(l for l in lines if not l.startswith("#"))
    hit = sorted(n for n in names if len(n) >= 4 and re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", body_text))
    if hit:
        warn.append(f"内部名が本文にある: {', '.join(hit[:12])}{' …' if len(hit) > 12 else ''}")

    # ---- 隣接以外のモジュール参照
    rtl_dir = Path(a.rtl_dir) if a.rtl_dir else Path(a.rtl).parent
    modules = {p.stem for p in rtl_dir.glob("*.v")}
    focus = Path(a.rtl).stem
    allowed = {focus}
    gp = Path(a.graph) if a.graph else next((c for c in (md.parent / "graph.json", REPO / "tools" / "blockdiag" / "graph.json") if c.exists()), Path("graph.json"))
    if gp.exists():
        import json
        g = json.loads(gp.read_text(encoding="utf-8"))
        B = {b["id"]: b for b in g["blocks"]}
        ids = {b["id"] for b in g["blocks"] if b["module"] == focus or (b["parent"] in B and B[b["parent"]]["module"] == focus)}
        for e in g["edges"]:
            if (e["src"] in ids) != (e["dst"] in ids):
                o = e["dst"] if e["src"] in ids else e["src"]
                allowed.add(B[o]["module"])
        for i in ids:
            if B[i]["parent"] in B:
                allowed.add(B[B[i]["parent"]]["module"])
        allowed.add(g["root"])
    refs = sorted({m for m in re.findall(r"`([A-Za-z_]\w*)`", body_text) if m in modules and m not in allowed})
    if refs and gp.exists():
        warn.append(f"隣接以外のモジュール参照: {', '.join(refs)}")

    for x in ng:
        print(f"NG   {x}")
    for x in warn:
        print(f"WARN {x}")
    print(f"{md.name}: NG {len(ng)}, WARN {len(warn)}, 規則 {len(rules)} 本, ポート {len(ports)} -> " + ("OK" if not ng else "NG"))
    return 1 if ng else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
