# Synapse / SQL Server → Snowflake migration plan: `banking-etl-warehouse`

This repo is the reference Synapse-dialect workload for the Chubb Synapse → Snowflake consolidation.
This document is the **contract** every work unit (one child session / one PR each) builds against.
If a unit needs to deviate from a contract below, it must say so in its PR description.

## 1. Inventory

### 1.1 Source system (`data_sources/`)

| Source | Format | Rows | Columns (source type) | Consumed by |
|---|---|---|---|---|
| `sample.dbo.state` | SQL Server table (in `sample.bak`) | 9 | `state_id INT PK`, `state_name VARCHAR(50)` | Load_DimCustomer |
| `sample.dbo.city` | SQL Server table | 52 | `city_id INT PK`, `city_name VARCHAR(50)`, `state_id INT FK→state` | Load_DimCustomer |
| `sample.dbo.customer` | SQL Server table | 20 | `customer_id INT PK`, `customer_name VARCHAR(50)`, `address VARCHAR(MAX)`, `city_id INT FK→city`, `age VARCHAR(3)`, `gender VARCHAR(10)`, `email VARCHAR(50)` | Load_DimCustomer |
| `sample.dbo.account` | SQL Server table | 21 | `account_id INT PK`, `customer_id INT FK→customer`, `account_type VARCHAR(10)`, `balance INT`, `date_opened DATETIME2(0)`, `status VARCHAR(10)` | Load_DimAccount |
| `sample.dbo.branch` | SQL Server table | 5 | `branch_id INT PK`, `branch_name VARCHAR(50)`, `branch_location VARCHAR(50)` | Load_DimBranch |
| `sample.dbo.transaction_db` | SQL Server table | 10 | `transaction_id INT PK`, `account_id INT FK`, `transaction_date DATETIME2(0)`, `amount INT`, `transaction_type VARCHAR(50)`, `branch_id INT FK` | Load_FactTransaction |
| `transaction_excel.xlsx` (`Sheet1`) | Excel, 1 header row | 7 | same 6 columns; dates are Excel serial datetimes | Load_FactTransaction |
| `transaction_csv.csv` | CSV, `,`, `\n`, 1 header row, ISO-8859-15 | 12 | same 6 columns; `transaction_date` is `dd-MM-yyyy HH:mm:ss` | Load_FactTransaction |

`sample.bak` is a SQL Server 2019 backup (collation `SQL_Latin1_General_CP1_CI_AS`); Snowflake cannot read it.
This PR adds **`data_sources/sample_db_extract/*.csv`**: one CSV per source table, extracted by restoring the
backup into SQL Server 2022 (Docker) and exporting with `FOR JSON` → CSV (RFC 4180, `\n`, UTF-8, header row,
empty field = NULL, datetimes as `YYYY-MM-DD HH24:MI:SS`). These CSVs are the extracts the Snowflake loads
read and validation compares against. In production the equivalent is a Synapse `CETAS`/`COPY` export to ADLS.

### 1.2 Target warehouse (`sql_scripts/01_create_tables.sql`, database `DWH`, schema `dbo`)

| Table | Columns | Keys |
|---|---|---|
| `DimAccount` | `AccountID INT`, `CustomerID INT`, `AccountType VARCHAR(50)`, `Balance MONEY`, `DateOpened DATE`, `Status VARCHAR(50)` | PK `AccountID` |
| `DimBranch` | `BranchID INT`, `BranchName VARCHAR(100)`, `BranchLocation VARCHAR(255)` | PK `BranchID` |
| `DimCustomer` | `CustomerID INT`, `CustomerName VARCHAR(100)`, `Address VARCHAR(255)`, `CityName VARCHAR(100)`, `StateName VARCHAR(100)`, `Age INT`, `Gender VARCHAR(10)`, `Email VARCHAR(100)` | PK `CustomerID` |
| `FactTransaction` | `TransactionID INT`, `AccountID INT`, `TransactionDate DATETIME`, `Amount MONEY`, `TransactionType VARCHAR(50)`, `BranchID INT` | PK `TransactionID`; FK `AccountID→DimAccount`, FK `BranchID→DimBranch` |

