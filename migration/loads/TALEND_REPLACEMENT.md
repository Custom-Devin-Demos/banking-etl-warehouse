# Talend replacement (Unit 3: loads)

The four Talend jobs in `talend_jobs/*.zip` (inventory: `migration/PLAN.md` §1.5, §1.6) are replaced by
Snowflake-native objects in `migration/snowflake/loads/`. This file maps each job and component to its
replacement and lists the behaviour differences.

## Deploy order and objects

Deploy after Unit 1's DDL (`migration/snowflake/ddl/`: database, schemas, `DWH.*`, `OPS.ETL_ERROR_LOG`,
`OPS.FACTTRANSACTION_REJECTS`). The views bind to those tables.

| File | Objects |
|---|---|
| `00_file_formats.sql` | `OPS.FF_CSV_EXTRACT` (SQL Server extracts, converted Excel), `OPS.FF_CSV_TRANSACTION` (`transaction_csv.csv`) |
| `01_stage.sql` | `OPS.LANDING_STAGE` (internal, server-side encrypted); commented Azure external-stage example |
| `10_raw_tables.sql` | `RAW.STATE`, `RAW.CITY`, `RAW.CUSTOMER`, `RAW.ACCOUNT`, `RAW.BRANCH`, `RAW.TRANSACTION_DB`, `RAW.TRANSACTION_EXCEL`, `RAW.TRANSACTION_CSV` (+ `_SOURCE_FILE`, `_SOURCE_ROW`, `_LOADED_AT`) |
| `11_streams.sql` | append-only streams `RAW.STRM_TRANSACTION_DB` / `_EXCEL` / `_CSV` |
| `12_ops_work_tables.sql` | transient `OPS.FACTTRANSACTION_INCREMENT` (one incremental batch) |
| `20_sp_copy_raw.sql` | `OPS.SP_COPY_RAW(INCLUDE_FILE_SOURCES)`: `COPY INTO` all 8 RAW tables |
| `30_transform_views.sql` | the tMap / tUnite / tUniqRow logic as views (`OPS.V_DIM*_SRC`, `OPS.V_FACTTRANSACTION_*`) |
| `31`-`34_sp_load_*.sql` | `OPS.SP_LOAD_DIMBRANCH()`, `OPS.SP_LOAD_DIMACCOUNT()`, `OPS.SP_LOAD_DIMCUSTOMER()`, `OPS.SP_LOAD_FACTTRANSACTION()` (PLAN.md §3) |
| `35_sp_load_facttransaction_incremental.sql` | `OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL()` (stream-driven) |
| `50_tasks.sql` | task graph `OPS.TASK_LOAD_ROOT` → `DIMBRANCH` → `DIMACCOUNT` → `DIMCUSTOMER` → `FACTTRANSACTION` |
| `60_snowpipes.sql` | `OPS.PIPE_TRANSACTION_CSV`, `OPS.PIPE_TRANSACTION_EXCEL` |
| `90_run_full_load.sql` | one Talend-equivalent run plus expected counts (initial load / cutover / recovery) |

Landing the files (from a machine with SnowSQL / Snowflake CLI):

```sh
python migration/loads/xlsx_to_csv.py data_sources/transaction_excel.xlsx build/transaction_excel.csv
# PUT each file to its prefix: extracts/<table>/, files/transaction_csv/, files/transaction_excel/ (01_stage.sql)
```

## Jobs

| Talend job | Snowflake replacement | Target behaviour |
|---|---|---|
| `Load_DimBranch` | `COPY INTO RAW.BRANCH` → `OPS.V_DIMBRANCH_SRC` → `OPS.SP_LOAD_DIMBRANCH()` | insert-only `MERGE` into `DWH.DIMBRANCH` |
| `Load_DimAccount` | `COPY INTO RAW.ACCOUNT` → `OPS.V_DIMACCOUNT_SRC` → `OPS.SP_LOAD_DIMACCOUNT()` | insert-only `MERGE` into `DWH.DIMACCOUNT` |
| `Load_DimCustomer` | `COPY INTO RAW.CUSTOMER/CITY/STATE` → `OPS.V_DIMCUSTOMER_SRC` → `OPS.SP_LOAD_DIMCUSTOMER()` | insert-only `MERGE` into `DWH.DIMCUSTOMER` |
| `Load_FactTransaction` | `COPY INTO RAW.TRANSACTION_DB/_EXCEL/_CSV` → `OPS.V_FACTTRANSACTION_CANDIDATES` → `OPS.SP_LOAD_FACTTRANSACTION()` (full) or streams → `OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL()` | `INSERT OVERWRITE` (full) / `MERGE` (incremental) into `DWH.FACTTRANSACTION`; rejects to `OPS.FACTTRANSACTION_REJECTS` |
| run order (manual, dims then fact) | task graph in `50_tasks.sql`, or `90_run_full_load.sql` | serial, stops on the first failure |

## Components

