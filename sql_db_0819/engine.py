"""sql_db_0819.engine — a minimal SQL engine (extended expression evaluation).

Driven end-to-end by the official sqllogictest runner (see README.md). This
slice extends the engine to full expression evaluation so the pinned
sqllogictest corpus (select1-5, orderby/limit/distinct/cast/null anchors,
random/expr) is green:

* Statements: CREATE TABLE (typed columns, sizes, PRIMARY KEY constraints),
  INSERT INTO ... VALUES, SELECT.
* SELECT: [ALL|DISTINCT], expression list with column aliases, `*` / `t.*`,
  multi-table FROM (comma joins with predicate pushdown + hash-join
  constraints), WHERE, ORDER BY (position / alias / expression, ASC/DESC,
  NULL ordering), LIMIT/OFFSET.
* Expressions: literals, column refs (qualified/unqualified), arithmetic
  (+ - * / % DIV), || concatenation, comparisons (= != < <= > >=),
  AND/OR/NOT (three-valued), BETWEEN / NOT BETWEEN, IS [NOT] NULL,
  IN / NOT IN (lists and subqueries), LIKE, CASE (searched and simple),
  CAST(expr AS type), scalar functions (abs, coalesce, nullif, ifnull) and
  aggregates anywhere in the tree (count, sum, avg, min, max, with ALL /
  DISTINCT / * forms).

Semantics deliberately follow SQLite (the reference the sqllogictest
expected values are generated from):

* INTEGER division (and DIV) truncates toward zero; % takes the sign of the
  dividend; || concatenates with NULL propagation.
* NULL propagates through arithmetic and comparisons; AND/OR/NOT use
  three-valued logic; avg() ignores NULLs and returns REAL; count(*)
  counts rows; count(DISTINCT x) counts distinct non-NULL values.
* Comparisons follow SQLite type ordering (NULL < number < text) with column
  affinity applied to TEXT operands; ORDER BY puts NULLs first ascending.
* Scalar subqueries return NULL when empty; aggregates over zero rows:
  count=0, sum/avg/min/max=NULL.
* A table alias replaces the table name for qualified references.
* SELECT DISTINCT deduplicates result rows; aggregate queries (no GROUP BY)
  compute every aggregate once over the filtered row set and fold it in.

The module is pure stdlib. It never touches the network or the filesystem.
"""

from __future__ import annotations

import functools
import math
import re

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EngineError(Exception):
    """Raised for any SQL parse/execute error. Message must be single-line."""


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


def tokenize(sql):
    """Split SQL text into (kind, value) tokens.

    kind is one of 'id', 'num', 'str', 'op'. Comments ('--' line, '/* */'
    block) and whitespace are skipped.
    """
    toks = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if c == '-' and i + 1 < n and sql[i + 1] == '-':
            j = sql.find('\n', i)
            i = n if j == -1 else j + 1
            continue
        if c == '/' and i + 1 < n and sql[i + 1] == '*':
            j = sql.find('*/', i + 2)
            i = n if j == -1 else j + 2
            continue
        if c.isdigit():
            j = i
            while j < n and (sql[j].isdigit() or sql[j] == '.'):
                j += 1
            toks.append(('num', sql[i:j]))
            i = j
            continue
        if c == "'":
            j = i + 1
            buf = []
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(sql[j])
                j += 1
            toks.append(('str', ''.join(buf)))
            i = j
            continue
        if c.isalpha() or c == '_':
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == '_'):
                j += 1
            toks.append(('id', sql[i:j]))
            i = j
            continue
        two = sql[i:i + 2]
        if two in ('<=', '>=', '<>', '!=', '||'):
            toks.append(('op', two))
            i += 2
            continue
        if c in '()[],;+-*/%<>=.':
            toks.append(('op', c))
            i += 1
            continue
        raise EngineError('unexpected character: %r' % c)
    return toks


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

AGG_FUNCS = frozenset({'count', 'sum', 'avg', 'min', 'max'})


def type_affinity(typename):
    t = typename.upper()
    if 'INT' in t:
        return 'INTEGER'
    if 'CHAR' in t or 'CLOB' in t or t == 'TEXT':
        return 'TEXT'
    if t in ('REAL', 'FLOA', 'DOUB') or t.startswith('DOUBLE'):
        return 'REAL'
    if 'DEC' in t or t in ('NUMERIC', 'BOOLEAN', 'DATE', 'DATETIME',
                           'SIGNED', 'UNSIGNED'):
        return 'NUMERIC'
    return 'NUMERIC'


class CreateTable:
    __slots__ = ('name', 'columns')

    def __init__(self, name, columns):
        self.name = name.lower()
        self.columns = columns  # list of (name, affinity)


class Insert:
    __slots__ = ('table', 'colnames', 'tuples')

    def __init__(self, table, colnames, tuples):
        self.table = table.lower()
        self.colnames = [c.lower() for c in colnames] if colnames is not None else None
        self.tuples = tuples


class NoOp:
    """A parsed statement with no effect (CREATE INDEX, DROP INDEX, ...)."""
    __slots__ = ()


class Compound:
    """Compound SELECT: parts is a list of (op, select); op None for first."""

    __slots__ = ('parts', 'aliases', 'order_by', 'limit', 'offset')

    def __init__(self, parts, aliases, order_by, limit, offset):
        self.parts = parts
        self.aliases = aliases
        self.order_by = order_by
        self.limit = limit
        self.offset = offset


class Select:
    __slots__ = ('columns', 'aliases', 'distinct', 'tables', 'where',
                 'order_by', 'limit', 'offset')

    def __init__(self, columns, aliases, distinct, tables, where, order_by,
                 limit, offset):
        self.columns = columns          # list of expr nodes or ('star', qual)
        self.aliases = aliases          # list of alias names (or None)
        self.distinct = distinct
        self.tables = tables            # list of (table_name, alias)
        self.where = where
        self.order_by = order_by        # list of (key, asc_flag)
        self.limit = limit              # int or None
        self.offset = offset            # int or None


class Table:
    """In-memory table: column names, affinities, rows (dict col->value)."""

    __slots__ = ('colnames', 'affinity', 'rows')

    def __init__(self, colnames, affinity):
        self.colnames = colnames
        self.affinity = affinity
        self.rows = []


class Frame:
    """One scope of name resolution during evaluation.

    table: table name or None (SELECT without FROM)
    alias: alias or None
    row:   current row dict (mutated per-row while scanning), or None
    """

    __slots__ = ('table', 'alias', 'row')

    def __init__(self, table, alias, row=None):
        self.table = table
        self.alias = alias
        self.row = row