No views exist. No `DimCustomer` FK from `DimAccount` (not declared).

### 1.3 Stored procedures (`sql_scripts/02_create_procedures.sql`)

| Procedure | Params | Logic | Constructs |
|---|---|---|---|
| `sp_DailyTransaction` | `@start_date DATE`, `@end_date DATE` | `COUNT(TransactionID)`, `SUM(Amount)` per `CAST(TransactionDate AS DATE)` where that date `BETWEEN` the params, ordered by date | `SET NOCOUNT ON`, `CAST(... AS DATE)`, `[bracket]` alias, result set returned implicitly |
| `sp_BalancePerCustomer` | `@customer_name VARCHAR(100)` | CTE sums `+Amount` for `TransactionType='Deposit'` else `-Amount` per account; returns `CustomerName, AccountType, Balance AS InitialBalance, Balance + ISNULL(sum,0) AS CurrentBalance` for `CustomerName LIKE '%'+@customer_name+'%' AND Status='active'` | CTE, `CASE`, `ISNULL`, `+` string concat, `LIKE`, implicit result set, **no ORDER BY** |

**Bug in source:** `02_create_procedures.sql` fails as written (`'CREATE/ALTER PROCEDURE' must be the first statement in a query batch`) because a `PRINT` precedes each `CREATE PROCEDURE` without a `GO`. Baselines were produced after inserting `GO`.

### 1.4 T-SQL / Synapse construct inventory

| Construct | Present in repo? | Where |
|---|---|---|
| `IF NOT EXISTS (SELECT * FROM sys.databases ...) CREATE DATABASE`, `USE`, `GO`, `PRINT` | Yes | both scripts |
| `MONEY`, `DATETIME`, `INT`, `VARCHAR(n)`, `DATE` | Yes | DDL |
| `PRIMARY KEY` / `FOREIGN KEY` (enforced) | Yes | DDL |
| `SET NOCOUNT ON`, implicit result sets from procs | Yes | procs |
| `CAST(x AS DATE)`, `ISNULL`, `+` string concat, `LIKE` (case-insensitive collation), CTE, `CASE` | Yes | procs |
| `IDENTITY` | **No** (all keys are natural keys from source) | – |
| `MERGE` | **No** (Talend does INSERT / TRUNCATE+INSERT) | – |
| `TRY/CATCH`, `RAISERROR`/`THROW`, `@@ERROR` | **No** | – |
| `#temp` tables / table variables | **No** | – |
| `DATEADD` / `DATEDIFF` / `CONVERT(style)` / `GETDATE` | **No** | – |
| Synapse `DISTRIBUTION = HASH/ROUND_ROBIN/REPLICATE`, `CLUSTERED COLUMNSTORE INDEX`, `PARTITION`, index/query hints, `CTAS` | **No** | – |

Constructs marked **No** are still mapped in §2 because the wider Chubb Synapse estate uses them; units must
apply those mappings wherever they introduce the equivalent behaviour (e.g. error handling, idempotent upserts).

### 1.5 Talend jobs (`talend_jobs/*.zip`, Talend Open Studio project `IDX_INTERNSHIP`)

Run order per README: `Load_DimBranch` → `Load_DimAccount` → `Load_DimCustomer` → `Load_FactTransaction`.
All `tMSSqlOutput` components: `DIE_ON_ERROR=false` (failed rows go to an unused REJECT flow, job continues), `COMMIT_EVERY=10000`, encoding ISO-8859-15.

