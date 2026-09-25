# -*- coding: utf-8 -*-
"""Minimal block-diagram model -> SVG (own renderer) + draw.io XML (mxGraph).
Blocks/edges use explicit coordinates so the layout is deterministic and diff-able."""
from xml.sax.saxutils import escape, quoteattr

FONT_JP = "'Segoe UI','Yu Gothic UI','Meiryo','Noto Sans JP',sans-serif"
FONT_MONO = "Consolas,'Courier New',monospace"


class Block:
    # kind: rtl | ip | ext | note | body (blocks) ; containers/bands are separate lists
    # badges: [(text, fill)] を右上に小さく描く (クロックドメイン表示)。cdc=True で二重枠
    def __init__(self, id, name, func, x, y, w=150, h=56, kind="rtl", tag=None, note=None, badges=None, cdc=False):
        self.id, self.name, self.func = id, name, func
        self.x, self.y, self.w, self.h = x, y, w, h
        self.kind, self.tag, self.note = kind, tag, note
        self.badges, self.cdc = badges or [], cdc

    def anchor(self, spec):
        side, _, frac = spec.partition(":")
        f = float(frac) if frac else 0.5
        if side == "r": return (self.x + self.w, self.y + self.h * f)
        if side == "l": return (self.x, self.y + self.h * f)
        if side == "t": return (self.x + self.w * f, self.y)
        if side == "b": return (self.x + self.w * f, self.y + self.h)
        raise ValueError(spec)


class Edge:
    # kind: data | axi | ctrl | stat | serial ; pts = intermediate waypoints (corners auto-inserted)
    def __init__(self, src, sa, dst, da, label=None, kind="data", pts=None,
                 lab_seg=None, lab_dx=0, lab_dy=0, bidir=False, mid=None):
        self.src, self.sa, self.dst, self.da = src, sa, dst, da
        self.label, self.kind, self.pts = label, kind, pts or []
        self.lab_seg, self.lab_dx, self.lab_dy = lab_seg, lab_dx, lab_dy
        self.bidir, self.mid = bidir, mid   # mid: x (or y) of the middle segment of an auto Z-route


class Path:
    # free polyline (bus bar, legend sample) with the same edge styling
    def __init__(self, pts, kind="ctrl", label=None, lab_seg=None, lab_dx=0, lab_dy=0, arrow=True):
        self.pts, self.kind, self.label = pts, kind, label
        self.lab_seg, self.lab_dx, self.lab_dy, self.arrow = lab_seg, lab_dx, lab_dy, arrow


# ---------------------------------------------------------------- routing
def _side(a): return a.split(":")[0]


def route(index, e):
    S, T = index[e.src], index[e.dst]
    p0, p1 = S.anchor(e.sa), T.anchor(e.da)
    sh, dh = _side(e.sa) in "lr", _side(e.da) in "lr"
    if not e.pts:  # automatic Z-route when both anchors face the same axis
        if sh and dh and abs(p0[1] - p1[1]) > 0.5 and abs(p0[0] - p1[0]) > 0.5:
            mx = e.mid if e.mid is not None else (p0[0] + p1[0]) / 2
            return [p0, (mx, p0[1]), (mx, p1[1]), p1]
        if (not sh) and (not dh) and abs(p0[0] - p1[0]) > 0.5 and abs(p0[1] - p1[1]) > 0.5:
            my = e.mid if e.mid is not None else (p0[1] + p1[1]) / 2
            return [p0, (p0[0], my), (p1[0], my), p1]
    pts = [p0]
    horiz = sh  # direction of the segment leaving the source
    for q in e.pts + [p1]:
        cur = pts[-1]
        dx, dy = abs(cur[0] - q[0]) > 0.5, abs(cur[1] - q[1]) > 0.5
        if dx and dy:  # insert one corner, keeping the current direction first
            pts.append((q[0], cur[1]) if horiz else (cur[0], q[1]))
            horiz = not horiz
        elif dx: horiz = True
        elif dy: horiz = False
        pts.append(q)
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) > 0.5 or abs(p[1] - out[-1][1]) > 0.5: out.append(p)
    return out


