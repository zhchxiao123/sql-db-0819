"""Unit tests for the sql-db-0819 engine.

These target engine behavior (create/insert/query parsing & execution,
expression semantics) — not just the sqllogictest harness plumbing.
Run with:  python3 -m unittest discover -s tests -v
"""

import subprocess
import sys
import unittest
from pathlib import Path

from sql_db_0819.engine import Database, EngineError

REPO_ROOT = Path(__file__).resolve().parent.parent


def q(db, sql):
    """Run a query and return the raw row-major value list."""
    return db.execute_query(sql)


class TestCreateInsertSelect(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement(
            'CREATE TABLE t1(a INTEGER, b INTEGER, c INTEGER, d INTEGER, e INTEGER)')

    def test_insert_with_reordered_columns_and_select_where(self):
        self.db.execute_statement('INSERT INTO t1(e,c,b,d,a) VALUES(103,102,100,101,104)')
        self.db.execute_statement('INSERT INTO t1(a,c,d,e,b) VALUES(107,106,108,109,105)')
        rows = self.db.execute_query('SELECT a FROM t1 WHERE b > 101 ORDER BY 1')
        self.assertEqual(rows, [[107]])

    def test_select_returns_rows_in_insertion_order(self):
        self.db.execute_statement('INSERT INTO t1(a,b,c,d,e) VALUES(1,2,3,4,5)')
        self.db.execute_statement('INSERT INTO t1(a,b,c,d,e) VALUES(6,7,8,9,10)')
        rows = self.db.execute_query('SELECT a, e FROM t1')
        self.assertEqual(rows, [[1, 5], [6, 10]])

    def test_select_without_from(self):
        rows = self.db.execute_query('SELECT 1 + 2')
        self.assertEqual(rows, [[3]])

    def test_unknown_table_raises(self):
        with self.assertRaises(EngineError):
            self.db.execute_query('SELECT a FROM nope')


class TestExpressions(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_integer_division_truncates_toward_zero(self):
        self.assertEqual(q(self.db, 'SELECT 7/2'), [[3]])
        self.assertEqual(q(self.db, 'SELECT -7/2'), [[-3]])
        self.assertEqual(q(self.db, 'SELECT 7/-2'), [[-3]])

    def test_arithmetic_precedence(self):
        self.assertEqual(q(self.db, 'SELECT 1+2*3'), [[7]])
        self.assertEqual(q(self.db, 'SELECT (1+2)*3'), [[9]])

    def test_case_searched_and_simple(self):
        self.assertEqual(q(self.db, 'SELECT CASE WHEN 1<2 THEN 111 ELSE 222 END'), [[111]])
        self.assertEqual(q(self.db, 'SELECT CASE 2 WHEN 1 THEN 111 WHEN 2 THEN 222 ELSE 333 END'),
                         [[222]])
        self.assertEqual(q(self.db, 'SELECT CASE 9 WHEN 1 THEN 111 WHEN 2 THEN 222 ELSE 333 END'),
                         [[333]])

    def test_between_and_not_between(self):
        self.assertEqual(q(self.db, 'SELECT 5 BETWEEN 1 AND 10'), [[1]])
        self.assertEqual(q(self.db, 'SELECT 15 BETWEEN 1 AND 10'), [[0]])
        self.assertEqual(q(self.db, 'SELECT 15 NOT BETWEEN 1 AND 10'), [[1]])

    def test_is_null_and_coalesce(self):
        self.assertEqual(q(self.db, 'SELECT NULL IS NULL'), [[1]])
        self.assertEqual(q(self.db, 'SELECT 1 IS NULL'), [[0]])
        self.assertEqual(q(self.db, 'SELECT 1 IS NOT NULL'), [[1]])
        self.assertEqual(q(self.db, 'SELECT coalesce(NULL, 5, 7)'), [[5]])
        self.assertEqual(q(self.db, 'SELECT coalesce(NULL, NULL)'), [[None]])

    def test_abs(self):
        self.assertEqual(q(self.db, 'SELECT abs(-7), abs(7), abs(NULL)'), [[7, 7, None]])

    def test_comparison_ops(self):
        self.assertEqual(q(self.db, 'SELECT 1<2, 2<=2, 3>2, 3>=4, 5=5, 5<>6, 5!=6'),
                         [[1, 1, 1, 0, 1, 1, 1]])


class TestNullSemantics(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_null_propagates_through_arithmetic(self):
        self.assertEqual(q(self.db, 'SELECT NULL + 1'), [[None]])
        self.assertEqual(q(self.db, 'SELECT 1 * NULL'), [[None]])
        self.assertEqual(q(self.db, 'SELECT abs(NULL)'), [[None]])

    def test_null_comparison_is_null(self):
        self.assertEqual(q(self.db, 'SELECT NULL < 5'), [[None]])
        self.assertEqual(q(self.db, 'SELECT NULL = NULL'), [[None]])

    def test_three_valued_logic(self):
        self.assertEqual(q(self.db, 'SELECT NULL AND 0'), [[0]])
        self.assertEqual(q(self.db, 'SELECT NULL AND 1'), [[None]])
        self.assertEqual(q(self.db, 'SELECT NULL AND NULL'), [[None]])
        self.assertEqual(q(self.db, 'SELECT NULL OR 1'), [[1]])
        self.assertEqual(q(self.db, 'SELECT NULL OR 0'), [[None]])
        self.assertEqual(q(self.db, 'SELECT NOT NULL'), [[None]])
        self.assertEqual(q(self.db, 'SELECT NOT 0, NOT 5'), [[1, 0]])

    def test_null_not_between_is_excluded_in_where(self):
        db = Database()
        db.execute_statement('CREATE TABLE t1(a INTEGER, d INTEGER)')
        db.execute_statement('INSERT INTO t1(a, d) VALUES(1, 200)')
        db.execute_statement('INSERT INTO t1(a, d) VALUES(2, NULL)')
        # d NOT BETWEEN ... must NOT match rows where d IS NULL
        rows = db.execute_query('SELECT a FROM t1 WHERE d NOT BETWEEN 110 AND 150')
        self.assertEqual(rows, [[1]])


class TestAggregatesAndSubqueries(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement('CREATE TABLE t1(a INTEGER, b INTEGER, c INTEGER)')
        self.db.execute_statement('INSERT INTO t1(a,b,c) VALUES(10,1,100)')
        self.db.execute_statement('INSERT INTO t1(a,b,c) VALUES(20,2,NULL)')
        self.db.execute_statement('INSERT INTO t1(a,b,c) VALUES(30,3,300)')

    def test_count_and_avg(self):
        self.assertEqual(q(self.db, 'SELECT count(*) FROM t1'), [[3]])
        self.assertEqual(q(self.db, 'SELECT count(c) FROM t1'), [[2]])  # NULL c ignored
        self.assertEqual(q(self.db, 'SELECT avg(a) FROM t1'), [[20.0]])
        self.assertEqual(q(self.db, 'SELECT avg(c) FROM t1'), [[200.0]])  # NULL ignored

    def test_aggregate_over_empty_set(self):
        self.assertEqual(q(self.db, 'SELECT count(*) FROM t1 WHERE a > 100'), [[0]])
        self.assertEqual(q(self.db, 'SELECT avg(a) FROM t1 WHERE a > 100'), [[None]])

    def test_scalar_subquery(self):
        self.assertEqual(q(self.db, 'SELECT (SELECT avg(a) FROM t1) > 15'), [[1]])

    def test_correlated_count_subquery(self):
        rows = self.db.execute_query(
            'SELECT a, (SELECT count(*) FROM t1 AS x WHERE x.b<t1.b) FROM t1 ORDER BY 1')
        # row a=10 has 0 rows with b<1; a=20 has 1 (b=1); a=30 has 2 (b=1,2)
        self.assertEqual(rows, [[10, 0], [20, 1], [30, 2]])

    def test_exists_correlated_subquery(self):
        rows = self.db.execute_query(
            'SELECT a FROM t1 WHERE EXISTS(SELECT 1 FROM t1 AS x WHERE x.b<t1.b) ORDER BY 1')
        self.assertEqual(rows, [[20], [30]])

    def test_alias_replaces_table_name_in_subquery(self):
        # inside the subquery, t1 refers to the OUTER table (inner is x)
        rows = self.db.execute_query(
            'SELECT (SELECT count(*) FROM t1 AS x WHERE x.b < t1.b) FROM t1 WHERE a = 30')
        self.assertEqual(rows, [[2]])


class TestOrderBy(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement('CREATE TABLE t1(a INTEGER, b INTEGER)')
        self.db.execute_statement('INSERT INTO t1(a,b) VALUES(2, NULL)')
        self.db.execute_statement('INSERT INTO t1(a,b) VALUES(1, 5)')
        self.db.execute_statement('INSERT INTO t1(a,b) VALUES(3, 5)')

    def test_order_by_positional_and_stable(self):
        self.assertEqual(self.db.execute_query('SELECT a FROM t1 ORDER BY 1'), [[1], [2], [3]])
        # tie on b=5 keeps insertion order: a=1 before a=3; NULLs first
        self.assertEqual(self.db.execute_query('SELECT a, b FROM t1 ORDER BY 2, 1'),
                         [[2, None], [1, 5], [3, 5]])
        with self.assertRaises(EngineError):
            self.db.execute_query('SELECT a FROM t1 ORDER BY 2')

    def test_null_first_in_ascending(self):
        rows = self.db.execute_query('SELECT b FROM t1 ORDER BY 1')
        self.assertEqual(rows, [[None], [5], [5]])


class TestCliProtocol(unittest.TestCase):
    """Smoke test of the wire protocol the runner uses (harness plumbing)."""

    def run_cli(self, frames):
        req = b''
        for kind, types, sql in frames:
            b = sql.encode()
            if types:
                req += f'{kind} {types} {len(b)}\n'.encode() + b
            else:
                req += f'{kind} {len(b)}\n'.encode() + b
        req += b'X\n'
        p = subprocess.run(
            [sys.executable, str(REPO_ROOT / 'sql_db_0819' / 'cli.py')],
            input=req, capture_output=True, timeout=30)
        return p.stdout.decode().splitlines()

    def test_statement_and_query_frames(self):
        lines = self.run_cli([
            ('S', None, 'CREATE TABLE t1(a INTEGER, b INTEGER)'),
            ('S', None, 'INSERT INTO t1(a,b) VALUES(1,2)'),
            ('Q', 'II', 'SELECT a+b, a FROM t1'),
        ])
        self.assertEqual(lines[0], 'READY')
        self.assertEqual(lines[1:4], ['OK', 'OK', 'OK 2'])
        self.assertEqual(lines[4:6], ['3', '1'])
        self.assertEqual(lines[-1], 'BYE')

    def test_error_frame_on_bad_sql(self):
        lines = self.run_cli([('S', None, 'GARBAGE')])
        self.assertTrue(any(l.startswith('ERR ') for l in lines))


if __name__ == '__main__':
    unittest.main()
