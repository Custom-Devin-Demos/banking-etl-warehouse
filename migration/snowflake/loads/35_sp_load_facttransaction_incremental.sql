-- Unit 3 (loads): incremental Load_FactTransaction, run by the task graph (50_tasks.sql). Additive to the
-- PLAN.md §3 signatures: OPS.SP_LOAD_FACTTRANSACTION (full reload) stays the Talend-equivalent entry point and
-- the recovery path.
--
-- 1. Consume RAW.STRM_TRANSACTION_DB / _EXCEL / _CSV into OPS.FACTTRANSACTION_INCREMENT in one INSERT (all three
--    stream offsets advance at COMMIT, so a failure leaves them untouched and the batch is retried).
-- 2. OPS.V_FACTTRANSACTION_INCREMENT_ACTIONS re-decides every batch TRANSACTION_ID over all of its RAW rows with
--    the full-reload rules (DB > EXCEL > CSV, then earliest load; orphan check) and compares the winner to
--    DWH.FACTTRANSACTION.
-- 3. Rejected batch rows, and fact rows replaced or removed by step 4 -> OPS.FACTTRANSACTION_REJECTS.
-- 4. MERGE applies FACT_ACTION (INSERT / UPDATE / DELETE). The MERGE source has one row per TRANSACTIONID, so
--    the MERGE is deterministic.
-- Difference from the full reload: a row rejected as an orphan is not retried when its dimension row arrives
-- later; run OPS.SP_LOAD_FACTTRANSACTION() (re-evaluates all of RAW) after late dimension data.

USE DATABASE BANKING_DWH;

CREATE OR REPLACE PROCEDURE OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL()
RETURNS VARCHAR
LANGUAGE SQL
COMMENT = 'Incremental DWH.FACTTRANSACTION load from the RAW.TRANSACTION_* streams'
EXECUTE AS OWNER
AS
$$
DECLARE
    step VARCHAR DEFAULT 'start';
    run_ts TIMESTAMP_NTZ;
    batch_rows NUMBER DEFAULT 0;
    rows_inserted NUMBER DEFAULT 0;
    rows_updated NUMBER DEFAULT 0;
    rows_deleted NUMBER DEFAULT 0;
    rows_rejected NUMBER DEFAULT 0;
    err_code NUMBER;
    err_state VARCHAR;
    err_msg VARCHAR;
