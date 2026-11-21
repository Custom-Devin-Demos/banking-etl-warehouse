-- Unit 3 (loads): file formats for the Talend source files.
-- Replaces the parsing settings of tFileInputDelimited / tMSSqlInput (PLAN.md §1.5).

USE DATABASE BANKING_DWH;

-- SQL Server extracts (data_sources/sample_db_extract/*.csv, PLAN.md §1.1) and the CSV produced from the Excel
-- source by migration/loads/xlsx_to_csv.py: RFC 4180, UTF-8, header row, empty field = NULL,
-- datetimes as YYYY-MM-DD HH24:MI:SS.
CREATE OR REPLACE FILE FORMAT OPS.FF_CSV_EXTRACT
TYPE = CSV
FIELD_DELIMITER = ','
RECORD_DELIMITER = '\n'
SKIP_HEADER = 1
FIELD_OPTIONALLY_ENCLOSED_BY = '"'
EMPTY_FIELD_AS_NULL = TRUE
SKIP_BLANK_LINES = TRUE
TRIM_SPACE = FALSE
ERROR_ON_COLUMN_COUNT_MISMATCH = TRUE
TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS'
DATE_FORMAT = 'YYYY-MM-DD'
ENCODING = 'UTF8'
COMMENT = 'SQL Server extracts and xlsx_to_csv.py output: RFC 4180, UTF-8, header, ISO datetimes';

-- data_sources/transaction_csv.csv, matching the Talend tFileInputDelimited settings: ',' separator, '\n' rows,
-- 1 header row, CSV_OPTION=false (no quoting), REMOVE_EMPTY_ROW=true, ISO-8859-15, dd-MM-yyyy HH:mm:ss dates.
CREATE OR REPLACE FILE FORMAT OPS.FF_CSV_TRANSACTION
TYPE = CSV
FIELD_DELIMITER = ','
RECORD_DELIMITER = '\n'
SKIP_HEADER = 1
FIELD_OPTIONALLY_ENCLOSED_BY = NONE
EMPTY_FIELD_AS_NULL = TRUE
SKIP_BLANK_LINES = TRUE
TRIM_SPACE = FALSE
ERROR_ON_COLUMN_COUNT_MISMATCH = TRUE
TIMESTAMP_FORMAT = 'DD-MM-YYYY HH24:MI:SS'
ENCODING = 'ISO-8859-15'
COMMENT = 'transaction_csv.csv: Talend tFileInputDelimited settings, dd-MM-yyyy HH:mm:ss dates';
