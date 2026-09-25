#!/usr/bin/env python3
"""Portable entry point: prepare / check / review-prompt / run. Python 3.10+."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

SKILL = Path(__file__).resolve().parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
from check_edges import check_file


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def snapshot(folder, inputs_only=False):
    folder = Path(folder)
    m = json.loads((folder / "source-manifest.json").read_text(encoding="utf-8"))["target"]
    files = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(folder).as_posix()
        product = p.name == m + ".md" or p.name.startswith(m + "_exp_")
        is_input = (rel.startswith(("_rtl/", "_original/")) or p.name in
                    ("TASK.md", "template.md", "s1_io.md", "s1_graph.md", "source-manifest.json", "graph.json", m + "_block.drawio.svg")
                    or p.name.startswith("example_") or p.suffix in (".v", ".sv"))
        if is_input or (product and not inputs_only):
            files.append((rel, hashlib.sha256(p.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(files, ensure_ascii=False).encode("utf-8")).hexdigest()


def process(argv, log=None, timeout=900, cwd=None):
    """No shell interpolation; stdout/stderr and failures are retained."""
    try:
        r = subprocess.run([str(x) for x in argv], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        code, output = r.returncode, r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired) as ex:
        code, output = 2, str(ex)
    if log:
        Path(log).write_text(output, encoding="utf-8")
    return code, output


def prepare(args):
    folder = Path(args.out).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("--out must be empty; use a new directory to avoid stale artifacts")
    folder.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, SCRIPTS / "s1_materials.py", args.module,
               "--rtl-dir", Path(args.rtl_dir).resolve(), "--out", folder, "--encoding", args.encoding]
    for option in ("no_diagram", "trim"):
        if getattr(args, option):
            command.append("--" + option.replace("_", "-"))
    if args.file_list:
        command += ["--file-list", Path(args.file_list).resolve()]
    code, output = process(command, folder / "log-prepare.txt")
    print(output)
    if code:
        save(folder / "result.json", {"status": "input_blocked", "exit_code": code})
    return code


def renderer():
    candidates = [SCRIPTS / "wavedrom" / "render.js"]
    candidates += [p / "tools" / "wavedrom" / "render.js" for p in SKILL.parents]
    return next((p for p in candidates if p.exists()), None)


def machine_check(folder, render=True):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "source-manifest.json").read_text(encoding="utf-8"))
    module = manifest["target"]
    errors, warnings = [], []
    warnings += [f"{d['code']}: {d['source']}: {d['detail']}" for d in manifest.get("diagnostics", []) if d.get("severity") == "warning"]
    if manifest.get("status") != "ready":
        errors.append("source inventory is not ready")
    for entry in manifest["files"]:
        original = folder / entry["copy"]
        if not original.is_file():
            errors.append("missing source copy: " + entry["copy"])
        elif entry.get("copy_sha256") and hashlib.sha256(original.read_bytes()).hexdigest() != entry["copy_sha256"]:
            errors.append("source copy modified: " + entry["copy"])
    for entry in manifest["modules"].values():
        p = folder / entry["normalized"]
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != entry["sha256"]:
            errors.append("analysis source modified or missing: " + entry["normalized"])
    md = folder / (module + ".md")
    if not md.is_file():
        return [f"missing specification: {md.name}"], warnings
    text = md.read_text(encoding="utf-8")
    rules = set(re.findall(r"^\|\s*(R-\d+)\s*\|", text, re.M))
    if not rules:
        errors.append("no behavior rules")
    if re.search(r"\(記入\)|\bTODO\b|\bTBD\b|<module>", text):
        errors.append("unfilled placeholder in specification")
    refs = []
    for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        p = (folder / ref).resolve()
        if not p.is_relative_to(folder):
            errors.append("image reference outside output folder: " + ref)
            continue
        refs.append(p)
    waves = sorted(folder.glob(module + "_exp_*.json"))
    for p in waves:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            rid = p.stem.split("_exp_", 1)[1]
            if rid not in rules:
                errors.append(p.name + ": no matching rule")
            if (data.get("head") or {}).get("text") != rid:
                errors.append(p.name + ": head must contain only rule ID")
            if p.with_suffix(".svg") not in refs:
                errors.append(p.name + ": waveform is not referenced by the specification")
            if not data.get("signal"):
                errors.append(p.name + ": empty waveform")
            es, _, _ = check_file(p)
            errors += [p.name + ": " + e for e in es]
        except (ValueError, TypeError, KeyError, AttributeError) as ex:
            errors.append(p.name + ": invalid WaveDrom: " + str(ex))
    for p in refs:
        if "_exp_" in p.name and (p.suffix != ".svg" or not p.with_suffix(".json").is_file()):
            errors.append("waveform JSON missing: " + p.name)
    if not waves:
        errors.append("no expected waveform (for purely combinational modules provide an input/output example waveform)")
    if render and waves:
        node, script = shutil.which("node"), renderer()
        if not node or not script:
            errors.append("SVG renderer unavailable: install Node.js and the bundled wavedrom dependencies")
        else:
            # Render target artifacts only, never examples or another module's files.
            for p in waves:
                code, output = process([node, script, p, p.with_suffix(".svg")])
                if code:
                    errors.append("SVG render failed: " + output)
    for p in refs:
        if not p.is_file():
            errors.append("image missing: " + p.name)
    # Check final rendered artifacts, so fixed missing-image warnings do not persist.
    code, output = process([sys.executable, SCRIPTS / "check_func_spec.py", md,
                            "--rtl", folder / "_rtl" / (module + ".v"), "--rtl-dir", folder / "_rtl"])
    if code:
        errors.append(output)
    warnings += [line for line in output.splitlines() if line.startswith("WARN")]
    return errors, warnings


def review_prompt(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "source-manifest.json").read_text(encoding="utf-8"))
    module = manifest["target"]
    return f"""独立した機能仕様レビューを行う。作業フォルダ: {folder}
