# Snowflake DDL (unit 1)

DDL for database `BANKING_DWH`: the `RAW`/`DWH`/`OPS` schemas, the four star-schema tables and the `OPS`
operational tables from `migration/PLAN.md` §1.2 and §3. `RAW.*` tables, stages and file formats belong to the
loads unit (`migration/snowflake/loads/`); procedures belong to `migration/snowflake/procedures/`.

## Deploy order

Run the files in this order, as a role that can create databases (or with `BANKING_DWH` pre-created and
`USAGE` + `CREATE SCHEMA` granted). Every file is idempotent, so the whole list can be re-run on every deploy.

1. `00_database_schemas.sql`: database `BANKING_DWH`, schemas `RAW`, `DWH`, `OPS`
2. `01_ops_tables.sql`: `OPS.ETL_ERROR_LOG`, `OPS.FACTTRANSACTION_REJECTS`
3. `10_dim_branch.sql`: `DWH.DimBranch`
4. `11_dim_account.sql`: `DWH.DimAccount`
5. `12_dim_customer.sql`: `DWH.DimCustomer`
6. `20_fact_transaction.sql`: `DWH.FactTransaction` (FKs need steps 3 and 4)

Example with SnowSQL / Snowflake CLI:

```bash
for f in migration/snowflake/ddl/[0-9][0-9]_*.sql; do snow sql -f "$f"; done
```

After this, continue with `loads/` then `procedures/` (PLAN.md §4).

## Idempotency: `CREATE ... IF NOT EXISTS`, not `CREATE OR REPLACE`

| Object | Statement | Why |
|---|---|---|
| Database, schemas | `CREATE ... IF NOT EXISTS` | `OR REPLACE` on a database or schema drops every object in it, including other units' objects |
| Tables | `CREATE TABLE IF NOT EXISTS` | Redeploys keep the data. `CREATE OR REPLACE TABLE` would empty the dimensions, fact, error log and reject history on every deploy. Time Travel could recover them, but only within the retention period |
| Fact clustering key | `ALTER TABLE ... CLUSTER BY` after the create | `IF NOT EXISTS` skips existing tables, so the `ALTER` makes every deploy converge on the intended key |

