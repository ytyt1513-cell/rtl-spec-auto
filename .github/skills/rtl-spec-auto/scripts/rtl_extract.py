# -*- coding: utf-8 -*-
"""RTL から仕様書の骨格 (skeleton) を生成する。モデルは TODO の文章部分だけを書けばよい。

    py scripts/rtl_extract.py rtl/rec_ctrl.v                          # 骨格を標準出力へ
    py scripts/rtl_extract.py rtl/rec_ctrl.v -o doc/spec/rec_ctrl.md   # ファイルへ (既存があれば --force)
    py scripts/rtl_extract.py rtl/rec_ctrl.v --json                   # 抽出結果を JSON で
    py scripts/rtl_extract.py rtl/rec_ctrl.v --rtl-dir rtl            # 親モジュール探索の対象 (既定は RTL と同じ場所)

骨格に入るもの (すべて RTL から機械的に決まる。コメントが無い RTL でも出る):
  - 親モジュールと接続 (上流 / 下流のモジュール名と信号、親の外へ出る信号、未接続の出力) → 用途と周辺接続図
  - I/O 表 (Signal / Dir / Bits 確定。Description は行末コメント、無ければ使用箇所のヒント)
  - 動作モード表の骨格 (FSM の状態、状態中の出力値、遷移条件と優先順)、リセット値
  - 検証項目と境界値の候補 (基本フロー、値の境界、同期・捕捉、連続確認、同一サイクル競合、パルス、リセット)。根拠行付き
  - パラメータ表、ブロック図の種別とインスタンス一覧
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rtlparse import parse, parents, leaf_paths, IDENT, CLK_RST  # noqa: E402

NAME_GROUPS = [  # コメントが無いときのポート節分け (名前の接頭辞 / 接尾辞)
    (r"^(m_|s_)?axi", "AXI"), (r"ddr|dq|dqs|ck_|cke|odt|ras|cas|_we_n|ba\b|addr\b", "物理ピン"),
    (r"tmds|hdmi", "TMDS / HDMI"), (r"uart|rx\b|tx\b", "UART"), (r"sccb|scl|sda|i2c", "SCCB / I2C"),
    (r"_async$|_tgl$|_val$|cfg|ctrl|mode|_en$|enable", "設定 / 制御入力"), (r"vsync|hsync|de\b|pixel|line|frame|pclk", "映像"),
]
TOK = re.compile(r"\d+'[bdhoBDHO][0-9a-fA-F_xXzZ]+|\d+|" + IDENT + r"|==|!=|<=|>=|&&|\|\||!|~|[()<>|&^]")


def _table(rows, cols):
    widths = [max(len(c), *(len(str(r[i])) for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
    line = lambda cells: "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"
    return "\n".join([line(cols), "|" + "|".join("-" * (w + 2) for w in widths) + "|"] + [line(r) for r in rows])


def _mid(s, used):
    base = "".join(ch for ch in s.upper() if ch.isalnum())[:8] or "N"
    cand, n = base, 2
    while cand in used:
        cand, n = f"{base}{n}", n + 1
    used.add(cand)
    return cand


def _dedupe(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


# ---------------------------------------------------------------- 言い換え (内部名 → ポート語)
class Alias:
    def __init__(self, info):
        self.info = info
        self.widths = info.get("widths", {})
        self.ports = {p["name"]: p for p in info["ports"]}
        self.params = {p["name"] for p in info["params"]}
        self.states = set()
        for f in info.get("fsm", []):
            self.states |= {s for s in f["states"] if s != "default"}
        self.sync, self.capture = {}, {}          # reg -> port
        deep = set()                              # 多 bit 連鎖の 2 段目以降 (捕捉値から派生した内部カウンタ等) は言い換えない
        for port, chain in info.get("chains", {}).items():
            if self.widths.get(chain[0], 1) == 1 and len(chain) >= 1:
                for r in chain:
                    self.sync[r] = port
            else:
                self.capture[chain[0]] = port
                deep |= set(chain[1:])
        for r, p in info.get("alias", {}).items():
            if r in deep or r in self.sync or r in self.capture:
                continue
            if p in self.ports and self.ports[p]["dir"] == "out":
                self.sync[r] = p                  # assign out = reg
            elif self.widths.get(r, 1) != 1:
                self.capture[r] = p
        self.counters = {c["name"] for c in info["decisions"]["counters"]}

    def cond(self, c):
        """分岐条件 1 個を仕様の言葉に。'else' と '(内側の条件が不成立)' はそのまま日本語に。"""
        if c == "else":
            return "それ以外 (直前の条件が不成立)"
        if c.startswith("("):
            return c
        return self.expr(c)

    def is_internal(self, ident):
        return ident not in self.ports and ident not in self.params and ident not in self.sync and ident not in self.capture

    def word(self, ident):
        if ident in self.ports or ident in self.params:
            return f"`{ident}`"
        if ident in self.sync:
            p = self.sync[ident]
            return f"`{p}`" if self.ports.get(p, {}).get("dir") == "out" else f"`{p}` (同期後)"
        if ident in self.capture:
            return f"`{self.capture[ident]}` の捕捉値"
        if ident in self.states:
            return f"[状態 {ident}]"
        return f"[内部:{ident}]"

    def const(self, v):
        m = re.fullmatch(r"(\d+)'([bdhoBDHO])([0-9a-fA-F_xXzZ]+)", v)
        if not m:
            return v
        base = {"b": 2, "d": 10, "h": 16, "o": 8}[m.group(2).lower()]
        try:
            return str(int(m.group(3).replace("_", ""), base))
        except ValueError:
            return v

    def width(self, ident):
        w = self.widths.get(ident)
        if w is None and ident in self.sync:
            w = 1
        if w is None and ident in self.capture:
            w = self.widths.get(self.capture[ident])
        return w

    def expr(self, e):
        """条件式を仕様の言葉に直す。"""
        toks = TOK.findall(e)
        out, i = [], 0
        while i < len(toks):
            t = toks[i]
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if t in ("!", "~") and i + 1 < len(toks) and re.fullmatch(IDENT, nxt):
                out.append(f"{self.word(nxt)} が 0")
                i += 2
                continue
            if re.fullmatch(r"\d+'[bdhoBDHO][0-9a-fA-F_xXzZ]+|\d+", t):
                out.append(self.const(t))
            elif re.fullmatch(IDENT, t):
                if nxt in ("==", "!=", "<", ">", "<=", ">=") or (i > 0 and toks[i - 1] in ("==", "!=", "<", ">", "<=", ">=")):
                    out.append(self.word(t))
                elif self.width(t) == 1 or t in self.sync:
                    out.append(f"{self.word(t)} が 1")
                else:
                    out.append(self.word(t))
            else:
                out.append({"&&": "かつ", "||": "または", "==": "=", "!=": "≠", "&": "AND", "|": "OR", "^": "XOR"}.get(t, t))
            i += 1
        s = " ".join(out).replace("( ", "(").replace(" )", ")")
        return " ".join(s.split())


# ---------------------------------------------------------------- 使用箇所
def _usage(info, al):
    """ポートごとの使用箇所: [(line, kind, text)]。kind = cond / capture / out / inst"""
    code_lines = Path(info["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
    use = {p["name"]: [] for p in info["ports"]}
    where = {}
    for f in info.get("fsm", []):
        for it in f["items"]:
            for b in it["branches"]:
                where[b["line"]] = (it["state"], b["cond"])

    def ctx(line):
        best = None
        for l, (st, cond) in where.items():
            if l <= line and (best is None or l > best[0]):
                best = (l, st, cond)
        return f"[状態 {best[1]}] の分岐「{al.expr(best[2])}」" if best else ""

    for ln, l in enumerate(code_lines, 1):
        code = l.split("//")[0]
        for n in use:
            if not re.search(r"\b" + re.escape(n) + r"\b", code):
                continue
            if re.search(r"\b" + re.escape(n) + r"\s*<=", code) or re.search(r"\bassign\s+" + re.escape(n) + r"\b", code):
                use[n].append((ln, "out", " ".join(code.strip().split())))
            elif re.search(r"\bif\s*\(.*\b" + re.escape(n) + r"\b", code) or re.search(r"\bcase\s*\(.*\b" + re.escape(n), code):
                use[n].append((ln, "cond", ctx(ln)))
            elif re.search(r"<=\s*[^;]*\b" + re.escape(n) + r"\b", code):
                dst = re.match(r"\s*(" + IDENT + r")\s*<=", code)
                use[n].append((ln, "capture", f"{al.word(dst.group(1))} に取り込み" if dst else ""))
            elif re.search(r"\.\s*" + IDENT + r"\s*\([^)]*\b" + re.escape(n) + r"\b", code):
                use[n].append((ln, "inst", " ".join(code.strip().split())))
    return use


# ---------------------------------------------------------------- FSM の整理
def _paths(info, al, fvar_filter=None):
    """[(state, conds[str], assigns[dict], line)] を末端分岐ごとに返す。"""
    out = []
    for f in info.get("fsm", []):
        for it in f["items"]:
            if it["state"] == "default":
                continue
            for b in it["branches"]:
                for conds, assigns in leaf_paths(b):
                    out.append((f["var"], it["state"], conds, assigns, b["line"]))
    return out


def _steady_outputs(info, al):
    """状態ごとの出力値: 状態内の無条件代入 + その状態へ遷移する経路での代入 (経路で代入されない出力は遷移元の値を引き継ぐ)。
    {state: {out: set(values)}}"""
    outs = {p["name"] for p in info["ports"] if p["dir"] == "out"}
    res, fixed = {}, {}
    for f in info.get("fsm", []):
        for it in f["items"]:
            if it["state"] == "default":
                continue
            vals = {}
            for a in it["pre"]:
                if a["name"] in outs:
                    vals.setdefault(a["name"], set()).add(al.const(a["value"]))
            res[it["state"]] = vals
            fixed[it["state"]] = set(vals)
    paths = _paths(info, al)
    for _ in range(4):                          # 引き継ぎの不動点 (状態数が少ないので数回で収束)
        for var, st, conds, assigns, ln in paths:
            nxt = [a["value"] for a in assigns if a["name"] == var]
            if not nxt or nxt[-1] not in res:
                continue
            dst = res[nxt[-1]]
            for o in outs:
                if o in fixed.get(nxt[-1], set()):
                    continue
                here = [al.const(a["value"]) for a in assigns if a["name"] == o]
                if here:
                    dst.setdefault(o, set()).add(here[-1])
                elif o in res.get(st, {}):
                    dst.setdefault(o, set()).update(res[st][o])
    return res


# ---------------------------------------------------------------- 境界値候補
def _candidates(info, al, fname, code):
    rows = {k: [] for k in "NVSKCEG"}
    d = info["decisions"]
    ports = al.ports
    outs = {p["name"] for p in info["ports"] if p["dir"] == "out"}
    L = lambda ln: f"{fname}:L{ln}"
    lines = code.splitlines()

    def line_of(pattern):
        for i, l in enumerate(lines, 1):
            if re.search(pattern, l):
                return i
        return None

    # --- G: リセット
    rv = d["resets"]
    out_r = _dedupe([(al.word(r["name"]), al.const(r["value"]), r["line"]) for r in rv
                     if r["name"] in outs or al.sync.get(r["name"]) in outs])
    if out_r:
        rows["G"].append(("リセット中およびリセット解除直後", "、".join(f"{w}={v}" for w, v, _ in out_r) + "。初期状態から動作を開始する", L(out_r[0][2])))
    for w, v, ln in _dedupe([(al.word(r["name"]), al.const(r["value"]), r["line"]) for r in rv if r["name"] in al.capture]):
        rows["G"].append((f"{w} を書き換えずに使う (リセット既定値)", f"既定値 {v} で動作する", L(ln)))
    for e in info.get("edges", []):
        rows["G"].append((f"リセット解除時に `{e['port']}` が 1 のままである",
                          "同期段がリセット値 0 から立ち上がるためエッジを 1 回検出する。その扱いを明記する (要シム確認)", L(e["line"])))
    # --- S: 同期段 / 捕捉
    toggles = {e["port"] for e in info.get("edges", [])}
    for port, chain in info.get("chains", {}).items():
        if al.widths.get(chain[0], 1) != 1 or len(chain) < 2 or port in toggles:
            continue
        ln = line_of(r"\b" + re.escape(chain[0]) + r"\s*<=\s*" + re.escape(port) + r"\b") or ports[port]["line"]
        rows["S"].append((f"`{port}` の変化 (取り込みエッジ E)",
                          f"{len(chain)} 段の同期後 (E+{len(chain)}) に判定に使われる。出力への応答は E+{len(chain) + 1} (要シム確認)", L(ln)))
    for e in info.get("edges", []):
        rows["S"].append((f"`{e['port']}` の反転を取り込むエッジ E", "E+2 に値を捕捉する。E+2 に取り込む動作は旧値、E+3 以降は新値を使う (要シム確認)", L(e["line"])))
        rows["S"].append((f"`{e['port']}` と値バスを同一サイクルに更新する", "新値を捕捉する (送信側の契約として明記)", L(e["line"])))
        rows["S"].append((f"`{e['port']}` を連続サイクルで 2 回反転する", "最後の値を捕捉する", L(e["line"])))
        rows["S"].append((f"動作中に `{e['port']}` で値を書き換える", "現在の動作に影響しない / する、のどちらかを明記する", L(e["line"])))
    # --- V: 値の境界 (定数比較。カウンタと状態変数は除く)
    seen_v = set()
    for c in d["compares"]:
        lhs = c["lhs"].split("[")[0]
        if lhs in al.states or c["rhs"] in al.states or lhs in al.counters or lhs == "state":
            continue
        val = al.const(c["rhs"])
        subj = al.word(lhs)
        w = al.width(lhs)
        for cond, exp in [(f"{subj} {'=' if c['op'] == '==' else c['op']} {val}", "この値でのみ起きる動作を明記する")] + \
                ([(f"{subj} = {int(val) - 1}", f"{val} との境界。動作が変わるか変わらないかを明記する")] if val.isdigit() and int(val) > 0 else []) + \
                ([(f"{subj} = {int(val) + 1}", f"{val} との境界。動作が変わるか変わらないかを明記する")] if val.isdigit() else []) + \
                ([(f"{subj} = {2 ** w - 1} (幅 {w} bit の上限)", "上限でラップしない")] if isinstance(w, int) and w > 1 else []):
            if cond not in seen_v:
                seen_v.add(cond)
                rows["V"].append((cond, exp, L(c["line"])))
    # --- K: カウンタ (加算 = 連続確認、減算 = 回数)
    paths = _paths(info, al)
    for cnt in d["counters"]:
        cmp_ = [c for c in d["compares"] if c["lhs"].split("[")[0] == cnt["name"]]
        if not cmp_:
            continue
        th = al.const(cmp_[0]["rhs"])
        cl = cmp_[0]["line"]
        real = lambda cs: [c for c in cs if cnt["name"] not in c and c != "else" and not c.startswith("(")]
        hits = [(conds, ln) for _, _, conds, _, ln in paths if any(cnt["name"] in c for c in conds)]
        outer, outer_ln = (hits[0][0], hits[0][1]) if hits else ([], cl)
        outer_txt = "「" + " かつ ".join(al.expr(c) for c in real(outer)) + "」" if real(outer) else ""
        span = lambda a, b: f"{fname}:L{min(a, b)}-{max(a, b)}" if a != b else L(a)
        if cnt["dir"] == "+":
            n = int(th) + 1 if th.isdigit() else th
            rows["K"].append((f"条件{outer_txt}が {n} cycle 連続で成立", "成立時の動作を明記する (要シム確認)", span(outer_ln, cl)))
            rows["K"].append((f"条件{outer_txt}が {int(th) if th.isdigit() else 'N-1'} cycle 連続した後に途切れる", "成立しない", span(outer_ln, cl)))
            rows["K"].append((f"条件{outer_txt}が途中で 1 cycle 途切れる", "連続数を数え直す", span(outer_ln, cnt["line"])))
            if real(outer):
                lits = [t for t in re.split(r"\s*&&\s*", real(outer)[0]) if t.strip()]
                if len(lits) >= 2:
                    for lit in lits:
                        rows["K"].append((f"条件のうち「{al.expr(lit)}」だけが続く", "成立しない (すべての条件が同時に必要)", span(outer_ln, cl)))
        else:
            loads = _dedupe([a["value"] for _, _, _, assigns, _ in paths for a in assigns
                             if a["name"] == cnt["name"] and re.fullmatch(IDENT, a["value"]) and a["value"] != cnt["name"]])
            trig = [c for _, _, conds, assigns, _ in paths
                    if any(a["name"] == cnt["name"] and "-" in a["value"] for a in assigns) for c in real(conds)]
            trig_txt = (al.word(trig[-1]) if re.fullmatch(IDENT, trig[-1]) else al.expr(trig[-1])) if trig else "減算の契機"
            src = al.word(loads[0]) if loads else al.word(cnt["name"])
            rows["K"].append((f"{src} = N を読み込んだ後、{trig_txt} が N 回", "N 回目の翌 cycle に完了動作 (何が起きるかを明記。要シム確認)", L(cl)))
            rows["K"].append((f"{src} = {th} (完了判定の値そのもの)", "1 回目で完了する", L(cl)))
            if th.isdigit():
                rows["K"].append((f"{src} = {int(th) + 1} (減算を 1 回通る最小)", "2 回目で完了する", L(cnt["line"])))
    # --- N / C: 状態 × 分岐
    seen_n = set()
    for var, st, conds, assigns, ln in paths:
        cond_txt = " かつ ".join(al.cond(c) for c in conds)
        nxt = _dedupe([a["value"] for a in assigns if a["name"] == var])
        eff = "、".join(_dedupe(f"{al.word(a['name'])}={al.const(a['value'])}" for a in assigns if a["name"] in outs))
        if nxt:
            eff += ("。" if eff else "") + "遷移先: " + " / ".join(f"[状態 {n}]" for n in nxt)
        key = (st, cond_txt)
        if key in seen_n:
            continue
        seen_n.add(key)
        end = max([a["line"] for a in assigns] + [ln])
        rows["N"].append((f"[状態 {st}] で {cond_txt}", eff or "動作を明記する", L(ln) if end == ln else f"{fname}:L{ln}-{end}"))
    for f in info.get("fsm", []):
        for it in f["items"]:
            conds = [b for b in it["branches"] if b["cond"] != "else"]
            for i in range(len(conds)):
                for j in range(i + 1, len(conds)):
                    rows["C"].append((f"[状態 {it['state']}] で「{al.expr(conds[i]['cond'])}」と「{al.expr(conds[j]['cond'])}」が同時に成立",
                                      f"「{al.expr(conds[i]['cond'])}」側の動作 (if の順序による優先)",
                                      f"{fname}:L{conds[i]['line']}-{conds[j]['line']}"))
    # --- E: パルス入力 (同期段を通さず条件に使う 1 bit 入力)
    for p in info["ports"]:
        if p["dir"] != "in" or p["width"] != 1 or CLK_RST.search(p["name"]) or p["name"] in info.get("chains", {}):
            continue
        if any(re.search(r"\b" + re.escape(p["name"]) + r"\b", c) for _, _, conds, _, _ in paths for c in conds):
            rows["E"].append((f"`{p['name']}` が複数 cycle 高のまま", "受理は 1 回か毎 cycle かを明記する", L(p["line"])))
            rows["E"].append((f"`{p['name']}` を受け付けない状態で `{p['name']}`", "無視する (状態は変わらない)", L(p["line"])))
    return rows


# ---------------------------------------------------------------- 骨格
def _io_sections(info):
    clk_rst = set(info["clocks"]) | set(info["resets"])
    groups, order = {}, []
    have_groups = any(p["group"] for p in info["ports"])
    for p in info["ports"]:
        if p["name"] in clk_rst:
            key = "クロック / リセット"
        elif have_groups and p["group"]:
            key = p["group"]
        else:
            key = None
            for pat, g in NAME_GROUPS:
                if re.search(pat, p["name"], re.I):
                    key = g
                    break
            if key is None:
                key = {"in": "入力", "out": "出力", "inout": "双方向"}[p["dir"]]
            elif p["dir"] == "out" and key not in ("物理ピン", "TMDS / HDMI", "AXI"):
                key += " (出力)"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)
    if "クロック / リセット" in order:
        order.remove("クロック / リセット")
        order.insert(0, "クロック / リセット")
    return [(k, groups[k]) for k in order]


def skeleton(info, rtl_dir):
    name, fname = info["name"], Path(info["path"]).name
    code = Path(info["path"]).read_text(encoding="utf-8", errors="replace")
    al = Alias(info)
    use = _usage(info, al)
    pars = parents(rtl_dir, name) if rtl_dir else []
    par = pars[0] if pars else None
    dir_of = {p["name"]: p["dir"] for p in info["ports"]}
    outs = [p for p in info["ports"] if p["dir"] == "out"]
    L = []
    L.append(f"# {name} 機能仕様書")
    L.append("")
    L.append("<!-- rtl-spec-auto skeleton: TODO を埋め、埋め終えたら HTML コメントはすべて削除する。表の Signal / Dir / Bits と根拠行は変更しない -->")
    L.append("")
    # ---- 概要
    L.append("## モジュール概要")
    L.append("")
    hints = [f"モジュール名 {name}"]
    if info["clocks"]:
        hints.append("クロック " + " / ".join(f"`{c}`" for c in info["clocks"]))
    for f in info.get("fsm", []):
        hints.append("状態機械 " + " / ".join(s for s in f["states"] if s != "default") + f" ({fname}:L{f['line']})")
    if par:
        hints.append(f"親 {par['parent']} ({par['file']}:L{par['line']}、インスタンス {par['inst']})")
    for p in outs[:6]:
        w = [f"L{ln}" for ln, k, _ in use[p["name"]] if k == "out"]
        if w:
            hints.append(f"出力 `{p['name']}` の代入 {', '.join(w[:5])}")
    L.append("<!-- TODO: 3〜5 個の箇条書き。1 行目に役割、2 行目に動作クロックドメイン、残りに主要な責務。内部の状態名・信号名は書かない。")
    L.append("     材料: " + "; ".join(hints))
    if info["header"]:
        L.append("     RTL ヘッダ:")
        L.extend("     " + h for h in info["header"].splitlines())
    L.append("-->")
    L.append("")
    # ---- 用途
    L.append("## 想定用途・位置付け")
    L.append("")
    if par:
        ups = [f"`{m}` (" + " / ".join(f"`{s}`" for s in r["drives"]) + ")" for m, r in par["neighbors"].items() if r["drives"]]
        downs = [f"`{m}` (" + " / ".join(f"`{s}`" for s in r["receives"]) + ")" for m, r in par["neighbors"].items() if r["receives"]]
        ext_in = [s for s in par["to_parent_ports"] if dir_of.get(s) == "in"]
        ext_out = [s for s in par["to_parent_ports"] if dir_of.get(s) == "out"]
        L.append(f"- 上位モジュール `{par['parent']}` 内で使う。" + (f" 上流: {', '.join(ups)}。" if ups else "") + (f" 下流: {', '.join(downs)}。" if downs else ""))
        if ext_in:
            L.append(f"- `{par['parent']}` の外から受ける入力: " + ", ".join(f"`{s}`" for s in ext_in) + " <!-- TODO: 発生元 (親の外なので RTL からは分からない。分からなければ (推測) を付ける) -->")
        if ext_out:
            L.append(f"- `{par['parent']}` の外へ出る出力: " + ", ".join(f"`{s}`" for s in ext_out) + " <!-- TODO: 行き先 -->")
        if par["unconnected"]:
            L.append(f"- `{par['parent']}` では " + ", ".join(f"`{s}`" for s in par["unconnected"]) + " は未接続。")
        L.append("- <!-- TODO: どんな状況で使うか 1〜2 個 -->")
    else:
        L.append("<!-- TODO: 2〜4 個の箇条書き。親モジュールが RTL ディレクトリに見つからないので、上流 / 下流は名前と使用箇所から推定し (推測) を付ける -->")
    L.append("")
    # ---- I/O
    L.append("## インターフェース仕様 (I/O)")
    L.append("")
    for sec, ports in _io_sections(info):
        L.append(f"### {sec}")
        L.append("")
        rows = []
        for p in ports:
            desc = p["comment"]
            if not desc:
                brief = []
                for ln, k, t in use.get(p["name"], [])[:3]:
                    brief.append({"cond": f"L{ln} で判定に使用" + (f" ({t})" if t else ""), "capture": f"L{ln} {t}",
                                  "out": f"L{ln} で代入", "inst": f"L{ln} でサブモジュールへ接続"}[k])
                unc = par and p["name"] in par["unconnected"]
                desc = "<!-- TODO: " + ("; ".join(brief) if brief else "使用箇所なし") + (" (親では未接続)" if unc else "") + " -->"
            rows.append([p["name"], p["dir"], p["width"], desc])
        L.append(_table(rows, ["Signal", "Dir", "Bits", "Description"]))
        L.append("")
    L.append("<!-- Description は「接続する人が読んで配線と使い方が分かる」内容 (極性、パルス / レベル、有効条件、単位、相手モジュール)。"
             "Signal / Dir / Bits は変更しない -->")
    L.append("")
    # ---- ブロック図
    L.append("## ブロック図")
    L.append("")
    used = set()
    if info["is_wrapper"]:
        mods = _dedupe([i["module"] for i in info["instances"]])
        L.append(f"<!-- 種別: ラッパ (インスタンス {len(info['instances'])} 個、モジュール {len(mods)} 種)。### 内部構成 と ### 周辺接続 の 2 図。"
                 "ノードは 5〜7 個程度に束ねてよいが、モジュール名は図中に残す -->")
        L.append("")
        L.append("### 内部構成")
        L.append("")
        L.append("```mermaid")
        L.append("flowchart LR")
        ids = {m: _mid(m, used) for m in mods}
        for m in mods:
            L.append(f"    {ids[m]}[{m}]")
        # 子インスタンス間の接続 (共有配線) を機械抽出
        edges = {}
        for a in info["instances"]:
            for b in info["instances"]:
                if a is b or a["module"] == b["module"]:
                    continue
                aw = {w for e in a["conns"].values() for w in re.findall(IDENT, e)}
                bw = {w for e in b["conns"].values() for w in re.findall(IDENT, e)}
                shared = [w for w in aw & bw if not CLK_RST.search(w) and not re.fullmatch(r"\d+", w)]
                if shared:
                    key = tuple(sorted([a["module"], b["module"]]))
                    edges.setdefault(key, set()).update(shared)
        for (ma, mb), ws in list(edges.items())[:20]:
            L.append(f"    {ids[ma]} --- |{len(ws)} 本| {ids[mb]}")
        L.append("    %% TODO: 線は共有配線の本数。データの流れる向きに矢印を付け直し、ラベルを |機能単位の信号名| にする (AW/W/B は |AXI Write|)")
        L.append("```")
        L.append("")
    else:
        L.append("<!-- 種別: リーフ (インスタンスなし)。### 周辺接続 のみ。### 内部構成 は書かない -->")
        L.append("")
    L.append("### 周辺接続")
    L.append("")
    L.append("```mermaid")
    L.append("flowchart LR")
    this = _mid(name, used)
    if par:
        for mod, rel in par["neighbors"].items():
            mid = _mid(mod, used)
            if rel["drives"]:
                L.append(f"    {mid}[{mod}] -->|{' / '.join(rel['drives'])}| {this}[{name}]")
            if rel["receives"]:
                L.append(f"    {this}[{name}] -->|{' / '.join(rel['receives'])}| {mid}[{mod}]")
        ext_in = [s for s in par["to_parent_ports"] if dir_of.get(s) == "in"]
        ext_out = [s for s in par["to_parent_ports"] if dir_of.get(s) == "out"]
        if ext_in:
            L.append(f"    EXTIN[TODO 発生元] -->|{' / '.join(ext_in)}| {this}[{name}]")
        if ext_out:
            L.append(f"    {this}[{name}] -->|{' / '.join(ext_out)}| EXTOUT[TODO 行き先]")
        L.append("    %% 隣接モジュールと信号は親の接続から機械抽出 (向きは相手ポートの方向)。clk / rst は描かない。TODO のノード名だけ直す")
    else:
        L.append(f"    UP[TODO 上流モジュール] -->|TODO 信号| {this}[{name}]")
        L.append(f"    {this} -->|TODO 信号| DOWN[TODO 下流モジュール]")
    L.append("```")
    L.append("")
    # ---- 機能詳細
    L.append("## 機能詳細仕様")
    L.append("")
    L.append("<!-- TODO: ブラックボックス観点の箇条書き。入力 → 何サイクル後に → 出力。数値は RTL から拾い、各箇条書きの末尾に根拠行 (例: "
             f"{fname}:L108-110) を付ける。内部 FSM 状態名 / 内部信号名は書かない。分からないことは (推測) を付ける -->")
    L.append("")
    steady = _steady_outputs(info, al)
    paths = _paths(info, al)
    for f in info.get("fsm", []):
        L.append("### 動作モード")
        L.append("")
        rows = []
        for it in f["items"]:
            if it["state"] == "default":
                continue
            vals = steady.get(it["state"], {})
            outtxt = "、".join(f"`{o}`=" + ("/".join(sorted(v)) if len(v) > 1 else next(iter(v))) for o, v in vals.items()) or "<!-- TODO -->"
            trans = []
            for var, st, conds, assigns, ln in paths:
                if st != it["state"] or var != f["var"]:
                    continue
                nxt = _dedupe([a["value"] for a in assigns if a["name"] == var])
                if nxt:
                    trans.append(" かつ ".join(al.cond(c) for c in conds) + " → " + " / ".join(f"[状態 {n}]" for n in nxt))
            rows.append([f"<!-- TODO 名称 --> [状態 {it['state']}]", outtxt, "; ".join(trans) or "<!-- TODO -->", f"{fname}:L{it['line']}"])
        L.append(_table(rows, ["モード", "出力", "遷移 (if の順 = 優先順)", "根拠"]))
        L.append("")
        L.append("<!-- [状態 X] は内部名。日本語のモード名 (待機 / 計数 / 凍結 など) に置き換え、[内部:...] はポート名で言い換える。"
                 "`x` (同期後) は同期段を通った後の値。出力の 0/1 は経路により両方あり得る箇所 -->")
        L.append("")
    L.append("### リセット値")
    L.append("")
    rv = info["decisions"]["resets"]
    out_rv = _dedupe([(al.word(r["name"]), al.const(r["value"])) for r in rv
                      if dir_of.get(r["name"]) == "out" or dir_of.get(al.sync.get(r["name"])) == "out" or r["name"] in al.capture])
    if out_rv:
        L.append("- " + "、".join(f"{w}={v}" for w, v in out_rv) + f" ({fname}:L{rv[0]['line']}-{rv[-1]['line']})")
    else:
        L.append("- <!-- TODO: リセット中の出力値と根拠行 -->")
    L.append("")
    # ---- 境界値
    L.append("### 検証項目と境界値")
    L.append("")
    L.append("仕様が定める動作を検証項目 ID で列挙する。ID は TB の検証項目名と共通にする。根拠は RTL の行番号。")
    L.append("")
    L.append("<!-- 候補は RTL の判定点から機械生成した。条件を読みやすく直し、期待動作を RTL から確定して書く。"
             "意味のない候補は削除してよいが、根拠行の判定点が本文か表のどこかで参照されていること (check_spec が確認する)。"
             "[状態 X] / [内部:x] はポート名や日本語に言い換える。サイクル精度の主張には (要シム確認) を残す -->")
    L.append("")
    cats = [("N", "基本フロー (状態 × 入力)"), ("V", "値の境界"), ("S", "同期・捕捉"), ("K", "回数・連続確認"),
            ("C", "同一サイクルの競合 (優先順)"), ("E", "パルス入力"), ("G", "リセット")]
    cand = _candidates(info, al, fname, code)
    for key, title in cats:
        rows = cand.get(key, [])
        if not rows:
            continue
        L.append(f"#### {title}")
        L.append("")
        L.append(_table([[f"{key}-{i + 1:02d}", c, e, g, ""] for i, (c, e, g) in enumerate(rows[:24])],
                        ["ID", "条件", "期待動作", "根拠", "期待波形"]))
        L.append("")
    L.append("#### 結合")
    L.append("")
    L.append("- 他モジュールとの結合で確定する動作は RTL 単体からは決まらない。結合 TB を作るときに I-xx として追加する。")
    L.append("")
    # ---- タイミング
    L.append("## タイミング仕様")
    L.append("")
    L.append("### <!-- TODO 主要シーケンス名 -->")
    L.append("")
    L.append("```mermaid")
    L.append("sequenceDiagram")
    parts = list(par["neighbors"].keys())[:3] if par else []
    for mod in parts:
        L.append(f"    participant {_mid(mod, set())} as {mod}")
    L.append(f"    participant M as {name}")
    L.append("    %% TODO: 「誰が誰に何を渡すか」を時系列で。participant は周辺接続と同じモジュール")
    L.append("```")
    L.append("")
    # ---- パラメータ
    L.append("## パラメータ定義")
    L.append("")
    if info["params"]:
        L.append(_table([[p["name"], f"`{p['default']}`", p["comment"] or "<!-- TODO -->"] for p in info["params"]],
                        ["Parameter", "Default", "Description"]))
    else:
        L.append("本モジュールはパラメータを持たない。")
    L.append("")
    # ---- 注意
    L.append("## 非対応事項・注意点")
    L.append("")
    notes = []
    for p in info["ports"]:
        if p["dir"] == "in" and p["width"] == 1 and not CLK_RST.search(p["name"]) and p["name"] not in info.get("chains", {}):
            notes.append(f"- `{p['name']}` は同期段を通さずに使う。<!-- TODO: 同一クロックドメインの入力であること、パルス幅の前提を書く -->")
    if par:
        for u in par["unconnected"]:
            notes.append(f"- `{u}` は `{par['parent']}` では未接続。<!-- TODO: 代替手段があれば書く -->")
    L.extend(notes or ["- <!-- TODO -->"])
    L.append("- <!-- TODO: 扱わない機能、呼び出し側が保証すべき制約、依存する IP -->")
    L.append("")
    L.append("## まとめ")
    L.append("")
    L.append("- <!-- TODO: 3 行程度。断定形 -->")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rtl", help="rtl/<module>.v")
    ap.add_argument("-o", "--out", help="骨格の出力先 (省略時は標準出力)")
    ap.add_argument("--force", action="store_true", help="出力先が既にあっても上書きする")
    ap.add_argument("--json", action="store_true", help="抽出結果を JSON で出力する (骨格は出さない)")
    ap.add_argument("--rtl-dir", help="親モジュールを探すディレクトリ (既定: RTL と同じディレクトリ)")
    a = ap.parse_args()
    info = parse(a.rtl)
    rtl_dir = a.rtl_dir or str(Path(a.rtl).parent)
    if a.json:
        j = dict(info)
        j["internals"] = sorted(info["internals"])
        j["parents"] = parents(rtl_dir, info["name"])
        print(json.dumps(j, ensure_ascii=False, indent=1, default=str))
        return 0
    text = skeleton(info, rtl_dir)
    if a.out:
        out = Path(a.out)
        if out.exists() and not a.force:
            print(f"既にある: {out} (上書きするなら --force)", file=sys.stderr)
            return 2
        out.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"{out}: ポート {len(info['ports'])}、パラメータ {len(info['params'])}、インスタンス {len(info['instances'])}"
              f" ({'ラッパ' if info['is_wrapper'] else 'リーフ'})、FSM {len(info.get('fsm', []))}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