| Job | Sources | Transform | Target / action |
|---|---|---|---|
| `Load_DimBranch` | `tMSSqlInput` `SELECT branch_id, branch_name, branch_location FROM dbo.branch` | `tMap` 1:1 rename | `DimBranch`, `CREATE_IF_NOT_EXISTS` + `INSERT` (re-run ⇒ PK violations silently rejected ⇒ **insert-only, existing rows never updated**) |
| `Load_DimAccount` | `tMSSqlInput` `dbo.account` (6 cols) | `tMap` 1:1 rename; `date_opened` DATETIME2 → `DateOpened DATE` (time truncated) | `DimAccount`, `CREATE_IF_NOT_EXISTS` + `INSERT` (insert-only) |
| `Load_DimCustomer` | `tMSSqlInput` ×3: `dbo.customer`, `dbo.city`, `dbo.state` | `tMap`: `customer LEFT OUTER JOIN city ON city_id LEFT OUTER JOIN state ON city.state_id` (Talend default join model, unique match, load once); `UPCASE(customer_name)`, `UPCASE(address)`, `UPCASE(gender)`; `email` unchanged; `age` String → `Age INT` | `DimCustomer`, `CREATE_IF_NOT_EXISTS` + `INSERT` (insert-only) |
| `Load_FactTransaction` | `tMSSqlInput` `dbo.transaction_db`; `tFileInputExcel` `transaction_excel.xlsx` Sheet1, header 1; `tFileInputDelimited` `transaction_csv.csv`, `,`, header 1, date pattern `dd-MM-yyyy HH:mm:ss` | `tUnite` (DB, Excel, CSV) → `tUniqRow` key `transaction_id` (keeps **first** occurrence, duplicates discarded) → `tMap` 1:1 rename | `FactTransaction`, `TRUNCATE` + `INSERT` (full reload every run). FK violations rejected silently. |

### 1.6 Data behaviour discovered (drives semantics; reproduced in `migration/baseline/`)

- **Cross-source duplicates:** `transaction_id` 6, 7 (DB vs Excel) and 14, 15 (Excel vs CSV).
  14/15 are identical; **6/7 differ in `transaction_date`** (DB `2022-02-21`, Excel `2024-01-18/19`). Which row
  Talend kept depends on `tUnite` input order. **Decision (default, flagged for business sign-off):** source
  precedence `DB (1) > EXCEL (2) > CSV (3)` (the `tUnite` connection order). Implemented with
  `QUALIFY ROW_NUMBER() OVER (PARTITION BY transaction_id ORDER BY source_rank, _loaded_at) = 1`.
- **Orphan transactions:** CSV `transaction_id` 23, 24, 25 reference `account_id` 22/23, which do not exist in
  `account`. SQL Server rejected them via the enforced FK (`DIE_ON_ERROR=false` ⇒ silently dropped).
  Snowflake does **not** enforce FKs, so the load must filter them explicitly and write them to a reject table.
- Resulting SQL Server DWH: `DimBranch` 5, `DimAccount` 21, `DimCustomer` 20, `FactTransaction` **22** rows.
- Case-insensitive collation: `sp_BalancePerCustomer @customer_name='shelly'` matches `SHELLY JUWITA` in SQL
  Server. Snowflake `LIKE` is case-sensitive ⇒ must use `ILIKE` to preserve behaviour.

`migration/baseline/` holds the SQL Server ground truth produced by running the original DDL + procedures on
SQL Server 2022 and emulating the four Talend jobs (`emulate_talend_on_sqlserver.sql`): the four DWH tables and
the outputs of both procedures for the parameter sets in the file names. Every unit's output must reproduce these.

## 2. T-SQL / Synapse → Snowflake mapping

