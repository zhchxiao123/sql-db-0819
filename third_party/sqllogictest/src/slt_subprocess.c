/*
** Copyright (c) 2026 sql-db-0819 contributors
**
** This is a DbEngine implementation for the official sqllogictest runner
** (https://github.com/gregrahn/sqllogictest) that drives an external SQL
** engine over a pipe.  It is the documented extension point of the runner:
** sqllogictest.h defines the DbEngine interface and slt_sqlite.c notes
** "New database engine interfaces should have a single routine similar to
** this one.  The main() function below should be modified to call that
** routine upon startup."
**
** The engine under test is spawned as a subprocess (the -connection string
** is the command line to run; default "python3 -m sql_db_0819.cli", or the
** SQLDB0819_ENGINE_CMD environment variable) and is spoken to with a small
** length-prefixed line protocol:
**
**   parent -> child   "S <n>\n<sql>"            execute a statement
**   parent -> child   "Q <types> <n>\n<sql>"    execute a query
**   parent -> child   "X\n"                     shutdown
**   child  -> parent  "READY\n"                 on startup
**   child  -> parent  "OK\n" | "ERR <msg>\n"    after S
**   child  -> parent  "OK <count>\n<val>\n..."  after Q
**   child  -> parent  "BYE\n"                   after X
**
** Query results come back as a flat, row-major list of display strings
** (one per line) exactly like slt_sqlite.c fills azResult: NULL values are
** already rendered as the literal string "NULL", empty text as "(empty)".
*/
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

/*
** The subprocess database connection object
*/
typedef struct SubConn SubConn;
struct SubConn {
  pid_t pid;            /* child process id */
  int fdin;             /* write end of pipe to child stdin */
  int fdout;            /* read end of pipe from child stdout */
};

/*
** Read one line (up to and including '\n', which is replaced by '\0')
** from the child.  Return the length of the line, or -1 on EOF/error.
** The buffer is grown as needed.
*/
static int subReadLine(SubConn *p, char **pzBuf, int *pnBuf){
  int len = 0;
  int cap = (*pnBuf > 0) ? *pnBuf : 256;
  char *buf = *pzBuf;
  if( buf==0 ){
    buf = malloc(cap);
    if( buf==0 ) return -1;
    *pzBuf = buf;
    *pnBuf = cap;
  }
  for(;;){
    char c;
    ssize_t n;
    if( len+1 >= cap ){
      char *z;
      cap *= 2;
      z = realloc(buf, cap);
      if( z==0 ) return -1;
      buf = z;
      *pzBuf = buf;
      *pnBuf = cap;
    }
    n = read(p->fdout, &c, 1);
    if( n<=0 ) break;
    if( c=='\n' ) break;
    buf[len++] = c;
  }
  buf[len] = 0;
  return len;
}

/*
** Write all n bytes of buf to the child.
*/
static void subWriteAll(SubConn *p, const char *buf, int n){
  int off = 0;
  while( off<n ){
    ssize_t w = write(p->fdin, buf+off, n-off);
    if( w<=0 ){
      fprintf(stderr, "slt_subprocess: write to engine failed\n");
      exit(1);
    }
    off += (int)w;
  }
}

/*
** Send one protocol frame to the child.
*/
static void subSendFrame(SubConn *p, char kind, const char *types, const char *sql){
  char hdr[600];
  int n = (int)strlen(sql);
  if( types && types[0] ){
    snprintf(hdr, sizeof(hdr), "%c %s %d\n", kind, types, n);
  }else{
    snprintf(hdr, sizeof(hdr), "%c %d\n", kind, n);
  }
  subWriteAll(p, hdr, (int)strlen(hdr));
  subWriteAll(p, sql, n);
}

/*
** Open a connection: spawn the engine subprocess and wait for READY.
** zConnectStr is the command line to run.
*/
static int subConnect(
  void *NotUsed,
  const char *zConnectStr,
  void **ppConn,
  const char *zParam
){
  const char *zCmd = zConnectStr;
  int toChild[2];
  int fromChild[2];
  pid_t pid;
  SubConn *p;
  char *zLine = 0;
  int nLine = 0;
  int n;

  if( zCmd==0 || zCmd[0]==0 ) zCmd = getenv("SQLDB0819_ENGINE_CMD");
  if( zCmd==0 || zCmd[0]==0 ) zCmd = "python3 -m sql_db_0819.cli";

  if( pipe(toChild)!=0 || pipe(fromChild)!=0 ) return 1;
  pid = fork();
  if( pid<0 ) return 1;
  if( pid==0 ){
    /* Child: wire pipes to stdin/stdout and exec the engine command. */
    dup2(toChild[0], 0);
    dup2(fromChild[1], 1);
    close(toChild[0]); close(toChild[1]);
    close(fromChild[0]); close(fromChild[1]);
    execl("/bin/sh", "sh", "-c", zCmd, (char*)0);
    _exit(127);
  }
  close(toChild[0]);
  close(fromChild[1]);

  p = calloc(1, sizeof(*p));
  if( p==0 ) return 1;
  p->pid = pid;
  p->fdin = toChild[1];
  p->fdout = fromChild[0];

  n = subReadLine(p, &zLine, &nLine);
  if( n<0 || strcmp(zLine, "READY")!=0 ){
    fprintf(stderr, "slt_subprocess: engine did not become ready (got [%s])\n",
            zLine ? zLine : "");
    free(zLine);
    return 1;
  }
  free(zLine);
  *ppConn = (void*)p;
  return 0;
}

