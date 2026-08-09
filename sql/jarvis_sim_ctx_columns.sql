-- ============================================================================
-- 贾维斯模拟交易镜像 · D0 环境快照 ctx 列补齐（13诊断在途任务#4）
-- 版本: v1.0 (2026-08-09)  配套: jarvis_sim_mysql_init.sql（四张 jarvis_sim_* 表）
-- ============================================================================
-- 【背景】13诊断 D0 起，源表 twelve_sim_position/trade 落「开仓时刻市场环境
--   快照」11 列（9×ctx_* + context_tags + size_factor）；本脚本把同名列补进
--   MySQL 镜像表 jarvis_sim_position / jarvis_sim_trade。
--   列名/语义单一事实源：jarvis_sync_tasks_sim.py 的 _CTX_MIRROR_COLS
--   （逐字对齐 jarvis_twelve_trader.CTX_COLUMNS_DDL）。
-- 【为什么需要手动执行】同步账号 jarvis_sync 无 ALTER 权限（最小权限口径）；
--   同步器已内置「缺列自动降级旧映射」，本脚本执行前同步不断流（ctx 留空），
--   执行后 ≤10 分钟同步器自动探测到新列并开始回填增量行的 ctx 值。
-- 【执行】用具备 ALTER 权限的账号（如 root）：
--   mysql -uroot -p < jarvis_sim_ctx_columns.sql
-- 【幂等性】全脚本可重复执行：经 information_schema 判存在后才 ADD COLUMN
--   （MySQL 8.0 无 ADD COLUMN IF NOT EXISTS，用临时存储过程模拟）。
-- 【安全边界】只对 jarvis_sim_position / jarvis_sim_trade 加列（DEFAULT NULL，
--   在线 DDL INSTANT 级），不动数据、不动权限（表级 GRANT 已覆盖新列）。
-- 【历史行口径】已同步的存量行 ctx 恒 NULL（源侧 D0 上线前的行本就无快照）；
--   D0 之后的新增行随增量同步带值。jarvis_sim_trade_hist 归档表不加列
--   （归档列集独立，见 jarvis_sync_tasks_sim._TRADE_ARCHIVE_COLS）。
-- ============================================================================

USE `jiaweisi`;

DROP PROCEDURE IF EXISTS jarvis_add_ctx_col;

DELIMITER $$
CREATE PROCEDURE jarvis_add_ctx_col(
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

-- ── jarvis_sim_position（源: twelve_sim_position）──
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_regime',     "VARCHAR(16) DEFAULT NULL COMMENT '开仓时刻市场状态（trending/ranging/breakout）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_regime_dir', "VARCHAR(16) DEFAULT NULL COMMENT '状态方向（bullish/bearish/neutral）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_atr_pct',    "DECIMAL(10,4) DEFAULT NULL COMMENT '该TF ATR14相对收盘价（%）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_vol_bucket', "VARCHAR(8) DEFAULT NULL COMMENT '波动率分档（low/mid/high）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_wyckoff',    "VARCHAR(16) DEFAULT NULL COMMENT '1h威科夫语境 side-phase（如 acc-C）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_funding',    "DECIMAL(12,8) DEFAULT NULL COMMENT '该币最新8h资金费率（正=多头付）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_oi_btc_chg', "DECIMAL(10,4) DEFAULT NULL COMMENT 'BTC OI变化%（全市场杠杆水位代理口径）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_hour_utc',   "INT DEFAULT NULL COMMENT '开仓UTC小时（0-23，时段归因）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'ctx_btc_trend',  "VARCHAR(16) DEFAULT NULL COMMENT 'BTC 1h regime方向（带动过滤诊断）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'context_tags',   "VARCHAR(255) DEFAULT NULL COMMENT 'D2+上下文标签（逗号串）'");
CALL jarvis_add_ctx_col('jarvis_sim_position', 'size_factor',    "DECIMAL(10,4) DEFAULT NULL COMMENT 'D2+降权系数乘积（1.0=无降权）'");

-- ── jarvis_sim_trade（源: twelve_sim_trade）──
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_regime',     "VARCHAR(16) DEFAULT NULL COMMENT '开仓时刻市场状态（trending/ranging/breakout）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_regime_dir', "VARCHAR(16) DEFAULT NULL COMMENT '状态方向（bullish/bearish/neutral）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_atr_pct',    "DECIMAL(10,4) DEFAULT NULL COMMENT '该TF ATR14相对收盘价（%）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_vol_bucket', "VARCHAR(8) DEFAULT NULL COMMENT '波动率分档（low/mid/high）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_wyckoff',    "VARCHAR(16) DEFAULT NULL COMMENT '1h威科夫语境 side-phase（如 acc-C）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_funding',    "DECIMAL(12,8) DEFAULT NULL COMMENT '该币最新8h资金费率（正=多头付）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_oi_btc_chg', "DECIMAL(10,4) DEFAULT NULL COMMENT 'BTC OI变化%（全市场杠杆水位代理口径）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_hour_utc',   "INT DEFAULT NULL COMMENT '开仓UTC小时（0-23，时段归因）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'ctx_btc_trend',  "VARCHAR(16) DEFAULT NULL COMMENT 'BTC 1h regime方向（带动过滤诊断）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'context_tags',   "VARCHAR(255) DEFAULT NULL COMMENT 'D2+上下文标签（逗号串）'");
CALL jarvis_add_ctx_col('jarvis_sim_trade', 'size_factor',    "DECIMAL(10,4) DEFAULT NULL COMMENT 'D2+降权系数乘积（1.0=无降权）'");

DROP PROCEDURE IF EXISTS jarvis_add_ctx_col;

-- ============================================================================
-- 完成自检（手工执行，两表各应返回 11 行）：
--   SHOW COLUMNS FROM jarvis_sim_position WHERE Field LIKE 'ctx\_%'
--     OR Field IN ('context_tags','size_factor');
--   SHOW COLUMNS FROM jarvis_sim_trade    WHERE Field LIKE 'ctx\_%'
--     OR Field IN ('context_tags','size_factor');
-- ============================================================================