class Ctx:
    """Evaluation context: the database plus the frame stack."""

    __slots__ = ('db', 'env', 'agg_values')

    def __init__(self, db):
        self.db = db
        self.env = []
        self.agg_values = {}  # id(agg_node) -> computed constant


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

CLAUSE_KWS = frozenset({'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT',
                        'UNION', 'EXCEPT', 'INTERSECT', 'ALL', 'AS', 'ASC',
                        'DESC', 'OFFSET', 'JOIN', 'ON', 'INNER', 'LEFT',
                        'RIGHT', 'CROSS', 'FULL', 'NATURAL'})


def is_clause_kw(word):
    return word.upper() in CLAUSE_KWS


class Parser:
    __slots__ = ('toks', 'pos')

    def __init__(self, toks):
        self.toks = toks
        self.pos = 0

    def peek(self, k=0):
        i = self.pos + k
        return self.toks[i] if i < len(self.toks) else None

    def next(self):
        t = self.toks[self.pos] if self.pos < len(self.toks) else None
        self.pos += 1
        return t

    def is_op(self, op, k=0):
        t = self.peek(k)
        return t is not None and t[0] == 'op' and t[1] == op

    def is_id(self, word, k=0):
        t = self.peek(k)
        return t is not None and t[0] == 'id' and t[1].upper() == word

    def expect_op(self, op):
        t = self.next()
        if t != ('op', op):
            raise EngineError('expected %r got %r' % (op, t))

    def expect_id(self, word=None):
        t = self.next()
        if t is None or t[0] != 'id':
            raise EngineError('expected identifier got %r' % (t,))
        if word is not None and t[1].upper() != word:
            raise EngineError('expected %s got %r' % (word, t))
        return t[1]


def parse_statement(p):
    if p.is_id('CREATE'):
        p.next()
        if p.is_id('INDEX') or p.is_id('UNIQUE'):
            # CREATE [UNIQUE] INDEX ... ON ... (...) — parse and ignore
            if p.is_id('UNIQUE'):
                p.next()
            p.expect_id('INDEX')
            if p.is_id('IF'):
                p.next()
                p.expect_id('NOT')
                p.expect_id('EXISTS')
            p.expect_id()  # index name
            p.expect_id('ON')
            p.expect_id()  # table name
            p.expect_op('(')
            depth = 1
            while depth > 0:
                t = p.next()
                if t is None:
                    raise EngineError('unterminated index definition')
                if t[0] == 'op' and t[1] == '(':
                    depth += 1
                elif t[0] == 'op' and t[1] == ')':
                    depth -= 1
            return NoOp()
        p.expect_id('TABLE')
        name = p.expect_id()
        p.expect_op('(')
        cols = []
        while True:
            cname = p.expect_id()
            ctype = p.expect_id()
            if p.is_op('('):
                p.next()
                t = p.next()
                if t is None or t[0] != 'num':
                    raise EngineError('expected size after type')
                p.expect_op(')')
            # skip column constraints (PRIMARY KEY, NOT NULL, UNIQUE, ...)
            while not p.is_op(',') and not p.is_op(')'):
                t = p.next()
                if t is None:
                    raise EngineError('unterminated column definition')
                if t[0] == 'op' and t[1] == '(':
                    depth = 1
                    while depth > 0:
                        t2 = p.next()
                        if t2 is None:
                            raise EngineError('unterminated column definition')
                        if t2[0] == 'op' and t2[1] == '(':
                            depth += 1
                        elif t2[0] == 'op' and t2[1] == ')':
                            depth -= 1
            cols.append((cname.lower(), type_affinity(ctype)))
            if p.is_op(','):
                p.next()
                continue
            break
        p.expect_op(')')
        return CreateTable(name, cols)
    if p.is_id('INSERT'):
        p.next()
        p.expect_id('INTO')
        name = p.expect_id()
        colnames = None
        if p.is_op('('):
            p.next()
            colnames = []
            while True:
                colnames.append(p.expect_id())
                if p.is_op(','):
                    p.next()
                    continue
                break
            p.expect_op(')')
        p.expect_id('VALUES')
        tuples = []
        while True:
            p.expect_op('(')
            vals = []
            while True:
                vals.append(parse_expr(p))
                if p.is_op(','):
                    p.next()
                    continue
                break
            p.expect_op(')')
            tuples.append(vals)
            if p.is_op(','):
                p.next()
                continue
            break
        return Insert(name, colnames, tuples)
    if p.is_id('DROP'):
        p.next()
        if p.is_id('INDEX'):
            p.next()  # consume INDEX
            if p.is_id('IF'):
                p.next()
                p.expect_id('EXISTS')
            p.expect_id()  # index name
            return NoOp()
        if p.is_id('TABLE') or p.is_id('VIEW') or p.is_id('TRIGGER'):
            p.next()  # consume TABLE/VIEW/TRIGGER
            if p.is_id('IF'):
                p.next()
                p.expect_id('EXISTS')
            p.expect_id()  # object name
            return NoOp()
        raise EngineError('unsupported DROP statement')
    if p.is_id('SELECT'):
        return parse_select_stmt(p)
    raise EngineError('unsupported statement')


def parse_select_stmt(p):
    """Parse a (possibly compound) SELECT statement."""
    parts = [(None, parse_select(p))]
    while p.is_id('UNION') or p.is_id('EXCEPT') or p.is_id('INTERSECT'):
        if p.is_id('UNION'):
            p.next()
            all_flag = False
            if p.is_id('ALL'):
                p.next()
                all_flag = True
            op = ('union', all_flag)
        elif p.is_id('EXCEPT'):
            p.next()
            op = ('except', False)
        else:
            p.next()
            op = ('intersect', False)
        parts.append((op, parse_select(p)))
    if len(parts) == 1:
        return parts[0][1]
    # lift trailing ORDER BY / LIMIT from the last branch to the compound
    order_by = None
    limit = None
    offset = None
    last_sel = parts[-1][1]
    if last_sel.order_by is not None or last_sel.limit is not None:
        order_by = last_sel.order_by
        limit = last_sel.limit
        offset = last_sel.offset
        last_sel.order_by = None
        last_sel.limit = None
        last_sel.offset = None
    return Compound(parts, parts[0][1].aliases, order_by, limit, offset)


