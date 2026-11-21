-- DWH.SP_DAILYTRANSACTION: Snowflake Scripting port of dbo.sp_DailyTransaction
-- (sql_scripts/02_create_procedures.sql). Daily transaction count and total amount
-- for an inclusive date range, ordered by date.
-- Contract: migration/PLAN.md section 3. Mapping notes: migration/snowflake/procedures/README.md.
-- Errors: invalid parameters raise -20001/-20002; any error is logged to OPS.ETL_ERROR_LOG, then re-raised.

USE DATABASE BANKING_DWH;

CREATE OR REPLACE PROCEDURE DWH.SP_DAILYTRANSACTION(START_DATE DATE, END_DATE DATE)
RETURNS TABLE ("Date" DATE, TOTALTRANSACTIONS NUMBER, TOTALAMOUNT NUMBER(19, 4))
LANGUAGE SQL
COMMENT = 'Port of dbo.sp_DailyTransaction: daily COUNT/SUM of DWH.FactTransaction for START_DATE..END_DATE inclusive'
EXECUTE AS OWNER
AS
$$
DECLARE
    null_parameter EXCEPTION (-20001, 'SP_DAILYTRANSACTION: START_DATE and END_DATE must not be NULL');
    invalid_date_range EXCEPTION (-20002, 'SP_DAILYTRANSACTION: START_DATE must be on or before END_DATE');
    res RESULTSET;
    err_code NUMBER;
    err_state VARCHAR;
    err_message VARCHAR;
BEGIN
    IF (START_DATE IS NULL OR END_DATE IS NULL) THEN
        RAISE null_parameter;
    END IF;
    IF (START_DATE > END_DATE) THEN
        RAISE invalid_date_range;
    END IF;

    -- core-select:begin
    res := (
        SELECT
            CAST(TRANSACTIONDATE AS DATE) AS "Date",
            COUNT(TRANSACTIONID) AS TOTALTRANSACTIONS,
            CAST(SUM(AMOUNT) AS NUMBER(19, 4)) AS TOTALAMOUNT
        FROM DWH.FACTTRANSACTION
        WHERE CAST(TRANSACTIONDATE AS DATE) BETWEEN :START_DATE AND :END_DATE
        GROUP BY CAST(TRANSACTIONDATE AS DATE)
        ORDER BY "Date"
    );
    -- core-select:end
    RETURN TABLE(res);
EXCEPTION
    WHEN OTHER THEN
        err_code := SQLCODE;
        err_state := SQLSTATE;
        err_message := SQLERRM;
        INSERT INTO OPS.ETL_ERROR_LOG (PROCEDURE_NAME, SQLCODE, SQLSTATE, SQLERRM, CONTEXT)
            SELECT
                'DWH.SP_DAILYTRANSACTION',
                :err_code,
                :err_state,
                :err_message,
                OBJECT_CONSTRUCT_KEEP_NULL(
                    'START_DATE', TO_VARCHAR(:START_DATE, 'YYYY-MM-DD'),
                    'END_DATE', TO_VARCHAR(:END_DATE, 'YYYY-MM-DD')
                );
        RAISE;
END;
$$;
