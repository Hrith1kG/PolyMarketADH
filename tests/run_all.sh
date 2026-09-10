#!/usr/bin/env bash
# Regression suite for the execution-correctness fixes.
# Usage:  ./tests/run_all.sh          (expects .venv with requirements.txt installed)
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
fail=0
for t in tests/test_*.py; do
  echo "=== $t ==="
  if ! "$PY" "$t" 2>&1 | grep -Ev "use_container_width|will be removed|Please replace|ScriptRunContext|^$"; then
    fail=1
  fi
done
exit $fail