def parse_select(p):
    p.next()  # SELECT
    distinct = False
    if p.is_id('ALL'):
        p.next()
    elif p.is_id('DISTINCT'):
        p.next()
        distinct = True
    columns = []
    aliases = []
    while True:
        if p.is_op('*'):
            p.next()
            columns.append(('star', None))
            aliases.append(None)
        elif p.peek() and p.peek()[0] == 'id' and p.is_op('.', 1):
            # qualified reference: t1.* (star) or t1.a1 (column, parsed by parse_expr)
            save = p.pos
            qual = p.next()[1]
            p.next()  # '.'
            t = p.next()
            if t is not None and t[0] == 'op' and t[1] == '*':
                columns.append(('star', qual.lower()))
                aliases.append(None)
            else:
                p.pos = save
                columns.append(parse_expr(p))
                alias = None
                if p.is_id('AS'):
                    p.next()
                    alias = p.expect_id()
                elif p.peek() and p.peek()[0] == 'id' and not is_clause_kw(p.peek()[1]):
                    alias = p.next()[1]
                aliases.append(alias.lower() if alias else None)
        else:
            columns.append(parse_expr(p))
            alias = None
            if p.is_id('AS'):
                p.next()
                alias = p.expect_id()
            elif p.peek() and p.peek()[0] == 'id' and not is_clause_kw(p.peek()[1]):
                alias = p.next()[1]
            aliases.append(alias.lower() if alias else None)
        if p.is_op(','):
            p.next()
            continue
        break
    tables = []
    if p.is_id('FROM'):
        p.next()
        while True:
            tname = p.expect_id()
            alias = None
            if p.is_id('AS'):
                p.next()
                alias = p.expect_id()
            elif p.peek() and p.peek()[0] == 'id' and not is_clause_kw(p.peek()[1]):
                alias = p.next()[1]
            tables.append((tname.lower(), alias.lower() if alias else None))
            if p.is_op(','):
                p.next()
                continue
            break
    where = None
    if p.is_id('WHERE'):
        p.next()
        where = parse_expr(p)
    order_by = None
    if p.is_id('ORDER'):
        p.next()
        p.expect_id('BY')
        order_by = []
        while True:
            asc = True
            nulls = None  # None: engine default (NULLs first on ASC); True: NULLS FIRST; False: NULLS LAST
            if p.peek() and p.peek()[0] == 'num':
                pos = int(p.next()[1])
                key = pos
            else:
                key = parse_expr(p)
            if p.is_id('ASC'):
                p.next()
            elif p.is_id('DESC'):
                p.next()
                asc = False
            if p.is_id('NULLS'):
                p.next()
                if p.is_id('FIRST'):
                    p.next()
                    nulls = True
                elif p.is_id('LAST'):
                    p.next()
                    nulls = False
                else:
                    raise EngineError('expected FIRST or LAST after NULLS')
            order_by.append((key, asc, nulls))
            if p.is_op(','):
                p.next()
                continue
            break
    def _read_limit_number(what):
        neg = False
        if p.is_op('-'):
            p.next()
            neg = True
        elif p.is_op('+'):
            p.next()
        t = p.next()
        if t is None or t[0] != 'num':
            raise EngineError('%s expects a number' % what)
        v = int(t[1])
        return -v if neg else v

    limit = None
    offset = None
    if p.is_id('LIMIT'):
        p.next()
        limit = _read_limit_number('LIMIT')
        if p.is_op(','):
            p.next()
            offset = limit
            limit = _read_limit_number('LIMIT')
        elif p.is_id('OFFSET'):
            p.next()
            offset = _read_limit_number('OFFSET')
    return Select(columns, aliases, distinct, tables, where, order_by, limit,
                  offset)


def parse_expr(p):
    return parse_or(p)


def parse_or(p):
    left = parse_and(p)
    while p.is_id('OR'):
        p.next()
        right = parse_and(p)
        left = ('bin', 'OR', left, right)
    return left


def parse_and(p):
    left = parse_not(p)
    while p.is_id('AND'):
        p.next()
        right = parse_not(p)
        left = ('bin', 'AND', left, right)
    return left


def parse_not(p):
    if p.is_id('NOT'):
        p.next()
        return ('un', 'NOT', parse_not(p))
    return parse_cmp(p)


def parse_cmp(p):
    left = parse_add(p)
    while True:
        if p.is_id('IS'):
            p.next()
            neg = False
            if p.is_id('NOT'):
                p.next()
                neg = True
            p.expect_id('NULL')
            left = ('isnull', left, neg)
            continue
        if p.is_id('NOT'):
            if p.is_id('BETWEEN', 1):
                p.next()
                p.next()
                lo = parse_add(p)
                p.expect_id('AND')
                hi = parse_add(p)
                left = ('between', left, lo, hi, True)
                continue
            if p.is_id('IN', 1):
                p.next()
                p.next()
                left = parse_in_tail(p, left, True)
                continue
            if p.is_id('LIKE', 1):
                p.next()
                p.next()
                left = parse_like_tail(p, left, True)
                continue
            raise EngineError('unsupported NOT construct')
        if p.is_id('BETWEEN'):
            p.next()
            lo = parse_add(p)
            p.expect_id('AND')
            hi = parse_add(p)
            left = ('between', left, lo, hi, False)
            continue
        if p.is_id('IN'):
            p.next()
            left = parse_in_tail(p, left, False)
            continue
        if p.is_id('LIKE'):
            p.next()
            left = parse_like_tail(p, left, False)
            continue
        t = p.peek()
        if t is not None and t[0] == 'op' and t[1] in ('=', '<>', '!=', '<', '>', '<=', '>='):
            op = p.next()[1]
            right = parse_add(p)
            left = ('bin', op, left, right)
            continue
        break
    return left


def parse_in_tail(p, left, negated):
    p.expect_op('(')
    if p.is_id('SELECT'):
        sel = parse_select(p)
        p.expect_op(')')
        return ('in', left, ('subq', sel), negated)
    items = []
    if not p.is_op(')'):
        while True:
            items.append(parse_expr(p))
            if p.is_op(','):
                p.next()
                continue
            break
    p.expect_op(')')
    return ('in', left, ('list', items), negated)


def parse_like_tail(p, left, negated):
    pattern = parse_add(p)
    escape = None
    if p.is_id('ESCAPE'):
        p.next()
        escape = parse_add(p)
    return ('like', left, pattern, escape, negated)


def parse_add(p):
    left = parse_mul(p)
    while p.is_op('+') or p.is_op('-'):
        op = p.next()[1]
        right = parse_mul(p)
        left = ('bin', op, left, right)
    return left


def parse_mul(p):
    left = parse_concat(p)
    while p.is_op('*') or p.is_op('/') or p.is_op('%') or p.is_id('DIV'):
        if p.is_op('*') or p.is_op('/') or p.is_op('%'):
            op = p.next()[1]
        else:
            p.next()
            op = 'DIV'
        right = parse_concat(p)
        left = ('bin', op, left, right)
    return left


def parse_concat(p):
    left = parse_unary(p)
    while p.is_op('||'):
        p.next()
        right = parse_mul(p)
        left = ('bin', '||', left, right)
    return left


