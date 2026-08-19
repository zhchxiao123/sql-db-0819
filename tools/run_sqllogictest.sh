#!/usr/bin/env bash
# Run the official sqllogictest runner (--verify) against one or more .test
# files using the sql-db-0819 engine, and print a per-file PASS/FAIL line.
#
# Exit status is 0 only if every file reports 0 errors AND 0 skipped.
# The runner is the unmodified official binary from the pinned commit plus
# the registered subprocess engine driver (see README.md).
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${RUNNER:-$ROOT/third_party/sqllogictest/src/sqllogictest}"
ENGINE_CMD="${ENGINE_CMD:-python3 $ROOT/sql_db_0819/cli.py}"

status=0
for f in "$@"; do
  out="$("$RUNNER" --engine sql-db-0819 --connection "$ENGINE_CMD" --verify "$f" 2>&1)"
  rc=$?
  summary="$(printf '%s\n' "$out" | grep -E '[0-9]+ errors out of [0-9]+ tests' | tail -n 1)"
  nerr="$(printf '%s\n' "$summary" | sed -nE 's/^([0-9]+) errors.*/\1/p')"
  nskip="$(printf '%s\n' "$summary" | sed -nE 's/.* - ([0-9]+) skipped\..*/\1/p')"
  if [ -z "$nerr" ]; then nerr=-1; fi
  if [ -z "$nskip" ]; then nskip=-1; fi
  if [ "$rc" -eq 0 ] && [ "$nerr" -eq 0 ] && [ "$nskip" -eq 0 ]; then
    printf 'PASS %s (%s)\n' "$f" "$summary"
  else
    printf 'FAIL %s (%s; exit=%d)\n' "$f" "$summary" "$rc"
    status=1
  fi
done
exit $status