Downside: `IF NOT EXISTS` does not apply column changes to an existing table. Later schema changes ship as new,
numbered, additive scripts (e.g. `30_alter_*.sql` with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`), never by
editing a `CREATE`. Procedures, views, file formats and tasks hold no data, so other units use `CREATE OR
REPLACE` for them.

## Mapping decisions

| T-SQL / Synapse | Snowflake | Why / semantic difference |
|---|---|---|
| `IF NOT EXISTS (sys.databases) CREATE DATABASE DWH`, `USE DWH; GO` | `CREATE DATABASE IF NOT EXISTS BANKING_DWH`, `USE DATABASE BANKING_DWH;` | No `GO`; `;` ends statements. Objects are `schema.object`, so a renamed database only changes the `USE` line |
| `dbo` schema | `DWH` (star), `RAW` (landing), `OPS` (operations) | PLAN.md §3 |
| `PRINT` | Dropped | Plain DDL has no output channel; procedures use `SYSTEM$LOG_INFO` |
| `INT` | `INT` (= `NUMBER(38,0)`) | Wider than SQL Server's 32-bit INT, so every source value fits |
| `MONEY` | `NUMBER(19,4)` | Same precision and scale, exact. Difference: SQL Server rounds MONEY*MONEY and MONEY/MONEY to 4 places; Snowflake keeps more scale in intermediate results |
| `DATETIME` | `TIMESTAMP_NTZ(3)` | No time zone in the source. DATETIME rounds to 1/300 s (.000/.003/.007); NTZ(3) stores exact milliseconds |
| `DATE`, `VARCHAR(n)` | Same | Snowflake `VARCHAR(n)` counts characters (UTF-8), SQL Server counts bytes; nothing gets shorter |
| `PRIMARY KEY` | `NOT NULL` + `CONSTRAINT PK_* PRIMARY KEY (...) NOT ENFORCED RELY` | Snowflake only enforces `NOT NULL`. PK columns are explicitly `NOT NULL`, as SQL Server implies. Uniqueness is enforced by the loads (insert-only `MERGE`, `QUALIFY` dedup) and checked by validation |
| `FOREIGN KEY` (`FactTransaction` → `DimAccount`, `DimBranch`) | `FOREIGN KEY ... REFERENCES ... NOT ENFORCED RELY` | Not enforced: orphan rows, which SQL Server rejected (PLAN.md §1.6), are filtered by the fact load into `OPS.FACTTRANSACTION_REJECTS`. `RELY` lets the optimizer eliminate joins; it is only correct because the load guarantees the keys. No `DimAccount.CustomerID` FK, because the source has none |
| Nullable columns | Nullable | SQL Server columns are nullable by default; kept identical |
| Synapse `DISTRIBUTION = HASH/ROUND_ROBIN/REPLICATE`, `CLUSTERED COLUMNSTORE INDEX`, `PARTITION` | Fact: `CLUSTER BY (TO_DATE(TRANSACTIONDATE))`. Dimensions: none | Snowflake has no distribution; storage is columnar micro-partitions. The main fact access path is the day range in `sp_DailyTransaction`, so the fact clusters on the day (a raw timestamp key would have too many distinct values). Dimensions have 5-21 rows (one micro-partition), so a key would only cost reclustering credits. At 22 rows the fact key is a no-op too; it matters for production-sized Synapse facts. `ALTER TABLE ... SUSPEND RECLUSTER` stops Automatic Clustering costs if needed |
| `IDENTITY(1,1)` | `AUTOINCREMENT START 1 INCREMENT 1 ORDER` (`OPS.ETL_ERROR_LOG.LOG_ID`) | See below. The DWH keys are natural source keys, so no `IDENTITY` exists to port |
| Lineage | `FactTransaction.SOURCESYSTEM VARCHAR(10)`, `LOADEDAT TIMESTAMP_NTZ(3) DEFAULT SYSDATE()` | PLAN.md §3. `SYSDATE()` is UTC, unlike session-time-zone `CURRENT_TIMESTAMP()`. Nullable so the loads unit controls the values; excluded from parity checks |

Identifiers are written upper-case and unquoted (sqlfluff `CP02`). Snowflake upper-cases unquoted names anyway,
so `DimAccount`, `dimaccount` and `DIMACCOUNT` all resolve to the same table and existing report SQL still works.
`EMAIL` has a `-- noqa: RF04` because sqlfluff lists it as a keyword; Snowflake does not reserve it.

### `OPS` tables

- `OPS.ETL_ERROR_LOG`: exactly as in PLAN.md §3, plus a `NOT NULL` on `LOGGED_AT` and a declared, not enforced PK on `LOG_ID`.
- `OPS.FACTTRANSACTION_REJECTS`: the 6 source columns keep their source names (`TRANSACTION_ID`, `ACCOUNT_ID`,
  `TRANSACTION_DATE`, `AMOUNT`, `TRANSACTION_TYPE`, `BRANCH_ID`) because these are source rows that never made
  it into the DWH. `AMOUNT` is `NUMBER(19,4)` and `TRANSACTION_DATE` is `TIMESTAMP_NTZ(3)` (the DWH types),
  wider than the source `INT` / `DATETIME2(0)`, so rows from RAW or from the fact pipeline fit without loss.
  `REJECTREASON` is `NOT NULL`; `REJECTEDAT` defaults to `SYSDATE()`. There is no PK: the same transaction can
  be rejected more than once across runs.

### Synapse `IDENTITY` → Snowflake

Synapse example:

```sql
CREATE TABLE dbo.DimProduct (
    ProductKey INT IDENTITY(1,1) NOT NULL,
    ProductCode VARCHAR(20) NOT NULL
) WITH (DISTRIBUTION = REPLICATE, CLUSTERED COLUMNSTORE INDEX);
```

Option A, column-level (used for `ETL_ERROR_LOG.LOG_ID`):

```sql
CREATE TABLE IF NOT EXISTS DWH.DIMPRODUCT (
    PRODUCTKEY INT AUTOINCREMENT START 1 INCREMENT 1 ORDER NOT NULL,
    PRODUCTCODE VARCHAR(20) NOT NULL
);
```

Option B, a named sequence (when several tables share a key space, or the load must know the key before inserting):

```sql
CREATE SEQUENCE IF NOT EXISTS DWH.SEQ_DIMPRODUCT START = 1 INCREMENT = 1 ORDER;
CREATE TABLE IF NOT EXISTS DWH.DIMPRODUCT (
    PRODUCTKEY INT DEFAULT DWH.SEQ_DIMPRODUCT.NEXTVAL NOT NULL,
    PRODUCTCODE VARCHAR(20) NOT NULL
);
```

Semantic differences:

- Values are unique but can have gaps, as in Synapse, where `IDENTITY` is not contiguous across distributions.
  `ORDER` is written explicitly so the order does not depend on the account's `NOORDER_SEQUENCE_AS_DEFAULT`
  parameter. Use `NOORDER` for high-concurrency inserts when order does not matter.
- `SET IDENTITY_INSERT ON` does not exist and is not needed: an explicit value can be inserted into an
  `AUTOINCREMENT`/`DEFAULT` column at any time. The generator does **not** advance past it, so for a historical
  backfill set `START` to `MAX(key) + 1` from the Synapse extract (Option B: create the sequence with that
  `START`) instead of re-seeding later. `DBCC CHECKIDENT RESEED` has no equivalent: you recreate the sequence.
- `SCOPE_IDENTITY()` / `@@IDENTITY` do not exist. Draw `seq.NEXTVAL` first (Option B) or read the key back with
  the business key.
- A `CREATE OR REPLACE` of the table or sequence restarts numbering at `START`. Another reason these scripts use
  `IF NOT EXISTS`.

## Offline check

```bash
pip install sqlfluff==3.2.5 pytest
sqlfluff lint migration/snowflake/ddl/
pytest migration/snowflake/ddl/tests/
```

`tests/test_ddl_contract.py` parses every DDL file. It reads PLAN.md §1.2, applies the §2 type mapping and checks
that each table has exactly those columns, types and column order, plus the §3 lineage and `OPS` columns. It also
checks PK/FK targets, `NOT ENFORCED RELY`, `NOT NULL` PK columns, fact-only clustering, `IF NOT EXISTS`
everywhere, no db-qualified names, FK dependency order, that this README's deploy order matches the files,
and that sqlfluff parses every file without unparsable segments.

## Needs a live Snowflake account

- That the DDL runs, and that a second run is a no-op (no errors, row counts unchanged).
- `SHOW PRIMARY KEYS` / `SHOW IMPORTED KEYS` list the constraints with `rely = true`.
- `SYSTEM$CLUSTERING_INFORMATION('DWH.FACTTRANSACTION')` reports `LINEAR(TO_DATE(TRANSACTIONDATE))`.
- `OPS.ETL_ERROR_LOG.LOG_ID` generates 1, 2, 3… when `INSERT`s omit it.
- `DESCRIBE TABLE` types match the contract (e.g. `INT` shows as `NUMBER(38,0)`).
