-- Unit 1 / step 01: operational tables in OPS (contract: PLAN.md section 3).
-- CREATE TABLE IF NOT EXISTS: re-running never truncates the error log or the reject history.

USE DATABASE BANKING_DWH;

-- Written by every procedure's EXCEPTION handler before it re-raises.
-- LOG_ID shows the IDENTITY(1,1) mapping: AUTOINCREMENT with explicit START/INCREMENT and ORDER, so the
-- values increase monotonically whatever the account's NOORDER_SEQUENCE_AS_DEFAULT setting is. Like Synapse
-- IDENTITY, the values are unique but not guaranteed to be gap-free.
CREATE TABLE IF NOT EXISTS OPS.ETL_ERROR_LOG (
    LOG_ID INT AUTOINCREMENT START 1 INCREMENT 1 ORDER NOT NULL,
    LOGGED_AT TIMESTAMP_NTZ DEFAULT SYSDATE() NOT NULL,
    PROCEDURE_NAME VARCHAR,
    SQLCODE NUMBER,
    SQLSTATE VARCHAR(5),
    SQLERRM VARCHAR,
    CONTEXT VARIANT,
    CONSTRAINT PK_ETL_ERROR_LOG PRIMARY KEY (LOG_ID) NOT ENFORCED RELY
)
COMMENT = 'Error log written by procedure EXCEPTION handlers (replaces T-SQL CATCH logging)';

-- Rows the fact load removes because SQL Server's enforced FK or Talend tUniqRow would have dropped them.
-- Snowflake does not enforce FKs, so the load filters them explicitly and records them here.
-- The 6 source columns keep their source names (RAW.TRANSACTION_* layout). Amount and date use the wider
-- DWH types (NUMBER(19,4), TIMESTAMP_NTZ(3)) so a value read from either RAW or the fact pipeline fits
-- without loss.
CREATE TABLE IF NOT EXISTS OPS.FACTTRANSACTION_REJECTS (
    TRANSACTION_ID INT,
    ACCOUNT_ID INT,
    TRANSACTION_DATE TIMESTAMP_NTZ(3),
    AMOUNT NUMBER(19, 4),
    TRANSACTION_TYPE VARCHAR(50),
    BRANCH_ID INT,
    SOURCESYSTEM VARCHAR(10) COMMENT 'DB, EXCEL or CSV',
    REJECTREASON VARCHAR NOT NULL COMMENT 'ORPHAN_ACCOUNT, ORPHAN_BRANCH or DUPLICATE_LOWER_PRECEDENCE',
    REJECTEDAT TIMESTAMP_NTZ DEFAULT SYSDATE() NOT NULL
)
COMMENT = 'Fact rows rejected by OPS.SP_LOAD_FACTTRANSACTION (orphans and lower-precedence duplicates)';
