# -*- coding: utf-8 -*-
"""配布パッケージを作る: dist/rtl-spec-auto/ (ディレクトリ) と dist/rtl-spec-auto.zip。

    py .github/skills/rtl-spec-auto/deploy/pack.py

集めるもの: .github/skills/rtl-spec-auto/ 一式 (__pycache__ 除く)、.github/agents/rtl-spec-*.agent.md、
deploy/DEPLOY.md → README.md、deploy/install.py / install.sh → パッケージのルート、VERSION (コミットと日付)。
.ps1 は入れない (Gmail などのメールが遮断する拡張子)。
zip のエントリは並び順と日付を固定して再生成しても差分が出ないようにする。"""
import datetime
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SK = REPO / ".github" / "skills" / "rtl-spec-auto"
AG = REPO / ".github" / "agents"
DIST = REPO / "dist"
OUT = DIST / "rtl-spec-auto"
ZIP = DIST / "rtl-spec-auto.zip"


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / ".github" / "agents").mkdir(parents=True)
    shutil.copytree(SK, OUT / ".github" / "skills" / "rtl-spec-auto", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for a in sorted(AG.glob("rtl-spec-*.agent.md")):
        shutil.copy2(a, OUT / ".github" / "agents" / a.name)
    shutil.copy2(SK / "deploy" / "DEPLOY.md", OUT / "README.md")
    shutil.copy2(SK / "deploy" / "install.py", OUT / "install.py")
    shutil.copy2(SK / "deploy" / "install.sh", OUT / "install.sh")
    # .ps1 は同梱しない (Gmail 等のメールが遮断する拡張子。Windows でも install.py で展開できる)
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = "unknown"
    (OUT / "VERSION").write_text(f"rtl-spec-auto {commit} ({datetime.date.today().isoformat()})\n", encoding="utf-8", newline="\n")

    files = sorted(p for p in OUT.rglob("*") if p.is_file())
    fixed = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            arc = "rtl-spec-auto/" + p.relative_to(OUT).as_posix()
            zi = zipfile.ZipInfo(arc, date_time=fixed)
            zi.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if p.suffix in (".sh",) else 0o644
            zi.external_attr = (mode & 0xFFFF) << 16
            z.writestr(zi, p.read_bytes())
    print(f"{OUT.relative_to(REPO)}: {len(files)} files")
    print(f"{ZIP.relative_to(REPO)}: {ZIP.stat().st_size:,} bytes")
    for p in files:
        print("  " + p.relative_to(OUT).as_posix())
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