BEGIN
    IF (
        NOT SYSTEM$STREAM_HAS_DATA('RAW.STRM_TRANSACTION_DB')
        AND NOT SYSTEM$STREAM_HAS_DATA('RAW.STRM_TRANSACTION_EXCEL')
        AND NOT SYSTEM$STREAM_HAS_DATA('RAW.STRM_TRANSACTION_CSV')
    ) THEN
        RETURN TO_JSON(OBJECT_CONSTRUCT(
            'procedure', 'OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL', 'mode', 'INCREMENTAL', 'batch_rows', 0
        ));
    END IF;

    run_ts := SYSDATE();
    BEGIN TRANSACTION;

    step := 'clear_increment';
    DELETE FROM OPS.FACTTRANSACTION_INCREMENT;

    step := 'consume_streams';
    INSERT INTO OPS.FACTTRANSACTION_INCREMENT (
        TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID,
        SOURCESYSTEM, SOURCE_RANK, _SOURCE_FILE, _SOURCE_ROW, _LOADED_AT
    )
    SELECT
        TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID,
        'DB', 1, _SOURCE_FILE, _SOURCE_ROW, _LOADED_AT
    FROM RAW.STRM_TRANSACTION_DB
    WHERE METADATA$ACTION = 'INSERT'
    UNION ALL
    SELECT
        TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID,
        'EXCEL', 2, _SOURCE_FILE, _SOURCE_ROW, _LOADED_AT
    FROM RAW.STRM_TRANSACTION_EXCEL
    WHERE METADATA$ACTION = 'INSERT'
    UNION ALL
    SELECT
        TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID,
        'CSV', 3, _SOURCE_FILE, _SOURCE_ROW, _LOADED_AT
    FROM RAW.STRM_TRANSACTION_CSV
    WHERE METADATA$ACTION = 'INSERT';

    step := 'count_actions';
    SELECT COUNT(*) INTO :batch_rows FROM OPS.FACTTRANSACTION_INCREMENT;
    SELECT
        COUNT_IF(FACT_ACTION = 'INSERT'),
        COUNT_IF(FACT_ACTION = 'UPDATE'),
        COUNT_IF(FACT_ACTION = 'DELETE')
    INTO :rows_inserted, :rows_updated, :rows_deleted
    FROM OPS.V_FACTTRANSACTION_INCREMENT_ACTIONS;
    SELECT COUNT(*) INTO :rows_rejected FROM OPS.V_FACTTRANSACTION_INCREMENT_REJECTS;

    step := 'insert_rejects';
    INSERT INTO OPS.FACTTRANSACTION_REJECTS (
        TRANSACTION_ID, ACCOUNT_ID, TRANSACTION_DATE, AMOUNT, TRANSACTION_TYPE, BRANCH_ID, SOURCESYSTEM, REJECTREASON, REJECTEDAT
    )
    SELECT
        TRANSACTION_ID,
        ACCOUNT_ID,
        TRANSACTION_DATE,
        AMOUNT,
        TRANSACTION_TYPE,
        BRANCH_ID,
        SOURCESYSTEM,
        REJECTREASON,
        :run_ts
    FROM OPS.V_FACTTRANSACTION_INCREMENT_REJECTS
    UNION ALL
    SELECT
        F.TRANSACTIONID,
        F.ACCOUNTID,
        F.TRANSACTIONDATE,
        F.AMOUNT,
        F.TRANSACTIONTYPE,
        F.BRANCHID,
        F.SOURCESYSTEM,
        'DUPLICATE_LOWER_PRECEDENCE',
        :run_ts
    FROM DWH.FACTTRANSACTION AS F
    INNER JOIN OPS.V_FACTTRANSACTION_INCREMENT_ACTIONS AS V
        ON F.TRANSACTIONID = V.TRANSACTION_ID
    WHERE V.FACT_ACTION IN ('UPDATE', 'DELETE');

    step := 'merge_fact';
    MERGE INTO DWH.FACTTRANSACTION AS TGT
    USING (
        SELECT *
        FROM OPS.V_FACTTRANSACTION_INCREMENT_ACTIONS
        WHERE FACT_ACTION IN ('INSERT', 'UPDATE', 'DELETE')
    ) AS SRC
        ON TGT.TRANSACTIONID = SRC.TRANSACTION_ID
    WHEN MATCHED AND SRC.FACT_ACTION = 'DELETE' THEN DELETE
    WHEN MATCHED AND SRC.FACT_ACTION = 'UPDATE' THEN UPDATE SET
        ACCOUNTID = SRC.ACCOUNT_ID,
        TRANSACTIONDATE = SRC.TRANSACTION_DATE,
        AMOUNT = SRC.AMOUNT,
        TRANSACTIONTYPE = SRC.TRANSACTION_TYPE,
        BRANCHID = SRC.BRANCH_ID,
        SOURCESYSTEM = SRC.SOURCESYSTEM,
        LOADEDAT = :run_ts
    WHEN NOT MATCHED AND SRC.FACT_ACTION = 'INSERT' THEN INSERT (
        TRANSACTIONID, ACCOUNTID, TRANSACTIONDATE, AMOUNT, TRANSACTIONTYPE, BRANCHID, SOURCESYSTEM, LOADEDAT
    ) VALUES (
        SRC.TRANSACTION_ID,
        SRC.ACCOUNT_ID,
        SRC.TRANSACTION_DATE,
        SRC.AMOUNT,
        SRC.TRANSACTION_TYPE,
        SRC.BRANCH_ID,
        SRC.SOURCESYSTEM,
        :run_ts
    );

    COMMIT;

    RETURN TO_JSON(OBJECT_CONSTRUCT(
        'procedure', 'OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL',
        'target', 'DWH.FACTTRANSACTION',
        'mode', 'INCREMENTAL',
        'batch_rows', batch_rows,
        'rows_inserted', rows_inserted,
        'rows_updated', rows_updated,
        'rows_deleted', rows_deleted,
        'rows_rejected', rows_rejected
    ));
EXCEPTION
    WHEN OTHER THEN
        err_code := SQLCODE;
        err_state := SQLSTATE;
        err_msg := SQLERRM;
        ROLLBACK;
        INSERT INTO OPS.ETL_ERROR_LOG (PROCEDURE_NAME, SQLCODE, SQLSTATE, SQLERRM, CONTEXT)
        SELECT
            'OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL', :err_code, :err_state, :err_msg,
            OBJECT_CONSTRUCT('step', :step, 'batch_rows', :batch_rows);
        RAISE;
END;
$$;