# ---------------------------------------------------------------- styles
EDGE_STYLE = {  # (color, width, svg dash, drawio dash)
    "data":   ("#1565C0", 2.4, None, None),
    "axi":    ("#0D47A1", 3.2, None, None),
    "ctrl":   ("#6A1B9A", 1.5, "6,3", "6 3"),
    "stat":   ("#8E24AA", 1.1, "2,3", "2 3"),
    "serial": ("#546E7A", 1.6, "2,3", "2 3"),
    "clock":  ("#E65100", 1.6, "8,3", "8 3"),
    "reset":  ("#B71C1C", 1.3, "4,2", "4 2"),
}
KIND_STYLE = {  # (fill, stroke, svg dash, drawio dash)
    "rtl":  ("#FFFFFF", "#37474F", None, None),
    "ip":   ("#F5F5F5", "#78909C", "4,2", "4 2"),
    "ext":  ("#ECEFF1", "#90A4AE", "2,2", "2 2"),
    "note": ("#FFFFFF", "#B0BEC5", "1,2", "1 2"),
    "body": ("#FFFFFF", "#37474F", "3,3", "3 3"),   # ラッパ本体ロジック (インスタンスでない機能の塊)
}


def _esc(s): return escape(s, {'"': "&quot;"})


def _txt_w(s): return sum(6.0 if ord(ch) < 256 else 9.6 for ch in s)


# ---------------------------------------------------------------- SVG
def svg_text(x, y, s, size=11, weight="normal", fill="#212121", anchor="middle", family=FONT_JP):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}" font-family="{family}">{_esc(s)}</text>')