def parse_unary(p):
    if p.is_op('-') or p.is_op('+'):
        op = p.next()[1]
        return ('un', op, parse_unary(p))
    return parse_primary(p)


def parse_primary(p):
    t = p.peek()
    if t is None:
        raise EngineError('unexpected end of input')
    if t[0] == 'num':
        p.next()
        text = t[1]
        return ('lit', float(text)) if '.' in text else ('lit', int(text))
    if t[0] == 'str':
        p.next()
        return ('lit', t[1])
    if t[0] == 'op':
        if t[1] == '(':
            p.next()
            if p.is_id('SELECT'):
                sel = parse_select(p)
                p.expect_op(')')
                return ('subq', sel)
            e = parse_expr(p)
            p.expect_op(')')
            return e
        raise EngineError('unexpected token %r' % (t,))
    # identifier
    p.next()
    word = t[1].upper()
    if word == 'NULL':
        return ('lit', None)
    if word == 'CASE':
        return parse_case(p)
    if word == 'EXISTS':
        p.expect_op('(')
        sel = parse_select(p)
        p.expect_op(')')
        return ('exists', sel)
    if word == 'CAST':
        p.expect_op('(')
        expr = parse_expr(p)
        p.expect_id('AS')
        t2 = p.next()
        if t2 is None or t2[0] != 'id':
            raise EngineError('expected type in CAST')
        typewords = [t2[1]]
        while p.peek() and p.peek()[0] == 'id' and not p.is_op(')'):
            typewords.append(p.next()[1])
        p.expect_op(')')
        return ('cast', expr, ' '.join(typewords).upper())
    if p.is_op('('):
        # function call
        p.next()
        distinct = False
        if p.is_id('ALL'):
            p.next()
        elif p.is_id('DISTINCT'):
            p.next()
            distinct = True
        if p.is_op('*'):
            p.next()  # count(*)
            args = [('star',)]
        else:
            args = []
            if not p.is_op(')'):
                while True:
                    args.append(parse_expr(p))
                    if p.is_op(','):
                        p.next()
                        continue
                    break
        p.expect_op(')')
        return ('func', word.lower(), args, distinct)
    # column reference, possibly qualified
    name = t[1].lower()
    qual = None
    if p.is_op('.'):
        p.next()
        t2 = p.next()
        if t2 is not None and t2[0] == 'op' and t2[1] == '*':
            return ('star', name)
        if t2 is None or t2[0] != 'id':
            raise EngineError('expected column name after .')
        qual = name
        name = t2[1].lower()
    return ('col', qual, name)


def parse_case(p):
    # 'CASE' already consumed
    base = None
    if not p.is_id('WHEN'):
        base = parse_expr(p)
    whens = []
    while p.is_id('WHEN'):
        p.next()
        cond = parse_expr(p)
        p.expect_id('THEN')
        res = parse_expr(p)
        whens.append((cond, res))
    else_ = None
    if p.is_id('ELSE'):
        p.next()
        else_ = parse_expr(p)
    p.expect_id('END')
    return ('case', base, whens, else_)


# ---------------------------------------------------------------------------
# Values and semantics (SQLite-faithful)
# ---------------------------------------------------------------------------


def is_true(v):
    return v is not None and v != 0


def truth3(v):
    if v is None:
        return None
    return bool(v)


def sql_not(v):
    if v is None:
        return None
    return 0 if is_true(v) else 1


def sql_and(a, b):
    ta = truth3(a)
    tb = truth3(b)
    if ta is False or tb is False:
        return 0
    if ta is None or tb is None:
        return None
    return 1


def sql_or(a, b):
    ta = truth3(a)
    tb = truth3(b)
    if ta is True or tb is True:
        return 1
    if ta is None or tb is None:
        return None
    return 0


def value_class(v):
    """SQLite type ordering: NULL(0) < number(1) < text(2)."""
    if v is None:
        return 0
    if isinstance(v, str):
        return 2
    return 1


def to_number(text):
    """Convert a TEXT value to a number if it looks numeric, else None."""
    s = text.strip()
    if s == '':
        return None
    try:
        if any(ch in s for ch in '.eE'):
            return float(s)
        return int(s)
    except ValueError:
        return None


def format_number(v):
    """Render a number the way SQLite renders it for TEXT conversion."""
    if isinstance(v, int):
        return str(v)
    if v.is_integer():
        return '%.1f' % v
    return repr(v)


def apply_affinity(v, affinity):
    """Apply SQLite column affinity to a value (INSERT / comparison)."""
    if v is None or affinity is None:
        return v
    if affinity in ('INTEGER', 'NUMERIC', 'REAL'):
        if isinstance(v, str):
            n = to_number(v)
            if n is None:
                return v  # non-numeric text stays text
            if affinity == 'INTEGER' and isinstance(n, float) and n.is_integer():
                return int(n)
            return n
        if affinity == 'INTEGER' and isinstance(v, float) and v.is_integer():
            return int(v)
        return v
    if affinity == 'TEXT':
        if isinstance(v, (int, float)):
            return format_number(v)
        return v
    return v  # BLOB / none


def sql_compare(op, a, b):
    """Compare two values with SQLite type ordering."""
    if a is None or b is None:
        return None
    ca = value_class(a)
    cb = value_class(b)
    if ca == cb:
        result = (a < b) - (a > b)
    else:
        result = -1 if ca < cb else 1
    if op == '=':
        return 1 if result == 0 else 0
    if op in ('<>', '!='):
        return 1 if result != 0 else 0
    if op == '<':
        return 1 if result > 0 else 0
    if op == '>':
        return 1 if result < 0 else 0
    if op == '<=':
        return 1 if result >= 0 else 0
    if op == '>=':
        return 1 if result <= 0 else 0
    raise EngineError('unknown comparison operator %r' % op)


def sql_arith(op, a, b):
    if a is None or b is None:
        return None
    if isinstance(a, float) or isinstance(b, float):
        af, bf = float(a), float(b)
        if op == '+':
            return af + bf
        if op == '-':
            return af - bf
        if op == '*':
            return af * bf
        if op in ('/', 'DIV'):
            if bf == 0:
                return None  # SQLite: division by zero yields NULL
            return af / bf
        if op == '%':
            if bf == 0:
                return None
            return math.fmod(af, bf)
    # INTEGER arithmetic
    if op == '+':
        return a + b
    if op == '-':
        return a - b
    if op == '*':
        return a * b
    if op in ('/', 'DIV'):
        if b == 0:
            return None  # SQLite: division by zero yields NULL
        q = abs(a) // abs(b)
        return -q if (a < 0) != (b < 0) else q
    if op == '%':
        if b == 0:
            return None
        r = abs(a) % abs(b)
        return -r if a < 0 else r
    raise EngineError('unknown arithmetic operator %r' % op)


