# -*- coding: utf-8 -*-
"""Verilog / SystemVerilog モジュールからブラックボックス情報を取り出す (rtl-spec-auto 共通部)。

parse(<module.v>) が返す dict:
  name        モジュール名
  header      module 文より前のコメント (RTL ヘッダ) をプレーンテキストにしたもの
  ports       [{name, dir(in/out/inout), width(int か式文字列), group(直前のコメント行), comment(行末コメント), line}]
  params      [{name, default, comment}]        モジュールパラメータ (ユーザ可変)
  localparams [{name, default, comment}]        内部定数 (FSM 状態名など。仕様書には書かない)
  instances   [{module, inst, conns{port: expr}, line}]  サブモジュール / プリミティブのインスタンスと接続
  internals   set(内部 reg / wire / localparam 名)
  widths      {名前: ビット幅 (int か式)}       ポートと内部 reg / wire
  is_wrapper  インスタンスを 1 つ以上持つか
  clocks / resets  名前から推定したクロック / リセットポート
  alias       {内部 reg: ポート名}  ポートから 1 対 1 の代入 (同期段、捕捉レジスタ) でつながる内部名の言い換え
  chains      {ポート: [reg, reg, ...]}  ポートから始まる 1 対 1 代入の連鎖 (1 bit なら同期段、多 bit なら捕捉)
  edges       [{a, b, port, line}]  同期段どうしの XOR / AND-NOT によるエッジ検出
  decisions   {resets, compares, counters}  判定点 (行番号付き。境界値候補の材料)
  fsm         [{var, line, states, items: [{state, line, pre: [assign], branches: [{cond, nested, assigns, line}]}]}]
  nlines      ファイルの行数
ANSI 宣言 (module x (input wire a, ...)) と非 ANSI 宣言 (module x (a, ...); input a;) の両方を扱う。
コメントは同じ長さの空白に置き換えるので、返す行番号はすべて元ファイルの行番号。
"""
import re
from pathlib import Path

KEYWORDS = set("""module endmodule input output inout wire reg logic integer genvar parameter localparam
assign always always_ff always_comb always_latch initial begin end if else case casez casex endcase for while
repeat function endfunction task endtask generate endgenerate default posedge negedge or and not signed
unsigned typedef enum struct return void int bit byte time real string automatic""".split())
DIR_MAP = {"input": "in", "output": "out", "inout": "inout"}
IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"
CLK_RST = re.compile(r"clk|clock|rst|reset|aresetn", re.I)
_BEGIN = re.compile(r"\bbegin\b")
_END = re.compile(r"\bend\b(?!case|module|function|task|generate)")


# ---------------------------------------------------------------- 基本ユーティリティ
def _blank(m):
    return re.sub(r"[^\n]", " ", m.group(0))


def _blank_block_comments(text):
    return re.sub(r"/\*.*?\*/", _blank, text, flags=re.S)


def _blank_all_comments(text):
    return re.sub(r"//[^\n]*", _blank, _blank_block_comments(text))


def _strip_all_comments(text):
    return re.sub(r"//.*", "", re.sub(r"/\*.*?\*/", "", text, flags=re.S))


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _match_paren(text, start, open_="(", close=")"):
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _depth_delta(s):
    return len(_BEGIN.findall(s)) - len(_END.findall(s))


def width_of(rng):
    """'[7:0]' -> 8、'[W-1:0]' -> 'W-1:0' (式はそのまま)、None -> 1"""
    if not rng:
        return 1
    inner = rng.strip()[1:-1].strip()
    m = re.fullmatch(r"(\d+)\s*:\s*(\d+)", inner)
    if m:
        return abs(int(m.group(1)) - int(m.group(2))) + 1
    return inner


def _clean_comment(s):
    s = s.strip()
    s = re.sub(r"^(//|/\*|\*/|\*)+", "", s).strip()
    s = re.sub(r"(\*/)$", "", s).strip()
    return s


def _header_text(raw, module_pos):
    lines = []
    for line in raw[:module_pos].splitlines():
        t = line.strip()
        if not t or t.startswith("`"):
            continue
        t = _clean_comment(t)
        if not t or re.fullmatch(r"[=\-*/_#]+", t):
            continue
        lines.append(t)
    return "\n".join(lines)


