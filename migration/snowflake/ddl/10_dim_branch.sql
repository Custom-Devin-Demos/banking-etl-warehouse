-- Unit 1 / step 10: DWH.DimBranch (source: sql_scripts/01_create_tables.sql).
-- Dimension tables are tiny (5-21 rows): no CLUSTER BY. Synapse would REPLICATE them; Snowflake has no
-- distribution, and micro-partition pruning plus broadcast joins make replication unnecessary.

USE DATABASE BANKING_DWH;

CREATE TABLE IF NOT EXISTS DWH.DIMBRANCH (
    BRANCHID INT NOT NULL,
    BRANCHNAME VARCHAR(100),
    BRANCHLOCATION VARCHAR(255),
    CONSTRAINT PK_DIMBRANCH PRIMARY KEY (BRANCHID) NOT ENFORCED RELY
)
COMMENT = 'Branch dimension (was DWH.dbo.DimBranch). PK declared, not enforced.';