| Talend component / setting | Snowflake replacement | Behaviour differences |
|---|---|---|
| `tMSSqlInput` (`SELECT` all columns of `dbo.<table>`, ISO-8859-15) | Source extract CSV (`data_sources/sample_db_extract/*.csv`, produced from `sample.bak`) staged under `extracts/<table>/`, `COPY INTO RAW.<TABLE>` with `OPS.FF_CSV_EXTRACT` | Snowflake cannot query the Synapse/SQL Server source. The source system (or ADF) exports CSV; the copy is a file load, not a live query. `ON_ERROR = ABORT_STATEMENT`: a bad file fails the load, like a failing query did. |
| `tFileInputExcel` (`Sheet1`, `HEADER=1`, `DIE_ON_ERROR=false`) | `migration/loads/xlsx_to_csv.py` (openpyxl) → staged CSV under `files/transaction_excel/` → `COPY INTO RAW.TRANSACTION_EXCEL` (or `OPS.PIPE_TRANSACTION_EXCEL`) | `COPY INTO` cannot read xlsx. Datetimes are written as `YYYY-MM-DD HH24:MI:SS` from the stored value (display format `m/d/yy h:mm` ignored), rounded to the second. Blank rows are skipped. The converter fails on an unexpected header or values beyond the header instead of skipping bad rows (`DIE_ON_ERROR=false`): a silently shortened Excel file is worse than a failed run. |
| `tFileInputDelimited` (`,`, `\n`, `HEADER=1`, `REMOVE_EMPTY_ROW=true`, `CSV_OPTION=false`, `dd-MM-yyyy HH:mm:ss`, ISO-8859-15, `DIE_ON_ERROR=false`) | `OPS.FF_CSV_TRANSACTION` (same delimiter, header, `SKIP_BLANK_LINES`, no enclosure, `ENCODING = 'ISO-8859-15'`) + `TO_TIMESTAMP_NTZ($3, 'DD-MM-YYYY HH24:MI:SS')` in `COPY INTO RAW.TRANSACTION_CSV` (or `OPS.PIPE_TRANSACTION_CSV`) | Talend dropped unparsable rows (`DIE_ON_ERROR=false`); `COPY` aborts the file (batch) or skips the whole file (Snowpipe, `ON_ERROR = SKIP_FILE`, visible in `COPY_HISTORY`). Explicit format means the day-first date never depends on `TIMESTAMP_INPUT_FORMAT`. |
| `tMap` Load_DimBranch / Load_DimAccount (1:1, `DIE_ON_ERROR=true`) | `OPS.V_DIMBRANCH_SRC`, `OPS.V_DIMACCOUNT_SRC`: rename; `CAST(DATE_OPENED AS DATE)`; `BALANCE` → `NUMBER(19,4)` | `DATE_OPENED` time part truncated, as the SQL Server `DATE` column did. |
| `tMap` Load_DimCustomer (lookups city, state: `LOAD_ONCE`, `UNIQUE_MATCH`, left outer) | `OPS.V_DIMCUSTOMER_SRC`: customer `LEFT OUTER JOIN` city `LEFT OUTER JOIN` state; `UPPER(customer_name/address/gender)` for `StringHandling.UPCASE`; `CAST(AGE AS INT)` | A customer with an unknown city keeps NULL city/state (tested). `UNIQUE_MATCH` (last lookup row wins) is replaced by "latest RAW row per key", so lookup keys are unique by construction. `UPPER` is Unicode-aware; Java `toUpperCase` is locale-dependent (identical for this data). A non-numeric age fails the load (`CAST`, like the tMap `DIE_ON_ERROR=true`). |
| `tMap` Load_FactTransaction (1:1) | column mapping in `OPS.V_FACTTRANSACTION_CANDIDATES` | Adds lineage `SOURCESYSTEM` (`DB`/`EXCEL`/`CSV`) and `LOADEDAT` (PLAN.md §3). |
| `tUnite` (DB, Excel, CSV) | `UNION ALL` of the three RAW tables with `SOURCE_RANK` 1/2/3 | Talend's union order was the order the flows reached `tUnite`; Snowflake makes it an explicit rank. |
| `tUniqRow` (key `transaction_id`, keep first) | `ROW_NUMBER() OVER (PARTITION BY TRANSACTION_ID ORDER BY SOURCE_RANK, _LOADED_AT, _SOURCE_FILE, _SOURCE_ROW)`; rank > 1 → `DUPLICATE_LOWER_PRECEDENCE` reject | **Precedence DB > EXCEL > CSV needs business sign-off** (PLAN.md §1.6): IDs 6 and 7 differ between DB (2022-02-21) and Excel (2024-01-18/19); the baseline keeps DB. Duplicates are now logged in `OPS.FACTTRANSACTION_REJECTS` instead of discarded. Within one source, the earliest load wins (a re-delivered row never replaces the original). |
| `tMSSqlOutput` dims: `TABLE_ACTION=CREATE_IF_NOT_EXISTS` | Unit 1 DDL (`CREATE TABLE IF NOT EXISTS`) deployed once; load procedures never create tables | Table structure is versioned DDL, not created by the first load. |
| `tMSSqlOutput` dims: `DATA_ACTION=INSERT`, `DIE_ON_ERROR=false`, `COMMIT_EVERY=10000` | `MERGE INTO DWH.DIM* ... WHEN NOT MATCHED THEN INSERT` in one statement | Talend re-runs hit PK violations that were skipped row by row; the `MERGE` inserts new keys and leaves existing rows unchanged, so a re-run is a no-op (tested). One atomic statement instead of commits every 10000 rows. **SCD1 option**: add the `WHEN MATCHED ... THEN UPDATE` clause given in each procedure's header comment to overwrite changed attributes; not enabled because Talend never updated a dimension row. |
| `tMSSqlOutput` fact: `TABLE_ACTION=TRUNCATE`, `DATA_ACTION=INSERT` | `INSERT OVERWRITE INTO DWH.FACTTRANSACTION` inside one transaction with the reject insert | Truncate and insert are atomic: readers never see an empty or partial fact table; a failure rolls everything back. |
| `tMSSqlOutput` fact: `DIE_ON_ERROR=false` + enforced FKs | Orphan check against `DWH.DIMACCOUNT` / `DWH.DIMBRANCH` in the view; `ORPHAN_ACCOUNT` / `ORPHAN_BRANCH` rows written to `OPS.FACTTRANSACTION_REJECTS` | Snowflake does not enforce FKs, so without the check CSV IDs 23-25 (accounts 22/23) would load. SQL Server dropped them silently; they are now logged. Any other error fails the procedure (logged to `OPS.ETL_ERROR_LOG`, re-raised) instead of being skipped. |
| Job error handling | `EXCEPTION WHEN OTHER` → `INSERT INTO OPS.ETL_ERROR_LOG` (procedure, `SQLCODE`, `SQLSTATE`, `SQLERRM`, step context) → `RAISE` in every procedure | Fail-fast: the task graph stops at the failing task. |
| Job scheduling / run order | `50_tasks.sql`: root task (CRON, `CALL OPS.SP_COPY_RAW`) → dims in Talend order → fact; serverless; created suspended | Talend jobs were run by hand; the graph enforces dims before fact. |

