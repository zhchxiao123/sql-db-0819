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


class TestExtendedOperators(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_modulo(self):
        self.assertEqual(q(self.db, 'SELECT 7 % 3'), [[1]])
        self.assertEqual(q(self.db, 'SELECT -7 % 3'), [[-1]])  # sign of dividend
        self.assertEqual(q(self.db, 'SELECT 7 % 0'), [[None]])

    def test_concat(self):
        self.assertEqual(q(self.db, "SELECT 'a' || 'b'"), [['ab']])
        self.assertEqual(q(self.db, "SELECT 1 || 2"), [['12']])
        self.assertEqual(q(self.db, "SELECT 'v' || NULL"), [[None]])
        self.assertEqual(q(self.db, "SELECT 1.5 || 'x'"), [['1.5x']])

    def test_div_operator(self):
        self.assertEqual(q(self.db, 'SELECT 20 DIV 6'), [[3]])
        self.assertEqual(q(self.db, 'SELECT -20 DIV 6'), [[-3]])
        self.assertEqual(q(self.db, 'SELECT 20 DIV 0'), [[None]])

    def test_like(self):
        self.assertEqual(q(self.db, "SELECT 'abc' LIKE 'a%'"), [[1]])
        self.assertEqual(q(self.db, "SELECT 'abc' LIKE 'A%'"), [[1]])  # case-insensitive
        self.assertEqual(q(self.db, "SELECT 'abc' LIKE 'a_c'"), [[1]])
        self.assertEqual(q(self.db, "SELECT 'abc' LIKE 'b%'"), [[0]])
        self.assertEqual(q(self.db, "SELECT NULL LIKE '%'"), [[None]])
        self.assertEqual(q(self.db, "SELECT 'abc' NOT LIKE 'b%'"), [[1]])

    def test_division_by_zero_is_null(self):
        self.assertEqual(q(self.db, 'SELECT 1/0'), [[None]])
        self.assertEqual(q(self.db, 'SELECT 1.0/0'), [[None]])


class TestCast(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_cast_to_integer(self):
        self.assertEqual(q(self.db, "SELECT CAST('123' AS INTEGER)"), [[123]])
        self.assertEqual(q(self.db, "SELECT CAST('12.9' AS INTEGER)"), [[12]])
        self.assertEqual(q(self.db, "SELECT CAST('abc' AS INTEGER)"), [[0]])
        self.assertEqual(q(self.db, "SELECT CAST(' -12 ' AS INTEGER)"), [[-12]])
        self.assertEqual(q(self.db, 'SELECT CAST(1.5 AS INTEGER)'), [[1]])
        self.assertEqual(q(self.db, 'SELECT CAST(-1.5 AS INTEGER)'), [[-1]])
        self.assertEqual(q(self.db, 'SELECT CAST(NULL AS INTEGER)'), [[None]])
        self.assertEqual(q(self.db, "SELECT CAST('5' AS SIGNED)"), [[5]])

    def test_cast_to_real(self):
        self.assertEqual(q(self.db, "SELECT CAST('12.5' AS REAL)"), [[12.5]])
        self.assertEqual(q(self.db, "SELECT CAST('abc' AS REAL)"), [[0.0]])
        self.assertEqual(q(self.db, 'SELECT CAST(3 AS REAL)'), [[3.0]])

    def test_cast_to_text(self):
        self.assertEqual(q(self.db, 'SELECT CAST(123 AS TEXT)'), [['123']])
        self.assertEqual(q(self.db, 'SELECT CAST(1.5 AS TEXT)'), [['1.5']])
        self.assertEqual(q(self.db, 'SELECT CAST(NULL AS TEXT)'), [[None]])

    def test_cast_in_where(self):
        db = Database()
        db.execute_statement('CREATE TABLE t1(a INTEGER, c TEXT)')
        db.execute_statement("INSERT INTO t1(a, c) VALUES(1, '123')")
        db.execute_statement("INSERT INTO t1(a, c) VALUES(2, '45')")
        rows = db.execute_query('SELECT a FROM t1 WHERE CAST(c AS INTEGER) > 50 ORDER BY 1')
        self.assertEqual(rows, [[1]])


class TestInOperator(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_in_list(self):
        self.assertEqual(q(self.db, 'SELECT 2 IN (1, 2, 3)'), [[1]])
        self.assertEqual(q(self.db, 'SELECT 9 IN (1, 2, 3)'), [[0]])
        self.assertEqual(q(self.db, 'SELECT 3 NOT IN (1, 2)'), [[1]])

    def test_in_null_semantics(self):
        self.assertEqual(q(self.db, 'SELECT NULL IN (1, 2)'), [[None]])
        self.assertEqual(q(self.db, 'SELECT 1 IN (1, 2, NULL)'), [[1]])
        self.assertEqual(q(self.db, 'SELECT 9 IN (1, 2, NULL)'), [[None]])
        self.assertEqual(q(self.db, 'SELECT NULL NOT IN (1, 2)'), [[None]])

    def test_in_with_expressions(self):
        self.assertEqual(q(self.db, 'SELECT 2 + 3 IN (4, 5, 6)'), [[1]])


class TestDistinct(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement('CREATE TABLE t1(a INTEGER, b INTEGER, c TEXT)')
        for row in [(1, 10, 'x'), (1, 10, 'x'), (1, 20, 'y'), (2, 20, 'y'),
                    (2, 30, None), (None, 30, 'z'), (None, None, 'z')]:
            self.db.execute_statement(
                "INSERT INTO t1(a,b,c) VALUES(%s,%s,%s)" % (
                    'NULL' if row[0] is None else str(row[0]),
                    'NULL' if row[1] is None else str(row[1]),
                    'NULL' if row[2] is None else "'" + row[2] + "'"))

    def test_distinct_single_and_multi_column(self):
        self.assertEqual(q(self.db, 'SELECT DISTINCT a FROM t1'), [[1], [2], [None]])
        self.assertEqual(q(self.db, 'SELECT DISTINCT a, b FROM t1'),
                         [[1, 10], [1, 20], [2, 20], [2, 30], [None, 30], [None, None]])
        self.assertEqual(q(self.db, "SELECT DISTINCT c FROM t1"), [['x'], ['y'], [None], ['z']])

    def test_distinct_with_order_by(self):
        self.assertEqual(q(self.db, 'SELECT DISTINCT b FROM t1 ORDER BY b'), [[None], [10], [20], [30]])
        self.assertEqual(q(self.db, 'SELECT DISTINCT b FROM t1 ORDER BY b DESC'),
                         [[30], [20], [10], [None]])

    def test_count_distinct(self):
        self.assertEqual(q(self.db, 'SELECT COUNT(DISTINCT a) FROM t1'), [[2]])
        self.assertEqual(q(self.db, 'SELECT COUNT(DISTINCT b) FROM t1'), [[3]])


class TestLimitOffset(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement('CREATE TABLE t1(a INTEGER)')
        for v in [5, 1, 4, 2, 6, 3]:
            self.db.execute_statement('INSERT INTO t1(a) VALUES(%d)' % v)

    def test_limit_forms(self):
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY 1 LIMIT 3'), [[1], [2], [3]])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY 1 LIMIT 3 OFFSET 2'),
                         [[3], [4], [5]])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY 1 LIMIT 2, 3'),
                         [[3], [4], [5]])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY 1 LIMIT 0'), [])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY 1 LIMIT 10'),
                         [[1], [2], [3], [4], [5], [6]])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY 1 LIMIT 10 OFFSET 10'), [])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY 1 LIMIT -1'),
                         [[1], [2], [3], [4], [5], [6]])

    def test_limit_after_where(self):
        self.assertEqual(q(self.db, 'SELECT a FROM t1 WHERE a > 2 ORDER BY 1 LIMIT 2'),
                         [[3], [4]])


