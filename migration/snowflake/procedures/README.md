# Unit 2: reporting stored procedures (Synapse / SQL Server → Snowflake Scripting)

| File | Purpose |
|---|---|
| `sp_dailytransaction.sql` | `DWH.SP_DAILYTRANSACTION(START_DATE DATE, END_DATE DATE)`, port of `dbo.sp_DailyTransaction` |
| `sp_balancepercustomer.sql` | `DWH.SP_BALANCEPERCUSTOMER(CUSTOMER_NAME VARCHAR)`, port of `dbo.sp_BalancePerCustomer` |
| `example_calls.sql` | Example `CALL`s with the expected results, error examples, log query |
| `tests/` | Offline tests (no Snowflake account): baseline parity on DuckDB, structural/contract checks, lint |

Signatures are exactly those of `migration/PLAN.md` §3. Deploy after `ddl/` (needs `DWH.*` tables and
`OPS.ETL_ERROR_LOG`) and `loads/`; each script starts with `USE DATABASE BANKING_DWH;` and is idempotent
(`CREATE OR REPLACE PROCEDURE`). Run with e.g. `snow sql -f sp_dailytransaction.sql` or `snowsql -f ...`.

## Mapping decisions

| T-SQL (source) | Snowflake (port) | Why / semantic difference |
|---|---|---|
| `CREATE PROCEDURE sp_X @p ... AS BEGIN ... END` with an implicit result set | `CREATE OR REPLACE PROCEDURE DWH.SP_X(P ...) RETURNS TABLE (...) LANGUAGE SQL AS $$ ... RETURN TABLE(res); $$` | Snowflake procedures return only what they `RETURN`; a `RESULTSET` + `RETURN TABLE` is the documented way to return rows. Body is `$$`-quoted so it deploys through SnowSQL / Snowflake CLI / Python connector. `OR REPLACE` keeps redeploys idempotent (grants on the procedure must be re-applied or use future grants). |
| `@start_date`, `@end_date`, `@customer_name` | `START_DATE`, `END_DATE`, `CUSTOMER_NAME`, referenced as `:START_DATE` etc. inside SQL | §2 parameter mapping. `@customer_name VARCHAR(100)` silently truncated longer input in T-SQL; `VARCHAR` here does not truncate. |
| `PRINT '...'` before/after each `CREATE PROCEDURE`, `USE DWH; GO` | Dropped; `USE DATABASE BANKING_DWH;` | Deploy-time chatter, not procedure logic. `GO` has no Snowflake equivalent (`;` separates statements). |
| `SET NOCOUNT ON` | Dropped | Snowflake sends no "rows affected" messages. |
| Ownership chaining (caller needs `EXECUTE` only) | `EXECUTE AS OWNER` | Consumers need `USAGE` on the procedure, not `SELECT` on `DWH` tables, matching how the SQL Server procs were used. |
| `CAST(TransactionDate AS DATE) BETWEEN @start_date AND @end_date` | Same expression | Truncating the `TIMESTAMP_NTZ(3)` to `DATE` before `BETWEEN` keeps both ends inclusive, including transactions later in the day on `END_DATE`. |
| `COUNT(TransactionID)` (INT), `SUM(Amount)` (MONEY) | `COUNT(...)` → `NUMBER`; `CAST(SUM(AMOUNT) AS NUMBER(19, 4))` | §2: `MONEY` → `NUMBER(19,4)`. The cast keeps the declared return type. SQL Server raises arithmetic overflow above ±922,337,203,685,477.5807 (MONEY range); `NUMBER(19,4)` overflows only above 999,999,999,999,999.9999. |
| `[Date]` alias | `"Date"` (quoted) | §3 contract keeps the T-SQL column name and case. Callers must write `"Date"`; the other columns are unquoted and resolve upper-case (`TOTALTRANSACTIONS`, ...). |
| `ORDER BY [Date]` | `ORDER BY "Date"` | Unchanged. |
| CTE `TransactionSummary`, `CASE`, `LEFT JOIN` | Same | Supported as-is. |
| `ISNULL(ts.TotalTransactionAmount, 0)` | `COALESCE(TS.TOTALTRANSACTIONAMOUNT, 0)` | `ISNULL` does not exist in Snowflake with these semantics (§2). With two `NUMBER` arguments the result is identical. |
| `'%' + @customer_name + '%'` | `'%' \|\| :CUSTOMER_NAME \|\| '%'` | `+` on strings is an error in Snowflake. A NULL operand makes both forms NULL (but NULL is now rejected up front, see below). |
| `CustomerName LIKE ...` under `SQL_Latin1_General_CP1_CI_AS` | `C.CUSTOMERNAME ILIKE (...)` | Snowflake `LIKE` is case-sensitive; `ILIKE` keeps `'shelly'` matching `SHELLY JUWITA` (baseline). `%` and `_` are wildcards in both. T-SQL `[a-z]`/`[^...]` character classes are **not** LIKE syntax in Snowflake (brackets are literal), so a parameter containing `[` behaves differently. Case folding for non-Latin-1 characters follows Unicode rules in Snowflake vs code page 1252 rules in SQL Server. |
| `TransactionType = 'Deposit'`, `Status = 'active'` under CI collation | `UPPER(TRANSACTIONTYPE) = 'DEPOSIT'`, `UPPER(A.STATUS) = 'ACTIVE'` | Keeps case-insensitive equality, so a future `'DEPOSIT'`/`'Active'` from a source still counts as in SQL Server. SQL Server `=` also ignores trailing spaces; that is not reproduced; a test asserts the baseline data has no padding or case variants. Accent sensitivity (`_AS`) is the same. |
| No `ORDER BY` in `sp_BalancePerCustomer` | `ORDER BY A.ACCOUNTID` (added) | T-SQL returned rows in plan order (a clustered-PK scan of `DimAccount` in practice, not guaranteed). `AccountID` is the PK, so the order is total and deterministic, and it is exactly the baseline order. The output columns are unchanged (`AccountID` is not returned). |
| Unquoted mixed-case identifiers (`FactTransaction`, `AccountID`) | Written upper-case (`DWH.FACTTRANSACTION`, `ACCOUNTID`) | Same objects: Snowflake folds unquoted identifiers to upper case (§3). Upper case is what the repo's sqlfluff `CP02` rule accepts, so the embedded SELECTs lint clean on their own. Objects are schema-qualified (`DWH.`), never database-qualified. |
| No `TRY/CATCH`, no parameter checks | `DECLARE ... EXCEPTION (-200xx, '...')`, `IF ... RAISE`, `EXCEPTION WHEN OTHER THEN` → log → `RAISE;` | Required by PLAN §2/§5. See below. |

