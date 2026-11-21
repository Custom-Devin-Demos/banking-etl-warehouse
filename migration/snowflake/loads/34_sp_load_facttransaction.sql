-- Unit 3 (loads): replaces Talend job Load_FactTransaction (full reload, PLAN.md §1.5 / §1.6).
--   tMSSqlInput + tFileInputExcel + tFileInputDelimited -> RAW.TRANSACTION_DB / _EXCEL / _CSV (OPS.SP_COPY_RAW)
--   tUnite -> UNION ALL; tUniqRow(transaction_id) -> ROW_NUMBER() precedence DB > EXCEL > CSV;
--   tMap 1:1 -> OPS.V_FACTTRANSACTION_CANDIDATES (30_transform_views.sql)
--   tMSSqlOutput TABLE_ACTION=TRUNCATE + DATA_ACTION=INSERT -> INSERT OVERWRITE (atomic truncate + insert)
--   DIE_ON_ERROR=false + enforced FK -> orphan and duplicate rows written to OPS.FACTTRANSACTION_REJECTS
-- Everything runs in one transaction: the fact table is never seen empty or half loaded, and a failure rolls
-- back the reject rows too. The streams on RAW.TRANSACTION_* are consumed in the same transaction, because this
-- full reload already covers every RAW row; OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL continues from there.
-- Reject rows are a per-run log (REJECTEDAT = run time): every full reload re-logs the current rejects.

USE DATABASE BANKING_DWH;

CREATE OR REPLACE PROCEDURE OPS.SP_LOAD_FACTTRANSACTION()
RETURNS VARCHAR
LANGUAGE SQL
COMMENT = 'Replaces Talend Load_FactTransaction: full reload of DWH.FACTTRANSACTION with precedence and rejects'
EXECUTE AS OWNER
AS
$$
DECLARE
    step VARCHAR DEFAULT 'start';
    run_ts TIMESTAMP_NTZ;
    source_rows NUMBER DEFAULT 0;
    rows_inserted NUMBER DEFAULT 0;
    rejected_duplicate NUMBER DEFAULT 0;
    rejected_orphan_account NUMBER DEFAULT 0;
    rejected_orphan_branch NUMBER DEFAULT 0;
    err_code NUMBER;
    err_state VARCHAR;
    err_msg VARCHAR;
BEGIN
    run_ts := SYSDATE();
    BEGIN TRANSACTION;

    step := 'consume_streams';
    INSERT INTO OPS.FACTTRANSACTION_INCREMENT (TRANSACTION_ID)
    SELECT TRANSACTION_ID FROM RAW.STRM_TRANSACTION_DB WHERE FALSE
    UNION ALL
    SELECT TRANSACTION_ID FROM RAW.STRM_TRANSACTION_EXCEL WHERE FALSE
    UNION ALL
    SELECT TRANSACTION_ID FROM RAW.STRM_TRANSACTION_CSV WHERE FALSE;

    step := 'count_candidates';
    SELECT
        COUNT(*),
        COUNT_IF(REJECTREASON IS NULL),
        COUNT_IF(REJECTREASON = 'DUPLICATE_LOWER_PRECEDENCE'),
        COUNT_IF(REJECTREASON = 'ORPHAN_ACCOUNT'),
        COUNT_IF(REJECTREASON = 'ORPHAN_BRANCH')
    INTO :source_rows, :rows_inserted, :rejected_duplicate, :rejected_orphan_account, :rejected_orphan_branch
    FROM OPS.V_FACTTRANSACTION_CANDIDATES;

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
    FROM OPS.V_FACTTRANSACTION_CANDIDATES
    WHERE REJECTREASON IS NOT NULL;

    step := 'insert_overwrite_fact';
    INSERT OVERWRITE INTO DWH.FACTTRANSACTION (
        TRANSACTIONID, ACCOUNTID, TRANSACTIONDATE, AMOUNT, TRANSACTIONTYPE, BRANCHID, SOURCESYSTEM, LOADEDAT
    )
    SELECT
        TRANSACTION_ID,
        ACCOUNT_ID,
        TRANSACTION_DATE,
        AMOUNT,
        TRANSACTION_TYPE,
        BRANCH_ID,
        SOURCESYSTEM,
        :run_ts
    FROM OPS.V_FACTTRANSACTION_CANDIDATES
    WHERE REJECTREASON IS NULL;

    COMMIT;

    SYSTEM$LOG_INFO('OPS.SP_LOAD_FACTTRANSACTION loaded ' || rows_inserted || ' of ' || source_rows || ' rows');
    RETURN TO_JSON(OBJECT_CONSTRUCT(
        'procedure', 'OPS.SP_LOAD_FACTTRANSACTION',
        'target', 'DWH.FACTTRANSACTION',
        'mode', 'FULL_RELOAD',
        'source_rows', source_rows,
        'rows_inserted', rows_inserted,
        'rows_rejected', rejected_duplicate + rejected_orphan_account + rejected_orphan_branch,
        'rejected_by_reason', OBJECT_CONSTRUCT(
            'DUPLICATE_LOWER_PRECEDENCE', rejected_duplicate,
            'ORPHAN_ACCOUNT', rejected_orphan_account,
            'ORPHAN_BRANCH', rejected_orphan_branch
        )
    ));
EXCEPTION
    WHEN OTHER THEN
        err_code := SQLCODE;
        err_state := SQLSTATE;
        err_msg := SQLERRM;
        ROLLBACK;
        INSERT INTO OPS.ETL_ERROR_LOG (PROCEDURE_NAME, SQLCODE, SQLSTATE, SQLERRM, CONTEXT)
        SELECT 'OPS.SP_LOAD_FACTTRANSACTION', :err_code, :err_state, :err_msg, OBJECT_CONSTRUCT('step', :step);
        RAISE;
END;
$$;
