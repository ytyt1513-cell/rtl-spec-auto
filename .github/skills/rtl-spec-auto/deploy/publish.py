# -*- coding: utf-8 -*-
"""配布パッケージを公開リポジトリ (ytyt1513-cell/rtl-spec-auto、public) に反映する。

    py .github/skills/rtl-spec-auto/deploy/publish.py [--tag v0.1.1] [--clone-dir DIR] [--no-pack]

手順: pack.py で dist/ を再生成 → 公開リポジトリのローカル clone (無ければ gh repo clone) の中身を dist/rtl-spec-auto で置き換え
→ commit (VERSION をメッセージに) → push → --tag があれば Releases に dist/rtl-spec-auto.zip を添付する。
clone の既定の場所は元リポジトリの隣 (../rtl-spec-auto)。gh (GitHub CLI) でサインイン済みであること。"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
DIST = REPO / "dist" / "rtl-spec-auto"
ZIP = REPO / "dist" / "rtl-spec-auto.zip"
TOOL_REPO = "ytyt1513-cell/rtl-spec-auto"


def run(cmd, cwd=None, check=True):
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", help="Releases を作るタグ (例 v0.1.1)。省略時は push のみ")
    ap.add_argument("--clone-dir", help="公開リポジトリのローカル clone (既定 ../rtl-spec-auto)")
    ap.add_argument("--no-pack", action="store_true", help="dist を再生成しない")
    a = ap.parse_args()
    if not a.no_pack:
        run([sys.executable, str(REPO / ".github" / "skills" / "rtl-spec-auto" / "deploy" / "pack.py")])
    clone = Path(a.clone_dir).resolve() if a.clone_dir else (REPO.parent / "rtl-spec-auto").resolve()
    if not (clone / ".git").exists():
        run(["gh", "repo", "clone", TOOL_REPO, str(clone)])
        run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=clone, check=False)   # 空リポジトリの初回は main で始める
    else:
        run(["git", "pull", "-q", "--ff-only"], cwd=clone, check=False)                    # 空リポジトリでは失敗してよい
    for p in clone.iterdir():
        if p.name == ".git":
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(DIST, clone, dirs_exist_ok=True)
    (clone / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8", newline="\n")
    version = (DIST / "VERSION").read_text(encoding="utf-8").strip()
    run(["git", "add", "-A"], cwd=clone)
    if run(["git", "diff", "--cached", "--quiet"], cwd=clone, check=False).returncode != 0:
        run(["git", "commit", "-q", "-m", version], cwd=clone)
        run(["git", "push", "-q", "-u", "origin", "HEAD"], cwd=clone)
        print(f"pushed: {version}")
    else:
        print("変更なし (push しない)")
    if a.tag:
        run(["gh", "release", "create", a.tag, str(ZIP), "-R", TOOL_REPO, "--title", a.tag, "--notes", version])
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