| T-SQL / Synapse | Snowflake | Notes |
|---|---|---|
| `CREATE DATABASE DWH` + `IF NOT EXISTS (sys.databases)` | `CREATE DATABASE IF NOT EXISTS BANKING_DWH` + `CREATE SCHEMA IF NOT EXISTS` | Synapse dedicated pool = one DB; we split by schema (§3) |
| `USE DWH; GO` | `USE DATABASE/SCHEMA`; no batch separator (`;` only) | |
| `PRINT 'msg'` | `SYSTEM$LOG_INFO` inside procedures; omitted in plain DDL | |
| `INT` | `NUMBER(10,0)` (written `INT` is fine: alias of `NUMBER(38,0)`) | Use `INT` for keys |
| `MONEY` | `NUMBER(19,4)` | Exact; same precision/scale as SQL Server MONEY |
| `DATETIME` / `DATETIME2(n)` | `TIMESTAMP_NTZ(3)` / `TIMESTAMP_NTZ(n)` | No time zone in source; DATETIME precision ≈ 3 ms |
| `DATE` | `DATE` | |
| `VARCHAR(n)` / `VARCHAR(MAX)` / `NVARCHAR` | `VARCHAR(n)` / `VARCHAR` / `VARCHAR` | Snowflake strings are UTF-8; length is chars |
| `PRIMARY KEY` / `FOREIGN KEY` | Declared `PRIMARY KEY` / `FOREIGN KEY ... RELY` (metadata only, **not enforced**) | Enforcement moves into the load (dedup + orphan reject) and the validation harness |
| `NOT NULL` | `NOT NULL` (enforced) | |
| `IDENTITY(1,1)` | `NUMBER AUTOINCREMENT START 1 INCREMENT 1 ORDER`, or `CREATE SEQUENCE` + `DEFAULT seq.NEXTVAL` | Not used by this schema (natural keys). Surrogate keys, if added, use sequences |
| `DISTRIBUTION = HASH(col)` / `ROUND_ROBIN` / `REPLICATE` | Nothing / `CLUSTER BY (...)` on large tables | Micro-partitioning replaces distribution. Fact: `CLUSTER BY (TO_DATE(TransactionDate))`; dimensions: none |
| `CLUSTERED COLUMNSTORE INDEX`, nonclustered indexes, `WITH (INDEX=...)`, `OPTION (...)` hints | Dropped | Search Optimization Service only if a point-lookup need is proven |
| `PARTITION (col RANGE ...)` | Dropped; clustering key covers pruning | |
| `CTAS` | `CREATE TABLE ... AS SELECT` | |
| `#temp` / `@table` | `CREATE TEMPORARY TABLE` / `RESULTSET` / CTE | |
| `CREATE PROCEDURE ... AS BEGIN ... END` returning a result set | `CREATE OR REPLACE PROCEDURE ... RETURNS TABLE(...) LANGUAGE SQL AS $$ ... RETURN TABLE(res); $$` | Snowflake Scripting |
| `@param` | `param` (bind as `:param` inside SQL statements) | |
| `SET NOCOUNT ON` | Dropped | No equivalent / not needed |
| `BEGIN TRY ... END TRY BEGIN CATCH ... END CATCH` | `BEGIN ... EXCEPTION WHEN OTHER THEN ... END` | `ERROR_MESSAGE()`→`SQLERRM`, `ERROR_NUMBER()`→`SQLCODE`, `ERROR_STATE()`→`SQLSTATE` |
| `THROW` / `RAISERROR` | `RAISE` (re-raise) / `DECLARE e EXCEPTION (-20001, 'msg'); RAISE e;` | |
| `BEGIN TRAN / COMMIT / ROLLBACK` | `BEGIN TRANSACTION; COMMIT; ROLLBACK;` | |
| `MERGE` | `MERGE` | Snowflake errors on nondeterministic multi-match; dedup source first |
| `ISNULL(a,b)` | `COALESCE(a,b)` (or `IFNULL`) | |
| `'a' + 'b'` | `'a' \|\| 'b'` / `CONCAT` | `+` on strings is an error in Snowflake |
| `LIKE` under `*_CI_*` collation | `ILIKE` (or `COLLATE 'en-ci'`) | Snowflake comparison is case-sensitive by default |
| `x = 'literal'` under CI collation | `x = 'literal'` exact (data already matches case) or `UPPER()` / `COLLATE` | Document each choice |
| `CAST(dt AS DATE)` | `CAST(dt AS DATE)` / `TO_DATE(dt)` | |
| `CONVERT(type, x, style)` | `TO_CHAR/TO_DATE/TO_TIMESTAMP(x, 'fmt')` | |
| `DATEADD(day, n, d)` / `DATEDIFF` | `DATEADD(day, n, d)` / `DATEDIFF` | Same names; `DATEDIFF` counts boundaries in both |
| `GETDATE()` / `SYSDATETIME()` | `CURRENT_TIMESTAMP()` (session TZ!) / `SYSDATE()` for UTC NTZ | |
| `TOP n` | `LIMIT n` (`TOP` also supported) | |
| `[identifier]` | Unquoted identifier (upper-cased) or `"Identifier"` | We use unquoted names (§3) |
| Talend `tMSSqlInput` / `tFileInputDelimited` | External/internal stage + `COPY INTO` raw table | |
| Talend `tFileInputExcel` | xlsx → CSV conversion (`openpyxl`) before staging, **or** Snowpark Python procedure reading the staged xlsx | `COPY INTO` cannot parse xlsx |
| Talend `tMap` | `SELECT` with `UPPER`, joins, casts | |
| Talend `tUnite` + `tUniqRow` | `UNION ALL` + `QUALIFY ROW_NUMBER() ... = 1` | Precedence rule §1.6 |
| Talend `tMSSqlOutput` INSERT, `DIE_ON_ERROR=false` | `MERGE ... WHEN NOT MATCHED THEN INSERT` + reject table | |
| Talend `TRUNCATE` + `INSERT` | `INSERT OVERWRITE INTO` or incremental `MERGE` from a stream | |
| Talend job scheduling / run order | Task graph (`CREATE TASK ... AFTER ...`) | |

