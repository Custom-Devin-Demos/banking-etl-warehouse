# Synapse / SQL Server → Snowflake migration: `banking-etl-warehouse`

This directory migrates the T-SQL banking warehouse (`sql_scripts/`, Talend jobs in `talend_jobs/`, sources in
`data_sources/`) to Snowflake. It is the reference workload for the Chubb Azure Synapse dedicated SQL pool →
Snowflake consolidation.

- **Contract:** [PLAN.md](PLAN.md): inventory (§1), T-SQL → Snowflake mapping (§2), object names and env vars (§3),
  file ownership (§4). Everything below follows it.
- **Ground truth:** [baseline/](baseline/): the SQL Server 2022 output of the original DDL, procedures and Talend
  jobs. The Snowflake build must reproduce it.
- **CI:** [`.github/workflows/snowflake-migration.yml`](../.github/workflows/snowflake-migration.yml) (see [CI](#ci)).

## Contents

1. [Layout](#layout)
2. [Deploy order](#deploy-order)
3. [Cutover runbook](#cutover-runbook)
4. [Rollback plan](#rollback-plan)
5. [Known semantic differences](#known-semantic-differences)
6. [Running validation locally and live](#running-validation-locally-and-live)
7. [CI](#ci)

## Layout

Each directory is owned by one work unit / PR (PLAN.md §4 and §5).

| Path | Unit | Contents |
|---|---|---|
| [PLAN.md](PLAN.md) | plan | Inventory, mapping table, naming contract |
| [baseline/](baseline/) | plan | SQL Server ground truth: 4 DWH tables + procedure outputs, `emulate_talend_on_sqlserver.sql` |
| [`../data_sources/sample_db_extract/`](../data_sources/sample_db_extract/) | plan | CSV extracts of the 6 SQL Server source tables (from `sample.bak`) |
| [snowflake/ddl/](snowflake/ddl/) | 1 – DDL | `BANKING_DWH`, schemas `RAW`/`DWH`/`OPS`, dimension + fact tables, `OPS.ETL_ERROR_LOG`, `OPS.FACTTRANSACTION_REJECTS` |
| [snowflake/procedures/](snowflake/procedures/) | 2 – Procedures | `DWH.SP_DAILYTRANSACTION`, `DWH.SP_BALANCEPERCUSTOMER` (Snowflake Scripting) |
| [snowflake/loads/](snowflake/loads/) | 3 – Loads | Stage, file formats, `RAW.*` tables, `COPY INTO`, `OPS.SP_LOAD_*`, streams, tasks, Snowpipe |
| [loads/](loads/) | 3 – Loads | `TALEND_REPLACEMENT.md` (Talend job → Snowflake mapping), `xlsx_to_csv.py`, tests |
| [validation/](validation/) | 4 – Validation | `validate.py`, `sql/`, tests, `requirements.txt` |
| [tests/](tests/) | 5 – Docs/CI | Repo-level offline contract checks |
| [README.md](README.md) | 5 – Docs/CI | This runbook |

Object names (PLAN.md §3): database `BANKING_DWH`; `RAW` = one landing table per source + `_SOURCE_FILE`,
`_SOURCE_ROW`, `_LOADED_AT`; `DWH` = `DimAccount`, `DimBranch`, `DimCustomer`, `FactTransaction` (+ lineage columns
`SourceSystem`, `LoadedAt`) and the two reporting procedures; `OPS` = stages, file formats, streams, tasks, load
procedures, error log and rejects table. Identifiers are unquoted, so `DimAccount` resolves to `DIMACCOUNT`.

## Deploy order

Run every script with a role that owns `BANKING_DWH` (or can create it) and a warehouse. Scripts inside a directory
run in **lexical file-name order** (the numeric prefixes encode dependencies). Each script sets
`USE DATABASE BANKING_DWH;` at the top (the first DDL script creates the database before using it), and all scripts
are idempotent, so the sequence can be re-run.

| # | Step | What | Command (Snowflake CLI; SnowSQL `-f` works the same) |
|---|---|---|---|
| 1 | DDL | Database, schemas, `OPS` tables, dimensions, fact | `for f in $(ls migration/snowflake/ddl/*.sql \| sort); do snow sql -f "$f"; done` |
| 2 | Loads objects | Stage, file formats, `RAW.*`, `OPS.SP_LOAD_*`, streams, tasks (created **suspended**) | `for f in $(ls migration/snowflake/loads/*.sql \| sort); do snow sql -f "$f"; done` |
| 3 | Reporting procedures | `DWH.SP_DAILYTRANSACTION`, `DWH.SP_BALANCEPERCUSTOMER` | `for f in $(ls migration/snowflake/procedures/*.sql \| sort); do snow sql -f "$f"; done` |
| 4 | Stage files | Convert the xlsx, then `PUT` the extracts + CSV (or point an external stage at ADLS) | `python migration/loads/xlsx_to_csv.py` then `PUT`, see [loads/TALEND_REPLACEMENT.md](loads/TALEND_REPLACEMENT.md) |
| 5 | Run loads | `COPY INTO RAW.*`, then the load procedures in Talend run order | `CALL OPS.SP_LOAD_DIMBRANCH(); CALL OPS.SP_LOAD_DIMACCOUNT(); CALL OPS.SP_LOAD_DIMCUSTOMER(); CALL OPS.SP_LOAD_FACTTRANSACTION();` |
| 6 | Validate | Row counts, key sums, checksums, procedure outputs vs `baseline/` | `python migration/validation/validate.py` (see [below](#running-validation-locally-and-live)) |

`procedures/` deploys after `ddl/` and `loads/` because Snowflake Scripting resolves object names at call time, not
create time, but the procedures' error handlers write to `OPS.ETL_ERROR_LOG`, which must exist before the first call.
Exact stage, file format and task names are defined by unit 3; see [loads/TALEND_REPLACEMENT.md](loads/TALEND_REPLACEMENT.md).

Expected result of a full load from the repo extracts (matches `baseline/`): `DimBranch` 5, `DimAccount` 21,
`DimCustomer` 20, `FactTransaction` 22 rows; `OPS.FACTTRANSACTION_REJECTS` 7 rows (4 `DUPLICATE_LOWER_PRECEDENCE`
for `transaction_id` 6, 7, 14, 15 and 3 `ORPHAN_ACCOUNT` for 23, 24, 25).

## Cutover runbook

Roles: **MO** = migration owner, **DBA** = Synapse/Snowflake platform admin, **BO** = business data owner,
**CO** = consumer (BI/report) owners. Every step has an explicit go/no-go check; do not start the next step until it
passes. The rollback for each step is in [Rollback plan](#rollback-plan).

| Step | When | Owner | Action | Go / no-go check |
|---|---|---|---|---|
| 0. Prepare | T-14d | DBA, MO | Provision `BANKING_DWH` roles, warehouse, resource monitor; create a storage integration + external stage on the ADLS container used for the Synapse export; deploy to a non-prod Snowflake database and run the full sequence (Deploy order 1-6) against the repo extracts. BO signs off the open decisions (duplicate precedence, ordering; see [differences](#known-semantic-differences)). | CI green; non-prod `validate.py` reports 0 mismatches; BO sign-off recorded |
| 1. Freeze | T-0, start of window | MO, CO | Announce the window. Pause the Talend schedules (or ADF triggers) that write to the Synapse warehouse; block DDL on the Synapse `DWH` schema; record the freeze time. Reads stay open. | No Talend/ADF run active (`sys.dm_pdw_exec_requests` shows no loads); freeze time recorded |
| 2. Final Synapse extract | T-0 | DBA | Export every source and DWH table to ADLS with `CETAS` (one folder per table, Parquet or CSV with header, UTF-8), e.g. `CREATE EXTERNAL TABLE ext.FactTransaction_final WITH (LOCATION='cutover/FactTransaction/', DATA_SOURCE=adls_cutover, FILE_FORMAT=csv_utf8) AS SELECT * FROM dbo.FactTransaction;`. Capture control totals in Synapse at the same time: `COUNT(*)` and `SUM` of every key/amount column per table. | Every table has files in ADLS; control totals saved next to the extract |
| 3. Stage | T-0 | DBA | Point the Snowflake stage at the extract folder (external stage via the storage integration) or `PUT` the files to the internal stage; `LIST @stage` and compare file counts/sizes with ADLS. | File list matches the export |
| 4. Deploy | T-0 | MO | Deploy the tagged release to the production database in [Deploy order](#deploy-order) steps 1-3. Tasks stay suspended. | All scripts succeed; `SHOW TASKS IN SCHEMA OPS` shows every task `suspended` |
| 5. Initial load | T-0 | MO | Deploy order steps 4-5: `COPY INTO RAW.*`, then `CALL OPS.SP_LOAD_*` in order. Review `OPS.FACTTRANSACTION_REJECTS` and `OPS.ETL_ERROR_LOG`. | `COPY` returns no `LOAD_FAILED` files; rejects match expectations (7 rows for the repo data); `ETL_ERROR_LOG` empty |
| 6. Validate | T-0 | MO, BO | Run `python migration/validation/validate.py` against production (live mode) and compare its counts/sums with the step-2 Synapse control totals. | 0 mismatches on row counts, key sums, checksums and procedure outputs; BO signs off |
| 7. Parallel run | T+1d … T+Nd (N ≥ 5 business days, BO decides) | MO, CO | Unfreeze Synapse: Talend keeps loading Synapse, which stays the system of record. Feed Snowflake the same daily source files and trigger the incremental load manually on the same cadence (`EXECUTE TASK` on the root task; tasks stay suspended). Run `validate.py` and compare both procedures' outputs between Synapse and Snowflake after each load. CO run their reports on both and diff. | Every daily comparison clean; any diff explained and fixed; CO sign-off |
| 8. Switch consumers | T+N | CO, DBA | Second short freeze of Talend; final delta load and validation. Repoint BI/report connections to Snowflake (`BANKING_DWH`); change procedure calls from `EXEC sp_X @p=...` to `CALL DWH.SP_X(...)` (see result-set note in [differences](#known-semantic-differences)). Revoke write on Synapse `DWH`. | All consumers read Snowflake; smoke reports match the last Synapse run |
| 9. Resume tasks | T+N | MO | Resume the task graph: children first, root last (`ALTER TASK ... RESUME`, or `SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('OPS.<root task>')`); enable Snowpipe auto-ingest if used. Disable the Talend jobs permanently. | First scheduled run succeeds (`TABLE(INFORMATION_SCHEMA.TASK_HISTORY())`), validation clean |
| 10. Decommission | T+N+30d (retention agreed with BO) | DBA | Keep Synapse **paused** (not dropped) and the final ADLS extract during the retention window. After it: archive the extract to cold storage, drop the Synapse `DWH` objects, delete the Talend jobs and their credentials, remove the cutover storage-integration access. | Retention window passed with no rollback; BO approval to decommission |

## Rollback plan

Synapse stays intact and read-only (never dropped) until step 10, so every step before it can roll back by pointing
work back to Synapse. Step 10 is the **point of no return**.

| Step | Rollback | Data loss risk |
|---|---|---|
| 0. Prepare | Drop the non-prod database. Nothing in production has changed. | None |
| 1. Freeze | Resume the Talend schedules / ADF triggers; lift the DDL block. | None |
| 2. Final extract | Drop the `CETAS` external tables (`DROP EXTERNAL TABLE`, data files are kept) and delete the ADLS folder; unfreeze (step 1 rollback). | None |
| 3. Stage | `REMOVE @stage/...` or drop the external stage; unfreeze. | None |
| 4. Deploy | `DROP DATABASE BANKING_DWH` (or the schemas) and redeploy the previous release; or `UNDROP` within Time Travel if a drop was wrong. Unfreeze if abandoning the window. | None (Snowflake not yet used) |
| 5. Initial load | Fix and re-run: `TRUNCATE` the `RAW` and `DWH` tables and reload; or restore a table with Time Travel (`CREATE OR REPLACE TABLE t CLONE t AT (TIMESTAMP => '<before load>')`). Abandon → unfreeze. | None |
| 6. Validate | If mismatches cannot be fixed inside the window: abandon, unfreeze Synapse, keep Snowflake as non-prod for analysis. | None |
| 7. Parallel run | Stop feeding Snowflake; Synapse is still the system of record, consumers never moved. | None |
| 8. Switch consumers | Repoint consumers back to Synapse connections, restore Synapse write grants, resume Talend. Any data loaded only into Snowflake since the switch is re-loaded by Talend from the same source files. | Low: loads since the switch must be replayed on Synapse |
| 9. Resume tasks | `ALTER TASK <root> SUSPEND` (root first), then step 8 rollback. Re-enable Talend jobs. | Low (as step 8) |
| 10. Decommission | Not reversible once Synapse objects are dropped. Before the drop, restore from the archived extract into a new Synapse pool if needed. | High after drop |

## Known semantic differences

Behaviour that differs between SQL Server / Synapse / Talend and Snowflake, and how this migration handles it.
Rows marked **sign-off** need business approval before cutover (runbook step 0).

| # | Area | SQL Server / Synapse / Talend | Snowflake | Handling in this migration |
|---|---|---|---|---|
| 1 | String collation | `SQL_Latin1_General_CP1_CI_AS`: `=`, `LIKE`, `GROUP BY`, `DISTINCT` are case-insensitive | Comparisons are case-sensitive by default (binary collation) | `sp_BalancePerCustomer` uses `ILIKE` so `'shelly'` still matches `SHELLY JUWITA` (baseline `sp_BalancePerCustomer__shelly.csv`). Exact `=` filters (`Status = 'active'`, `TransactionType = 'Deposit'`) are kept exact because the data is already lower/Title case; new code must choose `ILIKE` / `UPPER()` / `COLLATE 'en-ci'` explicitly. |
| 2 | Foreign keys | `FactTransaction` FKs enforced; a violating `INSERT` fails | `PRIMARY KEY` / `FOREIGN KEY` (`RELY`) are metadata only, not enforced (only `NOT NULL` is) | The fact load filters orphans and writes them to `OPS.FACTTRANSACTION_REJECTS` (`ORPHAN_ACCOUNT` / `ORPHAN_BRANCH`); duplicates are removed before insert. The validation harness compares row counts and key sums with the baseline, which catches leaked duplicates or orphans. |
| 3 | Load error handling | Talend `tMSSqlOutput DIE_ON_ERROR=false`: rows failing PK/FK are sent to an unconnected REJECT flow and silently dropped; job reports success. `COMMIT_EVERY=10000` partial commits | Rejects are explicit rows in `OPS.FACTTRANSACTION_REJECTS` with a `RejectReason`; procedure errors are logged to `OPS.ETL_ERROR_LOG` and re-raised (the run fails visibly). `COPY INTO` defaults to `ON_ERROR = ABORT_STATEMENT` | Same rows end up in the DWH (CSV ids 23-25 rejected in both). Operators now see rejects instead of losing them; monitor the rejects table. Each load statement is atomic, so there are no partial-batch commits. |
| 4 | Cross-source duplicates (**sign-off**) | `tUnite` → `tUniqRow` keeps the first row per `transaction_id` in stream order, which is not guaranteed | Explicit precedence `DB > EXCEL > CSV` via `QUALIFY ROW_NUMBER() OVER (PARTITION BY transaction_id ORDER BY source_rank, _LOADED_AT) = 1` | Ids 6 and 7 differ between DB and Excel (`transaction_date` 2022-02-21 vs 2024-01-18/19); DB wins, matching the baseline. Losers go to the rejects table as `DUPLICATE_LOWER_PRECEDENCE`. Pending business sign-off. |
| 5 | `MONEY` | Fixed 4 decimals; arithmetic between `MONEY` values rounds to 4 dp; `MONEY * MONEY` / `MONEY / MONEY` lose precision | `NUMBER(19,4)`: same range and scale for storage. Expression result scale follows Snowflake rules (e.g. division returns a scale larger than 4), and results are rounded only when stored or cast | Stored values are identical. `SUM(Amount)` keeps scale 4. Any new division/multiplication must `ROUND(..., 4)` or cast to `NUMBER(19,4)` to match SQL Server. Display may drop trailing zeros in some clients; compare numerically. |
| 6 | `DATETIME` and time zones | `DATETIME` rounds to 1/300 s (.000/.003/.007); no time zone; `GETDATE()` = server local time | `TIMESTAMP_NTZ(3)`: exact milliseconds, no time zone. `CURRENT_TIMESTAMP()` returns `TIMESTAMP_LTZ` in the **session** `TIMEZONE`; `SYSDATE()` returns UTC NTZ | Source data has whole seconds, so values are identical. Metadata defaults use `SYSDATE()` (UTC), not local time. Never mix NTZ with LTZ/TZ columns without explicit conversion; set the account/user `TIMEZONE` deliberately (it changes LTZ display and `CURRENT_TIMESTAMP`, not stored NTZ values). |
| 7 | String concatenation | `'a' + 'b'`; with `CONCAT_NULL_YIELDS_NULL ON`, `+` returns NULL if any operand is NULL; `CONCAT()` treats NULL as `''` | `+` on strings is an error (numeric addition); use `\|\|` or `CONCAT()`, **both** return NULL if any operand is NULL | `LIKE '%' + @p + '%'` became `ILIKE '%' \|\| customer_name \|\| '%'`. In SQL Server a NULL parameter silently returns no rows; `SP_BALANCEPERCUSTOMER` raises a named exception instead (PLAN.md §5, unit 2). Wrap nullable operands in `COALESCE` when porting `CONCAT()`. |
| 8 | Procedure result sets | Procedures return result sets implicitly (`SELECT` at the end); callers use `EXEC sp_X @p = ...` | Procedures declare `RETURNS TABLE (...)` and `RETURN TABLE(resultset)`; callers use `CALL DWH.SP_X(...)`. To post-process, use `SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))` | Consumers must change call syntax (runbook step 8). Output column names are upper-case (`TOTALTRANSACTIONS`) except the quoted `"Date"`; clients that read columns by case-sensitive name must adapt. A procedure returns exactly one table. |
| 9 | Result ordering | `sp_DailyTransaction` has `ORDER BY [Date]`; `sp_BalancePerCustomer` has none (order depends on the plan) | No ordering guarantee without `ORDER BY`; parallel execution makes order vary run to run | Unit 2 adds a deterministic `ORDER BY` to `SP_BALANCEPERCUSTOMER` (documented in its PR). The validation harness canonicalises (sorts) result sets before comparing (PLAN.md §5, unit 4). Default NULL ordering also differs: SQL Server sorts NULLs first ascending, Snowflake last; add `NULLS FIRST` where it matters. |
| 10 | Error handling | `BEGIN TRY ... END TRY BEGIN CATCH ... END CATCH`, `ERROR_NUMBER()`/`ERROR_MESSAGE()`, `THROW`/`RAISERROR`, `XACT_STATE()` (none used in this repo, common in the Synapse estate) | Snowflake Scripting `BEGIN ... EXCEPTION WHEN OTHER THEN ... END`; `SQLCODE`/`SQLERRM`/`SQLSTATE`; `RAISE`; user exceptions `DECLARE e EXCEPTION (-20001, '...')` | Every new procedure's handler inserts into `OPS.ETL_ERROR_LOG` then `RAISE`s. Error codes and messages differ from SQL Server, so monitoring keyed on SQL Server error numbers must be rewritten. DDL commits implicitly; a handler that catches an error inside an explicit transaction must `ROLLBACK` itself. |
| 11 | `IDENTITY` | `IDENTITY(1,1)`: per-table counter; gaps on rollback; values ordered by insert | `AUTOINCREMENT`/`IDENTITY` or sequences: unique but **not gap-free**; `NOORDER` (default for new sequences when `NOORDER_SEQUENCE_AS_DEFAULT` is true) does not guarantee increasing values | Not used: all keys are natural source keys. `OPS.ETL_ERROR_LOG.LOG_ID` uses `AUTOINCREMENT` and is not relied on for ordering (use `LOGGED_AT`). If surrogate keys are introduced, use a sequence with explicit `ORDER` / `NOORDER`, never assume no gaps. |
| 12 | Legacy procedure script | `sql_scripts/02_create_procedures.sql` fails as written: `PRINT` precedes each `CREATE PROCEDURE` without a `GO` (`'CREATE/ALTER PROCEDURE' must be the first statement in a query batch`) | No batch separators: statements end with `;`; `CREATE OR REPLACE PROCEDURE` can follow any statement | Baselines were produced after adding `GO`. The legacy script is left unchanged (reference only, excluded from lint). Any Synapse deployment of it needs the `GO`s added. |
| 13 | Identifiers | `[Date]`, case-insensitive object names | Unquoted identifiers are stored upper-case; quoted ones are case-sensitive | Unquoted names everywhere except `"Date"` in `SP_DAILYTRANSACTION`, quoted to keep the original column name. Existing report SQL with unquoted names keeps working. |
| 14 | Empty strings, encoding, lengths | Talend reads the CSV as ISO-8859-15; empty CSV fields become `''` or NULL depending on component; `VARCHAR(n)` is bytes in code page 1252 | File format sets `ENCODING`; `EMPTY_FIELD_AS_NULL`; `VARCHAR(n)` counts characters (UTF-8) | Extracts are UTF-8 with empty field = NULL (PLAN.md §1.1); the transaction CSV file format must declare ISO-8859-15 (the Talend job encoding). Lengths can only grow, never truncate. |
| 15 | Integer arithmetic (estate-wide) | `INT / INT` truncates to an integer | `INT / INT` returns a decimal | Not used in this repo's procedures. When porting other Synapse code use `TRUNC(a / b)` or `DIV0` deliberately. |

## Running validation locally and live

All commands run from the repository root with Python 3.10+.

### Offline (no Snowflake account)

```bash
python -m venv .venv && . .venv/bin/activate
pip install "sqlfluff==3.2.5" "pytest>=7,<9"
for r in $(find migration -name 'requirements*.txt'); do pip install -r "$r"; done

sqlfluff lint migration/                          # snowflake dialect via repo .sqlfluff
python -m pytest migration -ra                     # all units' offline tests (live tests skip)
python migration/validation/validate.py --offline  # expected values from the extracts vs baseline/
```

With `SNOWFLAKE_ACCOUNT` unset, every live check skips and exits 0. The exact `validate.py` options are owned by
unit 4; run `python migration/validation/validate.py --help`.

### Live (against a Snowflake account)

Deploy and load first ([Deploy order](#deploy-order)), then export the PLAN.md §3 connection variables:

| Variable | Required | Meaning |
|---|---|---|
| `SNOWFLAKE_ACCOUNT` | yes (gates all live checks) | Account identifier, e.g. `myorg-myaccount` |
| `SNOWFLAKE_USER` | yes | Login name |
| `SNOWFLAKE_PASSWORD` | one of password / key | Password authentication |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | one of password / key | Path to a PKCS#8 private key (key-pair authentication) |
| `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | no | Passphrase for an encrypted private key |
| `SNOWFLAKE_ROLE` | recommended | Role with `USAGE` on `BANKING_DWH` and `SELECT` on `RAW`/`DWH`/`OPS` |
| `SNOWFLAKE_WAREHOUSE` | yes | Warehouse to run the checks |
| `SNOWFLAKE_DATABASE` | no (default `BANKING_DWH`) | Target database, if renamed |

```bash
export SNOWFLAKE_ACCOUNT=myorg-myaccount SNOWFLAKE_USER=MIGRATION_SVC SNOWFLAKE_ROLE=MIGRATION_VALIDATOR
export SNOWFLAKE_WAREHOUSE=MIGRATION_WH SNOWFLAKE_PRIVATE_KEY_PATH=$HOME/.snowflake/rsa_key.p8
python migration/validation/validate.py            # non-zero exit on any mismatch
python -m pytest migration -ra                     # also runs live-gated tests now
```

Prefer key-pair authentication for service users. Never commit credentials; [tests/](tests/) fails the build if a
password, key or SAS token literal appears in committed files under `migration/`.

## CI

[`.github/workflows/snowflake-migration.yml`](../.github/workflows/snowflake-migration.yml) runs on every pull
request and on pushes to `main`:

| Job | What | Needs credentials |
|---|---|---|
| `sqlfluff-lint` | `pip install sqlfluff==3.2.5`, `sqlfluff lint migration/` with the repo `.sqlfluff` (snowflake dialect) | No |
| `offline-tests` | Installs every `migration/**/requirements*.txt`, runs `pytest migration`; "no tests collected" (exit 5) counts as success so the job passes before other units land | No |
| `live-check-gate` | Detects whether the `SNOWFLAKE_ACCOUNT` secret is set | No |
| `live-validation` | Runs `python migration/validation/validate.py` with the §3 variables mapped from repository secrets; **skipped** when `SNOWFLAKE_ACCOUNT` is not configured, so the workflow stays green without credentials | Yes |

To enable the live job, add repository (or environment) secrets `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`,
`SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, optionally `SNOWFLAKE_DATABASE`, and either `SNOWFLAKE_PASSWORD` or
`SNOWFLAKE_PRIVATE_KEY` (the PEM **contents**; CI writes it to a temp file and sets `SNOWFLAKE_PRIVATE_KEY_PATH`) plus
optional `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`. Secrets are not passed to workflows from forks, so fork PRs skip it.
The live job validates whatever is already deployed in the target database; it does not deploy.

To run the same jobs locally with [`act`](https://github.com/nektos/act):
`act pull_request -j <job> -W .github/workflows/snowflake-migration.yml -P ubuntu-24.04=catthehacker/ubuntu:act-latest`
(run jobs one at a time: parallel `setup-python` runs share act's tool cache and can collide). Add
`-s SNOWFLAKE_ACCOUNT=...` to exercise the live job.
