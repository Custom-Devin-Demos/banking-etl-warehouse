-- DWH.SP_BALANCEPERCUSTOMER: Snowflake Scripting port of dbo.sp_BalancePerCustomer
-- (sql_scripts/02_create_procedures.sql). Current balance of every active account of the
-- customers whose name contains CUSTOMER_NAME (case-insensitive, like the SQL Server CI collation).
-- Contract: migration/PLAN.md section 3. Mapping notes: migration/snowflake/procedures/README.md.
-- Errors: a NULL parameter raises -20003; any error is logged to OPS.ETL_ERROR_LOG, then re-raised.

USE DATABASE BANKING_DWH;

CREATE OR REPLACE PROCEDURE DWH.SP_BALANCEPERCUSTOMER(CUSTOMER_NAME VARCHAR)
RETURNS TABLE (CUSTOMERNAME VARCHAR, ACCOUNTTYPE VARCHAR, INITIALBALANCE NUMBER(19, 4), CURRENTBALANCE NUMBER(19, 4))
LANGUAGE SQL
COMMENT = 'Port of dbo.sp_BalancePerCustomer: initial and current balance of active accounts, name match is ILIKE'
EXECUTE AS OWNER
AS
$$
DECLARE
    null_parameter EXCEPTION (-20003, 'SP_BALANCEPERCUSTOMER: CUSTOMER_NAME must not be NULL');
    res RESULTSET;
    err_code NUMBER;
    err_state VARCHAR;
    err_message VARCHAR;
BEGIN
    IF (CUSTOMER_NAME IS NULL) THEN
        RAISE null_parameter;
    END IF;

    -- core-select:begin
    res := (
        WITH TRANSACTIONSUMMARY AS (
            SELECT
                ACCOUNTID,
                SUM(
                    CASE
                        WHEN UPPER(TRANSACTIONTYPE) = 'DEPOSIT' THEN AMOUNT
                        ELSE -AMOUNT
                    END
                ) AS TOTALTRANSACTIONAMOUNT
            FROM DWH.FACTTRANSACTION
            GROUP BY ACCOUNTID
        )

        SELECT
            C.CUSTOMERNAME,
            A.ACCOUNTTYPE,
            A.BALANCE AS INITIALBALANCE,
            CAST(A.BALANCE + COALESCE(TS.TOTALTRANSACTIONAMOUNT, 0) AS NUMBER(19, 4)) AS CURRENTBALANCE
        FROM DWH.DIMCUSTOMER AS C
        INNER JOIN DWH.DIMACCOUNT AS A ON C.CUSTOMERID = A.CUSTOMERID
        LEFT JOIN TRANSACTIONSUMMARY AS TS ON A.ACCOUNTID = TS.ACCOUNTID
        WHERE
            C.CUSTOMERNAME ILIKE ('%' || :CUSTOMER_NAME || '%')
            AND UPPER(A.STATUS) = 'ACTIVE'
        ORDER BY A.ACCOUNTID
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
                'DWH.SP_BALANCEPERCUSTOMER',
                :err_code,
                :err_state,
                :err_message,
                OBJECT_CONSTRUCT_KEEP_NULL('CUSTOMER_NAME', :CUSTOMER_NAME);
        RAISE;
END;
$$;