## 3. Shared naming and object contract

All units use these exact names. Unquoted identifiers (Snowflake upper-cases them), so `DimAccount` resolves to
`DIMACCOUNT` and existing report SQL keeps working.

- Database: `BANKING_DWH`. Every script must work if the database is renamed, so reference objects as
  `schema.object` after `USE DATABASE BANKING_DWH;` at the top of each deploy script.
- Schemas:
  - `RAW`: landing tables written by `COPY INTO`, one per source, source column names, plus metadata columns
    `_SOURCE_FILE VARCHAR`, `_SOURCE_ROW NUMBER`, `_LOADED_AT TIMESTAMP_NTZ DEFAULT SYSDATE()`.
    Tables: `RAW.STATE`, `RAW.CITY`, `RAW.CUSTOMER`, `RAW.ACCOUNT`, `RAW.BRANCH`, `RAW.TRANSACTION_DB`,
    `RAW.TRANSACTION_EXCEL`, `RAW.TRANSACTION_CSV`. Raw columns are typed (e.g. `transaction_date TIMESTAMP_NTZ(0)`), parsed by the file format / COPY transform.
  - `DWH`: star schema. `DWH.DimAccount`, `DWH.DimBranch`, `DWH.DimCustomer`, `DWH.FactTransaction` with the
    exact column names of §1.2 and types per §2 (`MONEY`→`NUMBER(19,4)`, `DATETIME`→`TIMESTAMP_NTZ(3)`, `INT`→`INT`).
    `FactTransaction` additionally carries `SourceSystem VARCHAR(10)` (`DB`/`EXCEL`/`CSV`) and
    `LoadedAt TIMESTAMP_NTZ(3)`; these are lineage columns and are excluded from parity checks.
    Reporting procedures `DWH.SP_DAILYTRANSACTION`, `DWH.SP_BALANCEPERCUSTOMER`.
  - `OPS`: stages, file formats, streams, tasks, load procedures, and operational tables:
    - `OPS.ETL_ERROR_LOG (LOG_ID INT AUTOINCREMENT, LOGGED_AT TIMESTAMP_NTZ DEFAULT SYSDATE(), PROCEDURE_NAME VARCHAR, SQLCODE NUMBER, SQLSTATE VARCHAR(5), SQLERRM VARCHAR, CONTEXT VARIANT)`. Written by every procedure's `EXCEPTION` handler before re-raising.
    - `OPS.FACTTRANSACTION_REJECTS`: the 6 source columns + `SourceSystem`, `RejectReason VARCHAR` (`ORPHAN_ACCOUNT`, `ORPHAN_BRANCH`, `DUPLICATE_LOWER_PRECEDENCE`), `RejectedAt TIMESTAMP_NTZ`.
