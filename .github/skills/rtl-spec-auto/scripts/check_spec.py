# -*- coding: utf-8 -*-
"""仕様書 (doc/spec/<module>.md) の構造と記述ルールを RTL と突き合わせて検査する (レビュワーの代わりの機械検査)。

    py scripts/check_spec.py rec_ctrl                       # 1 モジュール
    py scripts/check_spec.py --all                          # spec ディレクトリの全 .md (既存文書向け、緩い)
    py scripts/check_spec.py --strict rec_ctrl              # 生成した仕様書向け (根拠行・境界値表・判定点の網羅も検査)
    py scripts/check_spec.py --rtl-dir rtl --spec-dir doc/spec rec_ctrl

NG (exit 1) にするもの:
  CHAPTER   9 章が順序どおりに揃っていない / 章番号が付いている
  IO        I/O 章の表が 4 列でない、Description が空、`###` 節がない
  DIAGRAM   ラッパに ### 内部構成 がない / リーフに ### 内部構成 がある / mermaid flowchart がない
  SEQ       タイミング仕様に mermaid sequenceDiagram がない
  PARAM     パラメータ表が RTL のパラメータ (名前) と一致しない / パラメータ無しの定型文がない
  LEFTOVER  骨格の HTML コメント、TODO、[状態 X] / [内部:x] の目印、テンプレの <…> プレースホルダが残っている
  INTERNAL  内部 reg / wire / localparam 名 (ポートでもパラメータでも他モジュールの端子でもない) を `…` で書いている
  STYLE     ですます調、主観語 (便利 / シンプル / 簡単 …)
--strict で追加:
  BOUNDARY  「検証項目と境界値」節がない / 表の列が ID・条件・期待動作・根拠 でない / ID の重複・書式違い / 期待動作が空
  CITE      根拠 (file.v:L12 / L12-15) が無い、行範囲がファイル外、根拠行に条件・期待動作の識別子が出てこない
  COVER     RTL の判定点 (定数比較、出力のリセット値、カウンタの閾値) が本文と表のどこからも参照されていない
WARN (exit には影響しない):
  DIAGRAM   内部構成の図にインスタンスのモジュール名が出てこない
  UNKNOWN   `…` の識別子が RTL ディレクトリのどこにも現れない (幻覚の疑い。レジスタ名等の正当な語もあり得る)
  INDEX     spec ディレクトリの README.md にリンクがない
  MARK      (推測) / (要シム確認) の件数 (残っていてよい。読み手への印)
  CHAPTER   基本 9 章以外の章がある
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rtlparse import parse, spec_io_rows, all_identifiers, all_port_names  # noqa: E402

CHAPTERS = ["モジュール概要", "想定用途・位置付け", "インターフェース仕様 (I/O)", "ブロック図", "機能詳細仕様",
            "タイミング仕様", "パラメータ定義", "非対応事項・注意点", "まとめ"]
SUBJECTIVE = ["便利", "シンプル", "簡単に", "嬉しい", "素晴らし", "と思う", "でしょう", "かもしれない", "たぶん", "おそらく"]
COMMON_WORDS = set("""x X n N i k TODO README ADR FSM AXI AXI4 DDR3 HDMI TMDS UART SCCB DVP MIG BRAM FIFO CDC LSB MSB RGB YUV
YUYV RGB565 QVGA VGA OK NG PC ID IP IF I2C SPI JTAG LED BTN SW PMOD LF CRLF UTF ASCII CR LFCR HOLD IDLE COUNT FROZEN
true false min max clog2 posedge negedge""".split())
CITE = re.compile(r"([A-Za-z_][\w.-]*\.s?v):L(\d+)(?:-(\d+))?")


def _chapters(lines):
    return [(i, l[3:].strip()) for i, l in enumerate(lines) if l.startswith("## ")]


def _section(lines, title):
    start = None
    for i, l in enumerate(lines):
        if l.startswith("## "):
            if start is not None:
                return lines[start + 1:i]
            if l[3:].strip() == title:
                start = i
    return lines[start + 1:] if start is not None else []


def _subsection(sec_lines, title, level="### "):
    start = None
    for i, l in enumerate(sec_lines):
        if l.startswith(level):
            if start is not None:
                return sec_lines[start + 1:i]
            if l[len(level):].strip() == title:
                start = i
    return sec_lines[start + 1:] if start is not None else []


def _code_blocks(sec_lines, lang):
    blocks, cur, inside = [], [], False
    for l in sec_lines:
        if l.strip().startswith("```"):
            if inside:
                blocks.append("\n".join(cur))
                cur, inside = [], False
            elif l.strip()[3:].strip().startswith(lang):
                inside = True
            continue
        if inside:
            cur.append(l)
    return blocks


def _strip_code(text):
    return re.sub(r"```.*?```", "", text, flags=re.S)


def _table_rows(lines):
    """Markdown 表を [(header, [cells...])] に。header は最初の行のセル。"""
    tables, cur = [], None
    for l in lines:
        if l.startswith("|"):
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if cur is None:
                cur = (cells, [])
                tables.append(cur)
            else:
                cur[1].append(cells)
        else:
            cur = None
    return tables


def check(name, rtl_dir, spec_dir, strict=False):
    ng, warn = [], []
    md = Path(spec_dir) / f"{name}.md"
    v = Path(rtl_dir) / f"{name}.v"
    if not v.exists():
        v = Path(rtl_dir) / f"{name}.sv"
    if not md.exists():
        return [f"spec がない: {md}"], []
    if not v.exists():
        return [f"RTL がない: {v}"], []
    info = parse(v)
    text = md.read_text(encoding="utf-8")
    lines = text.splitlines()
    rtl_lines = v.read_text(encoding="utf-8", errors="replace").splitlines()

    # CHAPTER
    titles = [t for _, t in _chapters(lines)]
    if titles != CHAPTERS:
        missing = [c for c in CHAPTERS if c not in titles]
        extra = [t for t in titles if t not in CHAPTERS]
        order_ok = [t for t in titles if t in CHAPTERS] == [c for c in CHAPTERS if c in titles]
        if missing or not order_ok:
            ng.append("CHAPTER 9 章が揃っていない:" + (f" 不足 {missing}" if missing else "") + ("" if order_ok else " 順序違い"))
        if extra:
            warn.append(f"CHAPTER 基本 9 章以外の章がある (必要なら残してよい): {extra}")
    if any(re.match(r"^#{1,3}\s+\d+[.．]", l) for l in lines):
        ng.append("CHAPTER 章番号 (1. 2. …) は使わない")

    # IO
    io = _section(lines, "インターフェース仕様 (I/O)")
    if not any(l.startswith("### ") for l in io):
        ng.append("IO インターフェース仕様に ### 節がない (属性ごとに節を分ける)")
    for hdr, rows in _table_rows(io):
        if hdr[:4] != ["Signal", "Dir", "Bits", "Description"]:
            ng.append(f"IO 表の列は Signal / Dir / Bits / Description: {' | '.join(hdr)}")
            continue
        for r in rows:
            if len(r) != 4:
                ng.append(f"IO 表の列数が 4 でない: {r[0]}")
            elif not r[3]:
                ng.append(f"IO Description が空: {r[0]}")

    # DIAGRAM
    bd = _section(lines, "ブロック図")
    subs = [l[4:].strip() for l in bd if l.startswith("### ")]
    flows = _code_blocks(bd, "mermaid")
    has_drawio = any(".drawio.svg" in l for l in bd)
    if not flows and not has_drawio:
        ng.append("DIAGRAM ブロック図に mermaid flowchart (または draw.io 図) がない")
    if info["is_wrapper"]:
        if "内部構成" not in subs:
            ng.append("DIAGRAM ラッパモジュールなので ### 内部構成 が必要")
        else:
            inner = "\n".join(_code_blocks(_subsection(bd, "内部構成"), "mermaid"))
            miss = [m for m in sorted({i["module"] for i in info["instances"]}) if m not in inner]
            if inner and miss:
                warn.append(f"DIAGRAM 内部構成の図に出てこないインスタンス: {miss}")
    elif "内部構成" in subs:
        ng.append("DIAGRAM リーフモジュールなので ### 内部構成 は書かない (### 周辺接続 のみ)")
    if "周辺接続" not in subs:
        ng.append("DIAGRAM ### 周辺接続 がない")
    for f in flows:
        if not re.match(r"\s*flowchart\s+(LR|TB|RL|BT)", f):
            ng.append("DIAGRAM ブロック図の mermaid は flowchart LR/TB で書く")

    # SEQ
    tm = _section(lines, "タイミング仕様")
    if not any(re.match(r"\s*sequenceDiagram", b) for b in _code_blocks(tm, "mermaid")):
        ng.append("SEQ タイミング仕様に mermaid sequenceDiagram がない")

    # PARAM
    pm = _section(lines, "パラメータ定義")
    rtl_params = [p["name"] for p in info["params"]]
    if rtl_params:
        listed = [r[0].strip("`") for hdr, rows in _table_rows(pm) if hdr and hdr[0] == "Parameter" for r in rows if r]
        miss = [p for p in rtl_params if p not in listed]
        extra = [p for p in listed if p not in rtl_params]
        if miss:
            ng.append(f"PARAM 表にない RTL パラメータ: {miss}")
        if extra:
            ng.append(f"PARAM RTL にないパラメータ: {extra}")
    elif "パラメータを持たない" not in "\n".join(pm) and "パラメータはない" not in "\n".join(pm):
        ng.append("PARAM パラメータ無しのモジュールは「本モジュールはパラメータを持たない。」と 1 行書く")

    # LEFTOVER
    if "<!--" in text:
        ng.append(f"LEFTOVER HTML コメントが {text.count('<!--')} 個残っている (骨格の TODO / ヒントは削除する)")
    if re.search(r"\bTODO\b", text):
        ng.append("LEFTOVER TODO が残っている")
    marks = re.findall(r"\[(状態|内部):[^\]]+\]", text)
    if marks:
        ng.append(f"LEFTOVER 骨格の目印 [状態 X] / [内部:x] が {len(marks)} 個残っている (日本語のモード名 / ポート名に言い換える)")
    for m in re.finditer(r"<([^<>\n/`]{1,40})>", _strip_code(text)):
        if m.group(1).startswith("!--"):
            continue                                  # HTML コメントは上で数えている
        if not re.fullmatch(r"(br|module_name|[a-z]+ .*)", m.group(1)) and re.search(r"[ぁ-んァ-ン一-龥]", m.group(1)):
            ng.append(f"LEFTOVER テンプレのプレースホルダが残っている: <{m.group(1)}>")

    # INTERNAL / UNKNOWN
    port_names = {p["name"] for p in info["ports"]}
    known = port_names | set(rtl_params) | {i["module"] for i in info["instances"]} | {i["inst"] for i in info["instances"]} | {info["name"]}
    known |= {p.stem for p in Path(rtl_dir).glob("*.v")} | {p.stem for p in Path(rtl_dir).glob("*.sv")}
    all_ids = all_identifiers(rtl_dir)
    other_ports = all_port_names(rtl_dir)
    prose = _strip_code(text)
    leaked, unknown = set(), set()
    for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*)`", prose):
        ident = m.group(1)
        if ident in known or ident in COMMON_WORDS:
            continue
        if ident in info["internals"] and ident not in other_ports:
            leaked.add(ident)
        elif ident not in all_ids and "_" in ident:
            unknown.add(ident)
    if leaked:
        ng.append(f"INTERNAL 内部信号 / 状態名を書いている: {sorted(leaked)}")
    if unknown:
        warn.append(f"UNKNOWN RTL ディレクトリに現れない識別子: {sorted(unknown)}")

    # STYLE
    for i, l in enumerate(lines):
        if l.strip().startswith("|") or l.strip().startswith("```"):
            continue
        if re.search(r"(です|ます|ません|でした|ました)[。)）]?\s*$", l.strip()):
            ng.append(f"STYLE ですます調 (行 {i + 1}): {l.strip()[:40]}")
        for w in SUBJECTIVE:
            if w in l:
                ng.append(f"STYLE 主観語「{w}」(行 {i + 1})")

    # MARK
    n_guess, n_sim = text.count("(推測)"), text.count("(要シム確認)")
    if n_guess or n_sim:
        warn.append(f"MARK (推測) {n_guess} 件、(要シム確認) {n_sim} 件")

    # INDEX
    readme = Path(spec_dir) / "README.md"
    if readme.exists() and f"({name}.md)" not in readme.read_text(encoding="utf-8"):
        warn.append(f"INDEX {readme} に {name}.md へのリンクがない")

    if strict:
        ng += _strict(info, lines, text, v, rtl_lines)
    return ng, warn


def _cite_ok(cite, v, rtl_lines, words, alias_of):
    """根拠 'file.v:L12-15' を検証。(問題文字列 or None)。words はポート / パラメータ名。alias_of はポート -> 内部の言い換え名の集合。"""
    fname, s, e = cite.group(1), int(cite.group(2)), int(cite.group(3) or cite.group(2))
    if Path(fname).name != v.name:
        return f"根拠のファイル名 {fname} が RTL ({v.name}) と違う"
    if s < 1 or e > len(rtl_lines) or e < s:
        return f"根拠の行範囲 L{s}-{e} がファイル外 (全 {len(rtl_lines)} 行)"
    seg = "\n".join(rtl_lines[s - 1:e])
    cands = set()
    for w in words:
        cands.add(w)
        cands |= alias_of.get(w, set())
    if words and not any(re.search(r"\b" + re.escape(w) + r"\b", seg) for w in cands):
        return f"根拠行 L{s}-{e} に条件 / 期待動作の識別子 {sorted(words)[:4]} (またはその内部名) が出てこない"
    return None


def _strict(info, lines, text, v, rtl_lines):
    ng = []
    fd = _section(lines, "機能詳細仕様")
    bsec = _subsection(fd, "検証項目と境界値")
    if not bsec:
        ng.append("BOUNDARY 機能詳細仕様に ### 検証項目と境界値 がない")
        return ng
    ids, cited_lines = [], set()
    port_names = {p["name"] for p in info["ports"]} | {p["name"] for p in info["params"]}
    alias_of = {}
    for reg, port in info.get("alias", {}).items():
        alias_of.setdefault(port, set()).add(reg)
    for port, chain in info.get("chains", {}).items():
        alias_of.setdefault(port, set()).update(chain)
    for hdr, rows in _table_rows(bsec):
        if hdr[:4] != ["ID", "条件", "期待動作", "根拠"]:
            ng.append(f"BOUNDARY 境界値表の列は ID / 条件 / 期待動作 / 根拠 (/ 期待波形): {' | '.join(hdr)}")
            continue
        for r in rows:
            if len(r) < 4:
                ng.append(f"BOUNDARY 列が足りない行: {r[0]}")
                continue
            rid, cond, exp, cite = r[0], r[1], r[2], r[3]
            if not re.fullmatch(r"[A-Z]{1,2}-\d{2}[a-z]?", rid):
                ng.append(f"BOUNDARY ID の書式は X-01: {rid}")
            if rid in ids:
                ng.append(f"BOUNDARY ID が重複: {rid}")
            ids.append(rid)
            if not exp.strip():
                ng.append(f"BOUNDARY 期待動作が空: {rid}")
            cm = CITE.search(cite)
            if not cm:
                ng.append(f"CITE 根拠 (例 {v.name}:L12-15) がない: {rid}")
                continue
            words = {w for w in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", cond + " " + exp) if w in port_names}
            prob = _cite_ok(cm, v, rtl_lines, words, alias_of)
            if prob:
                ng.append(f"CITE {rid}: {prob}")
            else:
                cited_lines.update(range(int(cm.group(2)), int(cm.group(3) or cm.group(2)) + 1))
    for cm in CITE.finditer("\n".join(fd)):
        cited_lines.update(range(int(cm.group(2)), int(cm.group(3) or cm.group(2)) + 1))
    # 機能詳細の箇条書きで数値を含むものに根拠がないか (WARN 相当だが strict では NG)
    for l in fd:
        s = l.strip()
        if s.startswith("- ") and re.search(r"\d", s) and not CITE.search(s) and "推測" not in s:
            ng.append(f"CITE 数値を含む箇条書きに根拠行がない: {s[:50]}")
    # COVER: RTL の判定点が参照されているか (行が根拠に含まれる、または値 / 識別子が本文にある)
    body = "\n".join(fd)
    d = info["decisions"]
    outs = {p["name"] for p in info["ports"] if p["dir"] == "out"}
    def covered(line, values):
        if line in cited_lines:
            return True
        return any(re.search(r"(?<![\d.])" + re.escape(str(val)) + r"(?![\d.])", body) for val in values)
    for c in d["compares"]:
        lhs = c["lhs"].split("[")[0]
        if lhs == "state" or c["rhs"] in {lp["name"] for lp in info["localparams"]}:
            continue
        val = _const(c["rhs"])
        vals = [val] + ([str(int(val) + 1)] if val.isdigit() else [])
        if not covered(c["line"], vals):
            ng.append(f"COVER 判定点が未記載: {c['lhs']} {c['op']} {c['rhs']} ({v.name}:L{c['line']})")
    for r in d["resets"]:
        if r["name"] in outs and not covered(r["line"], [_const(r["value"])]):
            ng.append(f"COVER 出力のリセット値が未記載: {r['name']} <= {r['value']} ({v.name}:L{r['line']})")
    return ng


def _const(val):
    m = re.fullmatch(r"(\d+)'([bdhoBDHO])([0-9a-fA-F_]+)", val)
    if not m:
        return val
    try:
        return str(int(m.group(3).replace("_", ""), {"b": 2, "d": 10, "h": 16, "o": 8}[m.group(2).lower()]))
    except ValueError:
        return val


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--strict", action="store_true", help="生成した仕様書向け: 根拠行・境界値表・判定点の網羅も検査する")
    ap.add_argument("--rtl-dir", default="rtl")
    ap.add_argument("--spec-dir", default="doc/spec")
    a = ap.parse_args(argv)
    names = a.names
    if a.all or not names:
        names = sorted(p.stem for p in Path(a.spec_dir).glob("*.md") if p.stem != "README")
    bad = 0
    for n in names:
        ng, warn = check(n, a.rtl_dir, a.spec_dir, strict=a.strict)
        print(f"{'NG' if ng else 'OK'} {n} (NG {len(ng)}, WARN {len(warn)})")
        for e in ng:
            print(f"   - NG   {e}")
        for w in warn:
            print(f"   - WARN {w}")
        bad += bool(ng)
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