class TestOrderByExtended(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement('CREATE TABLE t1(a INTEGER, b INTEGER, c TEXT)')
        self.db.execute_statement("INSERT INTO t1(a,b,c) VALUES(3,30,'thirty')")
        self.db.execute_statement("INSERT INTO t1(a,b,c) VALUES(1,NULL,'one')")
        self.db.execute_statement("INSERT INTO t1(a,b,c) VALUES(2,20,'two')")
        self.db.execute_statement("INSERT INTO t1(a,b,c) VALUES(NULL,10,'null-a')")

    def test_asc_desc(self):
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY a'), [[None], [1], [2], [3]])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY a DESC'), [[3], [2], [1], [None]])
        self.assertEqual(q(self.db, 'SELECT a, b FROM t1 ORDER BY 1 DESC, 2 ASC'),
                         [[3, 30], [2, 20], [1, None], [None, 10]])

    def test_nulls_first_last(self):
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY a NULLS FIRST'),
                         [[None], [1], [2], [3]])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY a NULLS LAST'),
                         [[1], [2], [3], [None]])
        self.assertEqual(q(self.db, 'SELECT a FROM t1 ORDER BY a DESC NULLS FIRST'),
                         [[None], [3], [2], [1]])

    def test_order_by_alias(self):
        self.assertEqual(q(self.db, 'SELECT a AS col1 FROM t1 ORDER BY col1 DESC'),
                         [[3], [2], [1], [None]])

    def test_order_by_expression(self):
        self.assertEqual(q(self.db, 'SELECT a + b AS s FROM t1 WHERE a IS NOT NULL AND b IS NOT NULL ORDER BY s'),
                         [[22], [33]])


