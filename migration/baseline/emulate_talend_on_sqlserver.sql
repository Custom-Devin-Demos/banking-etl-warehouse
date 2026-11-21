USE DWH;
SET NOCOUNT ON;
INSERT DimBranch SELECT branch_id,branch_name,branch_location FROM sample.dbo.branch;
INSERT DimAccount SELECT account_id,customer_id,account_type,balance,CAST(date_opened AS DATE),status FROM sample.dbo.account;
INSERT DimCustomer SELECT c.customer_id,UPPER(c.customer_name),UPPER(c.address),ci.city_name,s.state_name,c.age,UPPER(c.gender),c.email
  FROM sample.dbo.customer c LEFT JOIN sample.dbo.city ci ON ci.city_id=c.city_id LEFT JOIN sample.dbo.state s ON s.state_id=ci.state_id;
CREATE TABLE #u (src varchar(10), rnk int, transaction_id int, account_id int, transaction_date datetime, amount int, transaction_type varchar(50), branch_id int);
INSERT #u SELECT 'DB',1,transaction_id,account_id,transaction_date,amount,transaction_type,branch_id FROM sample.dbo.transaction_db;
INSERT #u VALUES ('EXCEL',2,6,6,'2024-01-18 13:10:00',50000,'Withdrawal',1),
('EXCEL',2,7,6,'2024-01-19 14:00:00',100000,'Payment',1),
('EXCEL',2,11,10,'2024-01-20 15:00:00',1000000,'Transfer',1),
('EXCEL',2,12,11,'2024-01-20 10:00:00',500000,'Deposit',1),
('EXCEL',2,13,12,'2024-01-20 12:10:00',500000,'Withdrawal',5),
('EXCEL',2,14,13,'2024-01-21 14:00:00',1500000,'Deposit',4),
('EXCEL',2,15,14,'2024-01-21 08:00:00',500000,'Transfer',3),
('CSV',3,14,13,'2024-01-21 14:00:00',1500000,'Deposit',4),
('CSV',3,15,14,'2024-01-21 08:00:00',500000,'Transfer',3),
('CSV',3,16,15,'2024-01-22 09:00:00',100000,'Deposit',1),
('CSV',3,17,16,'2024-01-22 13:10:00',100000,'Withdrawal',5),
('CSV',3,18,17,'2024-01-22 10:20:00',700000,'Deposit',5),
('CSV',3,19,18,'2024-01-22 11:00:00',30000,'Payment',2),
('CSV',3,20,19,'2024-01-22 15:00:00',2500000,'Deposit',2),
('CSV',3,21,20,'2024-01-22 11:30:00',150000,'Payment',4),
('CSV',3,22,21,'2024-01-22 10:45:00',800000,'Withdrawal',5),
('CSV',3,23,22,'2024-01-22 10:50:00',100000,'Withdrawal',1),
('CSV',3,24,23,'2024-01-22 11:10:00',300000,'Payment',1),
('CSV',3,25,23,'2024-01-22 14:30:00',400000,'Deposit',1);
WITH d AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY transaction_id ORDER BY rnk) rn FROM #u)
INSERT FactTransaction SELECT transaction_id,account_id,transaction_date,amount,transaction_type,branch_id FROM d
 WHERE rn=1 AND EXISTS (SELECT 1 FROM DimAccount a WHERE a.AccountID=d.account_id) AND EXISTS (SELECT 1 FROM DimBranch b WHERE b.BranchID=d.branch_id);
SELECT 'dups', u.src, u.transaction_id FROM #u u JOIN (SELECT transaction_id FROM #u GROUP BY transaction_id HAVING COUNT(*)>1) x ON x.transaction_id=u.transaction_id ORDER BY 3,1;
SELECT 'fk_reject', * FROM #u u WHERE NOT EXISTS (SELECT 1 FROM DimAccount a WHERE a.AccountID=u.account_id);
SELECT 'counts', (SELECT COUNT(*) FROM DimBranch),(SELECT COUNT(*) FROM DimAccount),(SELECT COUNT(*) FROM DimCustomer),(SELECT COUNT(*) FROM FactTransaction);