# ---------------------------------------------------------------- ポート / パラメータ
_PORT_DECL = re.compile(
    r"^\s*(?:\(\*[^)]*\*\)\s*)?(input|output|inout)\s+(?:(?:wire|reg|logic|var)\s+)?(?:signed\s+|unsigned\s+)?"
    r"(?:\[[^\]]+\]\s*)?(.*)$")
_RANGE = re.compile(r"\[[^\]]+\]")


def _parse_port_lines(block, first_line=1):
    """ポート宣言部 (ANSI の () 内、または非 ANSI の本体宣言) を行単位で読む。"""
    ports, group, last = [], "", None
    pending = []            # 直前の連続したコメント行 (先頭行を節名にする)
    for k, line in enumerate(block.splitlines()):
        code, _, comment = line.partition("//")
        comment = _clean_comment(comment)
        stripped = code.strip()
        if not stripped:
            if comment:
                pending.append(comment)
            continue
        if pending:
            g = pending[0]
            if g.count("(") > g.count(")"):
                g = g[: g.rfind("(")]
            group = re.sub(r"[\s(（:：、。]+$", "", g).strip() or group
            pending = []
        m = _PORT_DECL.match(code)
        if m:
            dir_ = DIR_MAP[m.group(1)]
            head = code[m.start(1): m.start(2)]
            rm = _RANGE.search(head)
            width = width_of(rm.group(0) if rm else None)
            rest = m.group(2)
            last = (dir_, width)
        else:
            if last is None:
                continue
            dir_, width = last
            rest = code
        rest = re.sub(r"\(\*[^)]*\*\)", "", rest)
        rest = rest.replace(");", ",").replace(";", ",")
        names = []
        for tok in rest.split(","):
            tok = tok.strip()
            tok = re.sub(r"=\s*.*$", "", tok).strip()
            tok = re.sub(r"\[[^\]]*\]\s*$", "", tok).strip()
            if re.fullmatch(IDENT, tok) and tok not in KEYWORDS:
                names.append(tok)
        for i, n in enumerate(names):
            ports.append(dict(name=n, dir=dir_, width=width, group=group, line=first_line + k,
                              comment=comment if i == len(names) - 1 or len(names) == 1 else ""))
    return ports


def _parse_param_block(block, keyword="parameter"):
    out = []
    for line in block.splitlines():
        code, _, comment = line.partition("//")
        comment = _clean_comment(comment)
        code = code.strip().rstrip(",").rstrip(";").strip()
        if not code:
            continue
        m = re.match(rf"^(?:{keyword}\s+)?(?:(?:integer|int|real|bit|logic|reg|wire|string|type)\s+)?(?:signed\s+|unsigned\s+)?"
                     rf"(?:\[[^\]]+\]\s*)?({IDENT})\s*=\s*(.+)$", code)
        if m and (keyword in line or out):
            out.append(dict(name=m.group(1), default=m.group(2).strip(), comment=comment))
    return out


# ---------------------------------------------------------------- インスタンス
def _instances(code, port_names, internals):
    out = []
    for m in re.finditer(r"(?<![.\w$])(" + IDENT + r")\s*(#\s*\((?:[^()]|\([^()]*\))*\))?\s+(" + IDENT + r")\s*\(", code):
        mod, inst = m.group(1), m.group(3)
        if mod in KEYWORDS or inst in KEYWORDS or mod.startswith("$") or mod in port_names or mod in internals:
            continue
        if mod in ("posedge", "negedge") or inst in port_names:
            continue
        open_ = m.end() - 1
        close = _match_paren(code, open_)
        conns = {}
        if close > 0:
            body = code[open_ + 1: close]
            for c in re.finditer(r"\.\s*(" + IDENT + r")\s*\(", body):
                e = _match_paren(body, c.end() - 1)
                conns[c.group(1)] = " ".join(body[c.end(): e].split()) if e > 0 else ""
        out.append(dict(module=mod, inst=inst, conns=conns, line=_line_of(code, m.start())))
    return out


# ---------------------------------------------------------------- 判定点 / 同期段 / FSM
def _conditions(code):
    """if (...) の条件式を (文字列, 行) で返す。"""
    out = []
    for m in re.finditer(r"\bif\s*\(", code):
        e = _match_paren(code, m.end() - 1)
        if e > 0:
            out.append((" ".join(code[m.end(): e].split()), _line_of(code, m.start())))
    return out


_CMP = re.compile(r"(" + IDENT + r"(?:\s*\[[^\]]+\])?)\s*(==|!=|>=|<=|>|<)\s*(\d+'[bdhoBDHO][0-9a-fA-F_xXzZ]+|\d+|" + IDENT + r")")