def sql_concat(a, b):
    if a is None or b is None:
        return None
    sa = a if isinstance(a, str) else format_number(a)
    sb = b if isinstance(b, str) else format_number(b)
    return sa + sb


def cast_value(v, typename):
    t = typename.upper()
    if v is None:
        return None
    if t in ('INT', 'INTEGER', 'SIGNED', 'UNSIGNED', 'TINYINT', 'SMALLINT',
             'MEDIUMINT', 'BIGINT', 'INT2', 'INT8', 'BOOLEAN'):
        if isinstance(v, str):
            n = to_number(v)
            return int(n) if n is not None else 0
        if isinstance(v, float):
            return int(v)
        return int(v)
    if t in ('REAL', 'FLOAT', 'DOUBLE', 'DOUBLE PRECISION', 'NUMERIC',
             'DECIMAL', 'DECIMAL(', 'SIGNED REAL'):
        if isinstance(v, str):
            n = to_number(v)
            return float(n) if n is not None else 0.0
        return float(v)
    if t in ('TEXT', 'CHAR', 'CHARACTER', 'VARCHAR', 'VARYING CHARACTER',
             'NCHAR', 'NATIVE CHARACTER', 'NVARCHAR', 'CLOB'):
        if isinstance(v, str):
            return v
        return format_number(v)
    if t == 'BLOB':
        if isinstance(v, str):
            return v
        return format_number(v)
    if t == 'NULL':
        return None
    raise EngineError('unknown CAST type: %s' % t)


def like_to_regex(pattern, escape):
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if escape is not None and c == escape:
            i += 1
            if i < n:
                out.append(re.escape(pattern[i]))
            i += 1
            continue
        if c == '%':
            out.append('.*')
        elif c == '_':
            out.append('.')
        else:
            out.append(re.escape(c))
        i += 1
    return '(?i)^' + ''.join(out) + '$'


def like_match(value, pattern, escape):
    if value is None or pattern is None:
        return None
    rx = like_to_regex(pattern, escape)
    return 1 if re.match(rx, value) else 0


def eval_in(v, items, negated):
    """SQLite IN semantics with three-valued logic."""
    if v is None:
        return None
    saw_null = False
    for item in items:
        if item is None:
            saw_null = True
            continue
        if v == item:
            return 1 if not negated else 0
    if saw_null:
        return None
    return 0 if not negated else 1


def cmp_values(a, b):
    """SQLite ORDER BY comparison: NULLs first, numbers < text."""
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    ca = value_class(a)
    cb = value_class(b)
    if ca != cb:
        return -1 if ca < cb else 1
    return (a > b) - (a < b)


def cmp_values_with_nulls(a, b, asc=True, nulls=None):
    """ORDER BY comparison honoring ASC/DESC and NULLS FIRST/LAST.

    SQLite default: NULLs sort first on ASC, last on DESC. An explicit
    NULLS FIRST/LAST overrides that.
    """
    if a is None and b is None:
        return 0
    if a is None or b is None:
        nulls_first = nulls if nulls is not None else asc
        if a is None:
            return -1 if nulls_first else 1
        return 1 if nulls_first else -1
    c = cmp_values(a, b)
    return c if asc else -c


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def resolve_column(ctx, qual, name):
    for fr in reversed(ctx.env):
        if fr.row is None:
            continue
        if qual is not None:
            if fr.alias == qual or (fr.alias is None and fr.table == qual):
                if name in fr.row:
                    return fr.row[name]
                raise EngineError('no such column: %s.%s' % (qual, name))
        else:
            if name in fr.row:
                return fr.row[name]
    raise EngineError('no such column: %s' % name)


def column_affinity(ctx, qual, name):
    for fr in reversed(ctx.env):
        if qual is not None:
            if fr.alias == qual or (fr.alias is None and fr.table == qual):
                t = ctx.db.tables.get(fr.table)
                if t is not None:
                    return t.affinity.get(name)
                return None
        else:
            if fr.table is not None and fr.row is not None and name in fr.row:
                t = ctx.db.tables.get(fr.table)
                if t is not None:
                    return t.affinity.get(name)
                return None
    return None


# ---------------------------------------------------------------------------
# Expression evaluation
# ---------------------------------------------------------------------------


def walk_expr(node, fn, stop=None):
    """Depth-first walk; fn(node) called pre-order; stop(node) prunes."""
    if node is None:
        return
    if stop is not None and stop(node):
        return
    fn(node)
    tag = node[0]
    if tag == 'bin':
        walk_expr(node[2], fn, stop)
        walk_expr(node[3], fn, stop)
    elif tag == 'un':
        walk_expr(node[2], fn, stop)
    elif tag == 'func':
        for a in node[2]:
            walk_expr(a, fn, stop)
    elif tag == 'case':
        base, whens, else_ = node[1], node[2], node[3]
        if base is not None:
            walk_expr(base, fn, stop)
        for cond, res in whens:
            walk_expr(cond, fn, stop)
            walk_expr(res, fn, stop)
        if else_ is not None:
            walk_expr(else_, fn, stop)
    elif tag == 'between':
        walk_expr(node[1], fn, stop)
        walk_expr(node[2], fn, stop)
        walk_expr(node[3], fn, stop)
    elif tag == 'isnull':
        walk_expr(node[1], fn, stop)
    elif tag == 'in':
        walk_expr(node[1], fn, stop)
        if node[2][0] == 'list':
            for item in node[2][1]:
                walk_expr(item, fn, stop)
    elif tag == 'like':
        walk_expr(node[1], fn, stop)
        walk_expr(node[2], fn, stop)
        if node[3] is not None:
            walk_expr(node[3], fn, stop)
    elif tag == 'cast':
        walk_expr(node[1], fn, stop)


def find_aggregates(node, out):
    def fn(n):
        if n[0] == 'func' and n[1] in AGG_FUNCS:
            out.append(n)
    walk_expr(node, fn, stop=lambda n: n[0] in ('subq', 'exists'))


def contains_aggregate(node):
    out = []
    find_aggregates(node, out)
    return bool(out)


