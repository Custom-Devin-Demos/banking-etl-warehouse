-- Unit 1 / step 12: DWH.DimCustomer (source: sql_scripts/01_create_tables.sql).
-- Denormalised customer + city + state, built by OPS.SP_LOAD_DIMCUSTOMER.
-- EMAIL is a sqlfluff keyword (RF04) but not a Snowflake reserved word; the contract name is kept.

USE DATABASE BANKING_DWH;

CREATE TABLE IF NOT EXISTS DWH.DIMCUSTOMER (
    CUSTOMERID INT NOT NULL,
    CUSTOMERNAME VARCHAR(100),
    ADDRESS VARCHAR(255),
    CITYNAME VARCHAR(100),
    STATENAME VARCHAR(100),
    AGE INT,
    GENDER VARCHAR(10),
    EMAIL VARCHAR(100), -- noqa: RF04
    CONSTRAINT PK_DIMCUSTOMER PRIMARY KEY (CUSTOMERID) NOT ENFORCED RELY
)
COMMENT = 'Customer dimension (was DWH.dbo.DimCustomer). PK declared, not enforced.';