def _decisions(code, ports, reset_names):
    resets, compares, counters = [], [], []
    rs = "|".join(re.escape(r) for r in reset_names) or "rst"
    for m in re.finditer(r"if\s*\(\s*!?\s*(?:" + rs + r")\s*(?:==\s*1'b[01])?\s*\)\s*(begin(.*?)\bend\b|[^;]*;)", code, re.S):
        gi = 2 if m.group(2) is not None else 1
        body = m.group(gi)
        for a in re.finditer(r"(" + IDENT + r")\s*<=\s*([^;]+);", body):
            resets.append(dict(name=a.group(1), value=" ".join(a.group(2).split()), line=_line_of(code, m.start(gi) + a.start())))
    seen = set()
    for cond, line in _conditions(code):
        for c in _CMP.finditer(cond):
            key = (c.group(1), c.group(2), c.group(3))
            if key in seen:
                continue
            seen.add(key)
            compares.append(dict(lhs=c.group(1), op=c.group(2), rhs=c.group(3), cond=cond, line=line))
    for m in re.finditer(r"\b(" + IDENT + r")\s*<=\s*\1\s*([-+])\s*(\d+'[bdh]\d+|\d+|" + IDENT + r")\s*;", code):
        counters.append(dict(name=m.group(1), dir=m.group(2), line=_line_of(code, m.start())))
    return dict(resets=resets, compares=compares, counters=counters)


def _chains(code, ports):
    """ポート -> reg -> reg の 1 対 1 代入の連鎖と、内部名 -> ポートの言い換え表。"""
    port_names = {p["name"] for p in ports}
    src_of = {}
    for m in re.finditer(r"\b(" + IDENT + r")\s*<=\s*(" + IDENT + r")\s*;", code):
        dst, src = m.group(1), m.group(2)
        if dst not in port_names and dst != src:
            src_of.setdefault(dst, src)
    chains, alias = {}, {}
    for p in port_names:
        chain, cur = [], p
        while True:
            nxt = [d for d, s in src_of.items() if s == cur and d not in chain]
            if not nxt:
                break
            cur = nxt[0]
            chain.append(cur)
            if len(chain) > 8:
                break
        if chain:
            chains[p] = chain
            for r in chain:
                alias[r] = p
    edges = []
    for m in re.finditer(r"\b(" + IDENT + r")\s*(\^|&\s*~|&&\s*!|\|)\s*(" + IDENT + r")\b", code):
        a, b = m.group(1), m.group(3)
        if a in alias and b in alias and alias[a] == alias[b] and a != b:
            edges.append(dict(a=a, b=b, port=alias[a], line=_line_of(code, m.start())))
    return chains, alias, edges


def _collect_assigns(seg, ln):
    return [dict(name=a.group(1), value=" ".join(a.group(2).split()), line=ln)
            for a in re.finditer(r"(" + IDENT + r")\s*<=\s*([^;]+);", seg)]


def _parse_block(lines, base):
    """行列 [(ln, text)] を深さ base の並びとして読み、(分岐外の代入, 分岐リスト) を返す。
    分岐 = {cond ('else' あり), line, assigns (直下の代入), sub (begin ブロック内の if 連鎖、再帰)}"""
    lines = list(lines)
    pre, branches, i, d = [], [], 0, 0
    while i < len(lines):
        ln, l = lines[i]
        cm = re.search(r"\bif\s*\(", l)
        em = re.search(r"\belse\b", l)
        start = None
        if cm and d + _depth_delta(l[: cm.start()]) == base:
            e = _match_paren(l, cm.end() - 1)
            cond = " ".join(l[cm.end(): e].split()) if e > 0 else l[cm.end():].strip()
            start = (cond, l[e + 1:] if e > 0 else "")
        elif em and branches and not cm and d + _depth_delta(l[: em.start()]) == base:
            start = ("else", l[em.end():])
        if start is None:
            pre += _collect_assigns(l, ln)
            d += _depth_delta(l)
            i += 1
            continue
        cond, rest = start
        br = dict(cond=cond, line=ln, assigns=[], sub=[])
        branches.append(br)
        bm = _BEGIN.search(rest)
        if bm:
            body, dd = [], 1
            after = rest[bm.end():]
            if after.strip():
                body.append((ln, after))
            i += 1
            while i < len(lines) and dd > 0:
                ln2, l2 = lines[i]
                toks = sorted([(t.start(), t.end(), 1) for t in _BEGIN.finditer(l2)] +
                              [(t.start(), t.end(), -1) for t in _END.finditer(l2)])
                cur, close = dd, None
                for s, e, delta in toks:                  # 行の途中で深さ 0 に戻る end を探す (end else if ... begin 対応)
                    cur += delta
                    if cur == 0:
                        close = (s, e)
                        break
                if close:
                    body.append((ln2, l2[: close[0]]))
                    tail = l2[close[1]:]
                    if tail.strip():
                        lines[i] = (ln2, tail)      # 同じ行の else ... を続けて処理
                    else:
                        i += 1
                    dd = 0
                    break
                body.append((ln2, l2))
                dd = cur
                i += 1
            br["assigns"], br["sub"] = _parse_block(body, 0)
        else:
            br["assigns"] = _collect_assigns(rest, ln)
            i += 1
        d = base
    return pre, branches


