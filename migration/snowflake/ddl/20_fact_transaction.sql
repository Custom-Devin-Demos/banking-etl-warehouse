-- Unit 1 / step 20: DWH.FactTransaction (source: sql_scripts/01_create_tables.sql).
-- Must run after 10_dim_branch.sql and 11_dim_account.sql: the FKs reference their primary keys.
-- DATETIME -> TIMESTAMP_NTZ(3): no time zone in the source; DATETIME resolution is about 3 ms.
-- PK and FKs are declared NOT ENFORCED RELY. Snowflake only enforces NOT NULL, so uniqueness and
-- referential integrity are enforced by OPS.SP_LOAD_FACTTRANSACTION (dedup + orphan reject) and checked by
-- the validation harness. RELY lets the optimizer use them (for example, join elimination), which is only
-- correct because the load guarantees them.
-- CLUSTER BY replaces Synapse DISTRIBUTION / CLUSTERED COLUMNSTORE / PARTITION. The reporting procedure
-- sp_DailyTransaction filters on CAST(TransactionDate AS DATE), so clustering on the day prunes
-- micro-partitions for date-range scans. Clustering on the raw timestamp would have too many distinct values.

USE DATABASE BANKING_DWH;

CREATE TABLE IF NOT EXISTS DWH.FACTTRANSACTION (
    TRANSACTIONID INT NOT NULL,
    ACCOUNTID INT,
    TRANSACTIONDATE TIMESTAMP_NTZ(3),
    AMOUNT NUMBER(19, 4),
    TRANSACTIONTYPE VARCHAR(50),
    BRANCHID INT,
    SOURCESYSTEM VARCHAR(10) COMMENT 'Lineage: DB, EXCEL or CSV. Excluded from parity checks.',
    LOADEDAT TIMESTAMP_NTZ(3) DEFAULT SYSDATE() COMMENT 'Lineage: load time (UTC). Excluded from parity checks.',
    CONSTRAINT PK_FACTTRANSACTION PRIMARY KEY (TRANSACTIONID) NOT ENFORCED RELY,
    CONSTRAINT FK_FACTTRANSACTION_DIMACCOUNT FOREIGN KEY (ACCOUNTID)
    REFERENCES DWH.DIMACCOUNT (ACCOUNTID) NOT ENFORCED RELY,
    CONSTRAINT FK_FACTTRANSACTION_DIMBRANCH FOREIGN KEY (BRANCHID)
    REFERENCES DWH.DIMBRANCH (BRANCHID) NOT ENFORCED RELY
)
CLUSTER BY (TO_DATE(TRANSACTIONDATE))
COMMENT = 'Transaction fact (was DWH.dbo.FactTransaction). PK/FK declared, not enforced.';

-- CREATE TABLE IF NOT EXISTS does not change an existing table. This ALTER makes every deploy converge on
-- the intended clustering key, even for a table created by an earlier version of this script.
ALTER TABLE DWH.FACTTRANSACTION CLUSTER BY (TO_DATE(TRANSACTIONDATE));
