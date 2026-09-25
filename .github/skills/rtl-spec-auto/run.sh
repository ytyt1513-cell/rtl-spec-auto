#!/usr/bin/env bash
# Compatibility launcher. State and validation live in run.py.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
    exec "$candidate" "$here/run.py" run "$@"
  fi
done
echo 'Python 3.10+ is required. Run run.py with an installed Python executable.' >&2
exit 2
