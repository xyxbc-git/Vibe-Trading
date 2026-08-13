-- ============================================================================
-- 贾维斯模拟交易镜像 · jarvis_sim_signal_log.note 列加宽（任务 X·1406 修复）
-- 版本: v1.0 (2026-08-13)  配套: jarvis_sim_toll_ratio_column.sql（同风格）
-- ============================================================================
-- 【背景】T2 SL 地板改写上线后，源表 twelve_sim_signal_log.note（pg TEXT，
--   无界）落入的改写留痕文案变长（实测最长 259 字符），镜像列 note 为
--   VARCHAR(255)，259 > 255 → 同步器持续报 MySQL 1406
--   "Data too long for column 'note'"（2026-08-13 12:01 起 220+ 次），
--   该表 id 游标停在失败批次前、镜像积压。本脚本把镜像列对齐源侧语义改 TEXT。
-- 【为什么需要手动执行】MySQL DDL 统一经用户确认后执行；jarvis_sync 账号
--   仅有 DML 权限（SELECT/INSERT/UPDATE/DELETE），ALTER 需特权账号。
--   本次经用户授权先例（镜像表列 DDL，改宽不改窄零数据风险）执行。
-- 【执行】用具备 ALTER 权限的账号（如 root）：
--   mysql -uroot -p < jarvis_sim_signal_log_note_widen.sql
-- 【幂等性】可重复执行：经 information_schema 判 note 当前 DATA_TYPE 非
--   text 时才 MODIFY（MySQL 无 MODIFY IF，用临时存储过程模拟）。
-- 【安全边界】只动 jarvis_sim_signal_log.note 一列；VARCHAR(255)→TEXT 纯
--   加宽（值域超集），不动数据/索引/权限（该列无索引；表级 GRANT 覆盖）；
--   3k 行量级 COPY 重建亚秒完成。执行后同步器无需重启，下一轮增量自动补进
--   积压行。
-- ============================================================================

USE `jiaweisi`;

DROP PROCEDURE IF EXISTS jarvis_widen_note_col;

DELIMITER $$
CREATE PROCEDURE jarvis_widen_note_col()
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'jarvis_sim_signal_log'
          AND column_name = 'note'
          AND data_type <> 'text'
    ) THEN
        ALTER TABLE `jarvis_sim_signal_log`
            MODIFY COLUMN `note` TEXT COLLATE utf8mb4_general_ci
            DEFAULT NULL COMMENT '备注（未应用原因等；源列 pg TEXT 无界，2026-08-13 由 VARCHAR(255) 加宽修 1406）';
    END IF;
END$$
DELIMITER ;

CALL jarvis_widen_note_col();

DROP PROCEDURE IF EXISTS jarvis_widen_note_col;

-- ============================================================================
-- 完成自检（手工执行，DATA_TYPE 应返回 text）：
--   SELECT DATA_TYPE FROM information_schema.columns
--   WHERE table_schema='jiaweisi' AND table_name='jarvis_sim_signal_log'
--     AND column_name='note';
-- ============================================================================