/*
** Evaluate the single SQL statement in zSql.  Return 0 on success,
** non-zero on any error.
*/
static int subStatement(
  void *pConn,
  const char *zSql,
  int bQuiet
){
  SubConn *p = (SubConn*)pConn;
  char *zLine = 0;
  int nLine = 0;
  int n;
  subSendFrame(p, 'S', 0, zSql);
  n = subReadLine(p, &zLine, &nLine);
  if( n<0 ){
    free(zLine);
    return 1;
  }
  if( strncmp(zLine, "OK", 2)==0 ){
    free(zLine);
    return 0;
  }
  if( !bQuiet && strncmp(zLine, "ERR", 3)==0 ){
    fprintf(stderr, "engine error: %s\n", zLine+4);
  }
  free(zLine);
  return 1;
}

/*
** Run a query and accumulate the results into an array of pointers to
** strings (flat, row-major).  NULL values arrive already rendered as
** "NULL"; empty strings as "(empty)".  Return 0 on success, 1 on error.
*/
static int subQuery(
  void *pConn,
  const char *zSql,
  const char *zType,
  char ***pazResult,
  int *pnResult
){
  SubConn *p = (SubConn*)pConn;
  char *zLine = 0;
  int nLine = 0;
  int n, i;
  int nVal = 0;
  char **az = 0;
  subSendFrame(p, 'Q', zType, zSql);
  n = subReadLine(p, &zLine, &nLine);
  if( n<0 ){
    free(zLine);
    return 1;
  }
  if( strncmp(zLine, "ERR", 3)==0 ){
    fprintf(stderr, "engine error: %s\n", zLine+4);
    free(zLine);
    return 1;
  }
  if( strncmp(zLine, "OK", 2)!=0 ){
    fprintf(stderr, "slt_subprocess: bad query response [%s]\n", zLine);
    free(zLine);
    return 1;
  }
  nVal = atoi(zLine+3);
  /* NOTE: zLine stays allocated; the value loop below reuses it. */
  if( nVal>0 ){
    az = calloc(nVal, sizeof(az[0]));
    if( az==0 ){ free(zLine); return 1; }
  }
  for(i=0; i<nVal; i++){
    n = subReadLine(p, &zLine, &nLine);
    if( n<0 ){
      int j;
      for(j=0; j<i; j++) free(az[j]);
      free(az);
      free(zLine);
      return 1;
    }
    az[i] = strdup(zLine);
  }
  free(zLine);
  *pazResult = az;
  *pnResult = nVal;
  return 0;
}

/*
** Free memory returned by xQuery.
*/
static int subFreeResults(
  void *pConn,
  char **azResult,
  int nResult
){
  int i;
  for(i=0; i<nResult; i++) free(azResult[i]);
  free(azResult);
  return 0;
}

/*
** Return the name reported by the engine.  This name is matched against
** "skipif"/"onlyif" tokens in test scripts (case-insensitively); it is
** deliberately not "SQLite" so the upstream test files never skip.
*/
static int subGetEngineName(
  void *pConn,
  const char **zName
){
  static const char *zEngineName = "sql-db-0819";
  *zName = zEngineName;
  return 0;
}

/*
** Close the connection: tell the child to shut down and reap it.
*/
static int subDisconnect(
  void *pConn
){
  SubConn *p = (SubConn*)pConn;
  int status = 0;
  char *zLine = 0;
  int nLine = 0;
  if( p==0 ) return 0;
  subWriteAll(p, "X\n", 2);
  subReadLine(p, &zLine, &nLine);   /* expect BYE (ignore result) */
  free(zLine);
  close(p->fdin);
  close(p->fdout);
  waitpid(p->pid, &status, 0);
  free(p);
  return 0;
}

/*
** Register the subprocess database engine with the main driver.
*/
void registerSubprocess(void){
  static const DbEngine subDbEngine = {
    "sql-db-0819",      /* zName */
    0,                  /* pAuxData */
    subConnect,         /* xConnect */
    subGetEngineName,   /* xGetEngineName */
    subStatement,       /* xStatement */
    subQuery,           /* xQuery */
    subFreeResults,     /* xFreeResults */
    subDisconnect       /* xDisconnect */
  };
  sqllogictestRegisterEngine(&subDbEngine);
}
