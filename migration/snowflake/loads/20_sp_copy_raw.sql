-- Unit 3 (loads): COPY INTO for every RAW table (replaces tMSSqlInput, tFileInputDelimited and, via
-- xlsx_to_csv.py, tFileInputExcel). Root of the task graph (50_tasks.sql) and step 1 of 90_run_full_load.sql.
--
-- * COPY load metadata skips files already loaded into the table (64 days), so a re-run only loads new files.
--   Re-landing the same file under a new name is harmless: the views dedup on key (dims: latest row,
--   fact: precedence), and identical fact re-deliveries are no-ops.
-- * ON_ERROR = ABORT_STATEMENT (fail-fast). Talend file inputs had DIE_ON_ERROR=false and silently skipped
--   unparsable rows; a bank load should not lose rows silently. A failed COPY is logged to OPS.ETL_ERROR_LOG
--   and raised; inspect it with SELECT * FROM TABLE(VALIDATE(RAW.<table>, JOB_ID => '_last')).
-- * Explicit TO_TIMESTAMP_NTZ formats so parsing never depends on session TIMESTAMP_INPUT_FORMAT.
-- * INCLUDE_FILE_SOURCES = FALSE when the Snowpipes in 60_snowpipes.sql load the file sources instead
--   (bulk COPY and Snowpipe keep separate load metadata and would both load the same file).

USE DATABASE BANKING_DWH;

CREATE OR REPLACE PROCEDURE OPS.SP_COPY_RAW(INCLUDE_FILE_SOURCES BOOLEAN DEFAULT TRUE)
RETURNS VARCHAR
LANGUAGE SQL
COMMENT = 'COPY INTO RAW.* from @OPS.LANDING_STAGE (replaces the Talend input components)'
EXECUTE AS OWNER
AS
$$
DECLARE
    step VARCHAR DEFAULT 'start';
    rows_before NUMBER;
    rows_after NUMBER;
    loaded OBJECT DEFAULT OBJECT_CONSTRUCT();
    err_code NUMBER;
    err_state VARCHAR;
    err_msg VARCHAR;