- Procedure signatures (Snowflake Scripting, `LANGUAGE SQL`):
  - `DWH.SP_DAILYTRANSACTION(START_DATE DATE, END_DATE DATE) RETURNS TABLE ("Date" DATE, TOTALTRANSACTIONS NUMBER, TOTALAMOUNT NUMBER(19,4))`
  - `DWH.SP_BALANCEPERCUSTOMER(CUSTOMER_NAME VARCHAR) RETURNS TABLE (CUSTOMERNAME VARCHAR, ACCOUNTTYPE VARCHAR, INITIALBALANCE NUMBER(19,4), CURRENTBALANCE NUMBER(19,4))`
  - Load procedures (unit 3): `OPS.SP_LOAD_DIMBRANCH()`, `OPS.SP_LOAD_DIMACCOUNT()`, `OPS.SP_LOAD_DIMCUSTOMER()`, `OPS.SP_LOAD_FACTTRANSACTION()`, each `RETURNS VARCHAR` (JSON summary of rows inserted/rejected).
- Connection env vars (validation harness, live CI checks, any helper script): `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`,
  `SNOWFLAKE_PASSWORD` **or** `SNOWFLAKE_PRIVATE_KEY_PATH` (+ optional `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`),
  `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE` (default `BANKING_DWH`). Live checks run only
  when `SNOWFLAKE_ACCOUNT` is set; otherwise they skip cleanly (exit 0).
- SQL style: `.sqlfluff` at repo root (snowflake dialect, raw templater, upper-case keywords/functions/types,
  120 cols). **No templating variables** (`&var`, `{{ }}`, `<% %>`) in committed SQL, so sqlfluff parses it.
  `sql_scripts/` (legacy T-SQL) is excluded via `.sqlfluffignore`. Every unit's SQL must pass
  `sqlfluff lint migration/` with this config. Pin `sqlfluff==3.2.5`.
- Python: 3.10+, `snowflake-connector-python`, `pytest`. Offline tests must not import the connector at module
  import time if it is not installed, and must never need credentials.

## 4. Repository layout and unit ownership

Each unit only creates/edits files it owns, so the five PRs merge without conflicts in any order.

```
migration/
  PLAN.md                         # this file (plan PR)
  baseline/                       # SQL Server ground truth (plan PR)
  README.md                       # unit 5
  snowflake/
    ddl/                          # unit 1: 00_database_schemas.sql, 01_ops_tables.sql, 10_dim_*.sql, 20_fact_*.sql, ...
    procedures/                   # unit 2: sp_dailytransaction.sql, sp_balancepercustomer.sql
    loads/                        # unit 3: stages, file formats, RAW tables, COPY INTO, load procs, streams, tasks
  loads/                          # unit 3: TALEND_REPLACEMENT.md, xlsx_to_csv.py, tests/
  validation/                     # unit 4: validate.py, sql/, tests/, requirements.txt
  tests/                          # unit 5: repo-level offline checks (e.g. layout/contract checks)
.github/workflows/snowflake-migration.yml   # unit 5
.sqlfluff, .sqlfluffignore        # plan PR
data_sources/sample_db_extract/   # plan PR
```

