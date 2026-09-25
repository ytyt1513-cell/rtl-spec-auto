# -*- coding: utf-8 -*-
"""RTL → ブロック図の生成 (段階 (b)+(c))。rtlgraph.py で graph.json を抽出し、2 枚の図を自動配置・自動配線で描く。

    py tools/blockdiag/gen.py            # rtl/ → graph.json → doc/architecture/img/ の 4 図 (rtl_func_diagram_d1 = 概要 [top 直下のみ]、
                                         #   rtl_func_diagram = 詳細 [ラッパ内部展開]、rtl_clock_diagram、rtl_reset_diagram)
    py tools/blockdiag/gen.py --check    # 再生成して生成物 (graph.json / 2 図) がコミット済と一致するか + 重なり 0 か (差分あれば exit 1)
    py tools/blockdiag/gen.py --drawio   # 純 .drawio XML も併せて出力
    py tools/blockdiag/gen.py --png      # 確認用 PNG も出す (Microsoft Edge のヘッドレス描画。無ければ何もしない)
    py tools/blockdiag/gen.py --focus <module> [--out <dir>]   # spec 1.2 用の周辺接続図 (対象 + 直接の相手だけ。README「周辺接続図」)

図 1 機能ブロック図: x = データ配線 (data / serial) の DAG の最長経路 rank (クラスタ = top 直下のラッパ / ブロック → その内部の順)、
  y = 隣接ブロックの平均位置 (barycenter) で並べた行。ラッパは枠、クロックドメインはブロック右上のバッジ、CDC ブロックは二重枠。
  stat 配線と、CTRL タグで表す制御レジスタ線は描かない。
図 1 は概要 (top 直下のみ、_d1) と詳細 (ラッパ内部展開) の 2 枚を出す。
図 2 クロック / リセット図: y = ドメイン帯 (board pin + CDC 対が隣接する順 clk_in / clk_cam / ui_clk / clk_pixel / clk_serial)、x = 帯ごとに詰めた列。配線は帯を跨ぐ信号 (CDC) のみ。
  クロック源とリセット源は帯見出しの文言で示す。隣接 2 帯に属する CDC ブロックは境界を跨いで置く。
図 3 リセット図: 同じ帯配置で、ブロック = リセット源 + 帯ごとの受け側まとめ、配線 = リセット線と解除ゲート信号のみ。
配線は「列間の溝 (ラベル域 + 配線ごとの縦トラック)」と「行間のチャネル (配線ごとの横トラック)」だけを通るので、
ブロックの上を通らず、同一線上でも重ならない (交差は残る)。生成後に重なり検査を行い、`--check` で exit 1 にする。"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = next((p for p in HERE.parents if (p / "rtl").is_dir() or (p / ".git").exists()), HERE.parents[1])   # 配布先 (skill/scripts/blockdiag) でも動く
sys.path.insert(0, str(HERE))
from bdlib import Block, Edge, Path as BPath, render_svg, render_drawio, _txt_w  # noqa: E402
import rtlgraph  # noqa: E402

OUT_DIR = REPO / "doc" / "architecture" / "img"
DOM_FILL = {"board pin": "#ECEFF1", "clk_in": "#F5F5F5", "clk_cam": "#FFF3E0", "ui_clk": "#E3F2FD",
            "clk_pixel": "#E8F5E9", "clk_serial": "#F3E5F5"}
_PALETTE = ["#FFFDE7", "#FCE4EC", "#E0F7FA", "#F1F8E9"]
PITCH = 16          # 端子の間隔 (ラベル高 13 px より広く)
TRACK_PITCH = 10    # 溝 / チャネル内のトラック間隔
LABEL_H = 13
GUT_MIN = 24
PAD_TITLE = 40      # ラッパ枠の上余白 (見出し)
PAD = 12            # ラッパ枠の他の余白
MAXCOL = 12         # 機能図の最大列数 (超えるクラスタは次の段に折り返す)
LEFT = 20
TITLE_H = 58


def _lab_w(s):
    """配線ラベルの幅 (複数行なら最長行)"""
    return max(_txt_w(l) for l in (s or "").split("\n"))


def _lab_h(s):
    return LABEL_H * (1 + (s or "").count("\n"))


def wrap_signals(names, max_px=140, max_lines=2, suffix=""):
    """信号名の列挙を ", " で繋ぎ、max_px を超えない行に詰める (焦点図のラベル)。max_lines に収まるまで行幅を広げる"""
    while True:
        lines, cur = [], ""
        for n in names:
            cand = f"{cur}, {n}" if cur else n
            if cur and _txt_w(cand + ",") > max_px:
                lines.append(cur + ",")
                cur = n
            else:
                cur = cand
        if cur:
            lines.append(cur)
        if len(lines) <= max_lines:
            return "\n".join(lines) + suffix
        max_px += 20


def dom_fill(d):
    if d not in DOM_FILL:
        DOM_FILL[d] = _PALETTE[len(DOM_FILL) % len(_PALETTE)]
    return DOM_FILL[d]


def wrap_text(s, max_px, max_lines=2):
    if not s:
        return []
    out, cur = [], ""
    for ch in s:
        if _txt_w(cur + ch) > max_px and cur:
            out.append(cur)
            cur = ch
            if len(out) == max_lines:
                break
        else:
            cur += ch
    if len(out) < max_lines and cur:
        out.append(cur)
    elif cur:
        out[-1] = out[-1][:-1] + "…"
    return out


# ---------------------------------------------------------------- グラフ
class Graph:
    def __init__(self, g, depth=2, collapse_modules=()):
        self.raw = g
        self.blocks = {b["id"]: b for b in g["blocks"]}
        self.domains = list(g["domains"])
        ws = [b for b in self.blocks.values() if b["kind"] == "wrapper"
              and ((depth == 1 and b["parent"] == g["root"]) or b["module"] in collapse_modules)]
        if ws:
            self._collapse(ws)
        self.nodes = [b for b in self.blocks.values() if b["kind"] != "wrapper"]
        self.node_ids = [b["id"] for b in self.nodes]
        self.edges = [e for e in self.raw["edges"] if e["src"] in self.blocks and e["dst"] in self.blocks]

    def _collapse(self, ws):
        """ラッパ ws を 1 ブロックずつに畳む (--depth 1 は top 直下の全ラッパ、--focus はそのラッパだけ)"""
        for w in ws:
            desc = [b for b in self.blocks.values() if b["id"] != w["id"] and self._under(b["id"], w["id"])]
            ids = {b["id"] for b in desc}
            clocks = sorted({c for b in desc for c in b["clocks"]}, key=self.dom_key)
            self.blocks[w["id"]] = dict(w, kind="rtl", count=1, clocks=clocks, clock_out=[], cdc=len(clocks) >= 2,
                                        tags=sorted({t for b in desc for t in b["tags"]}), parent=w["parent"])
            for b in desc:
                del self.blocks[b["id"]]
            for e in self.raw["edges"]:
                for k in ("src", "dst"):
                    if e[k] in ids:
                        e[k] = w["id"]
        seen, edges = set(), []
        for e in self.raw["edges"]:
            key = (e["src"], e["dst"], e["kind"])
            if e["src"] == e["dst"] or key in seen:
                continue
            seen.add(key)
            edges.append(e)
        self.raw["edges"] = edges

    def dom_key(self, d):
        return self.domains.index(d) if d in self.domains else 99

    def _under(self, bid, wid):
        b = self.blocks[bid]
        while b["parent"] is not None and b["parent"] in self.blocks:
            if b["parent"] == wid:
                return True
            b = self.blocks[b["parent"]]
        return False

    def ancestor(self, bid):
        b, root = self.blocks[bid], self.raw["root"]
        while b["parent"] not in (None, root):
            b = self.blocks[b["parent"]]
        return b["id"]

    def set_root_ports(self, rtl_dir):
        info = rtlgraph.parse(Path(rtl_dir) / f"{self.raw['root']}.v")
        self._inputs = {p["name"] for p in info["ports"] if p["dir"] == "in"}

    def ext_side(self, b):
        return "L" if any(p in self._inputs for p in b["insts"]) else "R"


# ---------------------------------------------------------------- rank / 並び
def rank_nodes(nodes, edges, fixed=None):
    """最長経路 rank。edges = [(src, dst, weight)]。DFS (重い辺を先に、出力の重いソースから) で見つけた後退辺は無視する。
    weight = バンドルの信号数なので、映像の本線 (共通 IF 4 本) が優先され、戻り線 (1 本) が後退辺になる"""
    fixed = fixed or {}
    adj = {n: {} for n in nodes}
    for s, d, w in edges:
        if s in adj and d in adj and s != d:
            adj[s][d] = adj[s].get(d, 0) + w
    state, back = {}, set()
    def dfs(u):
        state[u] = 1
        for v in sorted(adj[u], key=lambda v: (-adj[u][v], v)):
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2
    indeg = {n: 0 for n in nodes}
    for s, d, w in edges:
        if s in indeg and d in indeg and s != d:
            indeg[d] += w
    for n in sorted(nodes, key=lambda n: (indeg[n] != 0, fixed.get(n, 1), -sum(adj[n].values()), n)):
        if n not in state:
            dfs(n)
    rank = dict(fixed)
    changed = True
    while changed:
        changed = False
        for u in sorted(nodes):
            for v in adj[u]:
                if (u, v) in back or v in fixed:
                    continue
                if rank.get(u, 0) + 1 > rank.get(v, 0):
                    rank[v] = rank.get(u, 0) + 1
                    changed = True
    for n in nodes:
        rank.setdefault(n, 0)
    return rank


class Layout:
    def __init__(self):
        self.rect, self.col, self.row = {}, {}, {}
        self.containers = []          # (id, name, func, x, y, w, h)
        self.texts = []               # 重なり検査に含める文字枠 (x, y, w, h)


def node_dims(b, n_left, n_right, view, pitch=PITCH):
    name = b["name"] + (f" ×{b['count']}" if b.get("count", 1) > 1 else "")
    if b["kind"] == "ext":
        lines = wrap_text(", ".join(b["insts"][:6]) + (" …" if len(b["insts"]) > 6 else ""), 150, 2)
    elif b["kind"] == "note":
        lines = wrap_words(b.get("label") or "", 210)          # 受け側まとめブロック: モジュール名の列挙 (識別子で折り返す。切らない)
    else:
        lines = wrap_text(b.get("label") or "", 120 if view == "focus" else 150)
    tag = " / ".join(b["tags"]) if b.get("tags") and view == "func" else None
    badges = b.get("badge_list", b.get("clocks", [])) if b["kind"] != "ext" else []
    w = max(_txt_w(name) * 1.15 + 24, max([_txt_w(l) for l in lines] + [0]) + 16, (_txt_w(tag) * 0.85 + 16) if tag else 0,
            sum(_txt_w(d) * 0.8 + 11 for d in badges) + _txt_w(name) * 0.6, 110)
    h = 24 + 13 * len(lines) + (13 if tag else 0) + (8 if badges else 0)
    h = max(h, pitch * (max(n_left, n_right) + 1))
    return name, lines, tag, round(w), round(h)


# ---------------------------------------------------------------- 図 1 の配置
def layout_func(G, view_edges):
    nodes = G.node_ids
    cluster = {n: G.ancestor(n) for n in nodes}
    clusters = sorted(set(cluster.values()))
    members = {c: [n for n in nodes if cluster[n] == c] for c in clusters}
    rank_edges = [(e["src"], e["dst"], len(e["signals"])) for e in view_edges
                  if e["kind"] in ("data", "serial") and not all(re.search(r"ready", s) for s in e["signals"])]
    ext_L = [c for c in clusters if G.blocks[c]["kind"] == "ext" and G.ext_side(G.blocks[c]) == "L"]
    ext_R = [c for c in clusters if G.blocks[c]["kind"] == "ext" and G.ext_side(G.blocks[c]) == "R"]
    inner = [c for c in clusters if c not in ext_L and c not in ext_R]
    cedges = [(cluster[s], cluster[d], w) for s, d, w in rank_edges
              if cluster[s] != cluster[d] and cluster[s] not in ext_R and cluster[d] not in ext_L]
    crank = rank_nodes(inner + ext_L, cedges, fixed={c: 0 for c in ext_L})
    irank, width = {}, {}
    for c in clusters:
        ms = members[c]
        ir = rank_nodes(ms, [(s, d, w) for s, d, w in rank_edges if s in ms and d in ms])
        irank.update(ir)
        width[c] = max(ir.values()) + 1
    # クラスタの 2 次元詰め込み: 開始列 = 前段クラスタの (開始列 + 幅) の最大。MAXCOL を超えるときは次の段 (行の帯) に折り返す
    preds = {c: set() for c in clusters}
    for s, d, w in cedges:
        if s in crank and d in crank and crank[s] < crank[d]:
            preds[d].add(s)
    nbr = {n: set() for n in nodes}
    for e in view_edges:
        nbr[e["src"]].add(e["dst"]); nbr[e["dst"]].add(e["src"])
    order = sorted(inner, key=lambda c: (crank[c], -len(members[c]), c))
    start, strip, rows_of, placed = {}, {}, {}, []      # placed: (c, s, w, strip, r0, r1)
    depth = {c: max(len([m for m in members[c] if irank[m] == ic]) for ic in range(width[c])) for c in clusters}
    for c in ext_L:
        start[c], strip[c] = 0, 0
    cur, used = 0, False
    for c in order:
        s = max([start[p] + width[p] for p in preds[c] if strip.get(p) == cur] + [1])
        if s + width[c] > MAXCOL and used:
            cur, used = cur + 1, False
            s = 1
        start[c], strip[c], used = s, cur, True
    for c in ext_R:
        start[c], strip[c] = MAXCOL, 0
    # 行: 段ごとに、列範囲が重なる既配置クラスタの下に積む (barycenter 順に処理)
    pos = {}
    mean = lambda vals, fb: (sum(vals) / len(vals)) if vals else fb
    def cluster_bary(c):
        ext = [pos.get(k, 0) for m in members[c] for k in nbr[m] if cluster[k] != c]
        return mean(ext, mean([pos.get(m, 0) for m in members[c]], 0))
    def node_bary(m):
        return mean([pos.get(k, 0) for k in nbr[m]], pos.get(m, 0))
    L = Layout()
    L.ncol = max(start[c] + width[c] for c in clusters)
    for n in nodes:
        L.col[n] = start[cluster[n]] + irank[n]
    member_cols = {}
    nstrip = max(strip.values()) + 1
    for _ in range(4):
        placed = []
        strip_off = 0
        for st in range(nstrip):
            cs = sorted([c for c in clusters if strip[c] == st], key=lambda c: (start[c], cluster_bary(c), c))
            for c in cs:
                r0 = 0
                for (c2, s2, w2, st2, a, b) in placed:
                    if st2 == st and s2 < start[c] + width[c] and start[c] < s2 + w2:
                        r0 = max(r0, b + 1)
                icols = {}
                for m in members[c]:
                    icols.setdefault(irank[m], []).append(m)
                for ic, ms in sorted(icols.items()):
                    ms.sort(key=lambda m: (node_bary(m), m))
                    for i, m in enumerate(ms):
                        L.row[m] = strip_off + r0 + i
                        pos[m] = 60 * L.row[m] + 30
                member_cols[c] = icols
                placed.append((c, start[c], width[c], st, r0, r0 + depth[c] - 1))
                rows_of[c] = (strip_off + r0, strip_off + r0 + depth[c] - 1)
            strip_off += max([p[5] for p in placed if p[3] == st] + [-1]) + 1
    L.cluster_rows = rows_of
    L.nrow = max(L.row.values()) + 1
    L.cluster, L.members, L.clusters = cluster, members, clusters
    L.pick_channel = lambda rs, rd: rs + 1 if rd >= rs else rs      # チャネル k = 行 k の上の隙間 (k = nrow は最下)
    return L


def place_func(G, L):
    """行 / 列 / 溝 / チャネルの寸法から座標を決める。assign_tracks 済みが前提"""
    is_wr = lambda c: G.blocks[c]["kind"] == "wrapper"
    starts = {L.cluster_rows[c][0] for c in L.clusters if is_wr(c)}
    ends = {L.cluster_rows[c][1] + 1 for c in L.clusters if is_wr(c)}
    rowh = [0] * L.nrow
    for n in L.col:
        rowh[L.row[n]] = max(rowh[L.row[n]], L.dims[n][4])
    y = TITLE_H
    L.chan_y = {}
    L.row_top = []
    for k in range(L.nrow + 1):
        y += (PAD if k in ends else 8)
        L.chan_y[k] = y
        y += TRACK_PITCH * (len(L.chan_tracks[k]) + 1)
        y += (PAD_TITLE if k in starts else 8)
        if k < L.nrow:
            L.row_top.append(y)
            y += rowh[k]
    L.height = y + 30
    for n in L.col:
        name, lines, tag, w, h = L.dims[n]
        c, r = L.col[n], L.row[n]
        L.rect[n] = [L.cx[c] + (L.colw[c] - w) / 2, L.row_top[r] + (rowh[r] - h) / 2, w, h]
    for c in L.clusters:
        if not is_wr(c):
            continue
        ms = L.members[c]
        x0 = min(L.rect[m][0] for m in ms) - PAD; x1 = max(L.rect[m][0] + L.rect[m][2] for m in ms) + PAD
        y0 = L.row_top[L.cluster_rows[c][0]] - PAD_TITLE; y1 = L.row_top[L.cluster_rows[c][1]] + rowh[L.cluster_rows[c][1]] + PAD
        L.containers.append((c, G.blocks[c]["name"], G.blocks[c].get("label", ""), x0, y0, x1 - x0, y1 - y0))
        L.texts.append((x0 + 10, y0 + 2, min(x1 - x0 - 20, 260), 30))
        for w in sorted({G.blocks[m]["parent"] for m in ms} - {c}):
            if w in G.blocks and G.blocks[w]["kind"] == "wrapper":
                sub = [m for m in ms if G.blocks[m]["parent"] == w]
                sx0 = min(L.rect[m][0] for m in sub) - 6; sx1 = max(L.rect[m][0] + L.rect[m][2] for m in sub) + 6
                sy0 = min(L.rect[m][1] for m in sub) - 22; sy1 = max(L.rect[m][1] + L.rect[m][3] for m in sub) + 6
                L.containers.append((w, G.blocks[w]["name"], "", sx0, sy0, sx1 - sx0, sy1 - sy0))


# ---------------------------------------------------------------- 配線 (共通)
def assign_tracks(L, view_edges):
    """経路種別と溝 / チャネルのトラック数を決め、溝幅を確定する (座標はまだ不要)"""
    routes, label_w = [], {c: 0 for c in range(L.ncol + 2)}
    label_w_rev = {c: 0 for c in range(L.ncol + 2)}          # 逆隣接 (右列 → 左列) のラベル域。溝の右端 (トラックの右) に置く
    L.gut_tracks = {c: [] for c in range(L.ncol + 2)}
    L.chan_tracks = {k: [] for k in range(L.nrow + 1)}
    for e in view_edges:
        s, d = e["src"], e["dst"]
        cs, cd, rs, rd = L.col[s], L.col[d], L.row[s], L.row[d]
        r = dict(e=e, cs=cs, cd=cd, g1=cs + 1, g2=None, k=None)
        if cd == cs - 1 and getattr(L, "radj", False):   # 逆隣接の直結は --focus だけ (全体図は従来どおりチャネル経由)
            r["type"], r["g1"] = "radj", cs
            label_w[cs] = max(label_w[cs], _lab_w(e["label"]) + 14)      # 相手ブロックの右 (順方向ラベルと同じ域) に置く
            L.gut_tracks[cs].append(r)
            routes.append(r)
            continue
        label_w[cs + 1] = max(label_w[cs + 1], _lab_w(e["label"]) + 14)
        if cd == cs + 1:
            r["type"] = "adj"
        elif cd == cs:
            r["type"] = "same"
        else:
            r["type"], r["g2"], r["k"] = "chan", cd, L.pick_channel(rs, rd)
            L.chan_tracks[r["k"]].append(r)
        L.gut_tracks[r["g1"]].append(r)
        if r["g2"] is not None:
            L.gut_tracks[r["g2"]].append(r)
        routes.append(r)
    L.routes, L.label_w, L.label_w_rev = routes, label_w, label_w_rev
    L.gutw = [max(GUT_MIN, label_w[c] + TRACK_PITCH * (len(L.gut_tracks[c]) + 1) + label_w_rev[c] + 6) for c in range(L.ncol + 1)]
    x = LEFT
    L.gx, L.cx = [], []
    for c in range(L.ncol + 1):
        L.gx.append(x); x += L.gutw[c]
        if c < L.ncol:
            L.cx.append(x); x += L.colw[c]
    L.width = x + LEFT


def route_edges(L):
    rect = L.rect
    cy = lambda n: rect[n][1] + rect[n][3] / 2
    left, right = {}, {}
    for r in L.routes:
        s, d = r["e"]["src"], r["e"]["dst"]
        key = (s, d, r["e"]["kind"])
        if r["type"] == "radj":                       # 出る側の左辺 → 入る側の右辺 (溝 1 本で直結)
            left.setdefault(s, []).append((cy(d), key, r, "out"))
            right.setdefault(d, []).append((cy(s), key, r, "in"))
            continue
        right.setdefault(s, []).append((cy(d), key, r, "out"))
        (right if r["type"] == "same" else left).setdefault(d, []).append((cy(s), key, r, "in"))
    def anchors(side, phase):
        """端子 y を PITCH 格子に置く。右側 (出る / 同列入り) は phase 8、左側 (入る) は phase 0 なので、
        同じ溝を通る出線と入線が同じ y になることがない"""
        for n, lst in side.items():
            lst.sort(key=lambda t: (t[0], t[1]))
            x, y, w, h = rect[n]
            pitch = getattr(L, "pitch", PITCH)
            first = y + h / 2 - pitch * (len(lst) - 1) / 2
            first = round((first - phase) / pitch) * pitch + phase
            for i, (_, _, r, io) in enumerate(lst):
                r["ys" if io == "out" else "yd"] = first + i * pitch
    pitch = getattr(L, "pitch", PITCH)
    anchors(left, 0); anchors(right, pitch // 2)
    for c, lst in L.gut_tracks.items():
        if c > L.ncol:
            continue
        lst.sort(key=lambda r: (r["ys"], r["e"]["src"], r["e"]["dst"]))
        x0 = L.gx[c] + L.label_w[c] + TRACK_PITCH
        for i, r in enumerate(lst):
            r.setdefault("tx", {})[c] = x0 + i * TRACK_PITCH
    for k, lst in L.chan_tracks.items():
        lst.sort(key=lambda r: (min(r["tx"][r["g1"]], r["tx"][r["g2"]]), r["e"]["src"]))
        for i, r in enumerate(lst):
            r["hy"] = L.chan_y[k] + TRACK_PITCH * (i + 1)
    for r in L.routes:
        s, d = r["e"]["src"], r["e"]["dst"]
        sx = rect[s][0] + rect[s][2]
        dxl, dxr = rect[d][0], rect[d][0] + rect[d][2]
        ys, yd, x1 = r["ys"], r["yd"], r["tx"][r["g1"]]
        if r["type"] == "radj":
            sxl = rect[s][0]
            r["pts"] = [(sxl, ys), (x1, ys), (x1, yd), (dxr, yd)] if abs(ys - yd) > 0.5 else [(sxl, ys), (dxr, yd)]
            lw, lh = _lab_w(r["e"]["label"]) + 6, _lab_h(r["e"]["label"])
            r["label_box"] = (dxr + 3, yd - 1 - lh, lw, lh)           # 入る側 (相手ブロック) の右上、最終区間の上
            continue
        if r["type"] == "adj":
            pts = [(sx, ys), (x1, ys), (x1, yd), (dxl, yd)]
        elif r["type"] == "same":
            pts = [(sx, ys), (x1, ys), (x1, yd), (dxr, yd)]
        else:
            x2, hy = r["tx"][r["g2"]], r["hy"]
            pts = [(sx, ys), (x1, ys), (x1, hy), (x2, hy), (x2, yd), (dxl, yd)]
        out = [pts[0]]
        for p in pts[1:]:
            if abs(p[0] - out[-1][0]) > 0.5 or abs(p[1] - out[-1][1]) > 0.5:
                out.append(p)
        r["pts"] = out
        r["label_box"] = (sx + 3, ys - 1 - _lab_h(r["e"]["label"]), _lab_w(r["e"]["label"]) + 6, _lab_h(r["e"]["label"]))


# ---------------------------------------------------------------- 重なり検査
def _seg_hits_rect(p, q, R, tol=0.5):
    x0, y0, w, h = R
    x1, y1 = x0 + w, y0 + h
    if abs(p[0] - q[0]) < 0.5:
        return x0 + tol < p[0] < x1 - tol and max(min(p[1], q[1]), y0 + tol) < min(max(p[1], q[1]), y1 - tol)
    return y0 + tol < p[1] < y1 - tol and max(min(p[0], q[0]), x0 + tol) < min(max(p[0], q[0]), x1 - tol)


def _rects_overlap(a, b, tol=0.5):
    return a[0] + tol < b[0] + b[2] and b[0] + tol < a[0] + a[2] and a[1] + tol < b[1] + b[3] and b[1] + tol < a[1] + a[3]


def check_overlaps(L):
    v = []
    segs = [(r, p, q) for r in L.routes for p, q in zip(r["pts"], r["pts"][1:])]
    rects = L.rect
    for r, p, q in segs:
        for n, R in rects.items():
            if n not in (r["e"]["src"], r["e"]["dst"]) and _seg_hits_rect(p, q, R):
                v.append(("block×edge", f"{r['e']['src']}→{r['e']['dst']} crosses {n}"))
    for i, (r1, p1, q1) in enumerate(segs):
        for r2, p2, q2 in segs[i + 1:]:
            if r1 is r2:
                continue
            v1, v2 = abs(p1[0] - q1[0]) < 0.5, abs(p2[0] - q2[0]) < 0.5
            if v1 != v2:
                continue
            a = 0 if v1 else 1
            if abs(p1[a] - p2[a]) < 0.5 and min(max(p1[1 - a], q1[1 - a]), max(p2[1 - a], q2[1 - a])) - \
                    max(min(p1[1 - a], q1[1 - a]), min(p2[1 - a], q2[1 - a])) > 1:
                v.append(("edge×edge", f"{r1['e']['src']}→{r1['e']['dst']} || {r2['e']['src']}→{r2['e']['dst']}"))
    labels = [(r, r["label_box"]) for r in L.routes if r["e"]["label"]]
    for r, lb in labels:
        for r2, p, q in segs:
            if r2 is r and p == r["pts"][0]:
                continue
            if _seg_hits_rect(p, q, lb, 0):
                v.append(("label×edge", f"'{r['e']['label']}' ({r['e']['src']}→{r['e']['dst']}) hit by {r2['e']['src']}→{r2['e']['dst']}"))
        for n, R in rects.items():
            if _rects_overlap(lb, R):
                v.append(("label×block", f"'{r['e']['label']}' ({r['e']['src']}→{r['e']['dst']}) over {n}"))
        for t in L.texts:
            if _rects_overlap(lb, t):
                v.append(("label×text", f"'{r['e']['label']}' ({r['e']['src']}→{r['e']['dst']}) over text"))
    for i, (r1, a) in enumerate(labels):
        for r2, b in labels[i + 1:]:
            if _rects_overlap(a, b):
                v.append(("label×label", f"'{r1['e']['label']}' / '{r2['e']['label']}'"))
    for t in L.texts:
        for n, R in rects.items():
            if _rects_overlap(t, R):
                v.append(("text×block", f"text over {n}"))
        for r, p, q in segs:
            if _seg_hits_rect(p, q, t, 0):
                v.append(("text×edge", f"text at ({t[0]:.0f},{t[1]:.0f}) crossed by {r['e']['src']}→{r['e']['dst']}"))
    return v


# ---------------------------------------------------------------- bdlib モデル
def build_model(G, L, title, subtitle, legend, bands=None, band_texts=None):
    B, C, E, T, X, P = [], [], [], [], [], []
    T.append(dict(x=LEFT, y=28, s=title, size=17, weight="bold", anchor="start"))
    T.append(dict(x=LEFT, y=46, s=subtitle, size=10, fill="#546E7A", anchor="start"))
    for bd in bands or []:
        X.append(Block(bd["id"], bd["name"], bd["func"], LEFT, bd["y"], L.width - 2 * LEFT, bd["h"], note=bd["fill"]))
    for t in band_texts or []:
        T.append(t)
    for cid, name, func, x, y, w, h in L.containers:
        C.append(Block(cid, name, wrap_text(func, w - 20, 1)[0] if func else None, round(x), round(y), round(w), round(h)))
    for n, (x, y, w, h) in L.rect.items():
        b = G.blocks[n]
        name, lines, tag, _, _ = L.dims[n]
        doms = b.get("badge_list", b.get("clocks", [])) if b["kind"] != "ext" else []
        B.append(Block(n, name, "\n".join(lines), round(x), round(y), w, h,
                       kind=b["kind"] if b["kind"] in ("rtl", "ip", "ext", "body", "note") else "rtl",
                       tag=tag, badges=[(d, dom_fill(d)) for d in doms], cdc=bool(b.get("cdc"))))
    rect = L.rect
    for r in L.routes:
        e = r["e"]
        s, d = e["src"], e["dst"]
        rev = r["type"] == "radj"
        sa = f"{'l' if rev else 'r'}:{(r['ys'] - rect[s][1]) / rect[s][3]:.4f}"
        da = f"{'r' if r['type'] in ('same', 'radj') else 'l'}:{(r['yd'] - rect[d][1]) / rect[d][3]:.4f}"
        ed = Edge(s, sa, d, da, e["label"], kind=e["kind"], pts=r["pts"][1:-1], lab_seg=len(r["pts"]) - 2 if rev else 0)
        p1x = r["pts"][1][0]
        if rev:
            x1, dxr = r["pts"][-2][0], r["pts"][-1][0]
            ed.lab_dx = 3 + (_lab_w(e["label"]) + 6) / 2 - (x1 - dxr) / 2                                # 入る側の右 (最終区間の上) に固定
        else:
            ed.lab_dx = 3 + (_lab_w(e["label"]) + 6) / 2 - (p1x - (rect[s][0] + rect[s][2])) / 2         # 引き出し区間の上に固定
        E.append(ed)
    ly = L.height - 14
    T.append(dict(x=LEFT, y=ly + 4, s="凡例", size=11, weight="bold", anchor="start"))
    x0 = LEFT + 44
    for kind, s in legend:
        P.append(BPath([(x0, ly), (x0 + 36, ly)], kind, arrow=True))
        T.append(dict(x=x0 + 42, y=ly + 4, s=s, size=9.5, anchor="start"))
        x0 += 42 + _txt_w(s) + 24
    index = {b.id: b for b in B + C + X}
    return dict(bands=X, blocks=B, containers=C, edges=E, texts=T, lines=[], paths=P, index=index)


# ---------------------------------------------------------------- 図 1
def gen_func(G, depth):
    tag_src = {b["id"] for b in G.nodes if b["module"] == "uart_regio" and b["kind"] == "body"}
    view_edges = [e for e in G.edges if e["kind"] not in ("stat", "clock", "reset")
                  and not (e["kind"] == "ctrl" and e["src"] in tag_src and G.blocks[e["dst"]].get("tags"))]
    L = layout_func(G, view_edges)
    n_left, n_right = {n: 0 for n in L.col}, {n: 0 for n in L.col}
    for e in view_edges:
        n_right[e["src"]] += 1
        (n_right if L.col[e["dst"]] == L.col[e["src"]] else n_left)[e["dst"]] += 1
    L.dims = {n: node_dims(G.blocks[n], n_left[n], n_right[n], "func") for n in L.col}
    L.colw = [0] * L.ncol
    for n in L.col:
        L.colw[L.col[n]] = max(L.colw[L.col[n]], L.dims[n][3])
    assign_tracks(L, view_edges)
    place_func(G, L)
    route_edges(L)
    legend = [("data", "映像 / データ IF"), ("axi", "AXI4 / DDR3"), ("ctrl", "制御トリガ (CTRL_xx レジスタは紫タグ)"), ("serial", "外部シリアル")]
    model = build_model(G, L,
                        "Arty S7-50 HDMI Out — RTL 機能ブロック図 (RTL から自動生成)" + (" [top 直下のみ]" if depth == 1 else ""),
                        "横 = データの流れ (最長経路 rank)。枠 = ラッパ、点線 = ラッパ本体ロジック、二重枠 = CDC ブロック、右上バッジ = クロックドメイン。"
                        "stat 配線と CTRL レジスタ線は描かない (タグで表す)。生成: py tools/blockdiag/gen.py",
                        legend)
    return model, L, check_overlaps(L)


# ---------------------------------------------------------------- 図 2
def clock_freqs():
    fq = {}
    p = REPO / "doc" / "architecture" / "clock.md"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*`(\w+)`\s*\|\s*([\d.]+\s*MHz[^|]*)\|", line)
            if m:
                fq.setdefault(m.group(1), m.group(2).strip().split(" (")[0])
    return fq


def gen_clock(G, Lf):
    bands = ["board pin"] + G.domains
    bi = {d: i for i, d in enumerate(bands)}
    fq = clock_freqs()
    nodes = [b for b in G.nodes if b["clocks"] or b["kind"] == "ext"]
    ids = {b["id"] for b in nodes}
    view_edges = [e for e in G.edges if e["src"] in ids and e["dst"] in ids and e["cdc"] and e["kind"] not in ("clock", "reset")]
    # 帯見出し (列 0 に複数行): 周波数、クロック源 (外部供給なら ← ピン (デバイス))、リセット源 (IP 内部入力 = MIG sys_rst は除く)
    src_lines = {}
    for d in G.domains:
        gens = []
        for b in sorted(G.nodes, key=lambda b: b["name"]):
            if d not in b.get("clock_out", []):
                continue
            feed = sorted({f"{s} ({G.blocks[e['src']]['name']})" for e in G.edges if e["kind"] == "clock" and e["dst"] == b["id"]
                           and G.blocks[e["src"]]["kind"] == "ext" for s in e["signals"]})
            gens.append(b["name"] + (f" ← {', '.join(feed)}" if feed else ""))
        rsts = sorted({f"{sig} ← {G.blocks[e['src']]['name']}" for e in G.edges if e["kind"] == "reset"
                       and G.blocks[e["dst"]]["kind"] != "ip" and d in G.blocks[e["dst"]]["clocks"]
                       and len(G.blocks[e["dst"]]["clocks"]) == 1 for sig in e["signals"]})
        src_lines[d] = [fq.get(d, "")] + [f"源: {g}" for g in gens] + ([f"リセット: {' / '.join(rsts[:3])}"] if rsts else [])
    src_lines["board pin"] = ["非同期入力", "(外部デバイス / ボードピン)"]
    def primary(b):
        cl = b["clocks"]
        if b["kind"] == "ext" or not cl:
            return "board pin", None
        if len(cl) == 2 and abs(bi[cl[0]] - bi[cl[1]]) == 1 and not b["clock_out"]:
            return cl[0], cl[1]
        return cl[0], None
    L = layout_bands(nodes, bands, bi, primary, src_lines, view_edges, lambda b: Lf.col[b["id"]])
    legend = [("data", "帯を跨ぐ配線 = CDC (方式は clock.md CDC マトリクス)"), ("ctrl", "同 (制御)"), ("stat", "同 (ステータス)"),
              ("serial", "同 (外部シリアル)")]
    model = build_model(G, L, "Arty S7-50 HDMI Out — クロック / リセット図 (RTL から自動生成)",
                        "縦 = クロックドメイン帯 (CDC 対が隣接する順)。帯境界を跨ぐ二重枠 = CDC ブロック。帯内の配線は描かない。"
                        "クロック源とリセット源は左端の帯見出しに記す。生成: py tools/blockdiag/gen.py",
                        legend, bands=L.band_geo, band_texts=L.band_texts)
    return model, L, check_overlaps(L)


def wrap_words(s, max_px):
    """空白 / スラッシュ / 矢印を境界にして折り返す (識別子の途中で切らない)"""
    out, cur = [], ""
    for tok in re.findall(r"[^\s/←]+|[\s/←]+", s):
        if cur and _txt_w(cur + tok) > max_px and tok.strip():
            out.append(cur.rstrip())
            cur = tok.lstrip()
        else:
            cur += tok
    return out + ([cur.rstrip()] if cur.strip() else [])


def layout_bands(nodes, bands, bi, primary, head_lines, view_edges, col_key):
    """帯 (行) 配置の共通部 (図 2 / 図 3)。列 0 は帯見出し (ブロックもトラックも通らない)、列は帯ごとに col_key 順で詰める。
    隣接 2 帯を持つブロック (primary が (上, 下) を返す) は帯境界を跨いで置く。帯 = [上部: 跨ぎの下半分 + チャネル][積み上げ][下部: 跨ぎの上半分]"""
    L = Layout()
    span_of = {}
    for b in nodes:
        p, q = primary(b)
        L.row[b["id"]] = bi[p]
        b["badge_list"] = [c for c in b.get("clocks", []) if c not in (p, q)]
        if q:
            span_of[b["id"]] = (bi[p], bi[q])
    for i in range(len(bands)):
        for j, b in enumerate(sorted([b for b in nodes if L.row[b["id"]] == i], key=lambda b: (col_key(b), b["id"]))):
            L.col[b["id"]] = j + 1
    L.ncol = max(L.col.values()) + 1
    L.nrow = len(bands)
    L.pick_channel = lambda rs, rd: rd if rd > rs else rs          # チャネル k = 帯 k の上部
    n_left, n_right = {b["id"]: 0 for b in nodes}, {b["id"]: 0 for b in nodes}
    for e in view_edges:
        n_right[e["src"]] += 1
        (n_right if L.col[e["dst"]] == L.col[e["src"]] else n_left)[e["dst"]] += 1
    L.dims = {b["id"]: node_dims(b, n_left[b["id"]], n_right[b["id"]], "band") for b in nodes}
    L.colw = [0] * L.ncol
    L.colw[0] = 236
    head = {}
    for d in bands:
        lines = []
        for s in head_lines[d]:
            lines += wrap_words(s, L.colw[0] - 16)
        head[d] = lines[:7]
    for b in nodes:
        L.colw[L.col[b["id"]]] = max(L.colw[L.col[b["id"]]], L.dims[b["id"]][3])
    assign_tracks(L, view_edges)
    cell, span = {}, {}
    for b in nodes:
        (span if b["id"] in span_of else cell).setdefault((L.row[b["id"]], L.col[b["id"]]), []).append(b["id"])
    for k in list(cell) + list(span):
        (cell if k in cell else span)[k].sort()
    half = lambda i: L.dims[i][4] / 2 + 6
    band_geo, band_texts = [], []
    y = TITLE_H + 6
    L.chan_y = {}
    for i, d in enumerate(bands):
        top = y
        res_t = max([sum(half(x) for x in v) for (u, c), v in span.items() if u + 1 == i] + [0])
        res_b = max([sum(half(x) for x in v) for (u, c), v in span.items() if u == i] + [0])
        y += 22 + res_t
        L.chan_y[i] = y
        y += TRACK_PITCH * (len(L.chan_tracks[i]) + 1) + 4
        stack_top = y
        stack = max([sum(L.dims[x][4] + 12 for x in v) for (u, c), v in cell.items() if u == i] + [0])
        y = max(stack_top + stack + res_b + 10, top + 22 + 13 * len(head[d]) + 12)
        for (u, c), v in cell.items():
            if u != i:
                continue
            yv = stack_top
            for x_ in v:
                name, lines, tag, w, h = L.dims[x_]
                L.rect[x_] = [L.cx[c] + (L.colw[c] - w) / 2, yv, w, h]
                yv += h + 12
        band_geo.append(dict(id=f"bandbg:{d}", name="", func="", y=top, h=y - top - 4, fill=dom_fill(d)))   # 見出しは列 0 に自前で描く
        hx = L.cx[0] + 4
        band_texts.append(dict(x=hx, y=top + 16, s=d, size=12, weight="bold", fill="#37474F", anchor="start", family="Consolas,'Courier New',monospace"))
        for j, s in enumerate(head[d]):
            band_texts.append(dict(x=hx, y=top + 31 + 13 * j, s=s, size=9.5, fill="#546E7A", anchor="start"))
        L.texts.append((hx, top + 4, L.colw[0] - 8, 20 + 13 * len(head[d])))
        y += 4
    L.chan_y[L.nrow] = y
    y += TRACK_PITCH * (len(L.chan_tracks[L.nrow]) + 1)
    L.height = y + 34
    for (u, c), v in span.items():
        boundary = band_geo[u]["y"] + band_geo[u]["h"] + 2
        yv = boundary - sum(half(x) for x in v)
        for x_ in v:
            name, lines, tag, w, h = L.dims[x_]
            L.rect[x_] = [L.cx[c] + (L.colw[c] - w) / 2, yv + (half(x_) - h / 2), w, h]
            yv += 2 * half(x_)
    route_edges(L)
    L.band_geo, L.band_texts = band_geo, band_texts
    return L


# ---------------------------------------------------------------- 図 3
def gen_reset(G, Lf):
    """リセット図: ブロック = リセット源 (reset 配線の駆動元と、その間の中継 = MMCM) + 帯ごとの受け側まとめブロック。
    配線 = リセット線 (源 → 帯の受け側、信号名ごと 1 本) と、源どうしのリセット線 / 解除ゲート信号 (locked / init_done)"""
    bands = ["board pin"] + G.domains
    bi = {d: i for i, d in enumerate(bands)}
    fq = clock_freqs()
    resets = [e for e in G.edges if e["kind"] == "reset"]
    S0 = {e["src"] for e in resets}
    gate = re.compile(r"locked|init_done")
    relay = {e["src"] for e in G.edges     # 中継: リセットを受けて、源へ解除ゲート信号 (locked / init_done) を返すブロック (MMCM)
             if e["src"] not in S0 and e["dst"] in S0 and e["kind"] not in ("clock", "reset")
             and any(gate.search(s) for s in e["signals"]) and any(e["src"] == r["dst"] for r in resets)}
    S = S0 | relay
    rcv = {}            # domain -> [module names]。帯 = リセット信号の発生ドメイン (駆動 always のクロック / IP の生成クロック)
    view_edges, seen = [], set()
    for e in resets:
        if e["dst"] in S:
            view_edges.append(e)
            continue
        db = G.blocks[e["dst"]]
        for sig in e["signals"]:
            d = e.get("signal_domains", {}).get(sig) or (db["clocks"][0] if db["clocks"] else None)
            if d not in bi:
                continue
            rcv.setdefault(d, set()).add(db["name"])
            key = (e["src"], d, sig)
            if key not in seen:
                seen.add(key)
                view_edges.append(dict(src=e["src"], dst=f"rcv:{d}", kind="reset", label=sig, signals=[sig], cdc=False))
    for e in G.edges:   # 源どうしの解除ゲート信号
        if e["src"] in S and e["dst"] in S and e["kind"] in ("stat", "ctrl") and any(gate.search(s) for s in e["signals"]):
            view_edges.append(dict(e, kind="ctrl", label=" / ".join(s for s in e["signals"] if gate.search(s))))
    nodes = [G.blocks[s] for s in sorted(S)]
    for d, mods in rcv.items():
        bid = f"rcv:{d}"
        G.blocks[bid] = dict(id=bid, name=f"{d} 配下 ×{len(mods)}", module=None, kind="note", parent=None, count=1, insts=[],
                             clocks=[d], clock_out=[], tags=[], label=" / ".join(sorted(mods)), cdc=False)
        nodes.append(G.blocks[bid])
    def primary(b):
        if b["kind"] == "ext" or not b["clocks"]:
            return "board pin", None
        return b["clocks"][0], None
    head_lines = {"board pin": ["非同期入力 (BTN0)"]}
    for d in G.domains:
        sigs = sorted({s for e in view_edges if e["kind"] == "reset" and e["dst"] == f"rcv:{d}" for s in e["signals"]})
        inputs = sorted({f"{s} ← {G.blocks[e['src']]['name']}" for e in view_edges if e["kind"] == "reset" and e["dst"] in S
                         and primary(G.blocks[e["dst"]])[0] == d for s in e["signals"]})   # 源ブロックが描かれる帯にだけ出す
        head_lines[d] = [fq.get(d, "")] + ([f"リセット: {' / '.join(sigs)}"] if sigs else []) + \
                        ([f"源への入力: {' / '.join(inputs)}"] if inputs else [])
    L = layout_bands(nodes, bands, bi, primary, head_lines, view_edges, lambda b: Lf.col.get(b["id"], 99))
    legend = [("reset", "リセット線 (信号名ごとに 1 本)"), ("ctrl", "解除ゲート信号 (locked / init_done)")]
    model = build_model(G, L, "Arty S7-50 HDMI Out — リセット図 (RTL から自動生成)",
                        "縦 = クロックドメイン帯。ブロック = リセット源と各帯の受け側 (配下モジュールを列挙)。同期 FF は top 本体。"
                        "解除順序は architecture/reset.md。生成: py tools/blockdiag/gen.py",
                        legend, bands=L.band_geo, band_texts=L.band_texts)
    return model, L, check_overlaps(L)


# ---------------------------------------------------------------- CLI
def generate(rtl_dir, out_dir, drawio=False, write=True):
    g = rtlgraph.Extractor(rtl_dir).run().build()
    outputs = {HERE / "graph.json": json.dumps(g, ensure_ascii=False, indent=1)}
    viol, stats = [], []
    def emit(name, model, L, v, title):
        xml = render_drawio(model, round(L.width), round(L.height), title)
        outputs[out_dir / f"{name}.drawio.svg"] = render_svg(model, round(L.width), round(L.height), embed_xml=xml)
        if drawio:
            outputs[out_dir / f"{name}.drawio"] = xml
        viol.extend((name, *x) for x in v)
        stats.append(f"{name}: {len(model['blocks'])} blocks / {len(model['edges'])} edges, {round(L.width)}×{round(L.height)} px")
    G1 = Graph(json.loads(outputs[HERE / "graph.json"]), 1)
    G1.set_root_ports(rtl_dir)
    m1, L1, v1 = gen_func(G1, 1)
    emit("rtl_func_diagram_d1", m1, L1, v1, "RTL 機能ブロック図 (概要)")
    G = Graph(g, 2)
    G.set_root_ports(rtl_dir)
    mf, Lf, vf = gen_func(G, 2)
    emit("rtl_func_diagram", mf, Lf, vf, "RTL 機能ブロック図")
    mc, Lc, vc = gen_clock(G, Lf)
    emit("rtl_clock_diagram", mc, Lc, vc, "クロック / リセット図")
    mr, Lr, vr = gen_reset(G, Lf)
    emit("rtl_reset_diagram", mr, Lr, vr, "リセット図")
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        for p, s in outputs.items():
            p.write_text(s, encoding="utf-8", newline="\n")
    return outputs, viol, stats


def _focus_port_names(rtl_dir, G, focus_ids):
    """対象ブロックの各インスタンスについて {親のネット名: 対象モジュールのポート名} を親 RTL の接続から引く"""
    net2port = {}
    for fid in focus_ids:
        b = G.blocks[fid]
        for ipath in b["insts"]:
            parent_mod = G.blocks[b["parent"]]["module"] if b["parent"] in G.blocks else G.raw["root"]
            p = Path(rtl_dir) / f"{parent_mod}.v"
            if not p.exists():
                continue
            iname = ipath.split("/")[-1]
            for inst in rtlgraph.parse(p)["instances"]:
                if inst["inst"] == iname:
                    for port, net in inst["conns"].items():
                        net2port.setdefault(re.sub(r"\[.*", "", net.strip()), port)
    return net2port


def gen_focus(G, Lf, module, rtl_dir):
    """spec 1.2 用の周辺接続図: 対象モジュールのブロックと直接の相手だけを 3 列に置く (左 = 全体図 Lf で対象より左の相手、
    中央 = 対象、右 = 右の相手。同列なら対象へ入る相手を左)。配線は対象に接する全種別 (clock を除く) で、ラベルは対象のポート名の列挙。
    ラッパ枠は描かず、親が違う相手は名前に親モジュール名を添える。行は全体図の行順"""
    focus = [b["id"] for b in G.nodes if b["module"] == module]
    if not focus:
        raise SystemExit(f"--focus: モジュール {module} のブロックが graph に無い")
    net2port = _focus_port_names(rtl_dir, G, focus)
    view_edges = []
    for e in G.edges:
        if e["kind"] == "clock" or (e["src"] in focus) == (e["dst"] in focus):
            continue
        names = [net2port.get(s, s) for s in e["signals"]]
        view_edges.append(dict(e, label=wrap_signals(names, suffix=" (CDC)" if e.get("cdc") else "")))
    fcol = min(Lf.col.get(f, 0) for f in focus)
    nbrs = sorted({e["src"] if e["dst"] in focus else e["dst"] for e in view_edges}, key=lambda n: (Lf.row.get(n, 0), n))
    # 左右: ready を除いた信号数で「対象へ入る > 出る」なら左 (刺激を与える側)、少なければ右。同数なら対象が ready を返す相手 (要求元) は左、
    # 相手が ready を返すなら右。それでも決まらなければ全体図の列順 (対象より左なら左)
    is_ready = lambda s: bool(re.search(r"ready", s))
    def side(n):
        n_in = sum(1 for e in view_edges if e["src"] == n for s in e["signals"] if not is_ready(s))
        n_out = sum(1 for e in view_edges if e["dst"] == n for s in e["signals"] if not is_ready(s))
        if n_in != n_out:
            return "L" if n_in > n_out else "R"
        if any(is_ready(s) for e in view_edges if e["dst"] == n for s in e["signals"]):
            return "L"
        if any(is_ready(s) for e in view_edges if e["src"] == n for s in e["signals"]):
            return "R"
        return "L" if Lf.col.get(n, 0) <= fcol else "R"
    left = [n for n in nbrs if side(n) == "L"]
    right = [n for n in nbrs if side(n) == "R"]
    L = Layout()
    L.ncol, L.clusters, L.members, L.cluster_rows, L.cluster = 3, [], {}, {}, {}
    for i, n in enumerate(left):
        L.col[n], L.row[n] = 0, i
    for i, n in enumerate(right):
        L.col[n], L.row[n] = 2, i
    fr = max(0, (max(len(left), len(right)) - len(focus)) // 2)
    for i, f in enumerate(sorted(focus)):
        L.col[f], L.row[f] = 1, fr + i
    L.nrow = max(L.row.values()) + 1
    L.pick_channel = lambda rs, rd: rs + 1 if rd >= rs else rs
    L.radj = True
    maxlines = max(1 + e["label"].count("\n") for e in view_edges)
    L.pitch = PITCH * ((LABEL_H * maxlines + 3 + PITCH - 1) // PITCH)     # 端子間隔をラベル高に合わせる (16 の倍数)
    fparent = G.blocks[focus[0]]["parent"]
    blocks = {}
    for n in L.col:
        b = dict(G.blocks[n])
        pm = G.blocks[b["parent"]]["module"] if b["parent"] in G.blocks else None
        if n not in focus and b["kind"] not in ("ext",) and b["parent"] != fparent and pm and pm != b["module"]:
            b["name"] = f"{b['name']} ({pm})"
        blocks[n] = b
    n_left, n_right = {n: 0 for n in L.col}, {n: 0 for n in L.col}
    for e in view_edges:
        n_right[e["src"]] += 1
        (n_right if L.col[e["dst"]] == L.col[e["src"]] else n_left)[e["dst"]] += 1
    L.dims = {n: node_dims(blocks[n], n_left[n], n_right[n], "focus", L.pitch) for n in L.col}
    L.colw = [0] * L.ncol
    for n in L.col:
        L.colw[L.col[n]] = max(L.colw[L.col[n]], L.dims[n][3])
    assign_tracks(L, view_edges)
    Gv = type("GV", (), {})()
    Gv.blocks = blocks
    place_func(Gv, L)
    route_edges(L)
    names = {"data": "映像 / データ IF", "axi": "AXI4 / DDR3", "ctrl": "制御", "stat": "ステータス", "reset": "リセット", "serial": "外部シリアル"}
    legend = [(k, names[k]) for k in names if any(e["kind"] == k for e in view_edges)]
    model = build_model(Gv, L, f"{module} 周辺接続図 (RTL から自動生成)",
                        "中央 = 対象。左 = 全体図で上流側の相手、右 = 下流側。配線ラベル = 対象モジュールのポート名。右上バッジ = クロックドメイン、"
                        f"二重枠 = CDC ブロック。親が違う相手は名前に親を添える。生成: py tools/blockdiag/gen.py --focus {module}",
                        legend)
    return model, L, check_overlaps(L)


def generate_focus(rtl_dir, out_dir, module, drawio=False, write=True):
    g = rtlgraph.Extractor(rtl_dir).run().build()
    G = Graph(g, 2, collapse_modules=[module])   # 対象がラッパなら 1 ブロックに畳む (内部は spec の中身であり相手ではない)
    G.set_root_ports(rtl_dir)
    _, Lf, _ = gen_func(G, 2)                    # 全体図の行順 / 列順 (左右の最終判定) に使う
    model, L, viol = gen_focus(G, Lf, module, rtl_dir)
    name = f"{module}_block"
    xml = render_drawio(model, round(L.width), round(L.height), f"{module} 周辺接続図")
    outputs = {out_dir / f"{name}.drawio.svg": render_svg(model, round(L.width), round(L.height), embed_xml=xml)}
    if drawio:
        outputs[out_dir / f"{name}.drawio"] = xml
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        for p, s in outputs.items():
            p.write_text(s, encoding="utf-8", newline="\n")
    stats = [f"{name}: {len(model['blocks'])} blocks / {len(model['edges'])} edges, {round(L.width)}×{round(L.height)} px"]
    return outputs, [(name, *x) for x in viol], stats


def to_png(svg_paths):
    """Microsoft Edge のヘッドレス描画で確認用 PNG を出す (Windows 標準。無ければ何もしない)"""
    edge = next((p for p in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", shutil.which("msedge")] if p and Path(p).exists()), None)
    if not edge:
        print("png: msedge が見つからないので省略")
        return
    for p in svg_paths:
        m = re.search(r'width="(\d+)" height="(\d+)"', p.read_text(encoding="utf-8"))
        w, h = int(m.group(1)), int(m.group(2))
        scale = min(1.0, 4000 / w)
        subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},{h}",
                        f"--force-device-scale-factor={scale}", f"--screenshot={p.with_suffix('').with_suffix('.png')}", p.as_uri()],
                       capture_output=True, timeout=120)
        print(f"png: {p.with_suffix('').with_suffix('.png')}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rtl-dir", default=str(REPO / "rtl"))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--drawio", action="store_true")
    ap.add_argument("--png", action="store_true")
    ap.add_argument("--check", action="store_true", help="生成物がコミット済と一致し重なりが 0 なら exit 0")
    ap.add_argument("--focus", metavar="MODULE", help="spec 1.2 用の周辺接続図 <MODULE>_block.drawio.svg だけを --out に出す (既定 doc/spec/img)")
    a = ap.parse_args(argv)
    if a.focus:
        out = Path(a.out).resolve() if a.out != str(OUT_DIR) else REPO / "doc" / "spec" / "img"
        outputs, viol, stats = generate_focus(a.rtl_dir, out, a.focus, a.drawio, write=not a.check)
    else:
        outputs, viol, stats = generate(a.rtl_dir, Path(a.out).resolve(), a.drawio, write=not a.check)
    for v in viol:
        print(f"OVERLAP [{v[0]}] {v[1]}: {v[2]}")
    if a.check:
        stale = [p for p, s in outputs.items() if not p.exists() or p.read_text(encoding="utf-8") != s]
        for p in stale:
            print(f"STALE: {p.relative_to(REPO) if p.is_relative_to(REPO) else p} (再生成が必要: py tools/blockdiag/gen.py)")
        print(f"check: overlaps {len(viol)}, stale {len(stale)} -> " + ("OK" if not viol and not stale else "NG"))
        return 0 if not viol and not stale else 1
    for p in outputs:
        print(f"written {p.relative_to(REPO) if p.is_relative_to(REPO) else p}")
    for s in stats:
        print(s)
    if a.png:
        to_png([p for p in outputs if p.suffix == ".svg"])
    print(f"overlaps {len(viol)}")
    return 0 if not viol else 1


if __name__ == "__main__":
    sys.exit(main())
