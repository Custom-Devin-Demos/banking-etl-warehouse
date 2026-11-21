-- Unit 1 / step 11: DWH.DimAccount (source: sql_scripts/01_create_tables.sql).
-- MONEY -> NUMBER(19,4): same precision and scale, exact decimal arithmetic.
-- CustomerID has no FK to DimCustomer, matching the source schema (none declared there).

USE DATABASE BANKING_DWH;

CREATE TABLE IF NOT EXISTS DWH.DIMACCOUNT (
    ACCOUNTID INT NOT NULL,
    CUSTOMERID INT,
    ACCOUNTTYPE VARCHAR(50),
    BALANCE NUMBER(19, 4),
    DATEOPENED DATE,
    STATUS VARCHAR(50),
    CONSTRAINT PK_DIMACCOUNT PRIMARY KEY (ACCOUNTID) NOT ENFORCED RELY
)
COMMENT = 'Account dimension (was DWH.dbo.DimAccount). PK declared, not enforced.';