Deploy order (documented by unit 5): `ddl/` → `loads/` (stages, RAW, load procs, streams/tasks) → `procedures/` → run loads → `validation/`.

## 5. Work units (parallel, one PR each, all branch from this plan's branch)

| # | Unit | Owns | Done when |
|---|---|---|---|
| 1 | **Snowflake DDL** | `migration/snowflake/ddl/` | Database, `RAW`/`DWH`/`OPS` schemas; 4 DWH tables with §2 types, declared PK/FK (`RELY`), `CLUSTER BY` on fact instead of distribution, `OPS.ETL_ERROR_LOG`, `OPS.FACTTRANSACTION_REJECTS`; sequence/identity pattern documented (keys are natural); idempotent (`CREATE ... IF NOT EXISTS` / `OR REPLACE` choice justified); lints clean; PR explains every type/constraint decision |
| 2 | **Stored procedures** | `migration/snowflake/procedures/` | Both procs ported to Snowflake Scripting per §3 signatures, same results as `migration/baseline/sp_*.csv` (incl. `ILIKE` for CI collation, `COALESCE`, deterministic `ORDER BY` added and documented), `EXCEPTION` handler logging to `OPS.ETL_ERROR_LOG` then `RAISE`, parameter validation (`START_DATE > END_DATE`, NULLs) raising a named exception; example `CALL`s; lints clean |
| 3 | **Loads (Talend replacement)** | `migration/snowflake/loads/`, `migration/loads/` | Internal stage + file formats; `RAW.*` tables; `COPY INTO` for the 6 DB extracts and the CSV; xlsx handled (conversion script and/or Snowpark proc, justified); `OPS.SP_LOAD_*` procs reproducing each `tMap` (UPPER, LEFT JOINs, DATE truncation, `Age` cast) with insert-only `MERGE` for dims; fact load = union + precedence dedup + orphan reject to `OPS.FACTTRANSACTION_REJECTS`; incremental fact path using Streams on `RAW.TRANSACTION_*` + Task graph (and Snowpipe definition for the file sources, auto-ingest commented as environment-specific); `TALEND_REPLACEMENT.md` mapping each Talend component to its replacement; offline tests (e.g. xlsx conversion, dedup/orphan logic in Python mirrors SQL) reproduce `migration/baseline/FactTransaction.csv` |
| 4 | **Validation harness** | `migration/validation/` | `validate.py` (snowflake-connector-python, env-var connection §3) + SQL that, for every RAW and DWH table, compares row counts, key sums (`SUM` of every key and amount column) and per-row/sample checksums between the source extracts (`data_sources/`) or `migration/baseline/` and Snowflake; also compares both procedure outputs to baselines; deterministic canonicalisation documented; JSON + human report; non-zero exit on mismatch; `--offline` mode / pytest suite that computes the expected values from the extracts and checks them against `migration/baseline/` without Snowflake; live mode skipped when `SNOWFLAKE_ACCOUNT` unset |
| 5 | **Docs and CI** | `migration/README.md`, `migration/tests/`, `.github/workflows/snowflake-migration.yml` | Cutover runbook (freeze, final extract, deploy order, load, validate, switch consumers, decommission), rollback plan, known semantic differences (§1.6 + case sensitivity, FK non-enforcement, timestamp/TZ, MONEY rounding, ordering, `+` concat, `DIE_ON_ERROR` vs fail-fast), GitHub Actions job: `sqlfluff lint migration/` (snowflake dialect, `.sqlfluff`), `pytest migration` (tolerates other units' tests not yet existing), and a live job that runs `migration/validation/validate.py` only when Snowflake secrets are configured; CI green without any credentials |

Out of scope: dashboards/BI, Synapse-side export automation (documented as the production equivalent of §1.1),
re-modelling the star schema beyond the lineage columns above.
