-- Example calls for the unit 2 reporting procedures, with the expected results (SQL Server baseline,
-- migration/baseline/sp_*.csv). Run after ddl/, loads/ and the procedure scripts are deployed and loaded.

USE DATABASE BANKING_DWH;

-- 1. Daily summary, inclusive range. Expected (migration/baseline/sp_DailyTransaction__2024-01-18__2024-01-20.csv):
--    Date       | TOTALTRANSACTIONS | TOTALAMOUNT
--    2024-01-20 |                 3 | 2000000.0000
CALL DWH.SP_DAILYTRANSACTION('2024-01-18'::DATE, '2024-01-20'::DATE);

-- 2. Full history. Expected 9 rows (sp_DailyTransaction__2022-01-01__2024-12-31.csv):
--    2022-01-01 | 2 |  1100000.0000      2022-03-24 | 1 | 2000000.0000
--    2022-01-11 | 2 | 11000000.0000      2024-01-20 | 3 | 2000000.0000
--    2022-02-21 | 3 |   350000.0000      2024-01-21 | 2 | 2000000.0000
--    2022-03-05 | 1 |  5000000.0000      2024-01-22 | 7 | 4380000.0000
--    2022-03-15 | 1 |   300000.0000
CALL DWH.SP_DAILYTRANSACTION('2022-01-01'::DATE, '2024-12-31'::DATE);

-- 3. Balance per customer, case-insensitive substring match (SQL Server CI collation -> ILIKE).
--    Expected (sp_BalancePerCustomer__shelly.csv), ordered by AccountID:
--    CUSTOMERNAME  | ACCOUNTTYPE | INITIALBALANCE | CURRENTBALANCE
--    SHELLY JUWITA | saving      |   1500000.0000 |   1600000.0000
--    SHELLY JUWITA | checking    |  25000000.0000 |  14000000.0000
CALL DWH.SP_BALANCEPERCUSTOMER('shelly');

-- 4. Empty string matches every customer: 18 active accounts (sp_BalancePerCustomer__all.csv).
CALL DWH.SP_BALANCEPERCUSTOMER('');

-- 5. Post-process a procedure result with SQL (the CALL output itself cannot be used in a FROM clause).
CALL DWH.SP_BALANCEPERCUSTOMER('ani');
SELECT
    CUSTOMERNAME,
    SUM(CURRENTBALANCE) AS TOTAL_CURRENT_BALANCE
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
GROUP BY CUSTOMERNAME;

-- 6. Parameter validation (T-SQL returned an empty result set for these; the port raises instead):
--    CALL DWH.SP_DAILYTRANSACTION('2024-01-20'::DATE, '2024-01-18'::DATE);
--      -> error -20002 (SQLSTATE P0001): SP_DAILYTRANSACTION: START_DATE must be on or before END_DATE
--    CALL DWH.SP_DAILYTRANSACTION(NULL, '2024-01-18'::DATE);
--      -> error -20001 (SQLSTATE P0001): SP_DAILYTRANSACTION: START_DATE and END_DATE must not be NULL
--    CALL DWH.SP_BALANCEPERCUSTOMER(NULL);
--      -> error -20003 (SQLSTATE P0001): SP_BALANCEPERCUSTOMER: CUSTOMER_NAME must not be NULL
--    Each failure is first written to OPS.ETL_ERROR_LOG:
SELECT
    LOGGED_AT,
    PROCEDURE_NAME,
    SQLCODE,
    SQLSTATE,
    SQLERRM,
    CONTEXT
FROM OPS.ETL_ERROR_LOG
WHERE PROCEDURE_NAME IN ('DWH.SP_DAILYTRANSACTION', 'DWH.SP_BALANCEPERCUSTOMER')
ORDER BY LOGGED_AT DESC
LIMIT 10;