対象: {module}.md と {module}_exp_*.json / SVG。source-manifest.json、対象・内部・親・隣接 RTL と _original/ を読む。
執筆者のログや説明は読まない。本文、原本、波形から反証し、必要な依存 RTL を省略しない。
原本が不足、解析失敗、未読箇所を設計上の「未規定」と混同しない。コメントの主張も原文の処理で検証する。
起動・解除直後、同時入力の優先順、連続入力と背圧、最小最大値、CDC の保持と初期値、完了時刻を該当回路で調べる。
本文と波形の値・サイクルを照合する。機械検査の合格は意味の正しさの根拠にしない。
成果物と入力は編集しない。{folder / 'review.json'} だけを書く。
JSON schema: {{"verdict":"approve または changes_requested", "artifact_sha256":"{snapshot(folder)}",
"findings":[{{"severity":"blocking または recommend または question", "rule":"R-xx または節名", "detail":"具体的な根拠・入力例"}}],
"input_assessment":"source-manifest の診断・未読/不明範囲をどう扱ったか。診断がなければその旨",
"reviewed_files":["相対パス"]}}
重要動作の判断ができない question が残る場合は changes_requested。approve は対象の全機能と入力診断を確認できた場合のみ。
設計課題を現動作どおりに仕様化し7.4へ残した場合は、その課題自体で文書を不合格にしない。
"""


def review_errors(folder):
    try:
        report = json.loads((folder / "review.json").read_text(encoding="utf-8"))
        errors = []
        if report.get("artifact_sha256") != snapshot(folder):
            errors.append("review is stale or refers to different artifacts")
        if report.get("verdict") != "approve":
            errors.append("independent review has not approved")
        findings = report.get("findings")
        if not isinstance(findings, list):
            errors.append("review findings must be a list")
        else:
            for finding in findings:
                if not isinstance(finding, dict) or finding.get("severity") not in ("blocking", "recommend", "question"):
                    errors.append("invalid review finding")
                elif finding["severity"] in ("blocking", "question"):
                    errors.append("unresolved review finding: " + str(finding.get("detail")))
        if not isinstance(report.get("input_assessment"), str) or not report["input_assessment"].strip():
            errors.append("input coverage assessment missing")
        if not isinstance(report.get("reviewed_files"), list) or not report["reviewed_files"]:
            errors.append("reviewed_files missing")
        else:
            for ref in report["reviewed_files"]:
                if not isinstance(ref, str):
                    errors.append("invalid reviewed_files entry")
                else:
                    p = (folder / ref).resolve()
                    if not p.is_relative_to(folder.resolve()) or not p.is_file():
                        errors.append("reviewed file missing or outside output: " + ref)
        return errors
    except (OSError, ValueError, TypeError, AttributeError) as ex:
        return ["independent review missing or invalid: " + str(ex)]


def check(folder, draft=False):
    folder = Path(folder).resolve()
    errors, warnings = machine_check(folder)
    if not draft:
        errors += review_errors(folder)
    result = dict(status="failed" if errors else ("draft_valid" if draft else "complete"),
                  errors=errors, warnings=warnings, artifact_sha256=snapshot(folder),
                  semantic_review="not_required_for_draft" if draft else "required")
    save(folder / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


def run(args):
    cli = shutil.which(args.copilot)
    if not cli:
        raise ValueError("Copilot CLI not found; use prepare / check / review-prompt with another model")
    args.out = str(Path(args.work).resolve() / args.module / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]))
    code = prepare(args)
    if code:
        return code
    folder = Path(args.out)
    inputs = snapshot(folder, inputs_only=True)
    version_code, version = process([cli, "--version"], timeout=30)
    save(folder / "execution.json", dict(backend="copilot", version=version.strip(), version_exit=version_code,
                                        writer_model=args.model, reviewer_model=args.reviewer or args.model,
                                        python=sys.version, max_rounds=args.rounds))
    feedback = ""
    for attempt in range(1, args.rounds + 1):
        prompt = ((folder / "TASK.md").read_text(encoding="utf-8") +
                  f"\n作業フォルダ: {folder}\n前回の検査/レビュー結果:\n{feedback}\n入力を変更しない。モデル自身はreview.jsonを書かない。")
        command = [cli, "--agent", "rtl-spec-writer", "-p", prompt,
                   "--allow-tool", "read,write", "--available-tools", "view,create,edit,apply_patch,glob,grep", "--no-ask-user"]
        if args.model:
            command += ["--model", args.model]
        code, output = process(command, folder / f"log-writer-{attempt}.txt", args.timeout)
        if snapshot(folder, inputs_only=True) != inputs:
            raise ValueError("writer changed input artifacts; run rejected")
        if code:
            save(folder / "result.json", dict(status="writer_failed", exit_code=code, detail=output))
            return code
        errors, warnings = machine_check(folder)
        if errors:
            feedback = "\n".join(errors)
            continue
        # Old approval is never reused after a new writer turn.
        save(folder / "review.json", {"verdict": "pending"})
        before = snapshot(folder)
        command = [cli, "--agent", "rtl-spec-reviewer", "-p", review_prompt(folder),
                   "--allow-tool", "read,write", "--available-tools", "view,create,edit,apply_patch,glob,grep", "--no-ask-user"]
        if args.reviewer or args.model:
            command += ["--model", args.reviewer or args.model]
        code, output = process(command, folder / f"log-reviewer-{attempt}.txt", args.timeout)
        if snapshot(folder) != before:
            raise ValueError("reviewer changed input/specification artifacts; run rejected")
        if code:
            save(folder / "result.json", dict(status="reviewer_failed", exit_code=code, detail=output))
            return code
        errors = review_errors(folder)
        if not errors:
            return check(folder)
        feedback = (folder / "review.json").read_text(encoding="utf-8") + "\n" + "\n".join(errors)
    save(folder / "result.json", dict(status="needs_revision", rounds=args.rounds, detail=feedback))
    print(f"未完了: {folder / 'result.json'}")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    for action in ("prepare", "run"):
        p = sub.add_parser(action)
        p.add_argument("module")
        p.add_argument("--rtl-dir", default="rtl")
        p.add_argument("--encoding", default="utf-8-sig")
        p.add_argument("--file-list")
        p.add_argument("--no-diagram", action="store_true")
        p.add_argument("--trim", action="store_true", help="optional excerpts; originals always retained")
        if action == "prepare":
            p.add_argument("--out", required=True)
        else:
            p.add_argument("--work", default="work")
            p.add_argument("--model", help="omit to use configured model; no speculative model default")
            p.add_argument("--reviewer")
            p.add_argument("--copilot", default="copilot")
            p.add_argument("--rounds", type=int, choices=(1, 2, 3), default=1)
            p.add_argument("--timeout", type=int, default=900)
            p.add_argument("--review", action="store_true", help="compatibility: review is always required")
    p = sub.add_parser("check")
    p.add_argument("folder")
    p.add_argument("--draft", action="store_true", help="machine checks only; never reports complete")
    p = sub.add_parser("review-prompt")
    p.add_argument("folder")
    args = ap.parse_args(argv)
    try:
        if args.action == "prepare":
            return prepare(args)
        if args.action == "run":
            return run(args)
        if args.action == "check":
            return check(args.folder, args.draft)
        print(review_prompt(args.folder))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as ex:
        folder = getattr(args, "out", None) or getattr(args, "folder", None)
        if folder and Path(folder).is_dir():
            save(Path(folder) / "result.json", dict(status="failed", error=str(ex)))
        print(f"未完了: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
