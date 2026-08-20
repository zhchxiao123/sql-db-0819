# sql-db-0819

A minimal SQL database engine, driven end-to-end by the **official sqllogictest
runner**. This is the first slice of the engine: a runnable scaffold plus the
thinnest vertical path — create a table, insert rows, run a `SELECT ... WHERE`,
with the official sqllogictest tool driving the engine and comparing results.

## Pinned sqllogictest

The official sqllogictest repository is **pinned** to this commit — every later
slice and the final "ALL" acceptance reference this pin:

```
repo:   https://github.com/gregrahn/sqllogictest
commit: c67f97bf3ca7e590d12e073408bcacaf2ff0f3a0
```

The pinned tree is vendored **minimally** under `third_party/sqllogictest/`
(pin recorded in `third_party/sqllogictest/PIN.txt`): only the runner sources
needed to build and drive this engine, plus the pinned test corpus:

```
third_party/sqllogictest/
├── PIN.txt          pin + exclusion record
├── COPYRIGHT.md     upstream license
├── about.wiki       upstream documentation
├── src/             runner build: sqllogictest.c, sqllogictest.h, md5.c,
│                    Makefile.no-odbc, slt_subprocess.c (our engine driver)
└── test/            select1-5.test, the feature anchors (orderby/limit/
                     distinct/cast/null — see below), evidence/, and
                     random/expr/ (the pinned random-expression corpus)
```

Excluded from the vendored tree (documented in PIN.txt):

- **`sqlite3.c` / `sqlite3.h` / `slt_sqlite.c` / `slt_odbc3.c`** — the built-in
  SQLite and ODBC engines. They are compiled out (`-DOMIT_SQLITE=1
  -DOMIT_ODBC=1`; the includes and `registerSqlite()` call in the runner are
  guarded accordingly). The SQLite amalgamation in particular trips secret
  scanners' generic API-key rule on benign C struct-member expressions whose
  names look like credential keys, so it is deliberately not vendored.
- `run-all-{mssql,pgsql,odbc}.bat` and the other Windows launcher scripts,
  `proto/`, `logo.gif`, the Windows Makefiles, and the generated suites
  `test/index` and `test/random` **except** `test/random/expr` (the expression
  slice needs that one generated suite; it is vendored in full).

To re-vendor the pinned tree from scratch:

```bash
git clone https://github.com/gregrahn/sqllogictest.git /tmp/sqllogictest
cd /tmp/sqllogictest && git checkout c67f97bf3ca7e590d12e073408bcacaf2ff0f3a0
mkdir -p /tmp/slt-min/src /tmp/slt-min/test
cp COPYRIGHT.md about.wiki /tmp/slt-min/
cp src/sqllogictest.c src/sqllogictest.h src/md5.c src/Makefile.no-odbc /tmp/slt-min/src/
cp test/select1.test test/select2.test test/select3.test test/select4.test test/select5.test /tmp/slt-min/test/
cp -r test/evidence /tmp/slt-min/test/
cp -r test/random/expr /tmp/slt-min/test/random/  # full random/expr suite (120 files)
# the feature anchors (test/orderby.test, limit, distinct, cast, null) do NOT
# exist at the pinned commit; they are substituted with locally-authored files
# covering the same features, generated and verified with the reference SQLite
# engine (see "Feature anchors" below).
# then copy the tree into third_party/sqllogictest/ and re-apply the driver:
#   src/slt_subprocess.c  (new file, engine driver)
#   src/sqllogictest.c    (+ guarded slt_sqlite.c/slt_odbc3.c includes; + registerSubprocess();
#                          + sqlite3_snprintf shim under OMIT_SQLITE)
#   src/Makefile.no-odbc  (OBJ=md5.o; INC=slt_subprocess.c; -DOMIT_ODBC=1 -DOMIT_SQLITE=1)
```

## Layout

```
sql_db_0819/engine.py          the SQL engine (parser + evaluator, stdlib only)
sql_db_0819/cli.py             protocol CLI spoken by the runner's engine driver
tests/test_engine.py           engine unit tests (stdlib unittest)
third_party/sqllogictest/      pinned official sqllogictest (minimal vendor + driver)
tools/run_sqllogictest.sh      per-file pass/fail wrapper around the runner
tools/run-acceptance.sh        acceptance runner: select1-5 + anchors + random/expr
tools/run_negative_test.sh     negative verification (a3)
test/negative.test             deliberately-wrong expectations fixture
Makefile                       build/unit/test/negative-test entry points
```

## Commands

