#!/usr/bin/env bash
# rtl-spec-auto を展開する。
#   ./install.sh <対象リポジトリのルート>   … <root>/.github/skills/rtl-spec-auto と <root>/.github/agents にコピー、.gitignore 追記
#   ./install.sh --user                    … ~/.copilot/skills/rtl-spec-auto と ~/.copilot/agents にコピー (個人環境、全リポジトリで有効)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if [[ -d "$here/.github/skills/rtl-spec-auto" ]]; then
  src="$here"                                  # 配布パッケージのルートから実行
else
  src="$(cd "$here/../../../.." && pwd)"       # 元リポジトリの .github/skills/rtl-spec-auto/deploy から実行
fi
[[ -d "$src/.github/skills/rtl-spec-auto" ]] || { echo "パッケージが見つからない: $src" >&2; exit 2; }

if [[ "${1:-}" == "--user" ]]; then
  dst_skill="$HOME/.copilot/skills/rtl-spec-auto"; dst_agents="$HOME/.copilot/agents"; root=""
else
  root="${1:-}"; [[ -n "$root" && -d "$root" ]] || { echo "usage: install.sh <repo-root> | --user" >&2; exit 2; }
  dst_skill="$root/.github/skills/rtl-spec-auto"; dst_agents="$root/.github/agents"
fi

mkdir -p "$dst_skill" "$dst_agents"
rm -rf "$dst_skill"
cp -r "$src/.github/skills/rtl-spec-auto" "$dst_skill"
find "$dst_skill" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp "$src"/.github/agents/rtl-spec-*.agent.md "$dst_agents/"
chmod +x "$dst_skill/run.sh" 2>/dev/null || true

if [[ -n "$root" ]]; then
  gi="$root/.gitignore"; touch "$gi"
  for line in "doc/spec/.rtl-spec-auto/" "doc/spec/*.new.md"; do
    grep -qxF "$line" "$gi" || echo "$line" >> "$gi"
  done
  echo "展開完了: $dst_skill, $dst_agents (.gitignore 追記済み)。git add .github .gitignore してコミットする。"
else
  echo "展開完了 (個人環境): $dst_skill, $dst_agents"
fi
echo "次: 対象リポジトリで copilot を起動し /model でモデル名を確認 → .agent.md の model: と run.sh の既定値を合わせる。"