BEGIN
    step := 'RAW.STATE';
    SELECT COUNT(*) INTO :rows_before FROM RAW.STATE;
    COPY INTO RAW.STATE (STATE_ID, STATE_NAME, _SOURCE_FILE, _SOURCE_ROW)
    FROM (
        SELECT $1, $2, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @OPS.LANDING_STAGE/extracts/state/
    )
    FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_EXTRACT')
    PATTERN = '.*[.]csv([.]gz)?'
    ON_ERROR = ABORT_STATEMENT;
    SELECT COUNT(*) INTO :rows_after FROM RAW.STATE;
    loaded := OBJECT_INSERT(loaded, 'RAW.STATE', rows_after - rows_before);

    step := 'RAW.CITY';
    SELECT COUNT(*) INTO :rows_before FROM RAW.CITY;
    COPY INTO RAW.CITY (CITY_ID, CITY_NAME, STATE_ID, _SOURCE_FILE, _SOURCE_ROW)
    FROM (
        SELECT $1, $2, $3, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @OPS.LANDING_STAGE/extracts/city/
    )
    FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_EXTRACT')
    PATTERN = '.*[.]csv([.]gz)?'
    ON_ERROR = ABORT_STATEMENT;
    SELECT COUNT(*) INTO :rows_after FROM RAW.CITY;
    loaded := OBJECT_INSERT(loaded, 'RAW.CITY', rows_after - rows_before);

    step := 'RAW.CUSTOMER';
    SELECT COUNT(*) INTO :rows_before FROM RAW.CUSTOMER;
    COPY INTO RAW.CUSTOMER (
        CUSTOMER_ID, CUSTOMER_NAME, ADDRESS, CITY_ID, AGE, GENDER, EMAIL, _SOURCE_FILE, _SOURCE_ROW
    )
    FROM (
        SELECT $1, $2, $3, $4, $5, $6, $7, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @OPS.LANDING_STAGE/extracts/customer/
    )
    FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_EXTRACT')
    PATTERN = '.*[.]csv([.]gz)?'
    ON_ERROR = ABORT_STATEMENT;
    SELECT COUNT(*) INTO :rows_after FROM RAW.CUSTOMER;
    loaded := OBJECT_INSERT(loaded, 'RAW.CUSTOMER', rows_after - rows_before);

    step := 'RAW.ACCOUNT';
    SELECT COUNT(*) INTO :rows_before FROM RAW.ACCOUNT;
    COPY INTO RAW.ACCOUNT (
        ACCOUNT_ID, CUSTOMER_ID, ACCOUNT_TYPE, BALANCE, DATE_OPENED, STATUS, _SOURCE_FILE, _SOURCE_ROW
    )
    FROM (
        SELECT
            $1, $2, $3, $4, TO_TIMESTAMP_NTZ($5, 'YYYY-MM-DD HH24:MI:SS'), $6,
            METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @OPS.LANDING_STAGE/extracts/account/
    )
    FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_EXTRACT')
    PATTERN = '.*[.]csv([.]gz)?'
    ON_ERROR = ABORT_STATEMENT;
    SELECT COUNT(*) INTO :rows_after FROM RAW.ACCOUNT;
    loaded := OBJECT_INSERT(loaded, 'RAW.ACCOUNT', rows_after - rows_before);

    step := 'RAW.BRANCH';
    SELECT COUNT(*) INTO :rows_before FROM RAW.BRANCH;
    COPY INTO RAW.BRANCH (BRANCH_ID, BRANCH_NAME, BRANCH_LOCATION, _SOURCE_FILE, _SOURCE_ROW)
    FROM (
        SELECT $1, $2, $3, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @OPS.LANDING_STAGE/extracts/branch/
    )
    FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_EXTRACT')
    PATTERN = '.*[.]csv([.]gz)?'
    ON_ERROR = ABORT_STATEMENT;
    SELECT COUNT(*) INTO :rows_after FROM RAW.BRANCH;
    loaded := OBJECT_INSERT(loaded, 'RAW.BRANCH', rows_after - rows_before);

    step := 'RAW.TRANSACTION_DB';
    SELECT COUNT(*) INTO :rows_before FROM RAW.TRANSACTION_DB;
    COPY INTO RAW.TRANSACTION_DB (
        TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID, _SOURCE_FILE, _SOURCE_ROW
    )
    FROM (
        SELECT
            $1, $2, TO_TIMESTAMP_NTZ($3, 'YYYY-MM-DD HH24:MI:SS'), $4, $5, $6,
            METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @OPS.LANDING_STAGE/extracts/transaction_db/
    )
    FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_EXTRACT')
    PATTERN = '.*[.]csv([.]gz)?'
    ON_ERROR = ABORT_STATEMENT;
    SELECT COUNT(*) INTO :rows_after FROM RAW.TRANSACTION_DB;
    loaded := OBJECT_INSERT(loaded, 'RAW.TRANSACTION_DB', rows_after - rows_before);

    IF (INCLUDE_FILE_SOURCES) THEN
        step := 'RAW.TRANSACTION_EXCEL';
        SELECT COUNT(*) INTO :rows_before FROM RAW.TRANSACTION_EXCEL;
        COPY INTO RAW.TRANSACTION_EXCEL (
            TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID, _SOURCE_FILE, _SOURCE_ROW
        )
        FROM (
            SELECT
                $1, $2, TO_TIMESTAMP_NTZ($3, 'YYYY-MM-DD HH24:MI:SS'), $4, $5, $6,
                METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
            FROM @OPS.LANDING_STAGE/files/transaction_excel/
        )
        FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_EXTRACT')
        PATTERN = '.*[.]csv([.]gz)?'
        ON_ERROR = ABORT_STATEMENT;
        SELECT COUNT(*) INTO :rows_after FROM RAW.TRANSACTION_EXCEL;
        loaded := OBJECT_INSERT(loaded, 'RAW.TRANSACTION_EXCEL', rows_after - rows_before);

        step := 'RAW.TRANSACTION_CSV';
        SELECT COUNT(*) INTO :rows_before FROM RAW.TRANSACTION_CSV;
        COPY INTO RAW.TRANSACTION_CSV (
            TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID, _SOURCE_FILE, _SOURCE_ROW
        )
        FROM (
            SELECT
                $1, $2, TO_TIMESTAMP_NTZ($3, 'DD-MM-YYYY HH24:MI:SS'), $4, $5, $6,
                METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
            FROM @OPS.LANDING_STAGE/files/transaction_csv/
        )
        FILE_FORMAT = (FORMAT_NAME = 'OPS.FF_CSV_TRANSACTION')
        PATTERN = '.*[.]csv([.]gz)?'
        ON_ERROR = ABORT_STATEMENT;
        SELECT COUNT(*) INTO :rows_after FROM RAW.TRANSACTION_CSV;
        loaded := OBJECT_INSERT(loaded, 'RAW.TRANSACTION_CSV', rows_after - rows_before);
    END IF;

    RETURN TO_JSON(OBJECT_CONSTRUCT('procedure', 'OPS.SP_COPY_RAW', 'rows_loaded', loaded));
EXCEPTION
    WHEN OTHER THEN
        err_code := SQLCODE;
        err_state := SQLSTATE;
        err_msg := SQLERRM;
        INSERT INTO OPS.ETL_ERROR_LOG (PROCEDURE_NAME, SQLCODE, SQLSTATE, SQLERRM, CONTEXT)
        SELECT
            'OPS.SP_COPY_RAW', :err_code, :err_state, :err_msg,
            OBJECT_CONSTRUCT('step', :step, 'rows_loaded', :loaded, 'include_file_sources', :INCLUDE_FILE_SOURCES);
        RAISE;
END;
$$;
