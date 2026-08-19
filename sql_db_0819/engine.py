"""sql_db_0819.engine — a minimal SQL engine.

This is the thinnest vertical slice of a SQL database engine, built to be
driven end-to-end by the official sqllogictest runner (see README.md).

Scope (exactly what the pinned sqllogictest select1/select2 need, plus a
little defensive generality):

* Statements: CREATE TABLE, INSERT INTO ... VALUES, SELECT.
* SELECT: expression list, optional FROM <table> [AS <alias>], WHERE,
  ORDER BY <position>,...; scalar subqueries, EXISTS subqueries.
* Expressions: literals, column refs (qualified/unqualified), arithmetic
  (+ - * /), comparisons (= <> != < > <= >=), AND/OR/NOT, BETWEEN /
  NOT BETWEEN, IS [NOT] NULL, CASE (searched and simple), abs(),
  coalesce(), aggregates count(*)/count(expr)/avg()/min()/max()/sum().

Semantics deliberately follow SQLite (the reference the sqllogictest
expected values are generated from):

* INTEGER division truncates toward zero (7/2=3, -7/2=-3).
* NULL propagates through arithmetic and comparisons; comparisons yield
  NULL when either side is NULL.
* AND/OR/NOT use three-valued logic.
* avg() ignores NULLs and returns REAL; count(*) counts all rows.
* Scalar subqueries return NULL when empty; aggregates over zero rows:
  count=0, avg/min/max/sum=NULL.
* ORDER BY puts NULLs first (ascending); sort is stable.
* A table alias replaces the table name for qualified references.

The module is pure stdlib. It never touches the network or the filesystem.
"""

from __future__ import annotations

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
        if two in ('<=', '>=', '<>', '!='):
            toks.append(('op', two))
            i += 2
            continue
        if c in '()[],;+-*/<>=.':
            toks.append(('op', c))
            i += 1
            continue
        raise EngineError('unexpected character: %r' % c)
    return toks


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


class CreateTable:
    __slots__ = ('name', 'columns')

    def __init__(self, name, columns):
        self.name = name.lower()
        self.columns = [(cname.lower(), ctype.upper()) for cname, ctype in columns]


class Insert:
    __slots__ = ('table', 'colnames', 'tuples')

    def __init__(self, table, colnames, tuples):
        self.table = table.lower()
        self.colnames = [c.lower() for c in colnames] if colnames is not None else None
        self.tuples = tuples


class Select:
    __slots__ = ('columns', 'table', 'alias', 'where', 'order_by')

    def __init__(self, columns, table, alias, where, order_by):
        self.columns = columns
        self.table = table.lower() if table is not None else None
        self.alias = alias.lower() if alias is not None else None
        self.where = where
        self.order_by = order_by


class Table:
    """In-memory table: column names plus rows (list of dict col->value)."""

    __slots__ = ('colnames', 'rows')

    def __init__(self, colnames):
        self.colnames = colnames
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

    __slots__ = ('db', 'env')

    def __init__(self, db):
        self.db = db
        self.env = []


AGG_FUNCS = frozenset({'count', 'avg', 'min', 'max', 'sum'})


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


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
        p.expect_id('TABLE')
        name = p.expect_id()
        p.expect_op('(')
        cols = []
        while True:
            cname = p.expect_id()
            ctype = p.expect_id()
            cols.append((cname, ctype))
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
    if p.is_id('SELECT'):
        return parse_select(p)
    raise EngineError('unsupported statement')


def parse_select(p):
    p.next()  # SELECT
    cols = []
    while True:
        cols.append(parse_expr(p))
        if p.is_op(','):
            p.next()
            continue
        break
    table = None
    alias = None
    where = None
    order_by = None
    if p.is_id('FROM'):
        p.next()
        table = p.expect_id()
        if p.is_id('AS'):
            p.next()
            alias = p.expect_id()
    if p.is_id('WHERE'):
        p.next()
        where = parse_expr(p)
    if p.is_id('ORDER'):
        p.next()
        p.expect_id('BY')
        order_by = []
        while True:
            t = p.next()
            if t is None or t[0] != 'num':
                raise EngineError('ORDER BY expects a column position')
            order_by.append(int(t[1]))
            if p.is_op(','):
                p.next()
                continue
            break
    return Select(cols, table, alias, where, order_by)


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
            raise EngineError('unsupported NOT construct')
        if p.is_id('BETWEEN'):
            p.next()
            lo = parse_add(p)
            p.expect_id('AND')
            hi = parse_add(p)
            left = ('between', left, lo, hi, False)
            continue
        t = p.peek()
        if t is not None and t[0] == 'op' and t[1] in ('=', '<>', '!=', '<', '>', '<=', '>='):
            op = p.next()[1]
            right = parse_add(p)
            left = ('bin', op, left, right)
            continue
        break
    return left


