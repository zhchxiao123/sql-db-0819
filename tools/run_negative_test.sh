#!/usr/bin/env bash
# Negative verification (acceptance a3): run the official sqllogictest runner
# against a .test file whose expected values are deliberately WRONG.  The
# runner MUST report a failure and exit non-zero — that proves the result
# comparison is real and not a blanket "all green".
#
# This script exits 0 when the runner correctly fails (i.e. the negative
# verification succeeded), and non-zero when the runner wrongly passes.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${RUNNER:-$ROOT/third_party/sqllogictest/src/sqllogictest}"
ENGINE_CMD="${ENGINE_CMD:-python3 $ROOT/sql_db_0819/cli.py}"
f="${1:?usage: run_negative_test.sh <file.test>}"

out="$("$RUNNER" --engine sql-db-0819 --connection "$ENGINE_CMD" --verify "$f" 2>&1)"
rc=$?
printf '%s\n' "$out" | grep -E 'wrong result|errors out of' || true
if [ "$rc" -ne 0 ]; then
  echo "NEGATIVE TEST OK: runner reported failure (exit $rc) for $f, as expected."
  exit 0
else
  echo "NEGATIVE TEST FAILED: runner exited 0 for $f — result comparison did not trigger."
  exit 1
fi
