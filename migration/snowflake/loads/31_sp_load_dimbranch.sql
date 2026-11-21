-- Unit 3 (loads): replaces Talend job Load_DimBranch (PLAN.md §1.5).
-- tMSSqlInput -> RAW.BRANCH (OPS.SP_COPY_RAW);
-- tMap -> OPS.V_DIMBRANCH_SRC (30_transform_views.sql);
-- tMSSqlOutput TABLE_ACTION=CREATE_IF_NOT_EXISTS -> table created by migration/snowflake/ddl (unit 1);
-- DATA_ACTION=INSERT + DIE_ON_ERROR=false -> MERGE ... WHEN NOT MATCHED THEN INSERT. Talend re-runs inserted
-- every row and SQL Server silently rejected the PK duplicates, so existing rows were never updated; the MERGE
-- gives the same end state without relying on an enforced PK (Snowflake does not enforce it).
--
-- SCD1 option (overwrite changed attributes; not the Talend behaviour, needs sign-off): add before the
-- WHEN NOT MATCHED clause:
--     WHEN MATCHED AND (
--         TGT.BRANCHNAME IS DISTINCT FROM SRC.BRANCHNAME
--         OR TGT.BRANCHLOCATION IS DISTINCT FROM SRC.BRANCHLOCATION
--     ) THEN UPDATE SET
--         BRANCHNAME = SRC.BRANCHNAME,
--         BRANCHLOCATION = SRC.BRANCHLOCATION
-- Error handling (PLAN.md §2 TRY/CATCH): EXCEPTION -> OPS.ETL_ERROR_LOG -> RAISE, so the task run fails.

USE DATABASE BANKING_DWH;

CREATE OR REPLACE PROCEDURE OPS.SP_LOAD_DIMBRANCH()
RETURNS VARCHAR
LANGUAGE SQL
COMMENT = 'Replaces Talend Load_DimBranch: insert-only MERGE from OPS.V_DIMBRANCH_SRC into DWH.DIMBRANCH'
EXECUTE AS OWNER
AS
$$
DECLARE
    step VARCHAR DEFAULT 'start';
    source_rows NUMBER DEFAULT 0;
    rows_inserted NUMBER DEFAULT 0;
    err_code NUMBER;
    err_state VARCHAR;
    err_msg VARCHAR;
BEGIN
    step := 'count_source';
    SELECT COUNT(*) INTO :source_rows FROM OPS.V_DIMBRANCH_SRC;

    step := 'merge';
    MERGE INTO DWH.DIMBRANCH AS TGT
    USING OPS.V_DIMBRANCH_SRC AS SRC
        ON TGT.BRANCHID = SRC.BRANCHID
    WHEN NOT MATCHED THEN INSERT (
        BRANCHID,
        BRANCHNAME,
        BRANCHLOCATION
    ) VALUES (
        SRC.BRANCHID,
        SRC.BRANCHNAME,
        SRC.BRANCHLOCATION
    );
    rows_inserted := SQLROWCOUNT;

    SYSTEM$LOG_INFO('OPS.SP_LOAD_DIMBRANCH inserted ' || rows_inserted || ' of ' || source_rows || ' source rows');
    RETURN TO_JSON(OBJECT_CONSTRUCT(
        'procedure', 'OPS.SP_LOAD_DIMBRANCH',
        'target', 'DWH.DIMBRANCH',
        'source_rows', source_rows,
        'rows_inserted', rows_inserted,
        'rows_already_present', source_rows - rows_inserted,
        'rows_rejected', 0
    ));
EXCEPTION
    WHEN OTHER THEN
        err_code := SQLCODE;
        err_state := SQLSTATE;
        err_msg := SQLERRM;
        INSERT INTO OPS.ETL_ERROR_LOG (PROCEDURE_NAME, SQLCODE, SQLSTATE, SQLERRM, CONTEXT)
        SELECT 'OPS.SP_LOAD_DIMBRANCH', :err_code, :err_state, :err_msg, OBJECT_CONSTRUCT('step', :step);
        RAISE;
END;
$$;