## Full vs incremental fact load

- **Full** (`OPS.SP_LOAD_FACTTRANSACTION()`, Talend-equivalent): re-derives the fact from all RAW rows; also
  consumes the streams so the next incremental run starts from there. Each run re-logs the current rejects
  (`REJECTEDAT` = run time); query the latest `REJECTEDAT` for the current set.
- **Incremental** (`OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL()`, task graph): reads new RAW rows from the
  streams into `OPS.FACTTRANSACTION_INCREMENT`, re-decides each affected `TRANSACTION_ID` over all its RAW rows
  with the full-reload view, and `MERGE`s inserts / updates / deletes. A late higher-precedence row replaces
  the fact row; a late higher-precedence orphan removes it; the replaced row is logged as a reject. Tested to
  give the baseline for every arrival order of the three sources.
- Limitation: an orphan is re-evaluated only when a row with the same `TRANSACTION_ID` arrives. After loading
  late dimension rows, run `OPS.SP_LOAD_FACTTRANSACTION()` to pick up previously rejected orphans.

## Snowpipe

`60_snowpipes.sql` defines pipes for the two file sources with the same transforms as `OPS.SP_COPY_RAW`.
With an internal stage they are fed via the Snowpipe REST API or `ALTER PIPE ... REFRESH`. Auto-ingest needs an
external stage and a notification integration (Azure Event Grid → Storage Queue); both are environment-specific
and left as commented templates. Use either the pipes or the batch file COPY (`CALL OPS.SP_COPY_RAW(FALSE)` in
the root task), not both, for the same prefix: pipe and bulk load metadata are separate.

## Excel: conversion script vs Snowpark

Chosen: `xlsx_to_csv.py` (openpyxl) before staging. Reasons: it runs and is tested offline in CI; the staged
CSV is the audit copy loaded by plain `COPY INTO` / Snowpipe with the same load-history deduplication as the
other files; no Snowpark/Anaconda package terms or warehouse runtime are needed. Alternative: a Snowpark Python
procedure reading the staged xlsx with `openpyxl` (`SnowflakeFile`) and writing `RAW.TRANSACTION_EXCEL`, useful
if no compute exists outside Snowflake; it would reuse `xlsx_to_csv.read_rows`.

## Verification

- Offline (`pytest migration/loads`, `requirements.txt`): the tests run the committed SQL (RAW DDL, the
  `COPY INTO` column transforms of `OPS.SP_COPY_RAW`, the views and the DML of each procedure) on DuckDB over
  `data_sources/` and reproduce `migration/baseline/DimBranch.csv`, `DimAccount.csv`, `DimCustomer.csv` and
  `FactTransaction.csv` (22 rows) exactly, plus the 7 expected rejects, idempotent re-runs, insert-only dims,
  left joins, orphan branches, and the incremental path for all 6 source arrival orders. Static tests check
  signatures, error handlers, the task chain, streams, and pipe/COPY consistency.
- Needs a live account (`SNOWFLAKE_ACCOUNT`; `tests/test_loads_live.py` is skipped without it): deploy and
  compile the procedures, tasks and pipes; `PUT` + `COPY INTO` with the real file formats (encoding, NULLs);
  stream offsets and transaction behaviour; task graph execution; Snowpipe ingestion.
