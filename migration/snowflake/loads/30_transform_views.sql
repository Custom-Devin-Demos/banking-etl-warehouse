-- Unit 3 (loads): the Talend tMap / tUnite / tUniqRow logic as views over RAW (PLAN.md §1.5, §1.6).
-- The OPS.SP_LOAD_* procedures MERGE / INSERT from these views, so the transformation is plain SQL that the
-- offline tests (migration/loads/tests/) execute on DuckDB against the extracts and compare to
-- migration/baseline/.
--
-- RAW keeps every load. Dimension sources take the latest copy of each key (_LOADED_AT, then file/row),
-- so re-landing an extract never produces duplicate keys in the MERGE source.

USE DATABASE BANKING_DWH;

-- Load_DimBranch tMap: 1:1 rename.
CREATE OR REPLACE VIEW OPS.V_DIMBRANCH_SRC
COMMENT = 'Load_DimBranch tMap: latest RAW.BRANCH row per BRANCH_ID, renamed to DWH.DIMBRANCH columns'
AS
SELECT
    B.BRANCH_ID AS BRANCHID,
    B.BRANCH_NAME AS BRANCHNAME,
    B.BRANCH_LOCATION AS BRANCHLOCATION
FROM RAW.BRANCH AS B
WHERE B.BRANCH_ID IS NOT NULL
QUALIFY
    ROW_NUMBER() OVER (
        PARTITION BY B.BRANCH_ID
        ORDER BY B._LOADED_AT DESC, B._SOURCE_FILE DESC, B._SOURCE_ROW DESC
    ) = 1;

-- Load_DimAccount tMap: 1:1 rename; DATE_OPENED DATETIME2(0) -> DATE (time truncated, as the DATE target
-- column did in SQL Server); BALANCE INT -> NUMBER(19,4) (MONEY).
CREATE OR REPLACE VIEW OPS.V_DIMACCOUNT_SRC
COMMENT = 'Load_DimAccount tMap: latest RAW.ACCOUNT row per ACCOUNT_ID, DATE_OPENED truncated to DATE'
AS
SELECT
    A.ACCOUNT_ID AS ACCOUNTID,
    A.CUSTOMER_ID AS CUSTOMERID,
    A.ACCOUNT_TYPE AS ACCOUNTTYPE,
    CAST(A.BALANCE AS NUMBER(19, 4)) AS BALANCE,
    CAST(A.DATE_OPENED AS DATE) AS DATEOPENED,
    A.STATUS
FROM RAW.ACCOUNT AS A
WHERE A.ACCOUNT_ID IS NOT NULL
QUALIFY
    ROW_NUMBER() OVER (
        PARTITION BY A.ACCOUNT_ID
        ORDER BY A._LOADED_AT DESC, A._SOURCE_FILE DESC, A._SOURCE_ROW DESC
    ) = 1;

-- Load_DimCustomer tMap: customer LEFT OUTER JOIN city ON city_id LEFT OUTER JOIN state ON city.state_id
-- (Talend default join model: left outer, unique match); UPCASE(customer_name / address / gender) -> UPPER;
-- email unchanged; age String -> Integer. CAST (not TRY_CAST) so a non-numeric age fails the load, as the
-- tMap (DIE_ON_ERROR=true) did.
CREATE OR REPLACE VIEW OPS.V_DIMCUSTOMER_SRC
COMMENT = 'Load_DimCustomer tMap: customer LEFT JOIN city LEFT JOIN state, UPPER name/address/gender, INT age'
AS
WITH CUSTOMER_LATEST AS (
    SELECT C.*
    FROM RAW.CUSTOMER AS C
    WHERE C.CUSTOMER_ID IS NOT NULL
    QUALIFY
        ROW_NUMBER() OVER (
            PARTITION BY C.CUSTOMER_ID
            ORDER BY C._LOADED_AT DESC, C._SOURCE_FILE DESC, C._SOURCE_ROW DESC
        ) = 1
),

CITY_LATEST AS (
    SELECT CI.*
    FROM RAW.CITY AS CI
    WHERE CI.CITY_ID IS NOT NULL
    QUALIFY
        ROW_NUMBER() OVER (
            PARTITION BY CI.CITY_ID
            ORDER BY CI._LOADED_AT DESC, CI._SOURCE_FILE DESC, CI._SOURCE_ROW DESC
        ) = 1
),

STATE_LATEST AS (
    SELECT S.*
    FROM RAW.STATE AS S
    WHERE S.STATE_ID IS NOT NULL
    QUALIFY
        ROW_NUMBER() OVER (
            PARTITION BY S.STATE_ID
            ORDER BY S._LOADED_AT DESC, S._SOURCE_FILE DESC, S._SOURCE_ROW DESC
        ) = 1
)