class TestMultiTableJoin(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement('CREATE TABLE t1(a1 INTEGER, b1 INTEGER, x1 VARCHAR(30))')
        self.db.execute_statement('CREATE TABLE t2(a2 INTEGER, b2 INTEGER, x2 VARCHAR(30))')
        self.db.execute_statement("INSERT INTO t1 VALUES(1, 10, 'one')")
        self.db.execute_statement("INSERT INTO t1 VALUES(2, 20, 'two')")
        self.db.execute_statement("INSERT INTO t2 VALUES(1, 100, 'hundred')")
        self.db.execute_statement("INSERT INTO t2 VALUES(3, 300, 'three-hundred')")

    def test_cartesian_with_where(self):
        rows = q(self.db, 'SELECT t1.a1, t2.a2 FROM t1, t2 ORDER BY 1, 2')
        self.assertEqual(rows, [[1, 1], [1, 3], [2, 1], [2, 3]])

    def test_equi_join(self):
        rows = q(self.db, 'SELECT t1.a1, t2.b2 FROM t1, t2 WHERE t1.a1 = t2.a2 ORDER BY 1')
        self.assertEqual(rows, [[1, 100]])

    def test_select_star(self):
        rows = q(self.db, 'SELECT * FROM t1 ORDER BY 1')
        self.assertEqual(rows, [[1, 10, 'one'], [2, 20, 'two']])

    def test_qualified_star(self):
        rows = q(self.db, 'SELECT t1.*, t2.a2 FROM t1, t2 WHERE t1.a1 = t2.a2')
        self.assertEqual(rows, [[1, 10, 'one', 1]])

    def test_unqualified_unique_column(self):
        # b1 only exists in t1; unqualified reference resolves
        rows = q(self.db, 'SELECT a2 FROM t1, t2 WHERE b1 = 10')
        self.assertEqual(rows, [[1], [3]])


class TestCompoundSelect(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement('CREATE TABLE t1(a INTEGER)')
        self.db.execute_statement('CREATE TABLE t2(a INTEGER)')
        for v in [1, 2, 3]:
            self.db.execute_statement('INSERT INTO t1(a) VALUES(%d)' % v)
        for v in [2, 3, 4]:
            self.db.execute_statement('INSERT INTO t2(a) VALUES(%d)' % v)

    def test_union(self):
        self.assertEqual(q(self.db, 'SELECT a FROM t1 UNION SELECT a FROM t2 ORDER BY 1'),
                         [[1], [2], [3], [4]])

    def test_union_all(self):
        rows = q(self.db, 'SELECT a FROM t1 UNION ALL SELECT a FROM t2 ORDER BY 1')
        self.assertEqual(rows, [[1], [2], [2], [3], [3], [4]])

    def test_except(self):
        self.assertEqual(q(self.db, 'SELECT a FROM t1 EXCEPT SELECT a FROM t2'), [[1]])
        self.assertEqual(q(self.db, 'SELECT a FROM t2 EXCEPT SELECT a FROM t1'), [[4]])

    def test_intersect(self):
        self.assertEqual(q(self.db, 'SELECT a FROM t1 INTERSECT SELECT a FROM t2'),
                         [[2], [3]])

    def test_compound_chain(self):
        self.assertEqual(
            q(self.db, 'SELECT a FROM t1 UNION SELECT a FROM t2 EXCEPT SELECT 3'),
            [[1], [2], [4]])


class TestAggregatesInExpressions(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute_statement('CREATE TABLE t1(a INTEGER)')
        for v in [10, 20, 30]:
            self.db.execute_statement('INSERT INTO t1(a) VALUES(%d)' % v)

    def test_aggregate_inside_arithmetic(self):
        self.assertEqual(q(self.db, 'SELECT COUNT(*) + 1 FROM t1'), [[4]])
        self.assertEqual(q(self.db, 'SELECT SUM(a) / COUNT(*) FROM t1'), [[20]])

    def test_aggregate_inside_case_and_functions(self):
        self.assertEqual(q(self.db, 'SELECT CASE WHEN COUNT(*) > 2 THEN 111 ELSE 222 END FROM t1'),
                         [[111]])
        self.assertEqual(q(self.db, 'SELECT NULLIF(SUM(a), 60) FROM t1'), [[None]])
        self.assertEqual(q(self.db, 'SELECT COALESCE(SUM(a), 0) FROM t1'), [[60]])
        self.assertEqual(q(self.db, 'SELECT COUNT(*) IN (3, 4) FROM t1'), [[1]])

    def test_count_all_and_distinct_forms(self):
        self.assertEqual(q(self.db, 'SELECT COUNT(ALL a) FROM t1'), [[3]])
        self.assertEqual(q(self.db, 'SELECT COUNT(DISTINCT a) FROM t1'), [[3]])
        self.assertEqual(q(self.db, 'SELECT COUNT(*) FROM t1'), [[3]])


class TestCreateIndexNoop(unittest.TestCase):
    def test_create_and_drop_index_are_accepted(self):
        db = Database()
        db.execute_statement('CREATE TABLE t1(a INTEGER, b INTEGER)')
        db.execute_statement('CREATE INDEX t1i ON t1(a, b)')
        db.execute_statement('CREATE UNIQUE INDEX t1u ON t1(a)')
        db.execute_statement('DROP INDEX t1i')
        db.execute_statement('INSERT INTO t1 VALUES(1, 2)')
        self.assertEqual(q(db, 'SELECT a FROM t1'), [[1]])

    def test_typed_columns_and_primary_key(self):
        db = Database()
        db.execute_statement('CREATE TABLE t5(a5 INTEGER PRIMARY KEY, b5 INTEGER, x5 VARCHAR(40))')
        db.execute_statement("INSERT INTO t5 VALUES(1, 11, 'row one')")
        rows = db.execute_query("SELECT x5 FROM t5 WHERE a5 = 1")
        self.assertEqual(rows, [['row one']])
