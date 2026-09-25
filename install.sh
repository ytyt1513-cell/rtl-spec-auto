#!/usr/bin/env bash
# Cross-platform installer logic lives in install.py.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
    exec "$candidate" "$here/install.py" "$@"
  fi
done
echo 'Python 3.10+ is required.' >&2
exit 2
