# -*- coding: utf-8 -*-
"""rtl-spec-auto を展開する (Windows / Linux 共通。Python 3 だけで動く)。

    py install.py <対象リポジトリのルート>   … <root>/.github/skills/rtl-spec-auto と <root>/.github/agents にコピー、.gitignore 追記
    py install.py --user                    … ~/.copilot/skills/rtl-spec-auto と ~/.copilot/agents にコピー (個人環境、全リポジトリで有効)

配布パッケージのルート (README.md と同じ場所) から実行しても、元リポジトリの .github/skills/rtl-spec-auto/deploy/ から実行してもよい。
"""
import shutil
import sys
from pathlib import Path

GITIGNORE_LINES = ["doc/spec/.rtl-spec-auto/", "doc/spec/*.new.md"]


def main(argv):
    here = Path(__file__).resolve().parent
    src = here if (here / ".github" / "skills" / "rtl-spec-auto").is_dir() else here.parents[3]
    if not (src / ".github" / "skills" / "rtl-spec-auto").is_dir():
        print(f"パッケージが見つからない: {src}", file=sys.stderr)
        return 2
    if len(argv) < 2:
        print(__doc__)
        return 2
    if argv[1] == "--user":
        root = None
        dst_skill = Path.home() / ".copilot" / "skills" / "rtl-spec-auto"
        dst_agents = Path.home() / ".copilot" / "agents"
    else:
        root = Path(argv[1]).resolve()
        if not root.is_dir():
            print(f"対象リポジトリが見つからない: {root}", file=sys.stderr)
            return 2
        dst_skill = root / ".github" / "skills" / "rtl-spec-auto"
        dst_agents = root / ".github" / "agents"

    if dst_skill.exists():
        shutil.rmtree(dst_skill)
    shutil.copytree(src / ".github" / "skills" / "rtl-spec-auto", dst_skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    dst_agents.mkdir(parents=True, exist_ok=True)
    for a in sorted((src / ".github" / "agents").glob("rtl-spec-*.agent.md")):
        shutil.copy2(a, dst_agents / a.name)
    try:
        (dst_skill / "run.sh").chmod(0o755)
    except Exception:
        pass

    if root is not None:
        gi = root / ".gitignore"
        existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        add = [l for l in GITIGNORE_LINES if l not in existing]
        if add:
            with gi.open("a", encoding="utf-8", newline="\n") as f:
                if existing and existing[-1] != "":
                    f.write("\n")
                f.write("\n".join(add) + "\n")
        print(f"展開完了: {dst_skill}, {dst_agents} (.gitignore に {len(add)} 行追記)。git add .github .gitignore してコミットする。")
    else:
        print(f"展開完了 (個人環境): {dst_skill}, {dst_agents}")
    print("次: 対象リポジトリで copilot を起動し /model でモデル名を確認 → .agent.md の model: と run.sh の既定値を合わせる。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv))
