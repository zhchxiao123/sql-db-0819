"""sql_db_0819.cli — subprocess protocol for the sqllogictest runner driver.

The official sqllogictest runner (third_party/sqllogictest) drives engines
through the DbEngine interface. Our C driver (src/slt_subprocess.c) spawns
this CLI and speaks a simple length-prefixed line protocol over stdin/stdout:

  parent -> child   "S <n>\\n<sql>"            execute a statement
  parent -> child   "Q <types> <n>\\n<sql>"    execute a query
  parent -> child   "X\\n"                     shutdown
  child  -> parent  "READY\\n"                 on startup
  child  -> parent  "OK\\n" | "ERR <msg>\\n"   after S
  child  -> parent  "OK <count>\\n<val>\\n..." | "ERR <msg>\\n"   after Q
  child  -> parent  "BYE\\n"                   after X

Values are rendered the same way the runner's reference SQLite driver
renders them: NULL -> "NULL", empty text -> "(empty)", control characters
-> '@', integer columns -> "%d", real columns -> "%.3f".

Run as:  python3 sql_db_0819/cli.py   (works from any CWD)
     or:  python3 -m sql_db_0819.cli  (from the repo root)
"""

import sys

try:
    from .engine import Database, EngineError
except ImportError:  # running as a plain script
    from engine import Database, EngineError


def sanitize(msg):
    return ' '.join(str(msg).split())


def render(value, type_char):
    """Render one result value for the runner, per the type string char."""
    if value is None:
        return 'NULL'
    if type_char == 'I':
        # sqlite3_column_int() truncates REALs toward zero; our I-type
        # results are always Python ints, but be defensive.
        return str(int(value))
    if type_char == 'R':
        return '%.3f' % float(value)
    # 'T' (text)
    s = str(value)
    if s == '':
        return '(empty)'
    return ''.join('@' if (ord(c) < 0x20 or ord(c) > 0x7E) else c for c in s)


def read_exact(fin, n):
    buf = b''
    while len(buf) < n:
        chunk = fin.read(n - len(buf))
        if not chunk:
            raise EOFError('engine stdin closed mid-frame')
        buf += chunk
    return buf


def main(argv=None):
    db = Database()
    fin = sys.stdin.buffer
    fout = sys.stdout
    fout.write('READY\n')
    fout.flush()
    while True:
        header = fin.readline()
        if not header:
            return 0
        header = header.decode('utf-8', 'replace').rstrip('\n')
        if header == 'X':
            fout.write('BYE\n')
            fout.flush()
            return 0
        parts = header.split(' ')
        try:
            if parts[0] == 'S':
                n = int(parts[1])
                sql = read_exact(fin, n).decode('utf-8', 'replace')
                try:
                    db.execute_statement(sql)
                    fout.write('OK\n')
                except EngineError as e:
                    fout.write('ERR %s\n' % sanitize(e))
            elif parts[0] == 'Q':
                types = parts[1]
                n = int(parts[2])
                sql = read_exact(fin, n).decode('utf-8', 'replace')
                try:
                    rows = db.execute_query(sql)
                    count = sum(len(r) for r in rows)
                    fout.write('OK %d\n' % count)
                    for row in rows:
                        for i, v in enumerate(row):
                            t = types[i] if i < len(types) else 'T'
                            fout.write(render(v, t) + '\n')
                except EngineError as e:
                    fout.write('ERR %s\n' % sanitize(e))
            else:
                fout.write('ERR bad frame header\n')
        except Exception as e:  # keep the protocol alive on unexpected errors
            fout.write('ERR %s\n' % sanitize(e))
        fout.flush()


if __name__ == '__main__':
    sys.exit(main())