def _fsm(code, localparams):
    """case (state) の各項目と、その直下の if / else if 連鎖 (優先順、入れ子は sub) を行単位で読む。"""
    lp = {l["name"] for l in localparams}
    lines = code.splitlines()
    out = []
    for m in re.finditer(r"\bcase[xz]?\s*\(\s*(" + IDENT + r")\s*\)", code):
        var = m.group(1)
        start = _line_of(code, m.start())          # 1 始まり
        depth, end = 0, None
        for i in range(start - 1, len(lines)):
            depth += len(re.findall(r"\bcase[xz]?\b", lines[i])) - len(re.findall(r"\bendcase\b", lines[i]))
            if depth == 0:
                end = i
                break
        if end is None:
            continue
        items, d, cur = [], 0, None
        for i in range(start, end):
            l = lines[i]
            lab = re.match(r"^\s*(" + IDENT + r"(?:\s*,\s*" + IDENT + r")*|default)\s*:(?!:)", l)
            if d == 0 and lab and (lab.group(1) == "default" or lab.group(1).split(",")[0].strip() in lp):
                cur = dict(state=lab.group(1).replace(" ", ""), line=i + 1, lines=[])
                items.append(cur)
                l = l[lab.end():]
            if cur is not None:
                cur["lines"].append((i + 1, l))
            d += _depth_delta(l)
        if not items or not any(s["state"] in lp for s in items):
            continue
        parsed = []
        for it in items:
            first = it["lines"][0][1] if it["lines"] else ""
            base = 1 if _BEGIN.search(first) else 0
            pre, branches = _parse_block(it["lines"], base)
            parsed.append(dict(state=it["state"], line=it["line"], pre=pre, branches=branches))
        out.append(dict(var=var, line=start, states=[i["state"] for i in parsed], items=parsed))
    return out


def leaf_paths(branch):
    """分岐を末端まで展開し [(条件リスト, 代入リスト)] を返す。"""
    if not branch["sub"]:
        return [([branch["cond"]], list(branch["assigns"]))]
    out = []
    for s in branch["sub"]:
        for conds, assigns in leaf_paths(s):
            out.append(([branch["cond"]] + conds, list(branch["assigns"]) + assigns))
    if not any(s["cond"] == "else" for s in branch["sub"]) and branch["assigns"]:
        out.append(([branch["cond"], "(内側の条件が不成立)"], list(branch["assigns"])))
    return out