SELECT
    C.CUSTOMER_ID AS CUSTOMERID,
    UPPER(C.CUSTOMER_NAME) AS CUSTOMERNAME,
    UPPER(C.ADDRESS) AS ADDRESS,
    CI.CITY_NAME AS CITYNAME,
    S.STATE_NAME AS STATENAME,
    CAST(C.AGE AS INT) AS AGE,
    UPPER(C.GENDER) AS GENDER,
    C.EMAIL -- noqa: RF04
FROM CUSTOMER_LATEST AS C
LEFT OUTER JOIN CITY_LATEST AS CI
    ON C.CITY_ID = CI.CITY_ID
LEFT OUTER JOIN STATE_LATEST AS S
    ON CI.STATE_ID = S.STATE_ID;

-- Load_FactTransaction (full reload): tUnite(DB, Excel, CSV) -> tUniqRow(transaction_id) -> tMap -> FK check.
-- One row per RAW row, with the reason it is rejected (NULL = goes to DWH.FACTTRANSACTION):
--   DUPLICATE_LOWER_PRECEDENCE  another row with the same TRANSACTION_ID wins on precedence
--                               DB (1) > EXCEL (2) > CSV (3), then earliest load (PLAN.md §1.6; Talend kept the
--                               first row of the tUnite order). Needs business sign-off.
--   ORPHAN_ACCOUNT / ORPHAN_BRANCH  the winning row references a missing DWH.DIMACCOUNT / DWH.DIMBRANCH key.
--                               SQL Server's enforced FK silently rejected these (DIE_ON_ERROR=false);
--                               Snowflake does not enforce FKs, so they are filtered here.
-- Dedup happens before the FK check, as in Talend (tUniqRow before tMSSqlOutput).
CREATE OR REPLACE VIEW OPS.V_FACTTRANSACTION_CANDIDATES
COMMENT = 'Load_FactTransaction: union of RAW.TRANSACTION_*, precedence dedup and orphan classification'
AS
WITH UNIONED AS (
    SELECT
        TRANSACTION_ID,
        ACCOUNT_ID,
        TRANSACTION_DATE,
        AMOUNT,
        TRANSACTION_TYPE,
        BRANCH_ID,
        'DB' AS SOURCESYSTEM,
        1 AS SOURCE_RANK,
        _SOURCE_FILE,
        _SOURCE_ROW,
        _LOADED_AT
    FROM RAW.TRANSACTION_DB
    UNION ALL
    SELECT
        TRANSACTION_ID,
        ACCOUNT_ID,
        TRANSACTION_DATE,
        AMOUNT,
        TRANSACTION_TYPE,
        BRANCH_ID,
        'EXCEL' AS SOURCESYSTEM,
        2 AS SOURCE_RANK,
        _SOURCE_FILE,
        _SOURCE_ROW,
        _LOADED_AT
    FROM RAW.TRANSACTION_EXCEL
    UNION ALL
    SELECT
        TRANSACTION_ID,
        ACCOUNT_ID,
        TRANSACTION_DATE,
        AMOUNT,
        TRANSACTION_TYPE,
        BRANCH_ID,
        'CSV' AS SOURCESYSTEM,
        3 AS SOURCE_RANK,
        _SOURCE_FILE,
        _SOURCE_ROW,
        _LOADED_AT
    FROM RAW.TRANSACTION_CSV
),

RANKED AS (
    SELECT
        U.*,
        ROW_NUMBER() OVER (
            PARTITION BY U.TRANSACTION_ID
            ORDER BY U.SOURCE_RANK ASC, U._LOADED_AT ASC, U._SOURCE_FILE ASC, U._SOURCE_ROW ASC
        ) AS PRECEDENCE_RN
    FROM UNIONED AS U
    WHERE U.TRANSACTION_ID IS NOT NULL
)

SELECT
    R.TRANSACTION_ID,
    R.ACCOUNT_ID,
    CAST(R.TRANSACTION_DATE AS TIMESTAMP_NTZ(3)) AS TRANSACTION_DATE,
    CAST(R.AMOUNT AS NUMBER(19, 4)) AS AMOUNT,
    R.TRANSACTION_TYPE,
    R.BRANCH_ID,
    R.SOURCESYSTEM,
    R.SOURCE_RANK,
    R._SOURCE_FILE,
    R._SOURCE_ROW,
    R._LOADED_AT,
    R.PRECEDENCE_RN,
    CASE
        WHEN R.PRECEDENCE_RN > 1 THEN 'DUPLICATE_LOWER_PRECEDENCE'
        WHEN A.ACCOUNTID IS NULL THEN 'ORPHAN_ACCOUNT'
        WHEN B.BRANCHID IS NULL THEN 'ORPHAN_BRANCH'
    END AS REJECTREASON
