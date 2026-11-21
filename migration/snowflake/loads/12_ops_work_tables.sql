-- Unit 3 (loads): work table for the incremental fact load. Holds one batch of stream rows (the 3 streams are
-- consumed into it in one statement), so the classification view and the MERGE read a stable set.
-- TRANSIENT: scratch data, no Fail-safe needed.

USE DATABASE BANKING_DWH;

CREATE TRANSIENT TABLE IF NOT EXISTS OPS.FACTTRANSACTION_INCREMENT (
    TRANSACTION_ID INT,
    ACCOUNT_ID INT,
    TRANSACTION_DATE TIMESTAMP_NTZ(0),
    AMOUNT INT,
    TRANSACTION_TYPE VARCHAR(50),
    BRANCH_ID INT,
    SOURCESYSTEM VARCHAR(10),
    SOURCE_RANK INT,
    _SOURCE_FILE VARCHAR,
    _SOURCE_ROW NUMBER,
    _LOADED_AT TIMESTAMP_NTZ
)
COMMENT = 'Current incremental batch read from RAW.STRM_TRANSACTION_* (overwritten every run)';
