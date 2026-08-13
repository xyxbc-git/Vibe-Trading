-- ============================================================================
-- 贾维斯模拟交易器 → RuoYi MySQL 镜像表初始化脚本（Vibe-Trading 子任务2）
-- 版本: v1.1 (2026-08-13)  配套: jarvis_mysql_init.sql（13 张主镜像表）
--   v1.1 任务 H/L：jarvis_sim_config 建表 DDL 合入 5 个周期纪律列
--     （tp_mode/tp_rr_ratio/tp_pct_factor/sl_mode/sl_atr_mult，NULL=跟随信号）；
--     存量库对应增量脚本为 RuoYi 侧 sql/jarvis_sim_discipline_20260813.sql
-- ============================================================================
-- 【前提】
--   1. MySQL >= 8.0；库 `jiaweisi` 已存在
--   2. jarvis_mysql_init.sql 已执行（jarvis_sync 账号已建立）
--   3. 用具备 CREATE / GRANT 权限的账号执行（如 root）
-- 【执行】
--   mysql -uroot -p < jarvis_sim_mysql_init.sql
-- 【幂等性】  全脚本可重复执行：CREATE TABLE IF NOT EXISTS 保护
-- 【安全边界】只新增 jarvis_sim_* 前缀表与对应授权，不触碰任何既有表/数据
-- 【时区口径】所有 DATETIME(3) 列统一存 **东八区（GMT+8）** 挂钟时间，
--            与 jarvis_mysql_init.sql / RuoYi druid serverTimezone=GMT%2B8 对齐
-- 【数据流向】
--   jarvis_sim_wallet / jarvis_sim_position / jarvis_sim_trade：
--     贾维斯本地 twelve_sim_* → 本镜像（单向推送，同步器 upsert）
--   jarvis_sim_config：
--     RuoYi 页面维护 → 同步器**反向回读** → 贾维斯本地 twelve_sim_config
--     （全链路唯一回读表，同步器对其只 SELECT）
-- 【枚举口径】
--   system_code/scope_system：turtle海龟 dow道氏 elliott艾略特 volatility波动率
--     gann江恩 chanlun缠论 rule123法则 gap缺口 martingale马丁 oscillator摆动
--     triple_rsi三重RSI arbitrage套利
--   tf/scope_tf：5m/15m/30m/1h/4h/1d
-- ============================================================================

USE `jiaweisi`;

