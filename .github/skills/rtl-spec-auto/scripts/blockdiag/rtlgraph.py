# -*- coding: utf-8 -*-
"""RTL 階層からブロック図用グラフ (graph.json) を機械抽出する (段階 (a): 抽出 + 抽象化)。

    py tools/blockdiag/rtlgraph.py                     # rtl/ → tools/blockdiag/graph.json
    py tools/blockdiag/rtlgraph.py --print             # 要約を表示 (ブロック / 配線 / CDC / 規則に当たらない線)
    py tools/blockdiag/rtlgraph.py --check-cdc         # 帯を跨ぐ配線と architecture/clock.md CDC マトリクスを突合

抽出するもの (すべて RTL から機械的に決まる):
  blocks   インスタンス (同一ラッパ内の同一モジュールは ×N に併合) / ラッパ本体ロジック (点線) / 外部デバイス (top ポート群) /
           IP・プリミティブ。各ブロックに RTL ヘッダ 1 行目のラベル、クロックドメイン (clk ポートに繋がる top 基準ネット)、
           CDC (ドメイン 2 つ以上)、CTRL_xx タグ (uart_regio のレジスタ出力を辿る)
  edges    ネット単位の駆動元 → 受け側を、ブロック対ごとに信号名規則 (KIND_RULES) でバンドルした配線。
           kind = data / axi / ctrl / stat / serial / clock / reset。帯を跨ぐ配線には cdc=true
  domains  クロックドメイン一覧 (posedge で使われるクロックポートに繋がるネット)
パーサは .github/skills/rtl-spec-auto/scripts/rtlparse.py を共用する。"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = next((p for p in HERE.parents if (p / "rtl").is_dir() or (p / ".git").exists()), HERE.parents[1])
for _c in (HERE.parent, REPO / ".github" / "skills" / "rtl-spec-auto" / "scripts"):   # rtlparse: 配布先 (skill/scripts/blockdiag) か元リポジトリ
    if (_c / "rtlparse.py").exists():
        sys.path.insert(0, str(_c))
        break
from rtlparse import parse, KEYWORDS, IDENT, _blank_all_comments, _match_paren  # noqa: E402

RST_RE = re.compile(r"^(?!ddr3_)(?!.*busy$).*(?:^|_)(?:a?rst|a?reset)n?(?:_|$)")   # DDR3 物理線と FIFO の rst_busy は除く
CLK_RE = re.compile(r"(?:^|_)(?:clk|clock)(?:_|$)|clk$")
NUM_RE = re.compile(r"^\d*'[bdhoBDHO]?[0-9a-fA-F_xXzZ]+$|^\d+$")
DOMAIN_ORDER = ["clk_in", "clk_cam", "ui_clk", "clk_pixel", "clk_serial"]   # 表示順のみ (内容には影響しない)

# 配線のバンドル規則: (信号名の正規表現, kind, ラベル or None=共通接頭辞から自動)。上から順に最初に当たったもの
KIND_RULES = [
    (r"^ddr3_", "axi", "DDR3 物理線"),
    (r"(?:^|_)[ms]_axi_|^mig_(?:aw|ar|w|b|r)(?:addr|len|size|burst|valid|ready|data|strb|last|resp|id)$", "axi", "AXI4"),
    (r"^tmds_|^TMDS_", "serial", "TMDS"),
    (r"(?:^|_)(?:txd|rxd|rtsn|ctsn)(?:_|$)", "serial", "UART"),
    (r"(?:^|_)(?:scl|sda)(?:_|$)", "serial", "SCCB"),
    (r"^cam_(?:data|href|vsync|pclk|xclk)$", "serial", "DVP"),
    (r"^led", "stat", "LED"),
    (r"^(?:src|pg|mux|cam_src|pix|ef)_(?:vsync|data|valid|ready|sof)(?:_w)?$", "data", "共通 IF"),
    (r"(?:^|_)(?:bank_|swap_|latest_done|done_bank|disp_bank_ddr)", "data", "bank IF"),
    (r"(?:^|_)(?:req_is_read|slave_addr|sub_addr|resp_valid|resp_rdata|resp_ack|wdata)(?:_|$)", "data", "SCCB 要求 IF"),
    (r"pixel_rgb|(?:^|_)rgb(?:_|$)|_(?:red|green|blue)$|^pix_[rgb]$", "data", "pixel RGB"),
    (r"(?:^|_)(?:de|h_cnt|v_cnt|hsync|vsync)(?:_|$)|^[xy]$", "data", "表示タイミング"),
    (r"^rx2?_(?:data|valid)$|^tx2?_(?:data|valid|ready)$|^txq", "data", "UART バイト IF"),
    (r"^tmds_|^TMDS_|_10b(?:_|$)", "serial", "TMDS"),
    (r"(?:^|_)(?:req_(?:addr|len|valid|ready)|rd_(?:data|valid|pop|addr|bank|rgb)|wr_(?:data|strb|valid|ready|en|bank|addr)|"
     r"burst_done|prog_full|empty|rst_busy)(?:_|$)", "data", None),
    (r"_async$|_tgl$|_val$|_sel$|^reg_|_en$|_th$|_mode$|freeze|trigger|reinit|dump_(?:start|active|bank)|_req$|_grant|"
     r"_pdn$|_pen$|_start$|_thresh|_cfg|_s[0-9]$|owner", "ctrl", None),
    (r"_done|_pulse$|frozen|disp_bank|frame_ready|_busy|_ok$|locked|init_done|_flag|_latch|overflow|counting|_armed|"
     r"_cnt(?:_|$)|_act|_fail|_pid|_ver|_ui$|_px$", "stat", None),
]

# ソースの無い IP / プリミティブのポート方向とクロック属性: (dir, is_clock)。AXI slave は名前規則で決める
_AXI_S_OUT = re.compile(r"(?:aw|w|ar)ready$|^b(?:id|resp|valid)$|^r(?:id|data|resp|last|valid)$")
PRIM = {
    "BUFG": {"I": ("in", True), "O": ("out", True)},
    "ODDR": {"C": ("in", True), "Q": ("out", False)},
    "OSERDESE2": {"CLK": ("in", True), "CLKDIV": ("in", True), "OQ": ("out", False), "TQ": ("out", False),
                  "OFB": ("out", False), "TFB": ("out", False), "SHIFTOUT1": ("out", False), "SHIFTOUT2": ("out", False)},
    "OBUFDS": {"I": ("in", False), "O": ("out", False), "OB": ("out", False)},
    "clk_wiz_hdmi": {"clk_in1": ("in", True), "reset": ("in", False), "locked": ("out", False)},
    "xpm_fifo_async": {"wr_clk": ("in", True), "rd_clk": ("in", True)},
    "mig_7series_0": {"sys_clk_i": ("in", True), "clk_ref_i": ("in", True), "ui_clk": ("out", True),
                      "ui_clk_sync_rst": ("out", False), "sys_rst": ("in", False), "aresetn": ("in", False),
                      "init_calib_complete": ("out", False), "mmcm_locked": ("out", False), "device_temp": ("out", False)},
}
_XPM_IN = {"rst", "wr_en", "din", "rd_en", "sleep", "injectsbiterr", "injectdbiterr"}


def prim_port(module, port):
    t = PRIM.get(module, {}).get(port)
    if t:
        return t
    if module == "clk_wiz_hdmi" and port.startswith("clk_"):
        return ("out", True)
    if module in ("ODDR", "OSERDESE2"):
        return ("in", False)
    if module == "xpm_fifo_async":
        return ("in", False) if port in _XPM_IN else ("out", False)
    if module == "mig_7series_0":
        if port.startswith("s_axi_"):
            return ("out", False) if _AXI_S_OUT.search(port[6:]) else ("in", False)
        if port.startswith("ddr3_"):
            return ("inout" if port in ("ddr3_dq", "ddr3_dqs_p", "ddr3_dqs_n") else "out", False)
        if port.startswith("app_"):
            return ("in", False) if port.endswith("_req") else ("out", False)
    return None


# ---------------------------------------------------------------- モジュール情報
class Mod:
    def __init__(self, path):
        self.path = path
        self.info = parse(path)
        self.name = self.info["name"]
        self.ports = {p["name"]: p for p in self.info["ports"]}
        self.label = header_label(self.info["header"], self.name)
        raw = path.read_text(encoding="utf-8", errors="replace")
        code = _blank_all_comments(raw)
        # 純粋な配線 (assign a = b; / wire a = b;) は本体ロジックではなくネットの別名として扱う。定数 (リテラル / パラメータ) の
        # tie-off は無視する。三ステート glue (assign pin = oe ? 1'b0 : 1'bz;) は pin ↔ oe の別名として扱う
        blank = lambda m: re.sub(r"[^\n]", " ", m.group(0))
        self.params = {p["name"] for p in self.info["params"]} | {p["name"] for p in self.info["localparams"]}
        self.aliases = []
        def alias(m):
            lhs, rhs = m.group(1), m.group(2)
            ids = [w for w in _idents(rhs) if w not in self.params]
            if len(ids) == 1 and (re.fullmatch(IDENT, rhs.strip()) or "'bz" in rhs.lower() or "'hz" in rhs.lower()):
                self.aliases.append((lhs, ids[0]))
                return blank(m)
            if not ids:
                return blank(m)          # 定数 tie-off
            return m.group(0)
        code = re.sub(r"\b(?:assign|wire(?:\s*\[[^\]]*\])?)\s+(" + IDENT + r")\s*=\s*([^;]*);", alias, code)
        self.wire_assigned = set(re.findall(r"\bwire(?:\s*\[[^\]]*\])?\s+(" + IDENT + r")\s*=", code))
        self.body = _body_code(code, self.info["instances"])
        self.clock_names = {m for m in re.findall(r"\bposedge\s+(" + IDENT + r")", code) if not RST_RE.search(m)}
        for inst in self.info["instances"]:      # 内包プリミティブのクロック端子に直結するポートもクロック (oserdes_10to1 等)
            for port, expr in inst["conns"].items():
                t = prim_port(inst["module"], port)
                if t and t[1]:
                    self.clock_names |= {w for w in _idents(expr) if w in self.ports}
        self.expand = False   # ソース付きモジュールを内包するときだけ展開する (Extractor が決める)
        self.regs = set()
        for m in re.finditer(r"\breg\b\s*(?:signed\s*)?(?:\[[^\]]*\]\s*)?([^;]*);", code):
            self.regs |= {x for x in re.findall(IDENT, m.group(1)) if x not in KEYWORDS}
        for m in re.finditer(r"\b(?:output|inout)\s+reg\b\s*(?:\[[^\]]*\]\s*)?(" + IDENT + ")", code):
            self.regs.add(m.group(1))
        # reg → それを駆動する always の posedge クロック (リセット信号のドメイン判定に使う)。
        # 宣言文 (初期値付きを含む) を空白化してから、always の範囲を begin/end の釣り合いで閉じる。LHS は reg 宣言名に限定し、
        # 複数のクロックで代入される reg は None (判定不能) にする
        decl_blanked = re.sub(r"^[ \t]*(?:\(\*[^)]*\*\)\s*)?(?:input|output|inout|wire|reg|integer|genvar|localparam|parameter)\b[^;]*;",
                              lambda m: re.sub(r"[^\n]", " ", m.group(0)), code, flags=re.M)
        self.reg_clock = {}
        for m in re.finditer(r"\balways\s*@\s*\(\s*posedge\s+(" + IDENT + r")[^)]*\)", decl_blanked):
            body = _always_body(decl_blanked, m.end())
            for lhs in re.findall(r"(?<![\w$.])(" + IDENT + r")(?:\s*\[[^\]]*\])*\s*(?:<=|=)(?!=)", body):
                if lhs not in self.regs:
                    continue
                if lhs in self.reg_clock and self.reg_clock[lhs] != m.group(1):
                    self.reg_clock[lhs] = None
                else:
                    self.reg_clock.setdefault(lhs, m.group(1))
        self.reg_clock = {k: v for k, v in self.reg_clock.items() if v}

    def port_dir(self, port):
        p = self.ports.get(port)
        return p["dir"] if p else None

    def body_uses(self, name):
        """本体ロジック (インスタンス接続と宣言を除いたコード) での name の使い方: (drives, reads)"""
        occ = list(re.finditer(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])", self.body))
        if not occ:
            return False, False
        drives = bool(re.search(r"\bassign\s+" + re.escape(name) + r"\b", self.body)) or name in self.wire_assigned
        if name in self.regs and re.search(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])(?:\s*\[[^\]]*\])*\s*(?:<=|=)(?!=)", self.body):
            drives = True
        n_drive = len(re.findall(r"(?:\bassign\s+)?(?<![\w$.])" + re.escape(name) + r"(?![\w$])(?:\s*\[[^\]]*\])*\s*(?:<=|=)(?!=)", self.body)) if drives else 0
        return drives, len(occ) > n_drive


def header_label(header, name):
    """RTL ヘッダ 1 行目の機能ラベル。「name — 説明」ならその説明、「name (ドメイン)」なら次の行。"""
    lines = [l.strip() for l in header.split("\n") if l.strip()]
    if not lines:
        return ""
    first = lines[0]
    if re.match(re.escape(name) + r"\b", first):
        rest = re.sub("^" + re.escape(name) + r"\s*(?:\([^)]*\))?\s*", "", first)
        rest = re.sub(r"^[—\-:：]\s*", "", rest).strip()
        if rest:
            return _short(rest)
        return _short(lines[1]) if len(lines) > 1 else ""
    return _short(first)


def _short(s):
    s = re.sub(r"^[「『]", "", s)
    s = re.split(r"[。」』]", s)[0]
    return s.strip()


def _always_body(code, pos):
    """always @(...) の直後 pos から、その always 文の本体を返す。begin なら釣り合う end まで、単文なら最初の ';' まで
    (if/else の単文連鎖は else まで拾う)"""
    m = re.match(r"\s*", code[pos:])
    i = pos + m.end()
    if code.startswith("begin", i):
        depth, j = 0, i
        for t in re.finditer(r"\bbegin\b|\bend\b(?!case|module|function|task|generate)", code[i:]):
            depth += 1 if t.group(0) == "begin" else -1
            if depth == 0:
                j = i + t.end()
                break
        else:
            j = len(code)
        return code[i:j]
    j = i
    while True:
        k = code.find(";", j)
        if k < 0:
            return code[i:]
        j = k + 1
        if not re.match(r"\s*else\b", code[j:]):
            return code[i:j]


def _body_code(code, instances):
    """インスタンス接続と宣言文を空白にしたコード (本体ロジックの判定用)。文字位置は元のまま"""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    mm = re.search(r"^\s*module\s+" + IDENT + r".*?;", code, re.M | re.S)
    if mm:
        code = code[:mm.start()] + blank(mm) + code[mm.end():]
    for m in list(re.finditer(r"(?<![.\w$])(" + IDENT + r")\s*(#\s*\((?:[^()]|\([^()]*\))*\))?\s+(" + IDENT + r")\s*\(", code)):
        if m.group(1) in KEYWORDS or m.group(1) in ("posedge", "negedge"):
            continue
        if not any(i["inst"] == m.group(3) for i in instances):
            continue
        e = _match_paren(code, m.end() - 1)
        if e > 0:
            seg = code[m.start(): e + 1]
            code = code[:m.start()] + re.sub(r"[^\n]", " ", seg) + code[e + 1:]
    code = re.sub(r"^[ \t]*(?:\(\*[^)]*\*\)\s*)?(?:input|output|inout|wire|reg|integer|genvar|localparam|parameter)\b[^;=]*;",
                  blank, code, flags=re.M)
    return code


# ---------------------------------------------------------------- 抽出
def _idents(expr):
    expr = re.sub(r"\d*'\s*[bdhoBDHO]\s*[0-9a-fA-F_xXzZ?]+", " ", expr)   # 数値リテラル (1'b1 / 4'h0 / 10'b1_0) を先に落とす
    return [w for w in re.findall(IDENT, expr) if w not in KEYWORDS and not re.match(r"\d", w)]


class Extractor:
    def __init__(self, rtl_dir):
        self.mods = {}
        for f in sorted(Path(rtl_dir).glob("*.v")):
            try:
                m = Mod(f)
                self.mods[m.name] = m
            except Exception as e:  # noqa: BLE001
                print(f"skip {f.name}: {e}", file=sys.stderr)
        used = {i["module"] for m in self.mods.values() for i in m.info["instances"]}
        for m in self.mods.values():
            m.expand = any(i["module"] in self.mods for i in m.info["instances"])
        roots = [n for n in self.mods if n not in used]
        self.root = "top" if "top" in roots else max(roots, key=lambda n: len(self.mods[n].info["instances"]))
        self.blocks = {}      # id -> dict
        self.nets = {}        # net id -> {"name", "ends": [(block, port, role, is_clock, is_reset)]}
        self.same = []        # 別名ネットの組 (assign a = b) → build() で併合する
        self.drv = {}         # (net id, block, port) -> 駆動側 always のクロックのネット id (reg 駆動のとき)

    # ---- ブロック
    def _block(self, bid, **kw):
        b = self.blocks.get(bid)
        if b is None:
            b = self.blocks[bid] = dict(id=bid, count=0, insts=[], clocks=set(), clock_out=set(), tags=set(), **kw)
        return b

    def _end(self, net, block, port, role, is_clock=False, is_reset=False):
        n = self.nets.setdefault(net, dict(name=net.split("/")[-1].lstrip("@"), ends=[]))
        n["ends"].append((block, port, role, is_clock, is_reset))

    # ---- 階層走査
    def run(self):
        root = self.mods[self.root]
        portmap = {}
        for p in root.info["ports"]:
            net = "@" + p["name"]
            portmap[p["name"]] = [net]
            grp = p["group"] or ("クロック / リセット入力" if (CLK_RE.search(p["name"]) or RST_RE.search(p["name"])) else "その他ピン")
            eid = "ext:" + re.split(r"[(（]", grp)[0].strip()
            b = self._block(eid, name=re.split(r"[(（]", grp)[0].strip(), module=None, kind="ext", parent=None, label=grp)
            b["insts"].append(p["name"])
            role = "src" if p["dir"] == "in" else "dst"
            self._end(net, eid, p["name"], role, is_clock=False, is_reset=bool(RST_RE.search(p["name"])))
            if p["dir"] == "inout":
                self._end(net, eid, p["name"], "src")
        self._walk(root, "", portmap, None)
        return self

    def _walk(self, mod, path, portmap, parent_id):
        """展開するラッパ mod (インスタンスパス path) の中を走査する。ラッパ自身は枠 (kind=wrapper) で端点を持たない"""
        wrapper_id = path or self.root
        self._block(wrapper_id, name=mod.name, module=mod.name, kind="wrapper", parent=parent_id, label=mod.label)
        local = lambda n: portmap[n] if n in portmap else [f"{path}/{n}"]
        touched = set()
        for a, b in mod.aliases:
            self.same.append((local(a)[0], local(b)[0]))
        for inst in mod.info["instances"]:
            child = self.mods.get(inst["module"])
            ipath = (path + "/" + inst["inst"]).lstrip("/")
            child_map = {}
            for port, expr in inst["conns"].items():
                ids = _idents(expr)
                if not ids:
                    continue
                nets = [n for w in ids for n in local(w)]
                touched.update(ids)
                child_map[port] = nets
            if child and child.expand:
                self._walk(child, ipath, child_map, wrapper_id)
                continue
            bid = f"{path}/{inst['module']}" if path else inst["module"]
            b = self._block(bid, name=inst["module"], module=inst["module"], kind="rtl" if child else "ip",
                            parent=wrapper_id, label=child.label if child else "")
            b["count"] += 1
            b["insts"].append(ipath)
            for port, nets in child_map.items():
                if child:
                    d = child.port_dir(port)
                    is_clk = port in child.clock_names
                    ck = child.reg_clock.get(port)
                    if d == "out" and ck in child_map:
                        for n in nets:
                            self.drv[(n, bid, port)] = child_map[ck][0]
                else:
                    t = prim_port(inst["module"], port)
                    d, is_clk = (t if t else (None, False))
                is_rst = bool(RST_RE.search(port)) and not is_clk
                for n in nets:
                    if d == "inout":
                        self._end(n, bid, port, "src", is_clk, is_rst)
                        self._end(n, bid, port, "dst", is_clk, is_rst)
                    else:
                        self._end(n, bid, port, {"in": "dst", "out": "src"}.get(d, "?"), is_clk, is_rst)
        # ラッパ本体ロジック: ポートと、インスタンスに繋がるローカルネットのうち本体が読む / 駆動するもの
        body_id = f"{wrapper_id}#body"
        # 候補: ポート、インスタンス端子に出る名前、別名の両側 (assign port = 内部 reg のとき駆動元は内部側の名前)
        cands = set(mod.ports) | touched | {a for a, _ in mod.aliases} | {b for _, b in mod.aliases}
        for w in sorted(cands):
            drives, reads = mod.body_uses(w)
            if not (drives or reads):
                continue
            b = self._block(body_id, name=f"{mod.name} 本体", module=mod.name, kind="body", parent=wrapper_id, label=mod.label)
            b["count"] = 1
            is_clk = w in mod.clock_names
            is_rst = bool(RST_RE.search(w)) and not is_clk
            for n in local(w):
                if drives:
                    self._end(n, body_id, w, "src", is_clk, is_rst)
                    if w in mod.reg_clock:
                        self.drv[(n, body_id, w)] = local(mod.reg_clock[w])[0]
                if reads:
                    self._end(n, body_id, w, "dst", is_clk, is_rst)
        if body_id in self.blocks:
            self.blocks[body_id]["insts"] = [wrapper_id]

    # ---- 抽象化
    def _merge_aliases(self):
        parent = {}
        def find(x):
            while parent.get(x, x) != x:
                x = parent[x]
            return x
        for a, b in self.same:
            ra, rb = find(a), find(b)
            if ra != rb:
                # 代表名は top 側 (@ポート / 浅いパス) を優先する
                keep, drop = sorted((ra, rb), key=lambda n: (not n.startswith("@"), n.count("/"), n))
                parent[drop] = keep
        merged, self.canon = {}, {}
        for nid, n in self.nets.items():
            r = find(nid)
            self.canon[nid] = r
            m = merged.setdefault(r, dict(name=r.split("/")[-1].lstrip("@"), ids=[], ends=[]))
            m["ids"].append(nid)
            m["ends"].extend(n["ends"])
        self.nets = merged

    def _src_domain(self, net, blk, port):
        """信号の発生ドメイン: reg 駆動なら駆動 always のクロック名、IP なら生成クロック、外部デバイスなら board pin"""
        for nid in net["ids"]:
            ck = self.drv.get((nid, blk, port))
            if ck is not None:
                return self.nets[self.canon.get(ck, ck)]["name"] if self.canon.get(ck, ck) in self.nets else ck.split("/")[-1].lstrip("@")
        b = self.blocks[blk]
        if b["kind"] == "ext":
            return "board pin"
        if len(b["clock_out"]) == 1:
            return next(iter(b["clock_out"]))
        return None

    @staticmethod
    def _sig_name(net, ext):
        """配線に書く信号名。外部デバイスに繋ぐ配線は top ポート名、内部配線は内部側の名前 (浅いパス優先)"""
        depth = lambda n: len([p for p in n.split("/")[:-1] if p])          # "/x" (top 直下) = 0、"u_bridge/fr_s1" = 1
        ids = sorted(net["ids"], key=lambda n: ((n.startswith("@") != ext), depth(n), len(n), n))
        return ids[0].split("/")[-1].lstrip("@")

    def build(self):
        self._merge_aliases()
        # 方向不明 (IP) の端点を相手側から逆算
        for n in self.nets.values():
            roles = {e[2] for e in n["ends"]}
            fixed = []
            for e in n["ends"]:
                if e[2] == "?":
                    r = "dst" if "src" in roles else ("src" if "dst" in roles else "?")
                    fixed.append((e[0], e[1], r, e[3], e[4]))
                else:
                    fixed.append(e)
            n["ends"] = fixed
        # クロックドメイン: クロックポート (posedge 使用) に繋がるネット名。ドメイン一覧は自作 RTL / 本体が使うクロックだけ
        for n in self.nets.values():
            for blk, port, role, is_clk, _ in n["ends"]:
                if is_clk:
                    self.blocks[blk]["clocks" if role == "dst" else "clock_out"].add(n["name"])
        doms = sorted({c for b in self.blocks.values() if b["kind"] in ("rtl", "body") for c in b["clocks"]},
                      key=lambda c: (DOMAIN_ORDER.index(c) if c in DOMAIN_ORDER else 99, c))
        for b in self.blocks.values():   # IP (MIG / MMCM / BUFG) は生成するクロックのドメインに属する。内部専用クロックは落とす
            b["clocks"] = (b["clocks"] | b["clock_out"]) & set(doms)
        # 外部デバイスが供給するクロック (cam_pclk → BUFG → clk_cam): そのデバイス発の配線はソース同期でありCDC ではない
        provides = {}
        for n in self.nets.values():
            ext_src = [e[0] for e in n["ends"] if e[2] == "src" and e[0].startswith("ext:")]
            gens = [self.blocks[e[0]]["clock_out"] for e in n["ends"] if e[3] and e[2] == "dst" and self.blocks[e[0]]["clock_out"]]
            for x in ext_src:
                for g in gens:
                    provides.setdefault(x, set()).update(g)
        self.provides = provides
        # CTRL タグ
        self._tags()
        # ネット → ブロック対の信号集合
        pairs = {}   # (src, dst, forced kind) -> {signal name: 発生ドメイン or None}
        for n in self.nets.values():
            srcs = {(e[0], e[1], e[3], e[4]) for e in n["ends"] if e[2] == "src"}
            dsts = {(e[0], e[3], e[4]) for e in n["ends"] if e[2] == "dst"}
            for s, sport, sclk, srst in srcs:
                for d, dclk, drst in dsts:
                    if s == d:
                        continue
                    key = (s, d, "clock" if (sclk or dclk) else ("reset" if (srst or drst) else None))
                    name = self._sig_name(n, s.startswith("ext:") or d.startswith("ext:"))
                    pairs.setdefault(key, {})[name] = self._src_domain(n, s, sport)
        edges = []
        for (s, d, forced), sigs in sorted(pairs.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")):
            groups = {}   # (kind, label) -> signals。同じ kind は 1 本に併合し、最大グループのラベルを採る
            for sig in sorted(sigs):
                key = re.sub(r"_w$", "", sig)
                if forced:
                    groups.setdefault((forced, None), []).append(sig)
                    continue
                for pat, kind, label in KIND_RULES:
                    if re.search(pat, key):
                        groups.setdefault((kind, label), []).append(sig)
                        break
                else:
                    groups.setdefault(("data", "?"), []).append(sig)
            by_kind = {}
            for (kind, label), ss in groups.items():
                by_kind.setdefault(kind, []).append((label, ss))
            for kind, gs in by_kind.items():
                gs.sort(key=lambda g: (g[0] in (None, "?"), -len(g[1])))
                ss = [x for _, g in gs for x in g]
                top_n = len(gs[0][1])   # 最大グループと同数のラベル付きグループは連結して意味を落とさない
                label = " / ".join(g[0] for g in gs if g[0] not in (None, "?") and len(g[1]) == top_n) or gs[0][0]
                unmatched = sorted(x for lb, g in gs if lb == "?" for x in g)   # 規則に当たらなかった信号 (併合後も残す)
                if label in (None, "?"):
                    label = _auto_label(ss)
                sd, dd = self.blocks[s]["clocks"], self.blocks[d]["clocks"]
                if kind in ("clock", "reset"):
                    cdc = False                                    # クロック / リセット線の CDC は定義しない
                elif s.startswith("ext:"):                         # 外部ピン発は非同期入力 (clock.md「board pin →」行)。
                    sd = {"board pin"}                             # 物理線 (→ IP) とソース同期入力 (供給クロックのドメイン) は除く
                    cdc = bool(dd) and self.blocks[d]["kind"] != "ip" and not (dd <= self.provides.get(s, set()))
                else:
                    cdc = bool(sd and dd and not (sd & dd))
                edges.append(dict(src=s, dst=d, kind=kind, label=label, signals=sorted(ss), unmatched=unmatched,
                                  cdc=cdc, from_domains=sorted(sd), to_domains=sorted(dd),
                                  signal_domains={x: sigs[x] for x in sorted(ss) if sigs.get(x)}))
        blocks = []
        for b in self.blocks.values():
            bb = dict(b)
            bb["clocks"] = sorted(b["clocks"], key=lambda c: (DOMAIN_ORDER.index(c) if c in DOMAIN_ORDER else 99, c))
            bb["clock_out"] = sorted(b["clock_out"])
            bb["cdc"] = len(bb["clocks"]) >= 2 and not bb["clock_out"]
            bb["tags"] = sorted(b["tags"])
            blocks.append(bb)
        wrappers = [b["id"] for b in blocks if b["kind"] == "wrapper"]
        return dict(root=self.root, domains=doms, wrappers=wrappers, blocks=blocks, edges=edges,
                    modules={n: dict(label=m.label, file=m.path.name, expand=m.expand) for n, m in self.mods.items()})

    def _tags(self):
        ur = self.mods.get("uart_regio")
        if not ur:
            return
        addr = {}
        for m in re.finditer(r"0x([0-9A-Fa-f]{2})\s+(?:R/W|R|W)\s+((?:CTRL|CMD|STAT)_[A-Z0-9]+)", ur.info["header"]):
            addr[m.group(1).upper()] = m.group(2)
        port_tag = {}
        for p in ur.info["ports"]:
            if p["dir"] != "out":
                continue
            m = re.search(r"0x([0-9A-Fa-f]{2})", p["comment"] or "")
            if m and m.group(1).upper() in addr:
                port_tag[p["name"]] = addr[m.group(1).upper()]
        ur_ids = {b["id"] for b in self.blocks.values() if b["module"] == "uart_regio"}
        for n in self.nets.values():
            tags = {port_tag[e[1]] for e in n["ends"] if e[0] in ur_ids and e[2] == "src" and e[1] in port_tag}
            if not tags:
                continue
            for blk, port, role, _, _ in n["ends"]:
                if role == "dst" and blk not in ur_ids and not blk.startswith("ext:"):
                    self.blocks[blk]["tags"] |= tags


def _auto_label(sigs):
    if len(sigs) == 1:
        return sigs[0]
    pre = sigs[0]
    for s in sigs[1:]:
        i = 0
        while i < min(len(pre), len(s)) and pre[i] == s[i]:
            i += 1
        pre = pre[:i]
    pre = pre.rstrip("_")
    return f"{pre}_* ({len(sigs)})" if len(pre) >= 2 else " / ".join(sigs[:3]) + (f" +{len(sigs) - 3}" if len(sigs) > 3 else "")


# ---------------------------------------------------------------- 検査 / 表示
def summary(g):
    print(f"root {g['root']}  domains {g['domains']}")
    print(f"{len(g['blocks'])} blocks / {len(g['edges'])} edges")
    for b in g["blocks"]:
        cnt = f" ×{b['count']}" if b["count"] > 1 else ""
        cdc = " CDC" if b["cdc"] else ""
        tags = f"  [{' / '.join(b['tags'])}]" if b["tags"] else ""
        print(f"  {b['kind']:<4} {b['id'] + cnt:<36} {'/'.join(b['clocks']):<22}{cdc:<4} {b['label'][:40]}{tags}")
    kinds = {}
    for e in g["edges"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("edges by kind:", kinds)
    for e in g["edges"]:
        if e["kind"] in ("clock", "reset"):
            continue
        flag = ("CDC " if e["cdc"] else "    ") + ("?? " if e["unmatched"] else "   ")
        print(f"  {flag}{e['kind']:<6} {e['src']:<28} -> {e['dst']:<28} {e['label']}  {e['unmatched'] if e['unmatched'] else ''}")


def check_cdc(g, clock_md):
    """帯を跨ぐ配線 (ブロック間 CDC) と CDC ブロック (複数ドメインのブロック) を clock.md CDC マトリクスと突合する。
    ブロック内部の CDC (2-FF 段など) は構造から見えないので、行の「実装箇所」が CDC ブロック / 本体を指していれば構造一致とする。"""
    rows = []
    for line in clock_md.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|$", line)
        if not m or ("→" not in m.group(1) and "⇔" not in m.group(1)):
            continue
        ft = re.sub(r"`", "", m.group(1))
        sigs = [re.sub(r"\[.*?\]", "", s) for s in (re.findall(r"`([^`]+)`", m.group(2)) or [m.group(2)])]
        rows.append((ft, sigs, m.group(2), m.group(4)))
    cdc = [e for e in g["edges"] if (e["cdc"] and e["kind"] not in ("clock", "reset"))
           or (e["kind"] == "reset" and e["src"].startswith("ext:"))]   # 外部ピン発のリセットも非同期入力
    cdc_blocks = [b for b in g["blocks"] if b["cdc"]]
    print(f"clock.md rows {len(rows)} / extracted: CDC edges {len(cdc)}, CDC blocks {len(cdc_blocks)} "
          f"({', '.join(b['name'] for b in cdc_blocks)})")
    print("-- CDC edges (from → to : signals)")
    for e in cdc:
        print(f"  {'/'.join(e['from_domains']):<10} → {'/'.join(e['to_domains']):<10} {e['src']:<26} -> {e['dst']:<26} {', '.join(e['signals'])}")
    norm = lambda s: re.sub(r"^reg_|_(?:ui|px|w|async)$", "", s)       # 接尾辞 (_ui / _px / _w) と reg_ を剥いで完全一致で照合
    edge_sigs = {}
    for e in cdc:
        for s in e["signals"]:
            edge_sigs.setdefault(norm(s), []).append(e)
    cdc_mods = {b["module"] for b in cdc_blocks} | {"top" if b["id"] == "top#body" else "" for b in cdc_blocks}
    print("-- clock.md rows: 構造一致 = 信号名が CDC 配線に出る (edge) / 実装箇所が CDC ブロック (block)。それ以外 (?) は目視確認")
    n_ok, used = 0, set()
    for ft, sigs, raw, where in rows:
        hits = [e for s in sigs if re.fullmatch(r"[\w.]+", s) for e in edge_sigs.get(norm(s), [])]
        by_block = any(re.search(r"\b" + re.escape(m) + r"\b", where) for m in cdc_mods if m)
        st = "edge " if hits else ("block" if by_block else "?    ")
        used.update(id(e) for e in hits)
        n_ok += st != "?    "
        print(f"  {st} {ft:<22} {raw[:60]:<60} | {where[:40]}")
    print(f"構造一致 {n_ok}/{len(rows)}")
    print("-- 抽出にあって clock.md のどの行にも当たらない CDC 配線 (信号名不一致 or 記載漏れ):")
    for e in cdc:
        if id(e) not in used:
            print(f"  {'/'.join(e['from_domains']):<10} → {'/'.join(e['to_domains']):<10} {e['src']:<26} -> {e['dst']:<26} {', '.join(e['signals'])}")



def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rtl-dir", default=str(REPO / "rtl"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "graph.json"))
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--check-cdc", action="store_true")
    a = ap.parse_args(argv)
    g = Extractor(a.rtl_dir).run().build()
    if a.print:
        summary(g)
    if a.check_cdc:
        check_cdc(g, REPO / "doc" / "architecture" / "clock.md")
    if not (a.print or a.check_cdc):
        Path(a.out).write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        print(f"written {a.out} ({len(g['blocks'])} blocks, {len(g['edges'])} edges, domains {g['domains']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
