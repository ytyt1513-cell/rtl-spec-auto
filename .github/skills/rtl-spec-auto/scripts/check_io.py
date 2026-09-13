# -*- coding: utf-8 -*-
"""spec の I/O 表 (Signal / Dir / Bits / Description) と RTL のポート宣言を突合する。

    py scripts/check_io.py                       # 全 spec (--spec-dir 内の *.md と同名の --rtl-dir/*.v)
    py scripts/check_io.py rec_ctrl              # 1 モジュール、詳細表示
    py scripts/check_io.py --rtl-dir rtl --spec-dir doc/spec rec_ctrl

判定:
  MISSING  RTL にあるが表にない          EXTRA  表にあるが RTL にない
  DIR      方向が違う                    WIDTH  幅が違う (数値で比較できた場合のみ)
  UNVERIF  RTL 幅がパラメータ式で数値比較できない (表の値はそのまま)
差動ペア (`TMDS_CLK_P/N`) と `a/b` 併記行は展開して比較する。exit 1 = 不一致あり。"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rtlparse import parse, spec_io_rows  # noqa: E402


def compare(name, rtl_dir, spec_dir, verbose=False):
    v, md = Path(rtl_dir) / f"{name}.v", Path(spec_dir) / f"{name}.md"
    if not v.exists():
        v = Path(rtl_dir) / f"{name}.sv"
    if not v.exists() or not md.exists():
        return None
    r = {p["name"]: (p["dir"], p["width"]) for p in parse(v)["ports"]}
    s = {k: (d, b) for k, (d, b, _, _) in spec_io_rows(md).items()}
    probs = []
    for p in sorted(set(r) - set(s)):
        probs.append(("MISSING", p, f"RTL {r[p][0]} [{r[p][1]}]"))
    for p in sorted(set(s) - set(r)):
        probs.append(("EXTRA", p, f"spec {s[p][0]} [{s[p][1]}]"))
    unverified = 0
    for p in sorted(set(r) & set(s)):
        rd, rw = r[p]
        sd, sw = s[p]
        if rd != {"input": "in", "output": "out"}.get(sd, sd):
            probs.append(("DIR", p, f"RTL {rd} / spec {sd}"))
        if isinstance(rw, int):
            if not re.fullmatch(r"\d+", sw) or int(sw) != rw:
                probs.append(("WIDTH", p, f"RTL {rw} / spec {sw}"))
        else:
            unverified += 1
            if verbose:
                print(f"  UNVERIF  {p:<24} RTL [{rw}] / spec {sw}")
    if verbose:
        for k, p, d in probs:
            print(f"  {k:<8} {p:<24} {d}")
    return len(r), len(s), probs, unverified


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="モジュール名 (省略時は spec ディレクトリの全 .md)")
    ap.add_argument("--rtl-dir", default="rtl")
    ap.add_argument("--spec-dir", default="doc/spec")
    a = ap.parse_args(argv)
    targets = a.names or sorted(p.stem for p in Path(a.spec_dir).glob("*.md") if p.stem != "README")
    total_ok = total = 0
    for n in targets:
        res = compare(n, a.rtl_dir, a.spec_dir, verbose=bool(a.names))
        if res is None:
            print(f"{n:<20} (rtl or spec not found)")
            continue
        nr, ns, probs, unv = res
        total += 1
        total_ok += (not probs)
        kinds = {}
        for k, _, _ in probs:
            kinds[k] = kinds.get(k, 0) + 1
        status = "OK " if not probs else "NG "
        print(f"{status}{n:<20} rtl {nr:>3} / spec {ns:>3} ports  " + (", ".join(f"{k} {c}" for k, c in sorted(kinds.items())) or "")
              + (f"  (unverified width {unv})" if unv else ""))
    print(f"--- {total_ok}/{total} specs match their RTL port lists")
    return 0 if total_ok == total else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
