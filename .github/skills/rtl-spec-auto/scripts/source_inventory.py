"""Lossless source inventory for S1. This is not an HDL elaborator.

Index module declarations recursively, preserve originals and report unsupported
syntax before a model is asked to write. Normalized files are analysis copies.
"""
import hashlib
import json
import re
from pathlib import Path

from rtlparse import IDENT, _blank_all_comments, _match_paren, instance_spans, parse, split_spans


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inventory(rtl_dir, out, encoding="utf-8-sig", file_list=None):
    root, out = Path(rtl_dir).resolve(), Path(out).resolve()
    if file_list:
        listing = Path(file_list).resolve()
        paths = [(listing.parent / s.strip()).resolve() for s in listing.read_text(encoding="utf-8-sig").splitlines()
                 if s.strip() and not s.lstrip().startswith("#")]
    else:
        paths = sorted(p for p in root.rglob("*") if p.suffix.lower() in (".v", ".sv") and p.is_file())
    paths = list(dict.fromkeys(paths))
    normalized = out / "_rtl"
    normalized.mkdir(parents=True, exist_ok=True)
    originals = out / "_original"
    originals.mkdir(exist_ok=True)
    report = {"schema": 1, "encoding": encoding, "files": [], "modules": {}, "diagnostics": []}

    def issue(code, source, detail, severity="error"):
        report["diagnostics"].append(dict(code=code, source=str(source), detail=detail, severity=severity))

    if not paths:
        issue("NO_SOURCES", root, "No .v/.sv files selected")
    for number, path in enumerate(paths):
        try:
            raw = path.read_text(encoding=encoding)  # never replace undecodable bytes
        except (OSError, UnicodeError) as ex:
            issue("SOURCE_READ", path, str(ex))
            continue
        copied = originals / f"{number:04d}_{path.name}"
        copied.write_text(raw, encoding="utf-8", newline="\n")
        report["files"].append(dict(path=str(path), sha256=digest(path), copy=copied.relative_to(out).as_posix(), copy_sha256=digest(copied)))
        code = _blank_all_comments(raw)
        # Choosing a build configuration is a caller decision, never a regex guess.
        directives = sorted(set(re.findall(r"`([A-Za-z_]\w*)", code)) - {"timescale", "default_nettype", "resetall"})
        if directives:
            issue("PREPROCESS_REQUIRED", path, "Preprocess with the project's defines/include paths: " + ", ".join(directives))
        if re.search(r"\b(interface|package|typedef|struct|union)\b|\\\S+", code):
            issue("UNSUPPORTED_SYNTAX", path, "interface/package/user types/escaped identifiers require an HDL frontend")
        if re.search(r"\w+\s+\w+\s*\[[^\]]+\]\s*\(", code):
            issue("INSTANCE_ARRAY", path, "instance arrays require elaboration")
        if re.search(r"\.\s*\*", code):
            issue("WILDCARD_PORTS", path, ".* requires elaboration")
        starts = list(re.finditer(r"\bmodule\s+(" + IDENT + r")\b", code))
        ends = list(re.finditer(r"\bendmodule\b", code))
        if len(starts) != len(ends) or not starts:
            issue("MODULE_BOUNDARY", path, "module/endmodule pairing is missing or ambiguous")
            continue
        previous_end = 0
        for start, end in zip(starts, ends):
            name = start.group(1)
            if start.start() < previous_end or end.start() < start.end():
                issue("MODULE_BOUNDARY", path, name)
                continue
            if name in report["modules"]:
                issue("DUPLICATE_MODULE", path, f"{name}: also in {report['modules'][name]['source']}")
                continue
            if name.casefold() in {n.casefold() for n in report["modules"]}:
                issue("COLLIDING_MODULE_PATH", path, f"{name}: case-only module names cannot share portable normalized paths")
                continue
            # Header comments retained; code outside modules (directives) stays in originals.
            prefix = raw[previous_end:start.start()]
            if _blank_all_comments(prefix).strip():
                prefix = ""
            body = prefix + raw[start.start():end.end()] + "\n"
            previous_end = end.end()
            dest = normalized / (name + ".v")
            dest.write_text(body, encoding="utf-8", newline="\n")
            item = dict(source=str(path), line=code[:start.start()].count("\n") + 1,
                        normalized=dest.relative_to(out).as_posix())
            report["modules"][name] = item
            try:
                info = parse(dest)
                if len({p["name"] for p in info["ports"]}) != len(info["ports"]):
                    raise ValueError("duplicate port declarations")
                item["ports"] = [p["name"] for p in info["ports"]]
                item["instantiated_modules"] = sorted({i["module"] for i in info["instances"]})
                # Non-ANSI port order is the header order, not declaration order.
                module_code = _blank_all_comments(body)
                declaration = re.search(r"\bmodule\s+" + re.escape(name) + r"\s*", module_code)
                pos = declaration.end()
                if module_code[pos:pos + 1] == "#":
                    pos = _match_paren(module_code, module_code.index("(", pos)) + 1
                while pos < len(module_code) and module_code[pos].isspace():
                    pos += 1
                header = module_code[pos + 1:_match_paren(module_code, pos)] if module_code[pos:pos + 1] == "(" else ""
                if header and not re.search(r"\b(input|output|inout)\b", header):
                    ordered = [x.strip() for x in header.split(",") if x.strip()]
                    if set(ordered) != set(item["ports"]):
                        raise ValueError("header ports do not match non-ANSI declarations")
                    item["ports"] = ordered
                if re.search(r"\bgenerate\b|\bgenvar\b", _blank_all_comments(body)):
                    issue("NOT_ELABORATED", path, f"{name}: generate branches/instance counts are not elaborated; inspect conditions", "warning")
            except (ValueError, IndexError) as ex:
                issue("PORT_PARSE", path, f"{name}: {ex}")

    # Normalize positional and shorthand connections for the existing graph parser.
    for name, item in report["modules"].items():
        missing = sorted(set(item.get("instantiated_modules", [])) - set(report["modules"]))
        if missing:
            issue("OPAQUE_MODULE", item["source"], f"{name}: missing implementation for {', '.join(missing)}; do not infer its timing", "warning")
        dest = out / item["normalized"]
        raw = dest.read_text(encoding="utf-8")
        code = _blank_all_comments(raw)
        edits = []
        for span in instance_spans(code):
            target, inst = span["module"], span["inst"]
            if target not in report["modules"]:
                continue
            opening, close = span["open"], span["close"]
            if close < 0:
                issue("CONNECTION_PARSE", item["source"], f"{name}.{inst}: unbalanced connections")
                continue
            body = code[opening + 1:close]
            spans = list(split_spans(body))
            args = [body[a:b].strip() for a, b in spans]
            ports = report["modules"][target].get("ports", [])
            if re.search(r"\.\s*\*", body):
                issue("WILDCARD_PORTS", item["source"], f"{name}.{inst}: .* requires elaboration")
            elif args and args[0].startswith("."):
                for a, b in spans:
                    short = re.fullmatch(r"\s*\.\s*(" + IDENT + r")\s*", body[a:b])
                    if short:
                        p = short.group(1)
                        edits.append((opening + 1 + a, opening + 1 + b, f".{p}({p})"))
            elif body.strip():
                if len(args) != len(ports):
                    issue("POSITIONAL_PORTS", item["source"], f"{name}.{inst}: {len(args)} arguments vs {len(ports)} ports")
                else:
                    edits.append((opening + 1, close, ", ".join(f".{p}({arg})" for p, arg in zip(ports, args))))
            if re.match(r"\s*,", code[close + 1:]):
                issue("MULTI_INSTANCE", item["source"], f"{name}: multiple instances in one declaration require expansion")
        for start, end, replacement in sorted(edits, reverse=True):
            raw = raw[:start] + replacement + raw[end:]
        dest.write_text(raw, encoding="utf-8", newline="\n")
        item["sha256"] = digest(dest)
    report["status"] = "blocked" if any(d["severity"] == "error" for d in report["diagnostics"]) else "ready"
    (out / "source-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