def eval_expr(node, ctx):
    tag = node[0]
    if tag == 'lit':
        return node[1]
    if tag == 'col':
        return resolve_column(ctx, node[1], node[2])
    if tag == 'bin':
        op = node[1]
        if op == 'AND':
            return sql_and(eval_expr(node[2], ctx), eval_expr(node[3], ctx))
        if op == 'OR':
            return sql_or(eval_expr(node[2], ctx), eval_expr(node[3], ctx))
        if op in ('=', '<>', '!=', '<', '>', '<=', '>='):
            a = eval_expr(node[2], ctx)
            b = eval_expr(node[3], ctx)
            a = apply_affinity(a, operand_affinity(node[2], ctx))
            b = apply_affinity(b, operand_affinity(node[3], ctx))
            return sql_compare(op, a, b)
        if op == '||':
            return sql_concat(eval_expr(node[2], ctx), eval_expr(node[3], ctx))
        return sql_arith(op, eval_expr(node[2], ctx), eval_expr(node[3], ctx))
    if tag == 'un':
        op = node[1]
        v = eval_expr(node[2], ctx)
        if op == 'NOT':
            return sql_not(v)
        if op == '-':
            return None if v is None else -v
        return v  # unary +
    if tag == 'func':
        name = node[1]
        if name in AGG_FUNCS:
            return ctx.agg_values.get(id(node))
        args = [eval_expr(a, ctx) for a in node[2]]
        if name == 'abs':
            return None if args[0] is None else abs(args[0])
        if name == 'coalesce':
            for a in args:
                if a is not None:
                    return a
            return None
        if name == 'nullif':
            a, b = args[0], args[1]
            if a is None or b is None:
                return a
            if a == b:
                return None
            return a
        if name == 'ifnull':
            return args[0] if args[0] is not None else args[1]
        raise EngineError('unknown function: %s' % name)
    if tag == 'case':
        base, whens, else_ = node[1], node[2], node[3]
        if base is not None:
            bv = eval_expr(base, ctx)
            for cond, res in whens:
                cv = eval_expr(cond, ctx)
                if bv is not None and cv is not None and bv == cv:
                    return eval_expr(res, ctx)
        else:
            for cond, res in whens:
                if is_true(eval_expr(cond, ctx)):
                    return eval_expr(res, ctx)
        return None if else_ is None else eval_expr(else_, ctx)
    if tag == 'between':
        v = eval_expr(node[1], ctx)
        lo = eval_expr(node[2], ctx)
        hi = eval_expr(node[3], ctx)
        r = sql_and(sql_compare('>=', v, lo), sql_compare('<=', v, hi))
        return sql_not(r) if node[4] else r
    if tag == 'isnull':
        v = eval_expr(node[1], ctx)
        is_null = v is None
        return 1 if (is_null != node[2]) else 0
    if tag == 'in':
        v = eval_expr(node[1], ctx)
        v = apply_affinity(v, operand_affinity(node[1], ctx))
        if node[2][0] == 'subq':
            rows = node[2][1].execute(ctx)
            items = [r[0] for r in rows]
        else:
            items = [eval_expr(i, ctx) for i in node[2][1]]
        return eval_in(v, items, node[3])
    if tag == 'like':
        v = eval_expr(node[1], ctx)
        pat = eval_expr(node[2], ctx)
        esc = eval_expr(node[3], ctx) if node[3] is not None else None
        sv = v if isinstance(v, str) else (format_number(v) if v is not None else None)
        r = like_match(sv, pat, esc)
        return sql_not(r) if node[4] else r
    if tag == 'cast':
        return cast_value(eval_expr(node[1], ctx), node[2])
    if tag == 'subq':
        return eval_scalar_subquery(node[1], ctx)
    if tag == 'exists':
        return eval_exists(node[1], ctx)
    raise EngineError('unknown expression node %r' % (tag,))


def operand_affinity(node, ctx):
    """Affinity of a column operand, else None (used for comparisons/IN)."""
    if node[0] == 'col':
        return column_affinity(ctx, node[1], node[2])
    return None


def eval_aggregate(node, rows, ctx, frames):
    """Compute an aggregate over the joined rows.

    rows: list of joined-row dicts {frame_index: row_dict}; frames: the
    select's frame list. Each frame's row is set per joined row while
    evaluating the argument expression.
    """
    func = node[1]
    args = node[2]
    distinct = node[3]
    star = len(args) == 1 and args[0] == ('star',)
    vals = []
    for jrow in rows:
        for i, fr in enumerate(frames):
            fr.row = jrow.get(i)
        if star:
            vals.append(1)
        else:
            vals.append(eval_expr(args[0], ctx))
    for fr in frames:
        fr.row = None
    if distinct:
        seen = set()
        uniq = []
        for v in vals:
            if v is None:
                continue
            key = (value_class(v), v)
            if key not in seen:
                seen.add(key)
                uniq.append(v)
        vals = uniq
    else:
        vals = [v for v in vals if v is not None]
    if func == 'count':
        if star:
            return len(rows)
        return len(vals)
    if func == 'sum':
        return sum(vals) if vals else None
    if func == 'avg':
        return (sum(vals) / len(vals)) if vals else None
    if func == 'min':
        return min(vals) if vals else None
    if func == 'max':
        return max(vals) if vals else None
    raise EngineError('unknown aggregate %s' % func)


def eval_scalar_subquery(sel, ctx):
    rows = sel.execute(ctx)
    if not rows:
        return None
    return rows[0][0]


def eval_exists(sel, ctx):
    rows = sel.execute(ctx)
    return 1 if rows else 0


# ---------------------------------------------------------------------------
# Statement execution
# ---------------------------------------------------------------------------


class Database:
    """A single in-memory SQL database (one connection)."""

    def __init__(self):
        self.tables = {}

    def execute_statement(self, sql):
        toks = tokenize(sql)
        p = Parser(toks)
        executed = 0
        while p.pos < len(toks):
            while p.is_op(';'):
                p.next()
            if p.pos >= len(toks):
                break
            stmt = parse_statement(p)
            self._run(stmt)
            executed += 1
            if p.is_op(';'):
                p.next()
        if executed == 0:
            raise EngineError('empty statement')

    def _run(self, stmt):
        if isinstance(stmt, NoOp):
            return
        if isinstance(stmt, CreateTable):
            if stmt.name in self.tables:
                raise EngineError('table %s already exists' % stmt.name)
            colnames = [c[0] for c in stmt.columns]
            affinity = {c[0]: c[1] for c in stmt.columns}
            self.tables[stmt.name] = Table(colnames, affinity)
            return
        if isinstance(stmt, Insert):
            t = self.tables.get(stmt.table)
            if t is None:
                raise EngineError('no such table: %s' % stmt.table)
            ctx = Ctx(self)
            for tup in stmt.tuples:
                vals = [eval_expr(e, ctx) for e in tup]
                if stmt.colnames is None:
                    if len(vals) != len(t.colnames):
                        raise EngineError('INSERT column count mismatch')
                    row = {}
                    for cname, v in zip(t.colnames, vals):
                        row[cname] = apply_affinity(v, t.affinity.get(cname))
                    t.rows.append(row)
                else:
                    if len(vals) != len(stmt.colnames):
                        raise EngineError('INSERT column count mismatch')
                    mapping = dict(zip(stmt.colnames, vals))
                    row = {}
                    for cname in t.colnames:
                        row[cname] = apply_affinity(mapping.get(cname),
                                                    t.affinity.get(cname))
                    t.rows.append(row)
            return
        if isinstance(stmt, Select):
            stmt.execute(Ctx(self))
            return
        raise EngineError('unsupported statement')

    def execute_query(self, sql):
        toks = tokenize(sql)
        p = Parser(toks)
        stmt = parse_statement(p)
        while p.is_op(';'):
            p.next()
        if p.pos < len(toks):
            raise EngineError('unexpected trailing tokens')
        if isinstance(stmt, Select):
            return stmt.execute(Ctx(self))
        if isinstance(stmt, Compound):
            return stmt.execute(Ctx(self))
        raise EngineError('statement is not a query')


