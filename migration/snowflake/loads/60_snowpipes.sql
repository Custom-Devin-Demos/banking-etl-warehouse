-- Unit 3 (loads): Snowpipe definitions for the file sources (event-driven alternative to the batch COPY in
-- OPS.SP_COPY_RAW). Deploy either these pipes or the file-source COPY in the root task, not both, for the same
-- prefix: Snowpipe and bulk COPY keep separate load metadata. When the pipes are deployed, change the root task
-- to CALL OPS.SP_COPY_RAW(FALSE).
--
-- Internal stage (default, this file): no AUTO_INGEST. Files are queued via the Snowpipe REST API
-- (insertFiles, e.g. from the Synapse/ADF export job), or picked up with
--   ALTER PIPE OPS.PIPE_TRANSACTION_CSV REFRESH;   -- files staged in the last 7 days
-- ON_ERROR = SKIP_FILE: a bad file is rejected as a whole and shows in
--   SELECT * FROM TABLE(INFORMATION_SCHEMA.COPY_HISTORY(TABLE_NAME => 'RAW.TRANSACTION_CSV', START_TIME => ...));
-- (ABORT_STATEMENT is not available for Snowpipe.) New rows flow through the RAW streams to the fact task.
--
-- CREATE PIPE IF NOT EXISTS: CREATE OR REPLACE PIPE drops the pipe's load history, and a later REFRESH would
-- load the same files again. To change a pipe definition: ALTER PIPE ... SET PIPE_EXECUTION_PAUSED = TRUE,
-- recreate, and only refresh files staged after the recreate.
--
-- Auto-ingest on ADLS Gen2 (environment-specific; Event Grid -> Storage Queue -> notification integration):
-- CREATE NOTIFICATION INTEGRATION BANKING_LANDING_EVENTS
--     ENABLED = TRUE
--     TYPE = QUEUE
--     NOTIFICATION_PROVIDER = AZURE_STORAGE_QUEUE
--     AZURE_STORAGE_QUEUE_PRIMARY_URI = 'https://<account>.queue.core.windows.net/<queue>'
--     AZURE_TENANT_ID = '<tenant-id>';
-- then, for each pipe below, read from the external stage of 01_stage.sql and add:
--     AUTO_INGEST = TRUE
--     INTEGRATION = 'BANKING_LANDING_EVENTS'

USE DATABASE BANKING_DWH;

CREATE PIPE IF NOT EXISTS OPS.PIPE_TRANSACTION_CSV
COMMENT = 'Snowpipe: transaction_csv.csv drops -> RAW.TRANSACTION_CSV (replaces tFileInputDelimited)'
AS
COPY INTO RAW.TRANSACTION_CSV (
    TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID, _SOURCE_FILE, _SOURCE_ROW
)
FROM (
    SELECT
        $1,
        $2,
        TO_TIMESTAMP_NTZ($3, 'DD-MM-YYYY HH24:MI:SS') AS TRANSACTION_DATE,
        $4,
        $5,
        $6,
        METADATA$FILENAME,
        METADATA$FILE_ROW_NUMBER
    FROM @OPS.LANDING_STAGE/files/transaction_csv/
)
FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_TRANSACTION')
PATTERN = '.*[.]csv([.]gz)?'
ON_ERROR = SKIP_FILE;

CREATE PIPE IF NOT EXISTS OPS.PIPE_TRANSACTION_EXCEL
COMMENT = 'Snowpipe: xlsx_to_csv.py output -> RAW.TRANSACTION_EXCEL (replaces tFileInputExcel)'
AS
COPY INTO RAW.TRANSACTION_EXCEL (
    TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID, _SOURCE_FILE, _SOURCE_ROW
)
FROM (
    SELECT
        $1,
        $2,
        TO_TIMESTAMP_NTZ($3, 'YYYY-MM-DD HH24:MI:SS') AS TRANSACTION_DATE,
        $4,
        $5,
        $6,
        METADATA$FILENAME,
        METADATA$FILE_ROW_NUMBER
    FROM @OPS.LANDING_STAGE/files/transaction_excel/
)
FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_EXTRACT')
PATTERN = '.*[.]csv([.]gz)?'
ON_ERROR = SKIP_FILE;
