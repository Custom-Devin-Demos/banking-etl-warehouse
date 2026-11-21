-- Unit 3 (loads): task graph replacing the Talend run order (README / PLAN.md §1.5):
--   TASK_LOAD_ROOT (COPY INTO RAW) -> TASK_LOAD_DIMBRANCH -> TASK_LOAD_DIMACCOUNT -> TASK_LOAD_DIMCUSTOMER
--   -> TASK_LOAD_FACTTRANSACTION (incremental, from the streams)
-- Serial like the Talend jobs: each child starts only after its predecessor succeeds, and a failure (the
-- procedures RAISE after logging to OPS.ETL_ERROR_LOG) stops the run. Serverless tasks
-- (USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE), so no environment-specific warehouse name is needed; replace with
-- WAREHOUSE = <name> to use a user-managed warehouse.
-- The schedule is an example (02:00 UTC daily). Tasks are created suspended. Enable after cutover with
--   SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('OPS.TASK_LOAD_ROOT');
-- and run once on demand with
--   EXECUTE TASK OPS.TASK_LOAD_ROOT;
-- Children must be suspended before CREATE OR REPLACE of a task in the graph: re-deploy with
--   ALTER TASK OPS.TASK_LOAD_ROOT SUSPEND;

USE DATABASE BANKING_DWH;

CREATE OR REPLACE TASK OPS.TASK_LOAD_ROOT
    USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE = 'XSMALL'
    SCHEDULE = 'USING CRON 0 2 * * * UTC'
    ALLOW_OVERLAPPING_EXECUTION = FALSE
    SUSPEND_TASK_AFTER_NUM_FAILURES = 3
    COMMENT = 'Root: COPY INTO RAW.* from @OPS.LANDING_STAGE (set FALSE when Snowpipe loads the file sources)'
AS
    CALL OPS.SP_COPY_RAW(TRUE);

CREATE OR REPLACE TASK OPS.TASK_LOAD_DIMBRANCH
    USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE = 'XSMALL'
    COMMENT = 'Replaces Talend Load_DimBranch'
    AFTER OPS.TASK_LOAD_ROOT
AS
    CALL OPS.SP_LOAD_DIMBRANCH();

CREATE OR REPLACE TASK OPS.TASK_LOAD_DIMACCOUNT
    USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE = 'XSMALL'
    COMMENT = 'Replaces Talend Load_DimAccount'
    AFTER OPS.TASK_LOAD_DIMBRANCH
AS
    CALL OPS.SP_LOAD_DIMACCOUNT();

CREATE OR REPLACE TASK OPS.TASK_LOAD_DIMCUSTOMER
    USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE = 'XSMALL'
    COMMENT = 'Replaces Talend Load_DimCustomer'
    AFTER OPS.TASK_LOAD_DIMACCOUNT
AS
    CALL OPS.SP_LOAD_DIMCUSTOMER();

CREATE OR REPLACE TASK OPS.TASK_LOAD_FACTTRANSACTION
    USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE = 'XSMALL'
    COMMENT = 'Replaces Talend Load_FactTransaction (incremental from RAW.STRM_TRANSACTION_*)'
    AFTER OPS.TASK_LOAD_DIMCUSTOMER
AS
    CALL OPS.SP_LOAD_FACTTRANSACTION_INCREMENTAL();