# ---------------------------------------------------------------------------
# SELECT execution
# ---------------------------------------------------------------------------


def _split_conjuncts(node):
    if node is not None and node[0] == 'bin' and node[1] == 'AND':
        return _split_conjuncts(node[2]) + _split_conjuncts(node[3])
    return [node] if node is not None else []


def _collect_refs(node, out):
    def fn(n):
        if n[0] == 'col':
            out.append((n[1], n[2]))
    walk_expr(node, fn, stop=lambda n: n[0] in ('subq', 'exists'))


def _col_owner(ctx, qual, name, frames):
    """Return the frame index owning a column ref, or None if unknown/ambiguous."""
    found = None
    for i, fr in enumerate(frames):
        t = ctx.db.tables.get(fr.table)
        if t is None or name not in t.colnames:
            continue
        if qual is not None:
            if fr.alias == qual or (fr.alias is None and fr.table == qual):
                return i
        else:
            if found is not None:
                return None  # ambiguous unqualified name
            found = i
    if qual is not None:
        return None
    return found


def _norm_key(v):
    if v is None:
        return None
    return (value_class(v), v)


def _col_value(row, col):
    """Value of a column ref node in a plain table row dict."""
    return row.get(col[2])


def Select_execute(self, ctx):
    db = ctx.db
    n_tables = len(self.tables)

    if n_tables == 0:
        frames = [Frame(None, None, {})]
        matched = [{}]  # one virtual row
    else:
        frames = []
        for tname, alias in self.tables:
            t = db.tables.get(tname)
            if t is None:
                raise EngineError('no such table: %s' % tname)
            frames.append(Frame(tname, alias, None))

    # ---- expand star columns ----------------------------------------------
    expanded = []
    if n_tables == 0:
        expanded = [(c, a) for c, a in zip(self.columns, self.aliases)
                    if c[0] != 'star']
    else:
        for col, alias in zip(self.columns, self.aliases):
            if col[0] == 'star':
                if col[1] is None:
                    for fr in frames:
                        t = db.tables[fr.table]
                        for cname in t.colnames:
                            expanded.append((('col', fr.alias or fr.table, cname), None))
                else:
                    for fr in frames:
                        if fr.alias == col[1] or (fr.alias is None and fr.table == col[1]):
                            t = db.tables[fr.table]
                            for cname in t.colnames:
                                expanded.append((('col', col[1], cname), None))
                            break
            else:
                expanded.append((col, alias))

    # ---- join rows ----------------------------------------------------------
    for fr in frames:
        ctx.env.append(fr)
    try:
        if n_tables == 0:
            matched = [{}]
        else:
            matched = _join_rows(self, ctx, frames)
    finally:
        for _ in frames:
            ctx.env.pop()

    # ---- aggregates ---------------------------------------------------------
    aggs = []
    for col, _alias in expanded:
        find_aggregates(col, aggs)
    is_agg = bool(aggs)

    ctx.env.extend(frames)
    try:
        if is_agg:
            for node in aggs:
                ctx.agg_values[id(node)] = eval_aggregate(node, matched, ctx, frames)
            for fr in frames:
                fr.row = None
            # aggregate queries produce a single row
            row_values = [eval_expr(c, ctx) for c, _a in expanded]
            pairs = [(row_values, None)]
        else:
            pairs = []
            for jrow in matched:
                for i, fr in enumerate(frames):
                    fr.row = jrow.get(i)
                values = [eval_expr(c, ctx) for c, _a in expanded]
                pairs.append((values, jrow))
            for fr in frames:
                fr.row = None
    finally:
        for _ in frames:
            ctx.env.pop()

    # ---- DISTINCT -----------------------------------------------------------
    if self.distinct:
        seen = set()
        dedup = []
        for values, jrow in pairs:
            key = tuple((value_class(v), v) for v in values)
            if key not in seen:
                seen.add(key)
                dedup.append((values, jrow))
        pairs = dedup

    # ---- ORDER BY -------------------------------------------------------------
    if self.order_by:
        pairs = _apply_order_by(self, pairs, frames, ctx)

    # ---- LIMIT / OFFSET -------------------------------------------------------
    if self.offset is not None and self.offset > 0:
        pairs = pairs[self.offset:]
    if self.limit is not None and self.limit >= 0:
        pairs = pairs[:self.limit]

    return [values for values, _jrow in pairs]