def parse_add(p):
    left = parse_mul(p)
    while p.is_op('+') or p.is_op('-'):
        op = p.next()[1]
        right = parse_mul(p)
        left = ('bin', op, left, right)
    return left


def parse_mul(p):
    left = parse_unary(p)
    while p.is_op('*') or p.is_op('/'):
        op = p.next()[1]
        right = parse_unary(p)
        left = ('bin', op, left, right)
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
    if p.is_op('('):
        # function call
        p.next()
        args = []
        if p.is_op('*'):
            p.next()  # count(*)
        else:
            while True:
                args.append(parse_expr(p))
                if p.is_op(','):
                    p.next()
                    continue
                break
        p.expect_op(')')
        return ('func', word.lower(), args)
    # column reference, possibly qualified
    name = t[1].lower()
    qual = None
    if p.is_op('.'):
        p.next()
        t2 = p.next()
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
# Value semantics (SQLite-faithful)
# ---------------------------------------------------------------------------


def is_true(v):
    """SQLite truthiness: NULL is false, 0 is false, everything else true."""
    return v is not None and v != 0


def truth3(v):
    """Three-valued truth: None -> unknown, else bool (nonzero = true)."""
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


def sql_cmp(op, a, b):
    if a is None or b is None:
        return None
    if isinstance(a, str) or isinstance(b, str):
        a, b = str(a), str(b)
    if op == '=':
        return 1 if a == b else 0
    if op in ('<>', '!='):
        return 1 if a != b else 0
    if op == '<':
        return 1 if a < b else 0
    if op == '>':
        return 1 if a > b else 0
    if op == '<=':
        return 1 if a <= b else 0
    if op == '>=':
        return 1 if a >= b else 0
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
        if op == '/':
            if bf == 0:
                raise EngineError('division by zero')
            return af / bf
    # INTEGER arithmetic
    if op == '+':
        return a + b
    if op == '-':
        return a - b
    if op == '*':
        return a * b
    if op == '/':
        if b == 0:
            raise EngineError('division by zero')
        # C-style integer division: truncate toward zero
        q = abs(a) // abs(b)
        return -q if (a < 0) != (b < 0) else q
    raise EngineError('unknown arithmetic operator %r' % op)


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def resolve_column(ctx, qual, name):
    for fr in reversed(ctx.env):
        if fr.row is None:
            continue
        if qual is not None:
            # An alias replaces the table name inside its own scope.
            if fr.alias == qual or (fr.alias is None and fr.table == qual):
                if name in fr.row:
                    return fr.row[name]
                raise EngineError('no such column: %s.%s' % (qual, name))
        else:
            if name in fr.row:
                return fr.row[name]
    raise EngineError('no such column: %s' % name)


# ---------------------------------------------------------------------------
# Expression evaluation
# ---------------------------------------------------------------------------


def contains_aggregate(node):
    """True if node contains an aggregate call NOT inside a subquery."""
    tag = node[0]
    if tag in ('subq', 'exists'):
        return False
    if tag == 'func':
        if node[1] in AGG_FUNCS:
            return True
        return any(contains_aggregate(a) for a in node[2])
    if tag == 'bin':
        return contains_aggregate(node[2]) or contains_aggregate(node[3])
    if tag == 'un':
        return contains_aggregate(node[2])
    if tag == 'case':
        base, whens, else_ = node[1], node[2], node[3]
        if base is not None and contains_aggregate(base):
            return True
        for cond, res in whens:
            if contains_aggregate(cond) or contains_aggregate(res):
                return True
        return else_ is not None and contains_aggregate(else_)
    if tag == 'between':
        return (contains_aggregate(node[1]) or contains_aggregate(node[2])
                or contains_aggregate(node[3]))
    if tag == 'isnull':
        return contains_aggregate(node[1])
    return False


