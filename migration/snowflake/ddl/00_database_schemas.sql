-- Unit 1 / step 00: database and schemas.
-- Replaces: IF NOT EXISTS (SELECT * FROM sys.databases WHERE name = 'DWH') CREATE DATABASE DWH; USE DWH; GO
-- Idempotent and data-preserving: IF NOT EXISTS never drops or recreates an existing container.
-- Identifiers are unquoted, so Snowflake stores them upper-cased (DimAccount -> DIMACCOUNT).
-- The other scripts only use schema-qualified names after USE DATABASE, so a renamed database only needs
-- the USE DATABASE line changed.

CREATE DATABASE IF NOT EXISTS BANKING_DWH
COMMENT = 'Banking warehouse migrated from Azure Synapse / SQL Server database DWH';

USE DATABASE BANKING_DWH;

CREATE SCHEMA IF NOT EXISTS RAW
COMMENT = 'Landing tables written by COPY INTO, one per source (owned by the loads unit)';

CREATE SCHEMA IF NOT EXISTS DWH
COMMENT = 'Star schema: DimAccount, DimBranch, DimCustomer, FactTransaction (was DWH.dbo)';

CREATE SCHEMA IF NOT EXISTS OPS
COMMENT = 'Stages, file formats, streams, tasks, load procedures and operational tables';