def _svg_poly(o, pts, kind, label, lab_seg, lab_dx, lab_dy, arrow=True, bidir=False):
    c, w, dash, _ = EDGE_STYLE[kind]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    dd = f' stroke-dasharray="{dash}"' if dash else ""
    me = f' marker-end="url(#ah_{kind})"' if arrow else ""
    ms = f' marker-start="url(#ah_{kind})"' if bidir else ""
    o.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{w}"{dd}{me}{ms}/>')
    if label:
        segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        i = lab_seg if lab_seg is not None else max(
            range(len(segs)), key=lambda k: abs(segs[k][1][0] - segs[k][0][0]) + abs(segs[k][1][1] - segs[k][0][1]))
        (ax, ay), (bx, by) = segs[i]
        mx, my = (ax + bx) / 2 + lab_dx, (ay + by) / 2 + lab_dy
        lines = label.split("\n")                       # 複数行ラベル (焦点図の信号名列挙)。1 行なら従来どおり
        tw, th = max(_txt_w(l) for l in lines) + 6, 13 * len(lines)
        if abs(ax - bx) < 0.5:  # vertical segment: label to the right
            rx, ry, tx, anchor = mx + 4, my - 7 - (th - 13) / 2, mx + 7, "start"
        else:
            rx, ry, tx, anchor = mx - tw / 2, my - 1 - th, mx, "middle"
        o.append(f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{tw:.1f}" height="{th}" rx="2" fill="#FFFFFF" fill-opacity="0.9"/>')
        for k, ln in enumerate(lines):
            o.append(svg_text(tx, ry + 10 + 13 * k, ln, 9, "normal", c, anchor))


def render_svg(model, width, height, embed_xml=None):
    o = []
    content = f" content={quoteattr(embed_xml)}" if embed_xml else ""
    o.append(f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             f'width="{width}" height="{height}" viewBox="0 0 {width} {height}"{content}>')
    o.append("<defs>" + "".join(
        f'<marker id="ah_{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="{7 if w > 2 else 6}" '
        f'markerHeight="{7 if w > 2 else 6}" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
        for k, (c, w, d, _) in EDGE_STYLE.items()) + "</defs>")
    o.append(f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>')
    for b in model["bands"]:
        o.append(f'<rect x="{b.x}" y="{b.y}" width="{b.w}" height="{b.h}" rx="6" fill="{b.note}" stroke="none"/>')
        o.append(svg_text(b.x + 8, b.y + 17, b.name, 12, "bold", "#37474F", "start", FONT_MONO))
        if b.func: o.append(svg_text(b.x + 8, b.y + 31, b.func, 9.5, "normal", "#546E7A", "start"))
    for l in model["lines"]:
        o.append(f'<line x1="{l[0]}" y1="{l[1]}" x2="{l[2]}" y2="{l[3]}" stroke="{l[4]}" stroke-width="1" stroke-dasharray="{l[5]}"/>')
    for t in model["texts"]:
        o.append(svg_text(t["x"], t["y"], t["s"], t.get("size", 12), t.get("weight", "normal"),
                          t.get("fill", "#212121"), t.get("anchor", "middle"), t.get("family", FONT_JP)))
    for c in model["containers"]:
        o.append(f'<rect x="{c.x}" y="{c.y}" width="{c.w}" height="{c.h}" rx="8" fill="none" '
                 f'stroke="#455A64" stroke-width="1.3" stroke-dasharray="7,4"/>')
        o.append(svg_text(c.x + 10, c.y + 16, c.name, 11.5, "bold", "#37474F", "start", FONT_MONO))
        if c.func: o.append(svg_text(c.x + 10, c.y + 29, c.func, 9.5, "normal", "#546E7A", "start"))
    for b in model["blocks"]:
        fill, stroke, dash, _ = KIND_STYLE[b.kind]
        dd = f' stroke-dasharray="{dash}"' if dash else ""
        o.append(f'<rect x="{b.x}" y="{b.y}" width="{b.w}" height="{b.h}" rx="5" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{dd}/>')
        if b.cdc:
            o.append(f'<rect x="{b.x + 3}" y="{b.y + 3}" width="{b.w - 6}" height="{b.h - 6}" rx="3" fill="none" stroke="{stroke}" stroke-width="1"/>')
        bx = b.x + b.w - 4
        for text, bfill in reversed(b.badges):
            bw = _txt_w(text) * 0.8 + 8
            bx -= bw
            o.append(f'<rect x="{bx:.1f}" y="{b.y + 4}" width="{bw:.1f}" height="11" rx="3" fill="{bfill}" stroke="none"/>')
            o.append(svg_text(bx + bw / 2, b.y + 12.5, text, 8, "normal", "#37474F", "middle", FONT_MONO))
            bx -= 3
        cx = b.x + b.w / 2
        funcs = [s for s in (b.func or "").split("\n") if s]
        n = 1 + len(funcs) + (1 if b.tag else 0)
        y = b.y + b.h / 2 - (n - 1) * 6.5 + 4 + (5 if b.badges else 0)
        o.append(svg_text(cx, y, b.name, 11.5, "bold", "#212121", "middle", FONT_MONO))
        for ln in funcs:
            y += 13; o.append(svg_text(cx, y, ln, 9.5, "normal", "#37474F"))
        if b.tag:
            y += 13; o.append(svg_text(cx, y, b.tag, 8.5, "normal", "#6A1B9A", "middle", FONT_MONO))
    for e in model["edges"]:
        _svg_poly(o, route(model["index"], e), e.kind, e.label, e.lab_seg, e.lab_dx, e.lab_dy, True, e.bidir)
    for p in model["paths"]:
        _svg_poly(o, p.pts, p.kind, p.label, p.lab_seg, p.lab_dx, p.lab_dy, p.arrow)
    o.append("</svg>")
    return "\n".join(o)


# ---------------------------------------------------------------- draw.io (mxGraph XML)
def _cell(id, value, style, x, y, w, h):
    return (f'<mxCell id="{id}" value={quoteattr(value)} style={quoteattr(style)} vertex="1" parent="1">'
            f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/></mxCell>')


def _html_block(b):
    v = ""
    if b.badges:
        v += "".join(f'<span style="font-size:8px;font-family:{FONT_MONO};background:{f};border-radius:3px;padding:0 3px;margin:0 1px">{escape(t)}</span>'
                     for t, f in b.badges) + "<br>"
    v += f'<b style="font-family:{FONT_MONO}">{escape(b.name)}</b>'
    for ln in [s for s in (b.func or "").split("\n") if s]:
        v += f'<br><span style="font-size:9.5px">{escape(ln)}</span>'
    if b.tag: v += f'<br><span style="font-size:8.5px;color:#6A1B9A;font-family:{FONT_MONO}">{escape(b.tag)}</span>'
    return v


def _edge_cell(id, pts, kind, label, src=None, dst=None, sa=None, da=None, bidir=False, arrow=True):
    c, w, _, ddash = EDGE_STYLE[kind]
    st = (f"edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;"
          f"strokeColor={c};strokeWidth={w:.1f};fontSize=9;fontColor={c};labelBackgroundColor=#FFFFFF;"
          f"endArrow={'block' if arrow else 'none'};endFill=1;")
    if bidir: st += "startArrow=block;startFill=1;"
    if ddash: st += f"dashed=1;dashPattern={ddash};"
    def frac(a, axis):
        side, _, f = a.partition(":"); f = float(f) if f else 0.5
        return {"l": (0, f), "r": (1, f), "t": (f, 0), "b": (f, 1)}[side]
    if sa: ex, ey = frac(sa, 0); st += f"exitX={ex:.3f};exitY={ey:.3f};exitDx=0;exitDy=0;"
    if da: ex, ey = frac(da, 0); st += f"entryX={ex:.3f};entryY={ey:.3f};entryDx=0;entryDy=0;"
    st_ = f' source="{src}"' if src else ""
    tg_ = f' target="{dst}"' if dst else ""
    geo = '<mxGeometry relative="1" as="geometry">'
    if not src: geo += f'<mxPoint x="{pts[0][0]:.0f}" y="{pts[0][1]:.0f}" as="sourcePoint"/>'
    if not dst: geo += f'<mxPoint x="{pts[-1][0]:.0f}" y="{pts[-1][1]:.0f}" as="targetPoint"/>'
    inner = pts[1:-1]
    if inner: geo += '<Array as="points">' + "".join(f'<mxPoint x="{x:.0f}" y="{y:.0f}"/>' for x, y in inner) + "</Array>"
    geo += "</mxGeometry>"
    return (f'<mxCell id="{id}" value={quoteattr((label or "").replace(chr(10), "<br>"))} style={quoteattr(st)} '
            f'edge="1" parent="1"{st_}{tg_}>{geo}</mxCell>')


def render_drawio(model, width, height, title="Page-1"):
    cells, n = [], [0]
    def nid(p): n[0] += 1; return f"{p}{n[0]}"
    for b in model["bands"]:
        v = f'<b style="font-family:{FONT_MONO};font-size:12px">{escape(b.name)}</b>'
        if b.func: v += f'<br><span style="font-size:9.5px;color:#546E7A">{escape(b.func)}</span>'
        cells.append(_cell(b.id, v, f"rounded=1;whiteSpace=wrap;html=1;fillColor={b.note};strokeColor=none;"
                           "align=left;verticalAlign=top;spacingLeft=6;spacingTop=2;", b.x, b.y, b.w, b.h))
    for l in model["lines"]:
        cells.append(_edge_cell(nid("ln"), [(l[0], l[1]), (l[2], l[3])], "stat", None, arrow=False)
                     .replace("strokeColor=#8E24AA", f"strokeColor={l[4]}").replace("dashPattern=2 3", "dashPattern=1 3"))
    for t in model["texts"]:
        anchor = t.get("anchor", "middle"); tw = _txt_w(t["s"]) * (t.get("size", 12) / 11) + 20
        x = t["x"] - (tw / 2 if anchor == "middle" else 0)
        fs = "1" if t.get("weight") == "bold" else "0"
        cells.append(_cell(nid("tx"), t["s"], f"text;html=1;align={'center' if anchor == 'middle' else 'left'};verticalAlign=middle;"
                           f"fontSize={t.get('size', 12)};fontStyle={fs};fontColor={t.get('fill', '#212121')};"
                           f"fontFamily={t.get('family', FONT_JP).split(',')[0].strip(chr(39))};", x, t["y"] - 12, tw, 20))
    for c in model["containers"]:
        v = f'<b style="font-family:{FONT_MONO}">{escape(c.name)}</b>'
        if c.func: v += f'<br><span style="font-size:9.5px;color:#546E7A">{escape(c.func)}</span>'
        cells.append(_cell(c.id, v, "rounded=1;whiteSpace=wrap;html=1;dashed=1;dashPattern=7 4;fillColor=none;"
                           "strokeColor=#455A64;strokeWidth=1.3;align=left;verticalAlign=top;spacingLeft=8;fontSize=11.5;", c.x, c.y, c.w, c.h))
    for b in model["blocks"]:
        fill, stroke, _, ddash = KIND_STYLE[b.kind]
        st = f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth=1.4;fontSize=11;"
        if ddash: st += f"dashed=1;dashPattern={ddash};"
        if b.cdc: st += "shape=ext;double=1;"
        cells.append(_cell(b.id, _html_block(b), st, b.x, b.y, b.w, b.h))
    for e in model["edges"]:
        cells.append(_edge_cell(nid("e"), route(model["index"], e), e.kind, e.label, e.src, e.dst, e.sa, e.da, e.bidir))
    for p in model["paths"]:
        cells.append(_edge_cell(nid("p"), p.pts, p.kind, p.label, arrow=p.arrow))
    body = "".join(cells)
    return (f'<mxfile host="Electron" type="device" version="24.7.17">'
            f'<diagram id="rtl-func-block" name={quoteattr(title)}>'
            f'<mxGraphModel dx="1400" dy="900" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" '
            f'fold="1" page="1" pageScale="1" pageWidth="{width}" pageHeight="{height}" math="0" shadow="0">'
            f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root></mxGraphModel></diagram></mxfile>')
