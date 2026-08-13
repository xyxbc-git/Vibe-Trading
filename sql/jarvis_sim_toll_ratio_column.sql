-- ============================================================================
-- 贾维斯模拟交易镜像 · T3 toll_ratio 列补齐（正期望重建·任务 K）
-- 版本: v1.0 (2026-08-13)  配套: jarvis_sim_mysql_init.sql / jarvis_sim_ctx_columns.sql
-- ============================================================================
-- 【背景】正期望重建 T3 起，源表 twelve_sim_trade 落 toll_ratio 列
--   （= 2×单边费率% ÷ 计划SL距离%，过路费占风险预算比——与杠杆/周期/标的
--   全部无关的门禁判据，attribution 归因分档同用此口径）；本脚本把同名列
--   补进 MySQL 镜像表 jarvis_sim_trade。
-- 【为什么需要手动执行】MySQL DDL 统一经用户确认后执行（与 ctx 列的同步器
--   自动 ALTER 不同，本列走人工路线）；同步器已内置「缺列自动降级旧映射」，
--   本脚本执行前同步不断流（toll_ratio 留空），执行后 ≤10 分钟同步器自动
--   探测到新列并开始带值同步增量行，无需重启。
-- 【执行】用具备 ALTER 权限的账号（如 root）：
--   mysql -uroot -p < jarvis_sim_toll_ratio_column.sql
-- 【幂等性】可重复执行：经 information_schema 判存在后才 ADD COLUMN
--   （MySQL 8.0 无 ADD COLUMN IF NOT EXISTS，用临时存储过程模拟）。
-- 【安全边界】只对 jarvis_sim_trade 加一列（DEFAULT NULL，在线 DDL INSTANT
--   级），不动数据、不动权限（表级 GRANT 已覆盖新列）。
-- 【历史行口径】已同步的存量行 toll_ratio 恒 NULL（id 游标增量不重灌旧行；
--   源侧 T3 上线前的行本就无此值）；T3 之后的新增行随增量同步带值。
--   jarvis_sim_trade_hist 归档表不加列（归档列集独立，见
--   jarvis_sync_tasks_sim._TRADE_ARCHIVE_COLS）。
-- ============================================================================

USE `jiaweisi`;

DROP PROCEDURE IF EXISTS jarvis_add_toll_col;

DELIMITER $$
CREATE PROCEDURE jarvis_add_toll_col(
    IN p_table VARCHAR(64), IN p_col VARCHAR(64), IN p_ddl TEXT)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = p_table AND column_name = p_col
    ) THEN
        SET @sql = CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN `',
                          p_col, '` ', p_ddl);
        PREPARE stmt FROM @sql;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

-- ── jarvis_sim_trade（源: twelve_sim_trade）──
CALL jarvis_add_toll_col('jarvis_sim_trade', 'toll_ratio',
    "DECIMAL(12,6) DEFAULT NULL COMMENT '过路费占比=2×单边费率%÷计划SL距离%（T3门禁判据，与杠杆无关；>0.2 结构性劣势，>1.0 下单即注定亏损）'");

DROP PROCEDURE IF EXISTS jarvis_add_toll_col;

-- ============================================================================
-- 完成自检（手工执行，应返回 1 行）：
--   SHOW COLUMNS FROM jarvis_sim_trade WHERE Field = 'toll_ratio';
-- ============================================================================
