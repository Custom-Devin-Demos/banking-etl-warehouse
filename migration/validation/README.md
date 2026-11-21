# Migration validation harness (Unit 4)

Compares the Snowflake warehouse (`BANKING_DWH`, schemas `RAW`/`DWH`/`OPS`, PLAN.md §3) with the source
extracts and the SQL Server ground truth in `migration/baseline/`.

| Check | Expected values (computed in Python) | Snowflake side |
|---|---|---|
| `RAW.STATE` … `RAW.BRANCH`, `RAW.TRANSACTION_DB` | `data_sources/sample_db_extract/*.csv` as extracted | `sql/raw/*.sql` |
| `RAW.TRANSACTION_EXCEL` | `data_sources/transaction_excel.xlsx` (stdlib xlsx reader) | `sql/raw/transaction_excel.*.sql` |
| `RAW.TRANSACTION_CSV` | `data_sources/transaction_csv.csv` | `sql/raw/transaction_csv.*.sql` |
| `DWH.DIM*`, `DWH.FACTTRANSACTION` | `migration/baseline/Dim*.csv`, `FactTransaction.csv` (no `SOURCESYSTEM`/`LOADEDAT`) | `sql/dwh/*.sql` |
| `OPS.FACTTRANSACTION_REJECTS` | Talend rules on the extracts: orphans 23/24/25 + lower-precedence duplicates 6/7 (EXCEL), 14/15 (CSV) | `sql/ops/*.sql` |
| `DWH.SP_DAILYTRANSACTION`, `DWH.SP_BALANCEPERCUSTOMER` | `migration/baseline/sp_*.csv` | `sql/procedures/*.sql` (`CALL` + `RESULT_SCAN(LAST_QUERY_ID())`) |

Per check: `ROW_COUNT`, `SUM_<col>` for every key and amount column, `CHECKSUM` (all rows) and, for large tables,
`SAMPLE_ROWS`/`SAMPLE_CHECKSUM` over a deterministic sample. On a mismatch the harness runs the matching
`*.rowhashes.sql` and reports the keys that are missing, unexpected or changed.

## Usage

```bash
pip install -r migration/validation/requirements.txt
python migration/validation/validate.py --offline            # no Snowflake; what CI runs
python migration/validation/validate.py --report report.json # live; skips (exit 0) if SNOWFLAKE_ACCOUNT is unset
python migration/validation/validate.py --require-live --only DWH.  # live, fail (exit 2) if not configured
python migration/validation/validate.py --write-sql          # regenerate sql/ after changing a spec
pytest migration/validation
```

Exit codes: `0` pass/skipped, `1` mismatch or query error, `2` configuration error.
Connection: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`
(default `BANKING_DWH`), and either `SNOWFLAKE_PASSWORD` or `SNOWFLAKE_PRIVATE_KEY_PATH`
(+ `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`); key-pair wins when both are set. CLI flags override env vars.
`snowflake.connector` is imported only when a live connection is opened.

`--offline` recomputes the DWH tables, rejects and procedure outputs from the extracts by applying the Talend rules
(PLAN.md §1.5/§1.6) in Python and asserts their metrics equal the baseline's. This proves the documented rules and
the canonicalisation reproduce SQL Server; live mode then proves Snowflake reproduces the same numbers.

## Canonicalisation

Python (`validate.py`, `canon_*`) and Snowflake (`sql/`) render every value to the same string before hashing:

| Type | Canonical form | Snowflake expression |
|---|---|---|
| NULL | `\x1e` (record separator), distinct from `''` | `COALESCE(..., CHR(30))` |
| Integer keys | base-10, no sign for positives, no leading zeros | `TO_VARCHAR(CAST(x AS NUMBER(38, 0)))` |
| Amounts (`MONEY` → `NUMBER(19,4)`) | exactly 4 decimals, half-up rounding (`100000.0000`) | `TO_VARCHAR(CAST(x AS NUMBER(38, 4)))` |
| Timestamps (`DATETIME` → `TIMESTAMP_NTZ(3)`) | `YYYY-MM-DD HH:MM:SS.fff`, truncated to ms, no time zone | `TO_VARCHAR(CAST(x AS TIMESTAMP_NTZ(3)), 'YYYY-MM-DD HH24:MI:SS.FF3')` |
| Dates | `YYYY-MM-DD` | `TO_VARCHAR(CAST(x AS DATE), 'YYYY-MM-DD')` |
| Strings | verbatim UTF-8; no trimming, no case folding | `CAST(x AS VARCHAR)` |

- Row hash: `SHA-256(hex)` of the canonical fields in spec order joined by `\x1f` (`CHR(31)`).
- Table checksum: `SHA-256` of the sorted row hashes joined by `,`. Order-independent, duplicate-sensitive;
  an empty table hashes `''`.
- Sample: rows whose key hash (`SHA-256` of the canonical key) has a 4-hex-digit prefix below
  `format(int(rate * 65536), '04x')`. Depends only on the key, so the same rows are sampled on both sides
  regardless of load order or table size. `DWH.FACTTRANSACTION` (the only fact table) uses a 50%
  sample in addition to the full checksum; on production volumes drop the full checksum and keep the sample.
- Sums are compared as `NUMBER(38,4)` strings, so `SUM` of an integer key is `231.0000` on both sides.
- Excel cells are serial numbers; they are converted with the 1900 date system and rounded to the millisecond.
- `OPS.FACTTRANSACTION_REJECTS` is compared on the 6 source columns + `SOURCESYSTEM` + `REJECTREASON`
  (`REJECTEDAT` is a load timestamp) after `SELECT DISTINCT`, so a re-run that re-appends the same rejects passes.

`tests/test_validation_sql.py` runs every generated metrics/rowhash query on DuckDB (transpiled from the Snowflake
dialect with sqlglot) and asserts it returns exactly the Python values; it skips if those packages are absent.