## Error handling and how it differs from T-SQL

Declared exceptions (user range −20000..−20999, unique across procedures; `SQLSTATE` is `P0001` for user exceptions):

| Code | Procedure | Raised when |
|---|---|---|
| −20001 | `SP_DAILYTRANSACTION` | `START_DATE` or `END_DATE` is NULL |
| −20002 | `SP_DAILYTRANSACTION` | `START_DATE > END_DATE` |
| −20003 | `SP_BALANCEPERCUSTOMER` | `CUSTOMER_NAME` is NULL (`''` is valid and matches everyone, as in T-SQL) |

Every error raised in the body (the validation exceptions above, or any runtime error such as a missing table or
a numeric overflow) reaches the single `EXCEPTION WHEN OTHER` handler, which copies `SQLCODE`, `SQLSTATE` and
`SQLERRM` into variables, inserts one row into `OPS.ETL_ERROR_LOG (PROCEDURE_NAME, SQLCODE, SQLSTATE, SQLERRM,
CONTEXT)` with the call's parameters in `CONTEXT` (`OBJECT_CONSTRUCT_KEEP_NULL`, so NULL parameters show up as
JSON `null`), and then re-raises the original exception with `RAISE;`. The caller sees the same code, state and message.

Differences from the original T-SQL behaviour:

1. **Invalid parameters now fail.** In T-SQL, NULL parameters (`BETWEEN NULL ...`, `LIKE NULL`) and
   `@start_date > @end_date` returned an **empty result set without error**. The port raises −20001/−20002/−20003.
   Callers that relied on "empty means nothing to show" must handle the error.
2. **Errors are logged.** The originals had no `TRY/CATCH`: errors propagated to the client and nothing was recorded.
3. **Log row and transactions.** Snowflake has no autonomous transactions. Under autocommit (the default) the log
   insert commits on its own. If the caller runs the `CALL` inside an explicit `BEGIN TRANSACTION` and then rolls back,
   the log row is rolled back as well. T-SQL `CATCH` + logging has the same limitation unless a table variable is used.