def eval_expr(node, ctx):
    tag = node[0]
    if tag == 'lit':
        return node[1]
    if tag == 'col':
        return resolve_column(ctx, node[1], node[2])
    if tag == 'bin':
        op = node[1]
        a = eval_expr(node[2], ctx)
        b = eval_expr(node[3], ctx)
        if op == 'AND':
            return sql_and(a, b)
        if op == 'OR':
            return sql_or(a, b)
        if op in ('=', '<>', '!=', '<', '>', '<=', '>='):
            return sql_cmp(op, a, b)
        return sql_arith(op, a, b)
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
            raise EngineError('aggregate %s outside aggregate context' % name)
        args = [eval_expr(a, ctx) for a in node[2]]
        if name == 'abs':
            return None if args[0] is None else abs(args[0])
        if name == 'coalesce':
            for a in args:
                if a is not None:
                    return a
            return None
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
        r = sql_and(sql_cmp('>=', v, lo), sql_cmp('<=', v, hi))
        return sql_not(r) if node[4] else r
    if tag == 'isnull':
        v = eval_expr(node[1], ctx)
        is_null = v is None
        return 1 if (is_null != node[2]) else 0
    if tag == 'subq':
        return eval_scalar_subquery(node[1], ctx)
    if tag == 'exists':
        return eval_exists(node[1], ctx)
    raise EngineError('unknown expression node %r' % (tag,))


def eval_aggregate(func, args, rows, ctx, frame):
    vals = []
    for row in rows:
        frame.row = row
        if func == 'count' and len(args) == 0:
            vals.append(1)
        else:
            vals.append(eval_expr(args[0], ctx))
    if func == 'count':
        if len(args) == 0:
            return len(vals)
        return sum(1 for v in vals if v is not None)
    non_null = [v for v in vals if v is not None]
    if func == 'avg':
        return (sum(non_null) / len(non_null)) if non_null else None
    if func == 'sum':
        return sum(non_null) if non_null else None
    if func == 'min':
        return min(non_null) if non_null else None
    if func == 'max':
        return max(non_null) if non_null else None
    raise EngineError('unknown aggregate %s' % func)


def eval_agg_expr(node, rows, ctx, frame):
    """Evaluate one column expression of an aggregate SELECT."""
    tag = node[0]
    if tag == 'func' and node[1] in AGG_FUNCS:
        return eval_aggregate(node[1], node[2], rows, ctx, frame)
    # subqueries and plain expressions in an aggregate SELECT are evaluated
    # once (no per-row scope); SQLite would reject bare column references
    # here, and our test files do not use them.
    return eval_expr(node, ctx)


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

    # -- statements ---------------------------------------------------------

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
        if isinstance(stmt, CreateTable):
            if stmt.name in self.tables:
                raise EngineError('table %s already exists' % stmt.name)
            self.tables[stmt.name] = Table([c[0] for c in stmt.columns])
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
                    t.rows.append(dict(zip(t.colnames, vals)))
                else:
                    if len(vals) != len(stmt.colnames):
                        raise EngineError('INSERT column count mismatch')
                    mapping = dict(zip(stmt.colnames, vals))
                    t.rows.append({c: mapping.get(c) for c in t.colnames})
            return
        if isinstance(stmt, Select):
            stmt.execute(Ctx(self))
            return
        raise EngineError('unsupported statement')

    # -- queries ------------------------------------------------------------

    def execute_query(self, sql):
        toks = tokenize(sql)
        p = Parser(toks)
        stmt = parse_statement(p)
        while p.is_op(';'):
            p.next()
        if p.pos < len(toks):
            raise EngineError('unexpected trailing tokens')
        if not isinstance(stmt, Select):
            raise EngineError('statement is not a query')
        return stmt.execute(Ctx(self))


def Select_execute(self, ctx):
    db = ctx.db
    if self.table is None:
        rows = [{}]
    else:
        t = db.tables.get(self.table)
        if t is None:
            raise EngineError('no such table: %s' % self.table)
        rows = t.rows
    frame = Frame(self.table, self.alias, None)
    ctx.env.append(frame)
    try:
        if self.where is not None:
            matched = []
            for row in rows:
                frame.row = row
                if is_true(eval_expr(self.where, ctx)):
                    matched.append(row)
        else:
            matched = rows
        if any(contains_aggregate(c) for c in self.columns):
            out = [[eval_agg_expr(c, matched, ctx, frame) for c in self.columns]]
        else:
            out = []
            for row in matched:
                frame.row = row
                out.append([eval_expr(c, ctx) for c in self.columns])
        if self.order_by:
            for k in reversed(self.order_by):
                idx = k - 1
                if out and (idx < 0 or idx >= len(out[0])):
                    raise EngineError('ORDER BY term out of range: %d' % k)
                out.sort(key=lambda r: (0,) if r[idx] is None else (1, r[idx]))
        return out
    finally:
        ctx.env.pop()


Select.execute = Select_execute
