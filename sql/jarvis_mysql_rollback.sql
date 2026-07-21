-- ============================================================================
-- 贾维斯 → RuoYi MySQL 镜像库回滚脚本（与 jarvis_mysql_init.sql 配套）
-- 版本: v1.0 (2026-07-19)
-- ============================================================================
-- ⚠⚠ 安全提示 ⚠⚠
--   1. DROP TABLE 会连同表内数据一起删除。jarvis_* 均为镜像表——源数据全部
--      留在贾维斯侧（PostgreSQL/SQLite），重跑同步器即可全量重建，但仍建议
--      回滚前确认：镜像表要么为空、要么数据确认可丢弃。
--   2. 本脚本只删除 init 脚本创建的对象（jarvis_* 表 / jarvis_sync 账号 /
--      jarvis_* 字典行），不触碰任何 RuoYi 既有表与数据。
--   3. 执行：mysql -uroot -p < jarvis_mysql_rollback.sql
-- ============================================================================

USE `jiaweisi`;

-- ---------------------------------------------------------------------------
-- C'. 删字典行（与 init C 段对应；先删 data 再删 type）
-- ---------------------------------------------------------------------------
DELETE FROM sys_dict_data WHERE dict_type IN ('jarvis_direction', 'jarvis_plan_status', 'jarvis_yes_no');
DELETE FROM sys_dict_type WHERE dict_type IN ('jarvis_direction', 'jarvis_plan_status', 'jarvis_yes_no');

-- ---------------------------------------------------------------------------
-- B'. 删同步账号（与 init B 段对应）
-- ---------------------------------------------------------------------------
DROP USER IF EXISTS 'jarvis_sync'@'%';
FLUSH PRIVILEGES;

-- ---------------------------------------------------------------------------
-- A'. 删 13 张镜像表（与 init A 段对应，倒序）
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS jarvis_sync_state;
DROP TABLE IF EXISTS jarvis_tape_flow_snap;
DROP TABLE IF EXISTS jarvis_limit_order;
DROP TABLE IF EXISTS jarvis_position;
DROP TABLE IF EXISTS jarvis_outcome;
DROP TABLE IF EXISTS jarvis_snapshot;
DROP TABLE IF EXISTS jarvis_market_snapshot;
DROP TABLE IF EXISTS jarvis_force_order_min;
DROP TABLE IF EXISTS jarvis_tape_bar;
DROP TABLE IF EXISTS jarvis_intraday_prediction;
DROP TABLE IF EXISTS jarvis_reco_plan;
DROP TABLE IF EXISTS jarvis_signal_change;
DROP TABLE IF EXISTS jarvis_signal_state;

-- ============================================================================
-- 完成自检（手工执行）：
--   SELECT COUNT(*) FROM information_schema.tables
--    WHERE table_schema='jiaweisi' AND table_name LIKE 'jarvis\_%';   -- 应为 0
--   SELECT user, host FROM mysql.user WHERE user='jarvis_sync';       -- 应为空
-- ============================================================================
