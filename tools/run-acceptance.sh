#!/usr/bin/env bash
# Run every acceptance target through the official sqllogictest runner and
# print one summary line per file. Exits nonzero if any file reports errors.
#
# Usage: tools/run-acceptance.sh [test-dir]
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
RUNNER="$REPO/third_party/sqllogictest/src/sqllogictest"
CLI="python3 $REPO/sql_db_0819/cli.py"
TESTDIR="${1:-$REPO/third_party/sqllogictest/test}"

fail=0

run_one() {
  local f="$1"
  local out rc summary
  out="$(mktemp)"
  "$RUNNER" --engine sql-db-0819 --connection "$CLI" --verify "$f" >"$out" 2>&1
  rc=$?
  summary="$(grep -oE '[0-9]+ errors out of [0-9]+ tests[^.]*' "$out" | tail -1)"
  if [ $rc -eq 0 ]; then
    echo "PASS $f - $summary"
  else
    echo "FAIL $f - $summary"
    fail=1
  fi
  rm -f "$out"
}

# 1. select1-5 (engine-neutral anchors)
for n in 1 2 3 4 5; do
  run_one "$TESTDIR/select$n.test"
done

# 2. feature anchors (substituted for the pinned commit; see README)
for n in orderby limit distinct cast null; do
  run_one "$TESTDIR/$n.test"
done

# 3. random expression tests (all files)
for f in "$TESTDIR"/random/expr/*.test; do
  run_one "$f"
done

exit $fail
