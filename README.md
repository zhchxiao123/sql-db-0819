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
└── test/            select1-5.test + evidence/ (the pinned sqllogictest corpus)
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
  `test/random` / `test/index`.

To re-vendor the pinned tree from scratch:

```bash
git clone https://github.com/gregrahn/sqllogictest.git /tmp/sqllogictest
cd /tmp/sqllogictest && git checkout c67f97bf3ca7e590d12e073408bcacaf2ff0f3a0
mkdir -p /tmp/slt-min/src /tmp/slt-min/test
cp COPYRIGHT.md about.wiki /tmp/slt-min/
cp src/sqllogictest.c src/sqllogictest.h src/md5.c src/Makefile.no-odbc /tmp/slt-min/src/
cp test/select1.test test/select2.test test/select3.test test/select4.test test/select5.test /tmp/slt-min/test/
cp -r test/evidence /tmp/slt-min/test/
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

`CREATE TABLE`, `INSERT INTO ... VALUES`, `SELECT` with `WHERE`, `ORDER BY`,
expressions (`+ - * /`, comparisons, `AND/OR/NOT`, `BETWEEN` / `NOT BETWEEN`,
`IS [NOT] NULL`, searched and simple `CASE`, `abs()`, `coalesce()`), the
aggregates `count(*)`/`count(expr)`/`avg()`/`min()`/`max()`/`sum()`, scalar
subqueries, correlated subqueries and `EXISTS`. Semantics match SQLite (the
reference the sqllogictest expectations are generated from): INTEGER division
truncates toward zero, NULL propagates, `AND/OR/NOT` use three-valued logic,
`avg()` ignores NULLs and returns REAL, NULLs sort first ascending.

## Secrets

None. No credentials, tokens, or connection strings are committed anywhere in
this repository; the engine is pure stdlib Python with no network access. The
vendored third-party tree is kept minimal specifically to avoid shipping files
that trigger secret scanners (verified with gitleaks and detect-secrets: 0
findings).