-- ----------------------------------------------------------------------------
-- 1) 模拟交易参数配置（RuoYi 侧维护，同步器回读到贾维斯本地）
--    作用域：symbol 必填；scope_tf / scope_system 为 NULL 表示对该维度全量生效
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_sim_config (
  id              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol          VARCHAR(32)   NOT NULL COMMENT '交易对，如 BTCUSDT',
  scope_tf        VARCHAR(8)    DEFAULT NULL COMMENT '作用周期（5m/15m/30m/1h/4h/1d，NULL=全周期）',
  scope_system    VARCHAR(32)   DEFAULT NULL COMMENT '作用系统编码（turtle海龟 dow道氏 elliott艾略特 volatility波动率 gann江恩 chanlun缠论 rule123法则 gap缺口 martingale马丁 oscillator摆动 triple_rsi三重RSI arbitrage套利，NULL=全系统）',
  principal       DECIMAL(20,8) NOT NULL DEFAULT 100 COMMENT '本金 USDT',
  leverage        DECIMAL(10,2) DEFAULT NULL COMMENT '杠杆倍数',
  position_pct    DECIMAL(10,4) DEFAULT NULL COMMENT '单笔仓位比例%',
  stop_loss_pct   DECIMAL(10,4) DEFAULT NULL COMMENT '止损比例%',
  take_profit_pct DECIMAL(10,4) DEFAULT NULL COMMENT '止盈比例%',
  tp_mode         VARCHAR(16)   DEFAULT NULL COMMENT '止盈方式（NULL=跟随信号 signal跟随信号 fixed_rr固定RR倍数 pct_factor盈利比例系数）',
  tp_rr_ratio     DECIMAL(10,4) DEFAULT NULL COMMENT '固定RR倍数（tp_mode=fixed_rr 生效：止盈距离=止损距离×该倍数）',
  tp_pct_factor   DECIMAL(10,4) DEFAULT NULL COMMENT '盈利比例系数（tp_mode=pct_factor 生效：信号止盈距离×该系数）',
  sl_mode         VARCHAR(16)   DEFAULT NULL COMMENT '止损方式（NULL=跟随信号 signal跟随信号 atr_buffer ATR缓冲系数）',
  sl_atr_mult     DECIMAL(10,4) DEFAULT NULL COMMENT 'ATR缓冲系数（sl_mode=atr_buffer 生效：止损距离=该TF ATR14%×该系数）',
  enabled         CHAR(1)       NOT NULL DEFAULT '1' COMMENT '是否启用（0停用 1启用）',
  remark          VARCHAR(500)  DEFAULT NULL COMMENT '备注',
  create_time     DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间',
  update_time     DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_tf_sys (symbol, scope_tf, scope_system)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯模拟交易参数配置（RuoYi维护，回读到贾维斯）';

-- ----------------------------------------------------------------------------
-- 2) 模拟钱包（每 symbol×tf×system 一行，小表全量 upsert）  源: twelve_sim_wallet
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_sim_wallet (
  id               BIGINT        NOT NULL COMMENT '源库自增 id（直用，幂等）',
  symbol           VARCHAR(32)   NOT NULL COMMENT '交易对',
  tf               VARCHAR(8)    NOT NULL COMMENT '时间框架（5m/15m/30m/1h/4h/1d）',
  system_code      VARCHAR(32)   NOT NULL COMMENT '信号系统编码（源列 system；turtle海龟 dow道氏 elliott艾略特 volatility波动率 gann江恩 chanlun缠论 rule123法则 gap缺口 martingale马丁 oscillator摆动 triple_rsi三重RSI arbitrage套利）',
  name_cn          VARCHAR(64)   DEFAULT NULL COMMENT '系统中文名',
  principal        DECIMAL(20,8) DEFAULT NULL COMMENT '本金 USDT',
  balance          DECIMAL(20,8) DEFAULT NULL COMMENT '可用余额 USDT',
  equity           DECIMAL(20,8) DEFAULT NULL COMMENT '净值（余额+未实现盈亏）USDT',
  total_trades     INT           DEFAULT NULL COMMENT '累计平仓笔数',
  win_trades       INT           DEFAULT NULL COMMENT '盈利笔数',
  total_pnl        DECIMAL(20,8) DEFAULT NULL COMMENT '累计已实现盈亏 USDT',
  win_rate         DECIMAL(10,4) DEFAULT NULL COMMENT '胜率%',
  profit_factor    DECIMAL(10,4) DEFAULT NULL COMMENT '盈亏因子',
  max_drawdown_pct DECIMAL(10,4) DEFAULT NULL COMMENT '最大回撤%',
  src_updated_ts   DECIMAL(16,3) DEFAULT NULL COMMENT '源 updated_ts（epoch 秒）',
  create_time      DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像首次落库时间',
  update_time      DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_tf_sys (symbol, tf, system_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯模拟钱包（镜像）';

-- ----------------------------------------------------------------------------
-- 3) 模拟持仓（状态镜像，open/closed 整行覆盖）           源: twelve_sim_position
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_sim_position (
  id                 BIGINT         NOT NULL COMMENT '源库自增 id（直用，幂等）',
  symbol             VARCHAR(32)    NOT NULL COMMENT '交易对',
  tf                 VARCHAR(8)     NOT NULL COMMENT '时间框架（5m/15m/30m/1h/4h/1d）',
  system_code        VARCHAR(32)    NOT NULL COMMENT '信号系统编码（源列 system）',
  direction          VARCHAR(8)     DEFAULT NULL COMMENT '方向（long多 short空）',
  entry_price        DECIMAL(20,8)  DEFAULT NULL COMMENT '开仓价',
  entry_time         DATETIME(3)    DEFAULT NULL COMMENT '开仓时间',
  qty                DECIMAL(24,10) DEFAULT NULL COMMENT '数量',
  margin             DECIMAL(20,8)  DEFAULT NULL COMMENT '保证金 USDT',
  leverage           DECIMAL(10,2)  DEFAULT NULL COMMENT '杠杆倍数',
  position_pct       DECIMAL(10,4)  DEFAULT NULL COMMENT '仓位比例%',
  stop_loss          DECIMAL(20,8)  DEFAULT NULL COMMENT '止损价',
  take_profit        DECIMAL(20,8)  DEFAULT NULL COMMENT '止盈价',
  cur_price          DECIMAL(20,8)  DEFAULT NULL COMMENT '当前价',
  unrealized_pnl     DECIMAL(20,8)  DEFAULT NULL COMMENT '未实现盈亏 USDT',
  unrealized_pnl_pct DECIMAL(10,4)  DEFAULT NULL COMMENT '未实现盈亏%',
  status             VARCHAR(8)     DEFAULT NULL COMMENT '持仓状态（pending计划未成交 open持仓 closed已平 canceled计划失效 rejected门禁拒单）',
  cancel_reason      VARCHAR(64)    DEFAULT NULL COMMENT '计划失效原因（neutral转中性 flip信号翻转 plan_gone计划消失 incoherent计划不自洽）',
  cancel_time        DATETIME(3)    DEFAULT NULL COMMENT '计划失效时间',
  reject_reason      VARCHAR(64)    DEFAULT NULL COMMENT '门禁拒单原因码（sl_too_tight止损过窄 sl_below_atr低于ATR噪声带 rr_too_low盈亏比不足 fee_negative_ev费用负期望 circuit_breaker战绩熔断 tf_gate周期门禁 counter_trend逆高周期趋势）',
  create_time        DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像首次落库时间',
  update_time        DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  KEY idx_sym_tf_sys (symbol, tf, system_code),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯模拟持仓（镜像）';

-- ----------------------------------------------------------------------------
-- 4) 模拟成交流水（追加型，id 游标增量，PK 直用源 id）    源: twelve_sim_trade
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_sim_trade (
  id              BIGINT         NOT NULL COMMENT '源库自增 id（直用，幂等）',
  symbol          VARCHAR(32)    NOT NULL COMMENT '交易对',
  tf              VARCHAR(8)     NOT NULL COMMENT '时间框架（5m/15m/30m/1h/4h/1d）',
  system_code     VARCHAR(32)    NOT NULL COMMENT '信号系统编码（源列 system）',
  name_cn         VARCHAR(64)    DEFAULT NULL COMMENT '系统中文名',
  direction       VARCHAR(8)     DEFAULT NULL COMMENT '方向（long多 short空）',
  entry_price     DECIMAL(20,8)  DEFAULT NULL COMMENT '开仓价',
  entry_time      DATETIME(3)    DEFAULT NULL COMMENT '开仓时间',
  exit_price      DECIMAL(20,8)  DEFAULT NULL COMMENT '平仓价',
  exit_time       DATETIME(3)    DEFAULT NULL COMMENT '平仓时间',
  qty             DECIMAL(24,10) DEFAULT NULL COMMENT '数量',
  margin          DECIMAL(20,8)  DEFAULT NULL COMMENT '保证金 USDT',
  leverage        DECIMAL(10,2)  DEFAULT NULL COMMENT '杠杆倍数',
  stop_loss       DECIMAL(20,8)  DEFAULT NULL COMMENT '止损价',
  take_profit     DECIMAL(20,8)  DEFAULT NULL COMMENT '止盈价',
  exit_reason     VARCHAR(16)    DEFAULT NULL COMMENT '平仓原因（tp止盈 sl止损 flip信号反转 timeout超时 liq爆仓强平）',
  pnl             DECIMAL(20,8)  DEFAULT NULL COMMENT '已实现盈亏 USDT',
  pnl_pct         DECIMAL(10,4)  DEFAULT NULL COMMENT '已实现盈亏%',
  rr              DECIMAL(10,4)  DEFAULT NULL COMMENT '实际盈亏比',
  balance_after   DECIMAL(20,8)  DEFAULT NULL COMMENT '平仓后钱包余额 USDT',
  holding_minutes INT            DEFAULT NULL COMMENT '持仓时长（分钟）',
  src_ts          DECIMAL(16,3)  DEFAULT NULL COMMENT '源 ts（epoch 秒）',
  create_time     DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  PRIMARY KEY (id),
  KEY idx_sym_tf_sys_exit (symbol, tf, system_code, exit_time),
  KEY idx_exit_time (exit_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯模拟成交流水（镜像）';

-- ============================================================================
-- B. 同步账号授权（账号本体由 jarvis_mysql_init.sql 创建）
--    jarvis_sim_config 同步器仅回读（SELECT）；INSERT/UPDATE 留给 RuoYi 应用账号，
--    此处不授予，维持最小权限
-- ============================================================================

-- wallet/position/trade 含 DELETE：delete-absent / 台账重置清理需要
-- （jarvis_sync_tasks_sim.py 2026-07-31 起；存量环境增量授权见
-- jarvis_mysql_watchlist_cleanup.sql B2 段）
GRANT SELECT                         ON `jiaweisi`.`jarvis_sim_config`   TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_sim_wallet`   TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_sim_position` TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `jiaweisi`.`jarvis_sim_trade`    TO 'jarvis_sync'@'%';
FLUSH PRIVILEGES;

-- ============================================================================
-- 完成自检（手工执行）：
--   SELECT COUNT(*) FROM information_schema.tables
--    WHERE table_schema='jiaweisi' AND table_name LIKE 'jarvis\_sim\_%';  -- 应为 4
--   SHOW GRANTS FOR 'jarvis_sync'@'%';
-- ============================================================================