4. **If logging fails, that error wins.** If `OPS.ETL_ERROR_LOG` is missing or the owner lacks `INSERT`, the insert
   error is raised instead of the original error (no nested handler, to avoid hiding errors). `ddl/` creates the table
   before this script is deployed.
5. **No partial results, no batch semantics.** T-SQL streams rows and the effect of an error depends on its severity
   and `XACT_ABORT` (statement vs batch abort). In Snowflake the `CALL` either returns the full table or fails.
6. **Validation errors carry fixed messages.** Snowflake declared exceptions have constant messages; the offending
   values are in `OPS.ETL_ERROR_LOG.CONTEXT`, not in the message.
7. **Name resolution.** Like T-SQL deferred name resolution, `CREATE PROCEDURE` does not check that the `DWH` tables
   exist. A missing table is a runtime error, which is logged and re-raised.

## Example calls and expected results

Full list with error examples: `example_calls.sql`. Expected values are the SQL Server baselines in `migration/baseline/`.

```sql
CALL DWH.SP_DAILYTRANSACTION('2024-01-18'::DATE, '2024-01-20'::DATE);
-- "Date"=2024-01-20, TOTALTRANSACTIONS=3, TOTALAMOUNT=2000000.0000          (1 row)

CALL DWH.SP_DAILYTRANSACTION('2022-01-01'::DATE, '2024-12-31'::DATE);
-- 9 rows, 2022-01-01 | 2 | 1100000.0000 ... 2024-01-22 | 7 | 4380000.0000

CALL DWH.SP_BALANCEPERCUSTOMER('shelly');
-- SHELLY JUWITA | saving   |  1500000.0000 |  1600000.0000
-- SHELLY JUWITA | checking | 25000000.0000 | 14000000.0000

CALL DWH.SP_BALANCEPERCUSTOMER('');   -- 18 rows (every active account), ordered by AccountID
```

The `CALL` output cannot be used in a `FROM`; use `SELECT ... FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))` to
post-process it, and add an `ORDER BY` there if the order matters.

## Offline verification

```bash
pip install -r migration/snowflake/procedures/tests/requirements.txt
pytest migration/snowflake/procedures
sqlfluff lint migration/
```

- `tests/test_procedures_baseline.py` extracts each procedure's core SELECT (between `-- core-select:begin/end`),
  substitutes the parameters, transpiles it Snowflake → DuckDB with sqlglot and runs it on the baseline DWH CSVs
  loaded with the §2 types. It must reproduce all four `migration/baseline/sp_*.csv` row for row **in order**, also
  with the tables loaded in reverse order. It also checks the case-insensitive match (and that plain `LIKE` would lose
  it), inclusive date bounds, empty results, wildcard behaviour, and compares both procedures with an independent
  plain-Python computation.
- `tests/test_procedures_structure.py` checks that the signatures match PLAN.md §3, the script shape, that no T-SQL
  constructs are left, schema qualification, the validation order, exception codes, that the handler logs and then
  re-raises, that the embedded statements parse as Snowflake SQL (sqlglot), and that sqlfluff lints clean (the `$$`
  body is a string literal to sqlfluff, so the core SELECTs are also linted on their own).
- DuckDB/sqlglot/sqlfluff tests skip if those packages are not installed; the structural tests need only the standard library.

## Needs a live Snowflake account

- `CREATE` both procedures and `CALL` them against the loaded `DWH` tables (unit 4's harness compares the outputs
  with the baselines).
- Snowflake Scripting runtime behaviour that cannot be run offline: `RAISE;` re-raising the original
  `SQLCODE`/`SQLSTATE`/`SQLERRM`, `SQLSTATE = 'P0001'` for declared exceptions, the handler's `INSERT ... SELECT`
  with binds and `OBJECT_CONSTRUCT_KEEP_NULL`, and that `RETURN TABLE` keeps the `ORDER BY` order of the `CALL` output.
- `EXECUTE AS OWNER` privileges: a reporting role with only `USAGE` on the procedures can call them, and the owner
  role can insert into `OPS.ETL_ERROR_LOG`.