def _join_rows(self, ctx, frames):
    """Multi-table FROM: pushdown filter + hash-join + WHERE evaluation."""
    where = self.where
    conjuncts = _split_conjuncts(where)
    n = len(frames)

    table_filters = [[] for _ in range(n)]
    span = []
    equi = []

    for conj in conjuncts:
        # equi-join constraint: colA = colB across two tables
        if (conj[0] == 'bin' and conj[1] == '=' and conj[2][0] == 'col'
                and conj[3][0] == 'col'):
            ia = _col_owner(ctx, conj[2][1], conj[2][2], frames)
            ib = _col_owner(ctx, conj[3][1], conj[3][2], frames)
            if ia is not None and ib is not None and ia != ib:
                equi.append((conj[2], conj[3], ia, ib))
                continue
        refs = []
        _collect_refs(conj, refs)
        owners = set()
        unknown = False
        for qual, name in refs:
            o = _col_owner(ctx, qual, name, frames)
            if o is None:
                unknown = True
                break
            owners.add(o)
        if not unknown and len(owners) <= 1:
            if owners:
                table_filters[owners.pop()].append(conj)
            else:
                span.append(conj)
        else:
            span.append(conj)

    # pushdown filter each table
    filtered = []
    for i, fr in enumerate(frames):
        t = ctx.db.tables[fr.table]
        rows = t.rows
        if table_filters[i]:
            keep = []
            for row in rows:
                fr.row = row
                ok = True
                for conj in table_filters[i]:
                    if not is_true(eval_expr(conj, ctx)):
                        ok = False
                        break
                if ok:
                    keep.append(row)
            fr.row = None
            rows = keep
        filtered.append(rows)
        if not rows:
            return []

    # hash indexes for equi-join columns
    index = {}
    for colA, colB, ia, ib in equi:
        for table_idx, own_col in ((ia, colA), (ib, colB)):
            key = (table_idx, id(own_col))
            if key in index:
                continue
            idx = {}
            for r, row in enumerate(filtered[table_idx]):
                idx.setdefault(_norm_key(_col_value(row, own_col)), []).append(r)
            index[key] = idx

    cons_by_table = [[] for _ in range(n)]
    for colA, colB, ia, ib in equi:
        cons_by_table[ia].append((colA, colB, ib))
        cons_by_table[ib].append((colB, colA, ia))

    # greedy join order: start with the smallest filtered table, then always
    # pick the table with the most equality constraints to already-bound
    # tables (ties: smallest filtered size, then FROM index). This keeps the
    # frontier small for star/chain join shapes with 10+ tables.
    order = []
    remaining = set(range(n))
    bound = set()

    def constraint_count(i):
        return sum(1 for _own, _other, j in cons_by_table[i] if j in bound)

    start = min(remaining, key=lambda i: (len(filtered[i]), i))
    order.append(start)
    remaining.remove(start)
    bound.add(start)
    while remaining:
        nxt = min(remaining, key=lambda i: (-constraint_count(i),
                                            len(filtered[i]), i))
        order.append(nxt)
        remaining.remove(nxt)
        bound.add(nxt)

    combos = []

    def recurse(k, bound):
        if k == n:
            combos.append(dict(bound))
            return
        i = order[k]
        rows = filtered[i]
        cand = None
        for own_col, other_col, j in cons_by_table[i]:
            if j in bound:
                bv = _col_value(bound[j], other_col)
                idx = index[(i, id(own_col))]
                matches = idx.get(_norm_key(bv))
                if matches is None:
                    return
                ms = set(matches)
                cand = ms if cand is None else (cand & ms)
                if not cand:
                    return
        if cand is not None:
            candidates = [rows[r] for r in sorted(cand)]
        else:
            candidates = rows
        for row in candidates:
            bound[i] = row
            recurse(k + 1, bound)
        bound.pop(i, None)

    recurse(0, {})
    if not combos:
        return []

    # evaluate full WHERE per combo
    matched = []
    for combo in combos:
        for i, fr in enumerate(frames):
            fr.row = combo.get(i)
        if where is None or is_true(eval_expr(where, ctx)):
            matched.append(combo)
    for fr in frames:
        fr.row = None
    return matched


def _row_key(r):
    return tuple((value_class(v), v) for v in r)


def _distinct_rows(rows):
    seen = set()
    out = []
    for r in rows:
        k = _row_key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def Compound_execute(self, ctx):
    _op0, sel0 = self.parts[0]
    rows = sel0.execute(ctx)
    for op, sel in self.parts[1:]:
        rrows = sel.execute(ctx)
        kind, all_flag = op
        if kind == 'union':
            rows = (rows + rrows) if all_flag else _distinct_rows(rows + rrows)
        elif kind == 'except':
            rset = {_row_key(r) for r in rrows}
            rows = _distinct_rows([r for r in rows if _row_key(r) not in rset])
        elif kind == 'intersect':
            rset = {_row_key(r) for r in rrows}
            rows = _distinct_rows([r for r in rows if _row_key(r) in rset])
    if self.order_by:
        rows = _apply_compound_order_by(self, rows)
    if self.offset is not None and self.offset > 0:
        rows = rows[self.offset:]
    if self.limit is not None and self.limit >= 0:
        rows = rows[:self.limit]
    return rows


def _apply_compound_order_by(comp, rows):
    alias_to_pos = {}
    for idx, a in enumerate(comp.aliases):
        if a:
            alias_to_pos.setdefault(a, idx)
    resolved = []
    for key, asc, nulls in comp.order_by:
        if isinstance(key, int):
            resolved.append(('pos', key - 1, asc, nulls))
        elif key[0] == 'col' and key[1] is None and key[2] in alias_to_pos:
            resolved.append(('pos', alias_to_pos[key[2]], asc, nulls))
        else:
            resolved.append(('expr', key, asc, nulls))
    if rows:
        width = len(rows[0])
        for kind, arg, _asc, _n in resolved:
            if kind == 'pos' and (arg < 0 or arg >= width):
                raise EngineError('ORDER BY term out of range: %d' % (arg + 1))

    def cmp_rows(a, b):
        for kind, arg, asc, nulls in resolved:
            if kind == 'pos':
                va, vb = a[arg], b[arg]
            else:
                va, vb = None, None  # expression keys need row env; stable
            c = cmp_values_with_nulls(va, vb, asc, nulls)
            if c == 0:
                continue
            return c
        return 0

    rows.sort(key=functools.cmp_to_key(cmp_rows))
    return rows


Compound.execute = Compound_execute


def _apply_order_by(self, pairs, frames, ctx):
    """Sort (values, jrow) pairs by ORDER BY keys (position | alias | expr)."""
    alias_to_pos = {}
    for idx, a in enumerate(self.aliases):
        if a:
            alias_to_pos.setdefault(a, idx)

    resolved = []
    for key, asc, nulls in self.order_by:
        if isinstance(key, int):
            resolved.append(('pos', key - 1, asc, nulls))
        elif key[0] == 'col' and key[1] is None and key[2] in alias_to_pos:
            resolved.append(('pos', alias_to_pos[key[2]], asc, nulls))
        else:
            resolved.append(('expr', key, asc, nulls))

    # validate positional keys against the result width
    if pairs:
        width = len(pairs[0][0])
        for kind, arg, _asc, _n in resolved:
            if kind == 'pos' and (arg < 0 or arg >= width):
                raise EngineError('ORDER BY term out of range: %d' % (arg + 1))

    def key_value(pair, spec):
        kind, arg, asc, nulls = spec
        if kind == 'pos':
            return pair[0][arg]
        # expression: evaluate with the row's frames
        values, jrow = pair
        if jrow is None:
            return None
        for i, fr in enumerate(frames):
            fr.row = jrow.get(i)
        ctx.env.extend(frames)
        try:
            return eval_expr(arg, ctx)
        finally:
            for _ in frames:
                ctx.env.pop()

    def cmp_rows(a, b):
        for spec in resolved:
            va = key_value(a, spec)
            vb = key_value(b, spec)
            c = cmp_values_with_nulls(va, vb, spec[2], spec[3])
            if c == 0:
                continue
            return c
        return 0

    pairs.sort(key=functools.cmp_to_key(cmp_rows))
    return pairs


Select.execute = Select_execute
