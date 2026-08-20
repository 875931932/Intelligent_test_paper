#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for name in worker api; do
  file="$ROOT_DIR/var/run/$name.pid"
  if [[ -f "$file" ]]; then
    pid="$(cat "$file")"
    kill "$pid" 2>/dev/null || true
    rm -f "$file"
    echo "stopped $name"
  fi
done
