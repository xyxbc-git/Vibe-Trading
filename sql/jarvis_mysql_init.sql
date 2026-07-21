-- ============================================================================
-- 贾维斯 → RuoYi MySQL 镜像库初始化脚本
-- 版本: v1.0 (2026-07-19)  出处: Vibe-Trading/贾维斯-RuoYi同步-开发计划.md §2
-- ============================================================================
-- 【前提】
--   1. MySQL >= 8.0（实测目标 8.4.9，JSON / DATETIME(3) 全支持）
--   2. 库 `jiaweisi` 已存在（RuoYi 项目初始化时建立，application-druid.yml 同款）
--   3. 用具备 CREATE / CREATE USER / GRANT 权限的账号执行（如 root）
-- 【执行】
--   mysql -uroot -p < jarvis_mysql_init.sql
--   （脚本自带 USE `jiaweisi`，也可 mysql -uroot -p jiaweisi < 本文件）
-- 【执行顺序】 A 建表(13张) → B 同步账号+授权 → C 可选字典初始化
-- 【幂等性】  全脚本可重复执行：IF NOT EXISTS / WHERE NOT EXISTS 保护
-- 【安全边界】只新增 jarvis_* 前缀表与 jarvis_sync 账号，不触碰任何既有表/数据
-- 【回滚】    配套 jarvis_mysql_rollback.sql
-- 【时区口径】所有 DATETIME/DATETIME(3) 列统一存 **东八区（GMT+8）** 挂钟时间，
--            与 RuoYi druid 连接串 serverTimezone=GMT%2B8 对齐；同步器写入前
--            必须把源侧 UTC/epoch 时间换算为东八区（epoch 本身无时区，换算即可）
-- ⚠ 执行前必改：B 段 jarvis_sync 密码占位符 __CHANGE_ME_32CHARS__
-- ============================================================================

USE `jiaweisi`;

