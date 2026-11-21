-- Unit 3 (loads): landing stage that replaces the Talend file paths (C:/Data Rakamin/...) and the
-- tMSSqlInput reads of the sample database.
--
-- Layout (one prefix per RAW table, so every COPY / pipe reads exactly one source):
--   extracts/state/ extracts/city/ extracts/customer/ extracts/account/ extracts/branch/ extracts/transaction_db/
--       SQL Server / Synapse extracts (PLAN.md §1.1). Production: Synapse CETAS export to ADLS.
--   files/transaction_csv/      transaction_csv.csv as delivered (ISO-8859-15, dd-MM-yyyy dates)
--   files/transaction_excel/    CSV produced from transaction_excel.xlsx by migration/loads/xlsx_to_csv.py
--   files/transaction_xlsx/     raw .xlsx, only used by the optional Snowpark procedure (70_*.sql)
--
-- Upload (SnowSQL / Snowflake CLI, client side, not part of this deploy script):
--   PUT file://data_sources/sample_db_extract/account.csv @OPS.LANDING_STAGE/extracts/account/ AUTO_COMPRESS=TRUE;
--   PUT file://data_sources/transaction_csv.csv @OPS.LANDING_STAGE/files/transaction_csv/ AUTO_COMPRESS=TRUE;
--   PUT file://build/transaction_excel.csv @OPS.LANDING_STAGE/files/transaction_excel/ AUTO_COMPRESS=TRUE;

USE DATABASE BANKING_DWH;

-- IF NOT EXISTS: CREATE OR REPLACE STAGE would drop every staged file on each deploy.
CREATE STAGE IF NOT EXISTS OPS.LANDING_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    DIRECTORY = (ENABLE = TRUE)
    COMMENT = 'Landing area for source extracts and files (replaces Talend file paths and tMSSqlInput)';

-- Production variant on ADLS Gen2 (environment-specific; needs a storage integration created by an admin):
-- CREATE STORAGE INTEGRATION BANKING_ADLS_INT
--     TYPE = EXTERNAL_STAGE
--     STORAGE_PROVIDER = 'AZURE'
--     ENABLED = TRUE
--     AZURE_TENANT_ID = '<tenant-id>'
--     STORAGE_ALLOWED_LOCATIONS = ('azure://<account>.blob.core.windows.net/<container>/banking/');
-- CREATE STAGE IF NOT EXISTS OPS.LANDING_STAGE_ADLS
--     URL = 'azure://<account>.blob.core.windows.net/<container>/banking/'
--     STORAGE_INTEGRATION = BANKING_ADLS_INT;
-- Every COPY / pipe in this folder then reads @OPS.LANDING_STAGE_ADLS/<same prefix> instead.