FROM RANKED AS R
LEFT OUTER JOIN DWH.DIMACCOUNT AS A
    ON R.ACCOUNT_ID = A.ACCOUNTID
LEFT OUTER JOIN DWH.DIMBRANCH AS B
    ON R.BRANCH_ID = B.BRANCHID;

-- Incremental fact path. OPS.FACTTRANSACTION_INCREMENT holds the RAW rows that arrived since the last run (read
-- from the streams). Every TRANSACTION_ID in the batch is re-decided over all of its RAW rows with the same view
-- as the full reload, so after any sequence of batches DWH.FACTTRANSACTION equals a full reload (proved offline
-- in migration/loads/tests/test_fact_incremental.py), including a late higher-precedence row replacing a fact
-- row and a higher-precedence orphan removing one.
--   FACT_ACTION  INSERT  new valid winner
--                UPDATE  the winner differs from the current fact row
--                DELETE  no valid winner any more (the winner is an orphan), so the transaction leaves the fact
--                NONE    nothing to apply
CREATE OR REPLACE VIEW OPS.V_FACTTRANSACTION_INCREMENT_ACTIONS
COMMENT = 'Incremental Load_FactTransaction: one row per batch TRANSACTION_ID with the action to apply'
AS
WITH BATCH_IDS AS (
    SELECT DISTINCT I.TRANSACTION_ID
    FROM OPS.FACTTRANSACTION_INCREMENT AS I
    WHERE I.TRANSACTION_ID IS NOT NULL
),

WINNERS AS (
    SELECT C.*
    FROM OPS.V_FACTTRANSACTION_CANDIDATES AS C
    INNER JOIN BATCH_IDS AS BI
        ON C.TRANSACTION_ID = BI.TRANSACTION_ID
    WHERE C.REJECTREASON IS NULL
)

SELECT
    BI.TRANSACTION_ID,
    W.ACCOUNT_ID,
    W.TRANSACTION_DATE,
    W.AMOUNT,
    W.TRANSACTION_TYPE,
    W.BRANCH_ID,
    W.SOURCESYSTEM,
    CASE
        WHEN W.TRANSACTION_ID IS NULL AND F.TRANSACTIONID IS NOT NULL THEN 'DELETE'
        WHEN W.TRANSACTION_ID IS NULL THEN 'NONE'
        WHEN F.TRANSACTIONID IS NULL THEN 'INSERT'
        WHEN
            EQUAL_NULL(F.SOURCESYSTEM, W.SOURCESYSTEM)
            AND EQUAL_NULL(F.ACCOUNTID, W.ACCOUNT_ID)
            AND EQUAL_NULL(F.TRANSACTIONDATE, W.TRANSACTION_DATE)
            AND EQUAL_NULL(F.AMOUNT, W.AMOUNT)
            AND EQUAL_NULL(F.TRANSACTIONTYPE, W.TRANSACTION_TYPE)
            AND EQUAL_NULL(F.BRANCHID, W.BRANCH_ID)
            THEN 'NONE'
        ELSE 'UPDATE'
    END AS FACT_ACTION
FROM BATCH_IDS AS BI
LEFT OUTER JOIN WINNERS AS W
    ON BI.TRANSACTION_ID = W.TRANSACTION_ID
LEFT OUTER JOIN DWH.FACTTRANSACTION AS F
    ON BI.TRANSACTION_ID = F.TRANSACTIONID;

-- Incremental rejects: the batch's own RAW rows that lose (duplicate) or are orphans, with the same reasons as
-- the full reload. Fact rows replaced or removed by FACT_ACTION UPDATE / DELETE are logged by the procedure.
CREATE OR REPLACE VIEW OPS.V_FACTTRANSACTION_INCREMENT_REJECTS
COMMENT = 'Incremental Load_FactTransaction: rejected rows of the current batch'
AS
SELECT C.*
FROM OPS.V_FACTTRANSACTION_CANDIDATES AS C
INNER JOIN OPS.FACTTRANSACTION_INCREMENT AS I
    ON
        C.TRANSACTION_ID = I.TRANSACTION_ID
        AND C.SOURCESYSTEM = I.SOURCESYSTEM
        AND EQUAL_NULL(C._SOURCE_FILE, I._SOURCE_FILE)
        AND EQUAL_NULL(C._SOURCE_ROW, I._SOURCE_ROW)
        AND EQUAL_NULL(C._LOADED_AT, I._LOADED_AT)
WHERE C.REJECTREASON IS NOT NULL;
