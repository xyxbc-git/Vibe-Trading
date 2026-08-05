-- ============================================================================
-- 贾维斯 → RuoYi 镜像库 币种池（watchlist）收敛清理脚本
-- 版本: v1.0 (2026-08-05)  配套: jarvis_mysql_init.sql / jarvis_sim_mysql_init.sql
-- ============================================================================
-- 【背景】
--   2026-08-05 任务K 币种池收敛为 8 品种（见 jarvis_config.DEFAULTS.watchlist），
--   本地源库刻意保留退役币（SOL/BNB/XRP/DOGE/ADA 等）历史数据，但 MySQL 镜像
--   同为 upsert-only，旧币种行残留（jarvis-monitor-redesign.md §2.1 R2 同类问题），
--   导致若依「盘口分钟聚合」下拉（distinct symbol）显示非 watchlist 币种。
-- 【本脚本做三件事】
--   A. 只读预检：看每张表有多少非 watchlist 残留行（先跑，评估影响面）
--   B. 授权补齐：jarvis_sync 账号补 DELETE（新版同步器 delete-absent 清扫必需）
--   C. 一次性清理：删除三张在册镜像表中的非 watchlist 行（事务包裹，核对后提交）
-- 【执行顺序】
--   ① 跑 A 预检 → ② root 执行 B（必须，否则同步器清扫每轮报 1142）
--   → ③ 二选一：执行 C 手工清理；或部署新版 jarvis_sync_tasks_{a,b}.py 并重启
--     同步器，各表首轮清扫自动完成（等价于 C，且此后 watchlist 再变更也自动收敛）
--   → ④ 跑 E 验证
-- 【风险级别】
--   ⚠⚠ C 段为高风险 DB 写操作（DELETE 历史镜像行，本脚本执行后不可逆）。
--   ⚠⚠ 全脚本【待用户确认后执行】，禁止自动化/无人值守执行。
--   源侧（本地 SQLite/PG）数据不受任何影响；镜像可由同步器随时重灌当前币种数据，
--   但被删的退役币镜像历史无法从镜像自身恢复（源侧仍在，如需可另行导出备份：
--   mysqldump jiaweisi jarvis_tape_bar jarvis_signal_change jarvis_market_snapshot）。
-- 【watchlist 口径（2026-08-05）】
--   BTCUSDT / ETHUSDT / SNDKUSDT / SKHYUSDT / SPCXUSDT / XAUUSDT / CLUSDT / BZUSDT
--   ⚠ 若执行时 watchlist 已再次变更，需同步修改本脚本所有 IN 列表
--     （同步器侧无此问题：重启即跟随 jarvis_config 最新值）。
-- ============================================================================

USE `jiaweisi`;

-- ============================================================================
-- A. 只读预检（安全，先跑）：各在册镜像表的非 watchlist 残留分布
-- ============================================================================

SELECT 'jarvis_tape_bar' AS tbl, symbol, COUNT(*) AS rows_to_delete
  FROM jarvis_tape_bar
 WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT')
 GROUP BY symbol
UNION ALL
SELECT 'jarvis_signal_change', symbol, COUNT(*)
  FROM jarvis_signal_change
 WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT')
 GROUP BY symbol
UNION ALL
SELECT 'jarvis_market_snapshot', symbol, COUNT(*)
  FROM jarvis_market_snapshot
 WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT')
 GROUP BY symbol
 ORDER BY tbl, rows_to_delete DESC;

-- ============================================================================
-- B. 授权补齐（root 执行；幂等，可重复跑）
--    B1 三张在册镜像表补 DELETE——新版同步器 symbol 维度 delete-absent 必需。
--       jarvis_mysql_init.sql v1.0 的「jarvis_sync 无 DELETE」最小权限口径，自
--       delete-absent 纪律引入起修订为「仅清扫涉及的表有 DELETE」，其余表维持原状。
-- ============================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_tape_bar`        TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_signal_change`   TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_market_snapshot` TO 'jarvis_sync'@'%';

-- ── B2 sim 通道授权补记（可选，幂等无害）─────────────────────────────────────
--    jarvis_sync_tasks_sim.py 自 2026-07-31 起已依赖 DELETE（_delete_absent /
--    _detect_source_reset）与 trade_hist 归档 INSERT；若当时已在线上手工授权，
--    重复 GRANT 无害；若未授权，同步日志会持续报 1142，执行以下语句修复。
--    注：jarvis_sim_signal_log / jarvis_sim_trade_hist 的 DDL 未入仓（当时手工
--    建表），个别表在当前环境不存在时跳过对应行即可。

GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_sim_wallet`     TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_sim_position`   TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_sim_trade`      TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_sim_signal_log` TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT                 ON `jiaweisi`.`jarvis_sim_trade_hist` TO 'jarvis_sync'@'%';
FLUSH PRIVILEGES;

-- ============================================================================
-- C. 一次性清理（⚠ 高风险 DELETE·待用户确认后执行；与同步器首轮清扫二选一）
--    事务包裹：逐条核对 affected rows 与 A 段预检合计一致后再 COMMIT，
--    不一致直接 ROLLBACK 排查。
-- ============================================================================

START TRANSACTION;

DELETE FROM jarvis_tape_bar
 WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');

DELETE FROM jarvis_signal_change
 WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');

DELETE FROM jarvis_market_snapshot
 WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');

-- 核对三条 affected rows 与 A 段预检一致后手动执行：
-- COMMIT;
-- 不一致则：
-- ROLLBACK;

-- ============================================================================
-- D. 可选：已下线冻结表的残留清理（默认不执行——这些表的同步任务已于
--    2026-08-05 停止注册、若依页面/App 端已删除，无任何消费者，残留不影响
--    任何在线功能；仅在明确要求彻底收敛全库时按需放开）
--    注：jarvis_outcome 无 symbol 列（按 snapshot_id 关联），不在此列。
-- ============================================================================

-- DELETE FROM jarvis_signal_state        WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');
-- DELETE FROM jarvis_intraday_prediction WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');
-- DELETE FROM jarvis_snapshot            WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');
-- DELETE FROM jarvis_position            WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');
-- DELETE FROM jarvis_limit_order         WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');
-- DELETE FROM jarvis_force_order_min     WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');
-- DELETE FROM jarvis_reco_plan           WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');
-- DELETE FROM jarvis_tape_flow_snap      WHERE symbol NOT IN ('BTCUSDT','ETHUSDT','SNDKUSDT','SKHYUSDT','SPCXUSDT','XAUUSDT','CLUSDT','BZUSDT');

-- ============================================================================
-- E. 清理后验证：三张在册表 distinct symbol 应全部 ⊆ watchlist 8 币
--    （若依「盘口分钟聚合」下拉刷新后应只剩 watchlist 币种）
-- ============================================================================

SELECT 'jarvis_tape_bar' AS tbl, GROUP_CONCAT(DISTINCT symbol ORDER BY symbol) AS symbols
  FROM jarvis_tape_bar
UNION ALL
SELECT 'jarvis_signal_change', GROUP_CONCAT(DISTINCT symbol ORDER BY symbol)
  FROM jarvis_signal_change
UNION ALL
SELECT 'jarvis_market_snapshot', GROUP_CONCAT(DISTINCT symbol ORDER BY symbol)
  FROM jarvis_market_snapshot;

-- 授权验证：
--   SHOW GRANTS FOR 'jarvis_sync'@'%';
--   应看到 jarvis_tape_bar / jarvis_signal_change / jarvis_market_snapshot 含 DELETE
-- ============================================================================