| Command              | What it does                                                          |
|----------------------|-----------------------------------------------------------------------|
| `make build`         | Build the official sqllogictest runner binary (needs gcc + make)      |
| `make unit`          | Run engine unit tests (`python3 -m unittest discover -s tests -v`)    |
| `make test`          | Build, then run the official runner against this engine on `test/select1.test` and `test/select2.test` from the pinned commit; prints a per-file PASS/FAIL line with the runner's error/skip summary; exit 0 only if both pass with 0 skips |
| `bash tools/run-acceptance.sh` | Build first (`make build`), then run **every acceptance target** through the official runner: select1-5, the five feature anchors, and all 120 `test/random/expr/*.test` files; one PASS/FAIL line per file; exit 0 only if every file reports 0 errors (see `tools/run-acceptance.sh`) |
| `make negative-test` | Run the runner against `test/negative.test` (wrong expected values); asserts the runner reports failure and exits non-zero |
| `make clean`         | Remove build artifacts                                                 |

### Engine run entry point

The engine process is started by the runner driver as:

```bash
python3 sql_db_0819/cli.py          # from anywhere (self-contained)
python3 -m sql_db_0819.cli          # or from the repo root
```

It speaks a small length-prefixed line protocol on stdin/stdout (statement
`S`, query `Q`, shutdown `X`); see `sql_db_0819/cli.py` for the exact frames.
The runner's `--engine sql-db-0819 --connection "<cmd>"` selects it.

## How the runner drives this engine

The official runner (`third_party/sqllogictest/src/sqllogictest`) is built with
an additional `DbEngine` implementation (`src/slt_subprocess.c`, registered via
`registerSubprocess()` in `main()`). It spawns this engine as a subprocess and
forwards every statement/query from the `.test` files over a pipe; the engine
returns rendered result values; the runner compares them against the expected
values (per-value below the hash threshold, MD5 of the values above it) and
prints, per file:

```
0 errors out of 1031 tests in test/select1.test - 0 skipped.
```

The built-in SQLite and ODBC engines are compiled out of this build (see the
pin section), so the runner registers exactly one engine, `sql-db-0819`.

## Engine scope (this slice)

`CREATE TABLE` (typed columns with optional sizes and `PRIMARY KEY`
constraints; `CREATE [UNIQUE] INDEX` / `DROP INDEX` accepted as no-ops),
`INSERT INTO ... VALUES` (reordered column lists, column-affinity coercion),
`SELECT` with `SELECT ALL/DISTINCT`, `*` and `tN.*` expansion, column aliases,
multi-table `FROM t1, t2, ...` (comma joins with equality-constraint
optimization), `WHERE`, `ORDER BY` (positional / alias / expression keys,
`ASC`/`DESC`, `NULLS FIRST/LAST`), `LIMIT`/`OFFSET` (all three spellings),
and compound selects `UNION [ALL]` / `EXCEPT` / `INTERSECT`.

Expressions: arithmetic (`+ - * / % DIV`; `/` and `%` yield NULL on a zero
divisor, matching SQLite), comparisons (`= != < <= > >=` with SQLite type and
affinity semantics), logical (`AND OR NOT` with three-valued logic), string
concatenation `||`, `LIKE` (case-insensitive, `%`/`_` wildcards, `ESCAPE`),
`IN` / `NOT IN` (lists and subqueries), `BETWEEN`/`NOT BETWEEN`, `IS [NOT]
NULL`, searched and simple `CASE`, `CAST(... AS INTEGER/SIGNED/UNSIGNED/
REAL/NUMERIC/TEXT/...)`, scalar functions `abs()`, `coalesce()`, `nullif()`,
`ifnull()`, and aggregates `count(*)`/`count(expr)`/`count(ALL expr)`/
`count(DISTINCT expr)`/`sum()`/`avg()`/`min()`/`max()` usable **anywhere** in
an expression (inside `CASE` arms, function arguments, arithmetic). Semantics
match SQLite, the reference the sqllogictest expectations are generated from.

## Feature anchors (substitution)

The pinned commit has no `test/orderby.test`, `test/limit.test`,
`test/distinct.test`, `test/cast.test`, or `test/null.test`. Per the slice
requirement ("if an anchor file is absent at the pinned commit, substitute the
equivalent file covering the same feature"), those five files are locally
authored equivalents: each contains a small table plus queries exercising the
feature (ordering modes, limit spellings, distinctness, casts, NULL
semantics). Expected values were generated by the **reference SQLite engine**
via the official runner's completion mode and re-verified against it before
being committed, so the expectations are authoritative SQLite behavior.
The substitution is listed in the PR description / `result.json`.

## Secrets

None. No credentials, tokens, or connection strings are committed anywhere in
this repository; the engine is pure stdlib Python with no network access. The
vendored third-party tree is kept minimal specifically to avoid shipping files
that trigger secret scanners (verified with gitleaks and detect-secrets: 0
findings).