-- ============================================================================
-- A. 镜像表 DDL（13 张）
--    规范对齐 RuoYi 代码生成器：单列 BIGINT 主键 / 表 COMMENT 非空 / 逐列 COMMENT /
--    枚举注释全角括号 / 无 qrtz_、gen_ 前缀
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) 十二系统信号当前态（小表恒定行数，全量 upsert）    源: twelve_signal_state
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_signal_state (
  id             BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol         VARCHAR(32)   NOT NULL COMMENT '交易对，如 BTCUSDT',
  tf             VARCHAR(8)    NOT NULL COMMENT '时间框架（5m/15m/30m/1h/4h/1d）',
  system_code    VARCHAR(32)   NOT NULL COMMENT '信号系统标识（源列 system）',
  name_cn        VARCHAR(64)   DEFAULT NULL COMMENT '系统中文名',
  direction      VARCHAR(16)   DEFAULT NULL COMMENT '方向（bullish看多 bearish看空 neutral中性）',
  strength       DECIMAL(10,4) DEFAULT NULL COMMENT '强度 0~1',
  reasoning      TEXT          COMMENT '推理说明',
  levels_json    JSON          COMMENT '关键位快照',
  plan_json      JSON          COMMENT '交易计划快照',
  src_updated_ts DECIMAL(16,3) DEFAULT NULL COMMENT '源 updated_ts（epoch 秒）',
  src_changed_ts DECIMAL(16,3) DEFAULT NULL COMMENT '源 changed_ts（epoch 秒）',
  updated_at     DATETIME(3)   DEFAULT NULL COMMENT '信号最近计算时间',
  changed_at     DATETIME(3)   DEFAULT NULL COMMENT '信号最近实质变更时间',
  create_time    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像首次落库时间',
  update_time    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_tf_sys (symbol, tf, system_code),
  KEY idx_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯十二系统信号当前态（镜像）';

-- ----------------------------------------------------------------------------
-- 2) 信号变更流水（追加型，PK 直用源 id 幂等）          源: twelve_signal_changes
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_signal_change (
  id             BIGINT        NOT NULL COMMENT '源库自增 id（直用，保证幂等）',
  signal_time    DATETIME(3)   NOT NULL COMMENT '变更时间',
  src_ts         DECIMAL(16,3) NOT NULL COMMENT '源 ts（epoch 秒，游标列）',
  symbol         VARCHAR(32)   NOT NULL COMMENT '交易对',
  tf             VARCHAR(8)    NOT NULL COMMENT '时间框架',
  system_code    VARCHAR(32)   NOT NULL COMMENT '信号系统标识',
  name_cn        VARCHAR(64)   DEFAULT NULL COMMENT '系统中文名',
  prev_direction VARCHAR(16)   DEFAULT NULL COMMENT '变更前方向',
  new_direction  VARCHAR(16)   DEFAULT NULL COMMENT '变更后方向',
  prev_strength  DECIMAL(10,4) DEFAULT NULL COMMENT '变更前强度',
  new_strength   DECIMAL(10,4) DEFAULT NULL COMMENT '变更后强度',
  change_kinds   VARCHAR(128)  DEFAULT NULL COMMENT '变更类型（direction/strength/plan/levels 组合）',
  summary        VARCHAR(512)  DEFAULT NULL COMMENT '一句话摘要',
  prev_json      JSON          COMMENT '变更前完整快照',
  new_json       JSON          COMMENT '变更后完整快照',
  price          DECIMAL(20,8) DEFAULT NULL COMMENT '变更时价格',
  create_time    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  PRIMARY KEY (id),
  KEY idx_sym_tf_sys_time (symbol, tf, system_code, signal_time),
  KEY idx_signal_time (signal_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯信号变更流水（镜像，保留期长于源库50k裁剪）';

-- ----------------------------------------------------------------------------
-- 3) 推荐点位：十二系统共识交易计划快照（API 拉取，hash 判重）  源: /api/twelve/consensus
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_reco_plan (
  id             BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol         VARCHAR(32)   NOT NULL COMMENT '交易对，如 BTCUSDT',
  source_tf      VARCHAR(8)    DEFAULT NULL COMMENT '计划来源时间框架（MTF 融合选中）',
  side           VARCHAR(8)    DEFAULT NULL COMMENT '方向（long多 short空）',
  entry_lo       DECIMAL(20,8) DEFAULT NULL COMMENT '入场区间下沿',
  entry_hi       DECIMAL(20,8) DEFAULT NULL COMMENT '入场区间上沿',
  stop_loss      DECIMAL(20,8) DEFAULT NULL COMMENT '止损价',
  take_profit_1  DECIMAL(20,8) DEFAULT NULL COMMENT '止盈1',
  take_profit_2  DECIMAL(20,8) DEFAULT NULL COMMENT '止盈2',
  rr             DECIMAL(10,4) DEFAULT NULL COMMENT '盈亏比',
  position_pct   DECIMAL(10,4) DEFAULT NULL COMMENT '建议仓位%（1%权益风险反推）',
  plan_status    VARCHAR(16)   DEFAULT NULL COMMENT '计划状态（ok可执行 watch观望 neutral中性）',
  plan_reason    VARCHAR(512)  DEFAULT NULL COMMENT '状态原因',
  basis_json     JSON          COMMENT '贡献系统 slug 列表及口径说明',
  price          DECIMAL(20,8) DEFAULT NULL COMMENT '拉取时价格',
  direction      VARCHAR(16)   DEFAULT NULL COMMENT '共识方向（bullish看多 bearish看空 neutral中性）',
  confidence     DECIMAL(10,4) DEFAULT NULL COMMENT '共识置信度 0~1',
  plan_hash      VARCHAR(64)   NOT NULL COMMENT '计划内容哈希（判重）',
  as_of          DATETIME(3)   NOT NULL COMMENT '计划产生时间',
  create_time    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_hash (symbol, plan_hash),
  KEY idx_sym_asof (symbol, as_of),
  KEY idx_asof (as_of)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯推荐点位（十二系统共识交易计划快照）';

-- ----------------------------------------------------------------------------
-- 4) 推荐点位：4小时预测（PK 直用源 id）               源: intraday_predictions
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_intraday_prediction (
  id             BIGINT        NOT NULL COMMENT '源库自增 id（直用，幂等）',
  symbol         VARCHAR(32)   NOT NULL COMMENT '交易对',
  bar_time       DATETIME(3)   NOT NULL COMMENT '预测锚定4小时K收盘时间',
  src_bar_ts     DECIMAL(16,3) NOT NULL COMMENT '源 bar_ts（epoch 秒，游标列）',
  direction      VARCHAR(16)   DEFAULT NULL COMMENT '预测方向（up涨 down跌 sideways震荡）',
  prob           DECIMAL(10,4) DEFAULT NULL COMMENT '概率 0~1',
  tradeable      TINYINT       DEFAULT NULL COMMENT '是否可交易（0否 1是）',
  entry          DECIMAL(20,8) DEFAULT NULL COMMENT '入场价（最新收盘）',
  stop           DECIMAL(20,8) DEFAULT NULL COMMENT '止损价（ATR 倍数）',
  take           DECIMAL(20,8) DEFAULT NULL COMMENT '止盈价（ATR 倍数）',
  atr_pct        DECIMAL(10,4) DEFAULT NULL COMMENT 'ATR 百分比',
  oos_hit_rate   DECIMAL(10,4) DEFAULT NULL COMMENT '样本外命中率',
  p_value        DECIMAL(10,6) DEFAULT NULL COMMENT '显著性 p 值',
  reason         VARCHAR(512)  DEFAULT NULL COMMENT '预测依据摘要',
  why_text       TEXT          COMMENT '人话解释',
  outcome_ret    DECIMAL(10,4) DEFAULT NULL COMMENT '事后收益%（回填）',
  hit            TINYINT       DEFAULT NULL COMMENT '是否命中（回填，0否 1是）',
  create_time    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  update_time    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_bar (symbol, src_bar_ts),
  KEY idx_bar_time (bar_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯4小时预测点位（镜像）';

-- ----------------------------------------------------------------------------
-- 5) 盘口：tape 分钟聚合（镜像）                        源: tape_minute_bars
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_tape_bar (
  id             BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol         VARCHAR(32)   NOT NULL COMMENT '交易对',
  minute_time    DATETIME      NOT NULL COMMENT '分钟桶（东八区 GMT+8，源 minute epoch 秒换算）',
  src_minute     BIGINT        NOT NULL COMMENT '源 minute（epoch 秒，游标列）',
  buy_usd        DECIMAL(20,4) DEFAULT NULL COMMENT '主动买入额 USD',
  sell_usd       DECIMAL(20,4) DEFAULT NULL COMMENT '主动卖出额 USD',
  nr_buy_usd     DECIMAL(20,4) DEFAULT NULL COMMENT '非散户主动买入额 USD',
  nr_sell_usd    DECIMAL(20,4) DEFAULT NULL COMMENT '非散户主动卖出额 USD',
  open_price     DECIMAL(20,8) DEFAULT NULL COMMENT '分钟开盘价',
  close_price    DECIMAL(20,8) DEFAULT NULL COMMENT '分钟收盘价',
  high_price     DECIMAL(20,8) DEFAULT NULL COMMENT '分钟最高价',
  low_price      DECIMAL(20,8) DEFAULT NULL COMMENT '分钟最低价',
  trades_n       INT           DEFAULT NULL COMMENT '成交笔数',
  create_time    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_minute (symbol, src_minute),
  KEY idx_minute_time (minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯盘口分钟聚合（主动买卖与非散户资金流，镜像）';

-- ----------------------------------------------------------------------------
-- 6) 盘口：强平流水分钟聚合                             源: force_orders(SQLite) 聚合
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_force_order_min (
  id            BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol        VARCHAR(32)   NOT NULL COMMENT '交易对',
  minute_ts     DATETIME      NOT NULL COMMENT '分钟桶（东八区 GMT+8，源 trade_time 向下取整换算）',
  order_cnt     INT           NOT NULL DEFAULT 0 COMMENT '强平笔数',
  buy_cnt       INT           NOT NULL DEFAULT 0 COMMENT '买方向笔数（空头被强平）',
  sell_cnt      INT           NOT NULL DEFAULT 0 COMMENT '卖方向笔数（多头被强平）',
  qty_sum       DECIMAL(24,10) DEFAULT NULL COMMENT '数量合计',
  notional_sum  DECIMAL(20,4) DEFAULT NULL COMMENT '名义价值合计 USDT',
  notional_max  DECIMAL(20,4) DEFAULT NULL COMMENT '单笔最大名义价值',
  create_time   DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  update_time   DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_minute (symbol, minute_ts),
  KEY idx_minute (minute_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯强平流水分钟聚合（镜像）';

-- ----------------------------------------------------------------------------
-- 7) 币种市场情报快照（API 拉取，时序）                 源: /api/market-intel + /api/sentiment
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_market_snapshot (
  id                 BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol             VARCHAR(32)   NOT NULL COMMENT '交易对',
  snap_time          DATETIME(3)   NOT NULL COMMENT '快照时间（分钟对齐）',
  price              DECIMAL(20,8) DEFAULT NULL COMMENT '现价',
  price_chg_24h      DECIMAL(10,4) DEFAULT NULL COMMENT '24小时涨跌幅%',
  funding_rate       DECIMAL(12,8) DEFAULT NULL COMMENT '资金费率（仅部分币种有源）',
  oi_value           DECIMAL(24,4) DEFAULT NULL COMMENT '持仓量',
  oi_change_pct      DECIMAL(10,4) DEFAULT NULL COMMENT '持仓量变化%',
  long_pct           DECIMAL(10,4) DEFAULT NULL COMMENT '多头占比%',
  short_pct          DECIMAL(10,4) DEFAULT NULL COMMENT '空头占比%',
  ls_ratio           DECIMAL(10,4) DEFAULT NULL COMMENT '多空比',
  fng_value          INT           DEFAULT NULL COMMENT '恐贪指数（全市场）',
  fng_class          VARCHAR(32)   DEFAULT NULL COMMENT '恐贪分级',
  sentiment_score    DECIMAL(10,4) DEFAULT NULL COMMENT '情绪评分 -100~100',
  sentiment_bias     VARCHAR(16)   DEFAULT NULL COMMENT '情绪倾向（bullish看多 bearish看空 neutral中性）',
  sentiment_headline VARCHAR(255)  DEFAULT NULL COMMENT '情绪一句话结论',
  factors_json       JSON          COMMENT '情绪因子明细',
  create_time        DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_snap (symbol, snap_time),
  KEY idx_snap_time (snap_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯币种市场情报快照（资金费率持仓多空情绪）';

-- ----------------------------------------------------------------------------
-- 8) 每日决策快照（PK 直用源 id）                       源: snapshots
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_snapshot (
  id               BIGINT        NOT NULL COMMENT '源库自增 id',
  symbol           VARCHAR(32)   NOT NULL COMMENT '交易对',
  as_of_date       DATE          NOT NULL COMMENT '决策日',
  generated_at_utc VARCHAR(32)   DEFAULT NULL COMMENT '生成时间（UTC 字符串，保持源值）',
  price            DECIMAL(20,8) DEFAULT NULL COMMENT '决策时价格',
  conviction_score DECIMAL(10,4) DEFAULT NULL COMMENT '信心分',
  direction        VARCHAR(32)   DEFAULT NULL COMMENT '方向结论',
  position_pct     DECIMAL(10,4) DEFAULT NULL COMMENT '建议仓位%',
  dd_pct           DECIMAL(10,4) DEFAULT NULL COMMENT '回撤%',
  fng              INT           DEFAULT NULL COMMENT '恐贪指数',
  above_ma200      TINYINT       DEFAULT NULL COMMENT '是否在MA200上方（0否 1是）',
  dd30_active      TINYINT       DEFAULT NULL COMMENT '30日回撤保护是否触发（0否 1是）',
  stop_loss        DECIMAL(20,8) DEFAULT NULL COMMENT '止损价',
  take_profit      DECIMAL(20,8) DEFAULT NULL COMMENT '止盈价',
  decision_json    JSON          COMMENT '完整决策 JSON',
  src_created_ts   DECIMAL(16,3) DEFAULT NULL COMMENT '源 created_ts（游标列）',
  create_time      DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  update_time      DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_date (symbol, as_of_date),
  KEY idx_date (as_of_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯每日决策快照（镜像）';

-- ----------------------------------------------------------------------------
-- 9) 前向收益追踪（复合业务键 + 本地自增 id）           源: outcomes
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_outcome (
  id               BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  snapshot_id      BIGINT        NOT NULL COMMENT '快照 id（对应 jarvis_snapshot.id）',
  horizon          INT           NOT NULL COMMENT '前向窗口天数（7/30）',
  fwd_date         DATE          DEFAULT NULL COMMENT '前向到期日',
  fwd_price        DECIMAL(20,8) DEFAULT NULL COMMENT '到期价格',
  fwd_ret_pct      DECIMAL(10,4) DEFAULT NULL COMMENT '前向收益%',
  correct          TINYINT       DEFAULT NULL COMMENT '方向是否踩对（0否 1是）',
  src_evaluated_ts DECIMAL(16,3) DEFAULT NULL COMMENT '源 evaluated_ts（游标列，重评会更新）',
  create_time      DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  update_time      DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_snap_horizon (snapshot_id, horizon)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯前向收益追踪（镜像）';

-- ----------------------------------------------------------------------------
-- 10) 模拟仓（PK 直用源 id，open→closed 整行覆盖）      源: paper_positions
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_position (
  id                BIGINT         NOT NULL COMMENT '源库自增 id',
  symbol            VARCHAR(32)    NOT NULL COMMENT '交易对',
  status            VARCHAR(16)    NOT NULL COMMENT '仓位状态（open持仓 closed已平）',
  side              VARCHAR(8)     NOT NULL COMMENT '方向（buy买 sell卖）',
  qty               DECIMAL(24,10) DEFAULT NULL COMMENT '数量',
  signal_tf         VARCHAR(8)     DEFAULT NULL COMMENT '触发信号时间框架',
  entry_date        DATE           DEFAULT NULL COMMENT '开仓日',
  entry_price       DECIMAL(20,8)  DEFAULT NULL COMMENT '开仓价',
  stop_loss         DECIMAL(20,8)  DEFAULT NULL COMMENT '止损价',
  take_profit       DECIMAL(20,8)  DEFAULT NULL COMMENT '止盈价',
  time_stop_days    INT            DEFAULT NULL COMMENT '时间止损天数',
  conviction_score  DECIMAL(10,4)  DEFAULT NULL COMMENT '开仓信心分',
  exit_date         DATE           DEFAULT NULL COMMENT '平仓日',
  exit_price        DECIMAL(20,8)  DEFAULT NULL COMMENT '平仓价',
  exit_reason       VARCHAR(32)    DEFAULT NULL COMMENT '平仓原因（stop止损 take止盈 time时间 signal信号 manual手动）',
  realized_pnl_usdt DECIMAL(20,8)  DEFAULT NULL COMMENT '已实现盈亏 USDT',
  realized_pnl_pct  DECIMAL(10,4)  DEFAULT NULL COMMENT '已实现盈亏%',
  opened_at         DATETIME(3)    DEFAULT NULL COMMENT '开仓时间',
  closed_at         DATETIME(3)    DEFAULT NULL COMMENT '平仓时间',
  src_opened_ts     DECIMAL(16,3)  DEFAULT NULL COMMENT '源 opened_ts（游标列）',
  src_closed_ts     DECIMAL(16,3)  DEFAULT NULL COMMENT '源 closed_ts（游标列）',
  create_time       DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  update_time       DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  KEY idx_status (status),
  KEY idx_sym_opened (symbol, opened_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯模拟仓（镜像）';

-- ----------------------------------------------------------------------------
-- 11) 限价挂单（PK 直用源 id，状态流转整行覆盖）        源: limit_orders
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_limit_order (
  id             BIGINT         NOT NULL COMMENT '源库自增 id',
  symbol         VARCHAR(32)    NOT NULL COMMENT '交易对',
  side           VARCHAR(8)     NOT NULL COMMENT '方向（buy买 sell卖）',
  limit_price    DECIMAL(20,8)  NOT NULL COMMENT '限价',
  qty            DECIMAL(24,10) NOT NULL COMMENT '数量',
  notional_usdt  DECIMAL(20,8)  DEFAULT NULL COMMENT '名义价值 USDT',
  status         VARCHAR(16)    NOT NULL COMMENT '挂单状态（pending挂单中 filled已成交 cancelled已撤销）',
  stop_loss      DECIMAL(20,8)  DEFAULT NULL COMMENT '止损价',
  take_profit    DECIMAL(20,8)  DEFAULT NULL COMMENT '止盈价',
  time_stop_days INT            DEFAULT NULL COMMENT '时间止损天数',
  created_date   DATE           DEFAULT NULL COMMENT '挂单日',
  filled_price   DECIMAL(20,8)  DEFAULT NULL COMMENT '成交价',
  filled_at      DATETIME(3)    DEFAULT NULL COMMENT '成交时间',
  cancelled_at   DATETIME(3)    DEFAULT NULL COMMENT '撤销时间',
  position_id    BIGINT         DEFAULT NULL COMMENT '成交后关联 jarvis_position.id',
  note           VARCHAR(255)   DEFAULT NULL COMMENT '备注',
  src_created_ts DECIMAL(16,3)  DEFAULT NULL COMMENT '源 created_ts（游标列）',
  create_time    DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  update_time    DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯限价挂单（镜像）';

-- ----------------------------------------------------------------------------
-- 12) 盘口主体画像快照（二期表，先建空表占位）          源: /api/tape/flow
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_tape_flow_snap (
  id           BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol       VARCHAR(32)   NOT NULL COMMENT '交易对',
  snap_time    DATETIME(3)   NOT NULL COMMENT '快照时间',
  window_min   INT           DEFAULT NULL COMMENT '统计窗口分钟数',
  verdict      VARCHAR(255)  DEFAULT NULL COMMENT '综合判词',
  actor_json   JSON          COMMENT '各主体统计（散户/中户/机构/做市：金额、净额、多头占比、判词）',
  total_usd    DECIMAL(20,4) DEFAULT NULL COMMENT '窗口总成交额 USD',
  create_time  DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_snap (symbol, snap_time),
  KEY idx_snap_time (snap_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯盘口主体画像快照（二期）';

-- ----------------------------------------------------------------------------
-- 13) 同步链路心跳（同步器自写自读）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jarvis_sync_state (
  table_name   VARCHAR(64)   NOT NULL COMMENT '逻辑表名',
  cursor_value VARCHAR(64)   DEFAULT NULL COMMENT '当前游标（id 或 epoch 秒）',
  last_run_at  DATETIME(3)   DEFAULT NULL COMMENT '最近一次同步尝试',
  last_ok_at   DATETIME(3)   DEFAULT NULL COMMENT '最近一次成功',
  last_error   VARCHAR(512)  DEFAULT NULL COMMENT '最近错误（截断）',
  rows_total   BIGINT        NOT NULL DEFAULT 0 COMMENT '累计推送行数',
  lag_seconds  DECIMAL(12,3) DEFAULT NULL COMMENT '估算延迟秒数',
  update_time  DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '心跳更新时间',
  PRIMARY KEY (table_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯同步链路心跳';

-- ============================================================================
-- B. 同步专用账号 + 最小权限授权
--    ⚠ 执行前把 __CHANGE_ME_32CHARS__ 替换为 32 位随机密码（openssl rand -base64 24）
--    本地阶段（localhost 直连）不强制 SSL；迁云后按方案 §4.2 改：
--    ALTER USER 'jarvis_sync'@'%' REQUIRE SSL; 并将 '%' 收紧为固定出口 IP/隧道网段
-- ============================================================================

CREATE USER IF NOT EXISTS 'jarvis_sync'@'%' IDENTIFIED BY '__CHANGE_ME_32CHARS__';

GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_signal_state`        TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_signal_change`       TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_reco_plan`           TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_intraday_prediction` TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_tape_bar`            TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_force_order_min`     TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_market_snapshot`     TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_snapshot`            TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_outcome`             TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_position`            TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_limit_order`         TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_tape_flow_snap`      TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_sync_state`          TO 'jarvis_sync'@'%';
FLUSH PRIVILEGES;

-- 验证（手工执行）：
--   SHOW GRANTS FOR 'jarvis_sync'@'%';
--   用 jarvis_sync 连接后执行 DELETE 应报 1142 权限拒绝

-- ============================================================================
-- C. 可选：RuoYi 字典初始化（jarvis_direction / jarvis_plan_status）
--    供代码生成器页面绑定 dictType 用；不需要可整段注释掉
--    幂等：INSERT ... SELECT ... WHERE NOT EXISTS
-- ============================================================================

INSERT INTO sys_dict_type (dict_name, dict_type, status, create_by, create_time, remark)
SELECT '贾维斯信号方向', 'jarvis_direction', '0', 'admin', SYSDATE(), '贾维斯镜像表 direction 列'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_type WHERE dict_type = 'jarvis_direction');

INSERT INTO sys_dict_data (dict_sort, dict_label, dict_value, dict_type, list_class, is_default, status, create_by, create_time, remark)
SELECT 1, '看多', 'bullish', 'jarvis_direction', 'success', 'N', '0', 'admin', SYSDATE(), '看多信号'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_data WHERE dict_type = 'jarvis_direction' AND dict_value = 'bullish');

INSERT INTO sys_dict_data (dict_sort, dict_label, dict_value, dict_type, list_class, is_default, status, create_by, create_time, remark)
SELECT 2, '看空', 'bearish', 'jarvis_direction', 'danger', 'N', '0', 'admin', SYSDATE(), '看空信号'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_data WHERE dict_type = 'jarvis_direction' AND dict_value = 'bearish');

INSERT INTO sys_dict_data (dict_sort, dict_label, dict_value, dict_type, list_class, is_default, status, create_by, create_time, remark)
SELECT 3, '中性', 'neutral', 'jarvis_direction', 'info', 'Y', '0', 'admin', SYSDATE(), '中性信号'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_data WHERE dict_type = 'jarvis_direction' AND dict_value = 'neutral');

INSERT INTO sys_dict_type (dict_name, dict_type, status, create_by, create_time, remark)
SELECT '贾维斯计划状态', 'jarvis_plan_status', '0', 'admin', SYSDATE(), '贾维斯推荐点位 plan_status 列'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_type WHERE dict_type = 'jarvis_plan_status');

INSERT INTO sys_dict_data (dict_sort, dict_label, dict_value, dict_type, list_class, is_default, status, create_by, create_time, remark)
SELECT 1, '可执行', 'ok', 'jarvis_plan_status', 'success', 'N', '0', 'admin', SYSDATE(), '计划可执行'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_data WHERE dict_type = 'jarvis_plan_status' AND dict_value = 'ok');

INSERT INTO sys_dict_data (dict_sort, dict_label, dict_value, dict_type, list_class, is_default, status, create_by, create_time, remark)
SELECT 2, '观望', 'watch', 'jarvis_plan_status', 'warning', 'N', '0', 'admin', SYSDATE(), '条件未满足观望'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_data WHERE dict_type = 'jarvis_plan_status' AND dict_value = 'watch');

INSERT INTO sys_dict_data (dict_sort, dict_label, dict_value, dict_type, list_class, is_default, status, create_by, create_time, remark)
SELECT 3, '中性', 'neutral', 'jarvis_plan_status', 'info', 'Y', '0', 'admin', SYSDATE(), '无明确计划'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_data WHERE dict_type = 'jarvis_plan_status' AND dict_value = 'neutral');

INSERT INTO sys_dict_type (dict_name, dict_type, status, create_by, create_time, remark)
SELECT '贾维斯是否', 'jarvis_yes_no', '0', 'admin', SYSDATE(), '贾维斯镜像表 0/1 布尔列（tradeable/hit/above_ma200 等）；RuoYi 自带 sys_yes_no 值域为 Y/N 不适用'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_type WHERE dict_type = 'jarvis_yes_no');

INSERT INTO sys_dict_data (dict_sort, dict_label, dict_value, dict_type, list_class, is_default, status, create_by, create_time, remark)
SELECT 1, '是', '1', 'jarvis_yes_no', 'primary', 'N', '0', 'admin', SYSDATE(), '布尔真'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_data WHERE dict_type = 'jarvis_yes_no' AND dict_value = '1');

INSERT INTO sys_dict_data (dict_sort, dict_label, dict_value, dict_type, list_class, is_default, status, create_by, create_time, remark)
SELECT 2, '否', '0', 'jarvis_yes_no', 'info', 'Y', '0', 'admin', SYSDATE(), '布尔假'
WHERE NOT EXISTS (SELECT 1 FROM sys_dict_data WHERE dict_type = 'jarvis_yes_no' AND dict_value = '0');

-- ============================================================================
-- 完成自检（手工执行）：
--   SELECT COUNT(*) FROM information_schema.tables
--    WHERE table_schema='jiaweisi' AND table_name LIKE 'jarvis\_%';   -- 应为 13
--   SHOW CREATE TABLE jarvis_signal_state\G
-- ============================================================================