# ---------------------------------------------------------------- 本体
def parse(vpath):
    vpath = Path(vpath)
    raw = vpath.read_text(encoding="utf-8", errors="replace")
    text = _blank_block_comments(raw)        # 行コメントは残す (ポートのコメント用)。長さは raw と同じ
    code = _blank_all_comments(raw)          # 解析用。長さは raw と同じ
    mm = re.search(r"^\s*module\s+(" + IDENT + r")", code, re.M)
    if not mm:
        raise ValueError(f"module 文が見つからない: {vpath}")
    name = mm.group(1)
    header = _header_text(raw, mm.start())
    j = mm.end()
    params, port_block, port_first = [], "", 1
    while j < len(code) and code[j] in " \t\r\n":
        j += 1
    if code[j: j + 1] == "#":
        k = code.index("(", j)
        e = _match_paren(code, k)
        params = _parse_param_block(text[k + 1: e])
        j = e + 1
    while j < len(code) and code[j] in " \t\r\n":
        j += 1
    if code[j: j + 1] == "(":
        e = _match_paren(code, j)
        port_block = text[j + 1: e]
        port_first = _line_of(code, j + 1)
        j = e + 1
    semi = code.index(";", j)
    end = re.search(r"^\s*endmodule\b", code[semi:], re.M)
    body_start = semi + 1
    body_end = semi + end.start() if end else len(code)
    body_code = code[body_start: body_end]
    body_text = text[body_start: body_end]
    body_line = _line_of(code, body_start)

    ports = _parse_port_lines(port_block, port_first) if re.search(r"\b(input|output|inout)\b", port_block) else []
    if not ports:  # 非 ANSI: 本体の宣言を拾う
        sel = []
        for k, l in enumerate(body_text.splitlines()):
            if re.match(r"^\s*(input|output|inout)\b", l) or (l.strip().startswith("//") and not l.strip().startswith("//=")):
                sel.append((body_line + k, l))
        block = "\n".join(l for _, l in sel)
        ports = _parse_port_lines(block, sel[0][0] if sel else body_line)
        for l in body_text.splitlines():
            if re.match(r"^\s*parameter\b", l):
                params += _parse_param_block(l)

    localparams = []
    for m in re.finditer(r"^\s*localparam\b(.*?);", body_code, re.M | re.S):
        for item in re.split(r",(?![^\[]*\])", m.group(1)):
            pm = re.search(rf"({IDENT})\s*=\s*(.+)$", item.strip(), re.S)
            if pm:
                localparams.append(dict(name=pm.group(1), default=" ".join(pm.group(2).split()), comment=""))

    port_names = {p["name"] for p in ports}
    internals = set(lp["name"] for lp in localparams)
    widths = {p["name"]: p["width"] for p in ports}
    for m in re.finditer(r"^\s*(?:reg|wire|logic|integer|genvar|bit)\b(?:\s+signed|\s+unsigned)?((?:\s*\[[^\]]+\])*)\s*([^;=]+?)(?:=[^;]*)?;",
                         body_code, re.M):
        rng = _RANGE.search(m.group(1) or "")
        w = width_of(rng.group(0) if rng else None)
        for tok in m.group(2).split(","):
            tok = re.sub(r"\[[^\]]*\]", "", tok).strip()
            if re.fullmatch(IDENT, tok) and tok not in KEYWORDS:
                if tok not in port_names:
                    internals.add(tok)
                widths.setdefault(tok, w)

    instances = _instances(body_code, port_names, internals)
    for i in instances:
        i["line"] += body_line - 1
    clocks = [p["name"] for p in ports if re.search(r"clk|clock", p["name"], re.I)]
    resets = [p["name"] for p in ports if re.search(r"rst|reset|aresetn", p["name"], re.I)]

    info = dict(name=name, header=header, ports=ports, params=params, localparams=localparams,
                instances=instances, internals=internals, widths=widths, is_wrapper=bool(instances),
                clocks=clocks, resets=resets, path=str(vpath), nlines=raw.count("\n") + 1)
    try:
        chains, alias, edges = _chains(code, ports)
        for m in re.finditer(r"\b(" + IDENT + r")\s*<=\s*(" + IDENT + r")\s*;", code):      # 捕捉レジスタ
            if m.group(2) in port_names and m.group(1) not in port_names and m.group(1) not in alias:
                alias[m.group(1)] = m.group(2)
        for m in re.finditer(r"\bassign\s+(" + IDENT + r")\s*=\s*(" + IDENT + r")\s*;", code):   # 内部 reg → 出力
            if m.group(1) in port_names and m.group(2) not in port_names:
                alias.setdefault(m.group(2), m.group(1))
        info.update(chains=chains, alias=alias, edges=edges)
    except Exception:
        info.update(chains={}, alias={}, edges=[])
    try:
        info["decisions"] = _decisions(code, ports, resets)
    except Exception:
        info["decisions"] = dict(resets=[], compares=[], counters=[])
    try:
        info["fsm"] = _fsm(code, localparams)
    except Exception:
        info["fsm"] = []
    return info


# ---------------------------------------------------------------- リポジトリ横断
def _rtl_files(rtl_dir):
    return sorted(list(Path(rtl_dir).glob("*.v")) + list(Path(rtl_dir).glob("*.sv")))


def all_identifiers(rtl_dir):
    """rtl ディレクトリ全体に現れる識別子の集合 (幻覚チェック用)。"""
    ids = set()
    for v in _rtl_files(rtl_dir):
        ids |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _strip_all_comments(v.read_text(encoding="utf-8", errors="replace"))))
    return ids


