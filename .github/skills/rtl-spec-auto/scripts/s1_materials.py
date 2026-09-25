# -*- coding: utf-8 -*-
"""S1: 機能仕様書 (テンプレート v7) を AI に書かせるための材料を RTL だけから機械生成する (AI は使わない)。

    py .github/skills/rtl-spec-auto/scripts/s1_materials.py <module> --rtl-dir rtl --out work/<module> [--trim] [--all-diagrams doc/architecture/img]

<out>/ に作るもの:
  TASK.md                 執筆者 (AI) への指示。読むファイル、書くファイル、守ること。copilot -p にそのまま渡す
  <module>.v (+ 内部モジュール)   対象 RTL の写し
  <parent>.v、<neighbor>.v       親と隣接の RTL の写し (--trim で、対象と共有するネット / ポートに触れる行だけに絞る = 読む量を減らす)
  s1_io.md                2 章の信号表の骨格 (相手ごとの節、Signal / Dir / Bits は確定、種別 / 意味は記入欄) と 1.2 の表の骨格、パラメータ表
  s1_graph.md             接続表 (種別 / 相手 / 向き / 対象のポート名 / CDC)、クロックドメイン
  <module>_block.drawio.svg     周辺接続図 (tools/blockdiag/gen.py --focus と同じ)
  graph.json              接続グラフ (check_func_spec.py の隣接判定に使う)
  template.md / example_*.md    テンプレート v7 と見本 (スキル同梱)
--all-diagrams <dir> を付けると全体図 4 枚 (機能図 概要 / 詳細、クロック図、リセット図) も <dir> に出す (最初に 1 回)。
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
REPO = next((p for p in HERE.parents if (p / "rtl").is_dir() or (p / ".git").exists()), HERE.parents[3])
sys.path.insert(0, str(HERE))
from rtlparse import parse, parents  # noqa: E402
from source_inventory import inventory

BD = next((c for c in (HERE / "blockdiag", REPO / "tools" / "blockdiag") if (c / "gen.py").exists()), None)
if BD:
    sys.path.insert(0, str(BD))
    import gen as bdgen  # noqa: E402
    import rtlgraph  # noqa: E402

CLK_RST = re.compile(r"^(clk|rst|reset|aresetn|resetn)|_(clk|rst|clock|reset|resetn)$", re.I)


def rtl_file(rtl_dir, module):
    for ext in (".v", ".sv"):
        p = rtl_dir / f"{module}{ext}"
        if p.exists():
            return p
    return None


def children(rtl_dir, module, seen=None):
    """対象の内部モジュール (RTL があるものだけ、再帰)"""
    seen = seen if seen is not None else set()
    p = rtl_file(rtl_dir, module)
    if not p:
        return []
    out = []
    for inst in parse(p)["instances"]:
        m = inst["module"]
        if m in seen or not rtl_file(rtl_dir, m):
            continue
        seen.add(m)
        out.append(m)
        out += children(rtl_dir, m, seen)
    return out


def trim_rtl(text, names, ctx=2):
    """module 宣言〜ポート宣言の終わりと、names のどれかを含む行 (前後 ctx 行) だけを残す。省いた区間は // ... で示す"""
    lines = text.splitlines()
    keep = [False] * len(lines)
    head_end = None
    for i, l in enumerate(lines):
        if re.match(r"\s*module\b", l):
            head_end = i
            depth = 0
            for j in range(i, len(lines)):
                depth += lines[j].count("(") - lines[j].count(")")
                if ")" in lines[j] and depth <= 0 and ";" in lines[j]:
                    head_end = j
                    break
            break
    if head_end is not None:
        for k in range(0, head_end + 1):
            keep[k] = True
        for k in range(0, max(0, i - 12)):          # module 宣言より前のヘッダコメントは先頭 12 行だけ残す
            keep[k] = False
    pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b") if names else None
    for i, l in enumerate(lines):
        if pat and pat.search(l) or re.match(r"\s*endmodule", l):
            for k in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                keep[k] = True
    out, gap = [], False
    for i, l in enumerate(lines):
        if keep[i]:
            out.append(l)
            gap = False
        elif not gap:
            out.append("    // ... (s1_materials --trim: 対象と関係しない行を省略)")
            gap = True
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("module")
    ap.add_argument("--rtl-dir", default="rtl")
    ap.add_argument("--out", default=None, help="材料の出力先 (既定 work/<module>)")
    ap.add_argument("--trim", action="store_true", help="親 / 隣接 RTL を対象に関係する行だけに絞る (トークン削減)")
    ap.add_argument("--all-diagrams", metavar="DIR", default=None, help="全体図 4 枚も DIR に生成する")
    ap.add_argument("--no-diagram", action="store_true")
    ap.add_argument("--encoding", default="utf-8-sig", help="source encoding (e.g. cp932); decoding failures stop")
    ap.add_argument("--file-list", help="one source path per line, relative to list file; selects a build source set")
    a = ap.parse_args(argv)
    rtl_dir = Path(a.rtl_dir).resolve()
    m = a.module
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", m):
        ap.error("module must be an HDL identifier")
    out = Path(a.out or f"work/{m}").resolve()
    if (out / "source-manifest.json").exists():
        ap.error("出力先は既に材料を含む。別の --out を使う (旧成果物を混ぜない)")
    out.mkdir(parents=True, exist_ok=True)
    manifest = inventory(rtl_dir, out, a.encoding, a.file_list)
    for d in manifest["diagnostics"]:
        print(f"{d['severity'].upper()} {d['code']}: {d['source']}: {d['detail']}")
    if manifest["status"] == "blocked":
        print("入力の解決が必要: source-manifest.json。自動執筆は開始しない。", file=sys.stderr)
        return 2
    rtl_dir = out / "_rtl"
    tgt = rtl_file(rtl_dir, m)
    if not tgt:
        print(f"module が無い: {m}", file=sys.stderr)
        return 2
    info = parse(tgt)
    ports = info["ports"]
    kids = children(rtl_dir, m)
    pars = parents(rtl_dir, m)
    par_names = sorted({p["parent"] for p in pars})

    # ---- 親の接続: ネット → 対象のポート名
    net2port, port2net = {}, {}
    for p in pars:
        gps = parents(rtl_dir, p["parent"])                       # 親のポートを通って上位のネットに繋がる場合 (2 段まで) も対象のポート名へ引く
        for port, net in p["conns"].items():
            n = re.sub(r"\[.*", "", net.strip())
            net2port.setdefault(n, port)
            port2net.setdefault(port, n)
            for gp in gps:
                if n in gp["conns"]:
                    o = re.sub(r"\[.*", "", gp["conns"][n].strip())
                    net2port.setdefault(o, port)
                    for ggp in parents(rtl_dir, gp["parent"]):
                        if o in ggp["conns"]:
                            net2port.setdefault(re.sub(r"\[.*", "", ggp["conns"][o].strip()), port)
    unconnected = sorted({u for p in pars for u in p["unconnected"]})

    # ---- 接続グラフ (rtlgraph): 相手 / 向き / CDC
    g, nbr_mod, graph_rows, doms = None, {}, [], []
    if BD:
        try:
            g = rtlgraph.Extractor(str(rtl_dir)).run().build()
            B = {b["id"]: b for b in g["blocks"]}
            def under(bid):
                b = B[bid]
                while b is not None:
                    if b["module"] == m:
                        return True
                    b = B.get(b["parent"]) if b["parent"] in B else None
                return False
            ids = {i for i in B if under(i)}
            if not ids:
                raise ValueError("selected hierarchy does not contain target; use parent connection table")
            doms = sorted({c for i in ids for c in B[i]["clocks"]})
            for e in g["edges"]:
                if (e["src"] in ids) == (e["dst"] in ids):
                    continue
                o = e["dst"] if e["src"] in ids else e["src"]
                om = B[o]["module"] if B[o]["kind"] != "ext" else B[o]["name"]
                names = [net2port.get(s, s) for s in e["signals"]]
                graph_rows.append((e["kind"], om, "出力" if e["src"] in ids else "入力", names, e["signals"], bool(e.get("cdc"))))
                for n in names:
                    nbr_mod.setdefault(n, om)
        except Exception as ex:  # グラフが作れなくても材料は出す
            print(f"rtlgraph: 接続グラフを作れなかった ({ex})。s1_graph.md は親の接続だけで書く", file=sys.stderr)
            g = None
    for p in pars:
        for om, d in p["neighbors"].items():
            for port in d["drives"] + d["receives"] + d["shares"]:
                nbr_mod.setdefault(port, om)
    if not graph_rows:
        for p in pars:
            for port, net in p["conns"].items():
                direction = next((q["dir"] for q in ports if q["name"] == port), "unknown")
                graph_rows.append(("connection", p["parent"] + "." + p["inst"],
                                   "出力" if direction == "out" else "入力", [port], [net], False))
    neighbor_names = set(nbr_mod.values()) | {om for p in pars for om in p["neighbors"]}
    neighbors = sorted({om for om in neighbor_names if rtl_file(rtl_dir, om) and om != m and om not in kids})

    # ---- RTL の写し
    copied = []
    for mod in [m] + kids:
        shutil.copy2(rtl_file(rtl_dir, mod), out / rtl_file(rtl_dir, mod).name)
        copied.append(rtl_file(rtl_dir, mod).name)
    shared = {n for n in set(port2net.values()) | set(port2net) | set(net2port) if n and len(n) >= 3 and not CLK_RST.search(n)}   # clk / rst / 空 (未接続) は外す
    for mod in par_names + [n for n in neighbors if n not in par_names]:
        src = rtl_file(rtl_dir, mod)
        if not src or mod in kids:
            continue
        text = src.read_text(encoding="utf-8", errors="replace")
        if a.trim:
            names = set(shared)
            if mod in par_names:
                names |= {p["inst"] for p in pars} | set(neighbors)
            else:
                for p in parents(rtl_dir, mod):
                    for port, net in p["conns"].items():
                        if re.sub(r"\[.*", "", net.strip()) in shared:
                            names.add(port)
            text = f"// {src.name} (s1_materials --trim: {m} と共有するネット / ポートに触れる行だけ)\n" + trim_rtl(text, names)
        (out / src.name).write_text(text, encoding="utf-8", newline="\n")
        copied.append(src.name + (" (trim)" if a.trim else ""))

    # ---- s1_io.md
    groups = {}
    for p in ports:
        if CLK_RST.search(p["name"]):
            key = "クロック / リセット"
        elif p["name"] in unconnected:
            key = "親では未接続"
        elif p["name"] in nbr_mod:
            key = f"`{nbr_mod[p['name']]}`"
        else:
            key = "相手不明 (親の外、または親で未使用)"
        groups.setdefault(key, []).append(p)
    L = [f"# {m} — 2 章 信号表の骨格 (S1 機械生成。Signal / Dir / Bits は RTL どおり。種別 / 意味を書き、節の見出しを役割名にする)\n",
         "種別: レベル / パルス / 値 / ハンドシェイク。クロックを跨ぐ入力は「(非同期)」、内部で他方へ同期するものは「(CDC)」。\n"]
    for key, ps in groups.items():
        L.append(f"### 2.x {key}\n")
        L.append("| Signal | Dir | Bits | 種別 | 意味 |\n|---|---|---|---|---|")
        for p in ps:
            note = f" ({p['comment']})" if p.get("comment") else ""
            L.append(f"| {p['name']} | {p['dir']} | {p['width']} | (記入) | (記入){note} |")
        L.append("")
    if info["params"]:
        L.append("### 2.n パラメータ\n\n| 名前 | 既定 | 意味 |\n|---|---|---|")
        for pr in info["params"]:
            L.append(f"| {pr['name']} | {pr.get('default', '')} | (記入) |")
        L.append("")
    L.append("## 1.2 周辺接続の表の骨格\n\n| 相手 | 信号 | 向き | 役割 |\n|---|---|---|---|")
    for kind, om, d, names, sigs, cdc in graph_rows:
        L.append(f"| `{om}` | {', '.join(names)} | {'入力' if d == '入力' else '出力'} | (記入){' CDC' if cdc else ''} |")
    (out / "s1_io.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")

    # ---- s1_graph.md
    G = [f"# {m} — 接続表 (S1 機械生成、RTL から)\n",
         f"- 対象の RTL: {', '.join(copied[:1 + len(kids)])}。親: {', '.join(par_names) or '(なし = 最上位)'}。"
         f"隣接 (RTL あり): {', '.join(neighbors) or '(なし)'}。クロックドメイン: {', '.join(doms) or '(不明)'}\n",
         "CDC 列と図の印は名前・接続に基づく推定。外部入力の供給クロックや位相はここでは確定しない。原本で確認する。\n",
         "| 種別 | 相手 | 向き | 対象のポート | 親のネット | CDC (推定) |", "|---|---|---|---|---|---|"]
    for kind, om, d, names, sigs, cdc in graph_rows:
        G.append(f"| {kind} | `{om}` | {d} | {', '.join(names)} | {', '.join(sigs)} | {'要確認' if cdc else ''} |")
    if unconnected:
        G.append(f"\n親では未接続の出力: {', '.join(unconnected)}")
    (out / "s1_graph.md").write_text("\n".join(G) + "\n", encoding="utf-8", newline="\n")
    if g:
        (out / "graph.json").write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")

    # ---- 図
    fig = None
    if BD and not a.no_diagram:
        try:
            bdgen.generate_focus(str(rtl_dir), out, m)
            fig = f"{m}_block.drawio.svg"
        except (Exception, SystemExit) as ex:
            print(f"周辺接続図を作れなかった ({ex})", file=sys.stderr)
        if a.all_diagrams:
            outputs, viol, stats = bdgen.generate(str(rtl_dir), Path(a.all_diagrams).resolve())
            for s in stats:
                print("  " + s)
            if viol:
                print(f"  全体図の重なり {len(viol)} 件 (見た目の問題のみ)")

    # ---- テンプレートと見本
    for f in [SKILL / "template.md"] + sorted((SKILL / "examples").glob("*")):
        if f.is_file():
            shutil.copy2(f, out / f.name)

    # ---- TASK.md
    examples = sorted(p.name for p in (SKILL / "examples").glob("example_*.md"))
    figure_instruction = (f"1.2 の図は `![{m} 周辺接続]({fig})` と書く。" if fig else
                          "周辺接続図は未生成。1.2 は接続表と未生成理由を書き、存在しない図を参照しない。")
    manifest["target"] = m
    manifest["diagram"] = fig
    manifest["trim"] = a.trim
    manifest["parents"] = par_names
    if not fig:
        manifest["diagnostics"].append(dict(code="NO_DIAGRAM", severity="warning", source=m,
                                            detail="周辺接続図未生成。接続表と原文で境界を確認する"))
    if len(par_names) > 1:
        manifest["diagnostics"].append(dict(code="MULTIPLE_PARENTS", severity="warning", source=m,
                                            detail="複数の使用箇所。相手・設定を一つに決めつけない"))
    (out / "source-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    T = f"""# {m} の機能仕様書を書く (テンプレート v7、入力は RTL だけ)

あなたは FPGA 設計チームの技術ライターです。このフォルダにあるファイルだけを読み、モジュール {m} の機能仕様書を日本語で書いてください。
このフォルダの外 (他の RTL、既存の文書、Web) は読まないでください。

## 読むもの (このフォルダ)
- 対象 RTL: {', '.join(copied[:1 + len(kids)])}{' (内部モジュールを含む。仕様書は対象を 1 つのブラックボックスとして扱い、内部モジュールは相手にしない)' if kids else ''}
- 親 / 隣接 RTL: {', '.join(copied[1 + len(kids):]) or '(なし)'} — 相手がどう駆動するかを確認する用{'。(trim) は対象に関係する行だけの抜粋' if a.trim else ''}
- s1_io.md (2 章の信号表の骨格。Signal / Dir / Bits は変えない)、s1_graph.md (接続表)、{fig or '(周辺接続図なし)'} (1.2 に貼る図)
- template.md (テンプレート v7。**これに従う**)、{', '.join(examples) or '(見本なし)'} (書き味・密度・章立ての見本)
- source-manifest.json: 原本との対応、入力の診断。_original/ は原本の全文、_rtl/ はモジュール別の解析用コピー。
  抜粋で条件・保持期間・パラメータ設定が分からなければ必ず原本へ戻る。診断や未読箇所を「未規定」で隠さない。

## 書くもの (このフォルダに)
- {m}.md — 仕様書本体。{figure_instruction}
- {m}_exp_R-nn.json — 期待波形 (WaveDrom JSON、規則 ID ごと、枚数は必要なシナリオに応じる)。md からは `![R-nn]({m}_exp_R-nn.svg)` で参照する (SVG 化は別工程)
  2 クロックの図は clk 行を 2 本、同じ周期で描く (`period` は使わない)。head は規則 ID のみ。矢印は必要な遅延を説明する場合だけ1〜2本。即時応答に架空の遅延を作らない

## 守ること (template.md の要点)
- ポート群を接頭辞で呼ばない。親の接続から役割名を決め、1.1 で一度だけ対応付け、本文は役割名で通す。1 つの概念に 1 つの語
- 2 章は信号表だけ (種別 / 意味は静的属性、条件式を書かない)。3.1 は ID / 機能 / 概要 の 3 列。各機能は「説明 → 入力の駆動条件 → 出力の扱い → 規則表 → 期待波形 + 説明」
- 規則は 1 本 = 1〜2 文、15〜25 本は目安であり水増し・省略しない。ID は R-01 からの連番。内部名 (reg / wire / 状態名) を書かない
- 「入力変化から n cycle 後」は入力を与えた cycle を 0 とした cycle 番号。位置は「cycle n」で指す
- クロックを跨ぐ入力があれば「〜の取り込み (CDC)」の機能を設ける。相手の動作は規則にせず「出力の扱い」へ
- 断定形。「思われる」「おそらく」を使わない。決まらないことは「未規定」(「RTL からは決まらない」と書かない)。「(RTL コメントより)」も書かない
- 6 章に機能をまたぐ制約、非対応、駆動条件に反したときの動作 (設計欠陥に見えるものは行末に「(要確認)」、7.4 に一覧)
- 初期化とリセット、同時入力の優先順、連続入力、背圧、最小最大値、値付きトグルの保持を該当回路の原文で確認する。
  コメントと処理が違えば現動作を記述し相違を7.4へ。本文と期待波形の時刻・値を一致させる。図は証明ではない。

## 終わったら
3 行で報告: 規則の本数と波形の枚数、「未規定」と書いた箇所、テンプレートで決まっていなくて困った点。
"""
    (out / "TASK.md").write_text(T, encoding="utf-8", newline="\n")
    print(f"{out}: RTL {len(copied)} 本 ({', '.join(copied)}), ポート {len(ports)}, 接続 {len(graph_rows)} 本, 図 {'あり' if fig else 'なし'}")
    print(f"次: TASK.md に従って執筆 → python \"{SKILL / 'run.py'}\" check \"{out}\" --draft")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