def all_port_names(rtl_dir):
    """rtl ディレクトリ内の全モジュールのポート名の集合 (他モジュールの端子名は仕様書に書いてよい)。"""
    names = set()
    for v in _rtl_files(rtl_dir):
        try:
            names |= {p["name"] for p in parse(v)["ports"]}
        except Exception:
            pass
    return names


def parents(rtl_dir, module):
    """module をインスタンス化している親を探す。
    [{parent, inst, file, line, conns{port: expr}, unconnected[port],
      neighbors{module: {drives: [自ポート], receives: [自ポート], shares: [自ポート]}}, to_parent_ports[port]}]
    drives = 相手の出力が自分の入力を駆動 (上流)、receives = 自分の出力を相手が受ける (下流)、shares = 向き不明 / 同じ入力を共有。
    clk / rst は除く。"""
    out = []
    files = _rtl_files(rtl_dir)
    cache = {}

    def dirs_of(mod):
        if mod not in cache:
            cache[mod] = None
            for f in files:
                if f.stem == mod:
                    try:
                        cache[mod] = {p["name"]: p["dir"] for p in parse(f)["ports"]}
                    except Exception:
                        pass
        return cache[mod]

    for v in files:
        try:
            pinfo = parse(v)
        except Exception:
            continue
        mine = [i for i in pinfo["instances"] if i["module"] == module]
        if not mine:
            continue
        pports = {p["name"] for p in pinfo["ports"]}
        for me in mine:
            wires = {}
            for port, expr in me["conns"].items():
                if CLK_RST.search(port):
                    continue
                for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
                    if w not in KEYWORDS and not re.fullmatch(r"\d+'?[bdh]?[0-9a-fA-F_]*", w):
                        wires.setdefault(w, []).append(port)
            neighbors, to_parent = {}, []
            mdirs = dirs_of(module) or {}
            for other in pinfo["instances"]:
                if other is me:
                    continue
                odirs = dirs_of(other["module"]) or {}
                rel = dict(drives=set(), receives=set(), shares=set())
                for tport, expr in other["conns"].items():
                    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
                        if w not in wires:
                            continue
                        td = odirs.get(tport)
                        for mp in wires[w]:
                            md = mdirs.get(mp)
                            if td == "out" and md == "in":
                                rel["drives"].add(mp)
                            elif td == "in" and md == "out":
                                rel["receives"].add(mp)
                            else:
                                rel["shares"].add(mp)
                if any(rel.values()):
                    neighbors[other["module"]] = {k: sorted(s) for k, s in rel.items()}
            for w, ps in wires.items():
                if w in pports:
                    to_parent += ps
            unconnected = [p for p, e in me["conns"].items() if e == ""]
            out.append(dict(parent=pinfo["name"], inst=me["inst"], file=v.name, line=me["line"], conns=me["conns"],
                            unconnected=unconnected, neighbors=neighbors, to_parent_ports=sorted(set(to_parent))))
    return out


def spec_io_rows(mdpath):
    """spec の「インターフェース仕様」章の表を {signal: (dir, bits, description, section)} で返す。
    差動ペア `x_P/N` と `a/b` 併記行は展開する。"""
    rows, in_io, section = {}, False, ""
    for line in Path(mdpath).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_io = "インターフェース仕様" in line
            continue
        if in_io and line.startswith("### "):
            section = line[4:].strip()
            continue
        if not in_io or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Signal", "") or set(cells[0]) <= set("-: "):
            continue
        sig, dir_, bits = cells[0].strip("`"), cells[1].lower().strip("`"), cells[2].strip("`")
        desc = cells[3] if len(cells) > 3 else ""
        pn = re.fullmatch(r"([A-Za-z_]\w*)_([pP])/([nN])", sig)
        if pn:
            names = [f"{pn.group(1)}_{pn.group(2)}", f"{pn.group(1)}_{pn.group(3)}"]
            if re.fullmatch(r"(\d+)\+\1", bits):
                bits = bits.split("+")[0]
        elif "/" in sig:
            names = [s.strip() for s in sig.split("/") if re.fullmatch(r"[A-Za-z_]\w*", s.strip())]
        elif re.fullmatch(r"[A-Za-z_]\w*", sig):
            names = [sig]
        else:
            continue
        for n in names:
            rows[n] = (dir_, bits, desc, section)
    return rows
