# 贾维斯 → RuoYi MySQL 数据同步 · 开发计划

> 版本：v1.0（2026-07-19）
> 上游方案：`Vibe-Trading/贾维斯-RuoYi同步方案.md`（v1.0，用户已认可）
> 本文范围：调研结论 + 表结构 DDL + 同步架构 + 配置设计 + **可直接派发的任务拆解** + 验证方案。不含业务代码。
> 交付物两块：①贾维斯侧推送模块（`jarvis_sync.py` + launchd）②MySQL 表/初始化脚本（`sql/jarvis_mysql_init.sql`）。RuoYi 业务页面由用户自行开发（建表兼容其代码生成器）。

---

## 1. 现状调研结论

### 1.1 用户点名四类数据的落位（文件:行号为证）

#### ① 12信号（十二系统信号）

| 数据 | 位置 | 存储 | 结构要点 | 更新频率 |
|---|---|---|---|---|
| 信号当前态 | `jarvis_signal_history.py:53-101`（DDL）、`record_batch:221-316` | 源库表 `twelve_signal_state`（PG，懒建） | PK(symbol,tf,system)、direction/strength/reasoning/levels_json/plan_json/updated_ts/changed_ts | dashboard 缓存未命中重算时全量 upsert（signals 60-120s / consensus 180s TTL） |
| 信号变更流水 | 同上，`diff_signal:162-215` | 源库表 `twelve_signal_changes` | id 自增 PK、prev/new direction+strength、change_kinds、prev_json/new_json、price；50k 行滚动裁剪 | 仅实质变更追加（方向翻转必记；强度Δ≥0.15；计划价>0.2%） |

**关键事实**：这两表**只由 dashboard API 重算路径写入**（`jarvis_dashboard.py:3660-3776`），`jarvis_daemon.py` 的 twelve_step（`105-162`）只写 JSON 状态文件+告警，**不调 record_batch**。若无人访问界面，表会长期无新数据。→ 应对见 §3.2「API 通道一石二鸟」。

#### ② 推荐点位

| 数据源 | 位置 | 存储 | 点位字段 | 频率 |
|---|---|---|---|---|
| **共识综合交易计划**（主源） | `jarvis_twelve_systems.py` `_aggregate_trade_plan_ex:1011-1133`，经 `/api/twelve/consensus` 暴露 | **不落库**，仅 API/内存缓存 180s | side、entry_zone[lo,hi]、stop_loss、take_profit_1/2、rr、min_rr、position_pct、basis[]、plan_status{state,reason}、source_tf | 180s TTL 惰性重算 |
| 4h 预测点位 | 引擎 `jarvis_intraday_predict.py:297-370`；落库 `jarvis_intraday_trader.py:69-93,281-297` | 源库表 `intraday_predictions`，UNIQUE(symbol,bar_ts) | direction、prob、tradeable、entry/stop/take、atr_pct、oos_hit_rate、p_value、outcome_ret、hit（后补） | daemon --intraday，每 4h 收盘+120s |
| 每日决策快照 | `jarvis_journal.py:51-146`；点位来自 `jarvis_brief.score_and_plan` | 源库表 `snapshots`，UNIQUE(symbol,as_of_date) | stop_loss、take_profit、position_pct（固定百分比口径，非结构位） | daemon 每日每币 1 条 |
| 仓位计算器 | `jarvis_position_calc.py:299-537`、API `dashboard:4298-4377` | **不落库**，请求现算 | entry/sl/take_profits(RR 分档)/liquidation | 按需 |

**结论**：推荐点位以**共识 trade_plan 为主源**（需新建镜像表经 API 拉取），`intraday_predictions`、`snapshots` 为辅源（已落库可直接增量同步）。仓位计算器不纳入（无自然持久化语义）。

#### ③ 盘口数据

| 数据源 | 位置 | 存储 | 频率 |
|---|---|---|---|
| **tape 分钟聚合 K**（唯一已落库盘口） | `jarvis_tape_classify.py:558-571`（DDL）、`710-726`（flush） | 源库表 `tape_minute_bars`：symbol、minute(epoch秒) PK、buy/sell_usd、nr_buy/nr_sell_usd、OHLC、trades_n；**保留 14 天** | WS 实时聚合，后台每 30s flush 完结分钟 |
| 强平流水 | `jarvis_ws_stream.py:177-188` | 独立 SQLite `~/.vibe-trading/jarvis_ws_force_orders.db` 表 `force_orders`（永远 SQLite，不走 jarvis_db） | WS forceOrder 逐条 |
| 足迹图 footprint | `tape_classify:382-427,866-1025` | **纯内存** ≤4h（FOOT_MINUTES_MAX=240） | WS 实时 |
| 主体画像 breakdown | `tape_classify:134-171` | **纯内存即时计算**（retail/mid/inst/maker、long_pct、verdict_cn） | 查询时 |
| 大单流 whale_tape / 深度 depth_view / delta/CVD / 清算面板与磁吸 | `whale_tape:235` / `depth_view:128` / `delta_flow:262` / `liquidation:217`、`liq_map:275` | **均不落盘**（内存或惰性 REST 现算） | WS 实时或 API 惰性 |

**结论**：第一期同步 `tape_minute_bars`（增量游标）+ `force_orders` 分钟聚合（方案 §3.2 G7 已定）；足迹/主体画像等纯内存数据列**二期可选**（HTTP 快照方式，见 §2.2 表 9）。

#### ④ B种数据（币种市场数据）

调研确认：代码与文档中「币种」= watchlist 交易对（`jarvis_config.py:62`，默认 7 币：BTCUSDT/ETHUSDT/SOLUSDT/BNBUSDT/XRPUSDT/DOGEUSDT/ADAUSDT，launchd 启动参数同款）。**无独立"B种"表**；实质是按币种的市场情报/情绪数据，全部**不落盘**：

| 数据源 | 位置 | 内容 | TTL/频率 |
|---|---|---|---|
| market-intel | `jarvis_market_intel.py:40,176-230`，API `/api/market-intel` | funding_rate（BTC/ETH/SOL/BNB）、oi、long_short（默认仅 BTCUSDT）、fng、price_24h | 内存 TTL 120s~1h |
| sentiment | `jarvis_sentiment.py:439+`，API `/api/sentiment?symbol=X` | score(-100~100)、bias、headline、factors[]、sl_tp_advice | API 60s TTL |
| radar 扫描 | `jarvis_radar.py:87-146`，`POST /api/actions/radar` | 多币 conviction/entry_zone/stop_loss/take_profit_ref | 手动/低频 |

**结论**：新建 `jarvis_market_snapshot` 时序快照表，同步器定时（默认 5min）拉 API 写入。此解读（B种=币种市场数据）已按仓库证据推定，**建议主控向用户一句话确认**；若用户实指其它（如"B 类合约数据"），表结构可平移复用。

### 1.2 RuoYi 侧关键证据

| 项 | 值 | 证据 |
|---|---|---|
| 库名 | **`jiaweisi`**（非方案假设的 ry-vue） | `application-druid.yml:9` |
| 连接 | `jdbc:mysql://localhost:3306/jiaweisi?...serverTimezone=GMT%2B8`，root/qaz123456 | 同上 :9-11 |
| MySQL 服务器 | 本机 Homebrew **mysqld 8.4.9**（arm64）监听 3306（实测 lsof） | 本机探测 |
| RuoYi 版本 | **3.9.2** / JDK17 / Spring Boot 4.0.6 / PageHelper / Redis localhost:6379 | `pom.xml:9-20`、`application.yml` |
| 代码生成器 | `autoRemovePre=false`（jarvis_ 前缀不会去掉，类名带 Jarvis）；主键靠 `COLUMN_KEY=PRI`；bigint→Long、decimal→BigDecimal、datetime→Date；**表注释必填**；`qrtz_`/`gen_` 前缀禁用；字典下拉需生成界面手工绑 dictType | `generator.yml`、`GenUtils.java`、`GenTableMapper.xml:83,111` |
| 定时任务 | ruoyi-quartz 可用（`sys_job` + `@Component` Bean），可承接镜像表归档清理 | `ruoyi-admin/pom.xml:45-48`、`RyTask.java` |

**建表规范**（DDL 已按此写）：InnoDB、显式 utf8mb4、单列 bigint 主键 AUTO_INCREMENT（或直用源 id）、表 COMMENT 必填、逐列 COMMENT、枚举注释用全角括号 `（0xx 1yy）`、状态列名以 `status` 结尾。

### 1.3 与方案 md 的差异标注（以实际代码为准）

| # | 方案 md 描述 | 实际情况 | 处理 |
|---|---|---|---|
| D1 | 库名占位 `ry-vue`，"待盘点确认" | 实为 **`jiaweisi`** | DDL/授权语句全部替换 |
| D2 | MySQL 版本未知，JSON 列"待确认" | 8.4.9，JSON/DATETIME(3) 全支持 | DDL 按 8.x 定稿 |
| D3 | 方案八表未覆盖"推荐点位/盘口 tape/币种市场" | 用户点名要 | 本计划新增 4 张 P0 表（§2.2 表 9-12） |
| D4 | 方案假设信号两表持续有数据 | 实际只有 dashboard 访问才写 | API 通道定时拉 consensus 触发源侧落库（§3.2） |
| D5 | root/qaz123456 | 本机 CLI 实测 `Access denied`（可能密码不同步或仅限 RuoYi 应用侧网络路径） | 列为 T0 前置确认项；同步器不用 root，建专用账号 |

---

## 2. 目标表结构 DDL

### 2.1 总表清单与优先级

| # | MySQL 表 | 数据类别 | 源 | 优先级 | DDL 出处 |
|---|---|---|---|---|---|
| 1 | jarvis_signal_state | 12信号当前态 | DB 增量 | **P0** | 方案 §2.1（沿用，见修订） |
| 2 | jarvis_signal_change | 12信号变更流水 | DB 增量 | **P0** | 方案 §2.1（沿用） |
| 3 | jarvis_reco_plan | 推荐点位（共识计划） | **API 拉取** | **P0** | 本文 §2.2 |
| 4 | jarvis_intraday_prediction | 推荐点位（4h 预测） | DB 增量 | **P0** | 本文 §2.2 |
| 5 | jarvis_tape_bar | 盘口分钟聚合 | DB 增量 | **P0** | 本文 §2.2 |
| 6 | jarvis_force_order_min | 盘口强平分钟聚合 | SQLite 聚合 | **P0** | 方案 §2.1（沿用） |
| 7 | jarvis_market_snapshot | 币种市场情报快照 | **API 拉取** | **P0** | 本文 §2.2 |
| 8 | jarvis_snapshot | 每日决策快照 | DB 增量 | P1 | 方案 §2.1（沿用） |
| 9 | jarvis_outcome | 前向收益 | DB 增量 | P1 | 方案 §2.1（沿用） |
| 10 | jarvis_position | 模拟仓 | DB 增量 | P1 | 方案 §2.1（沿用） |
| 11 | jarvis_limit_order | 限价挂单 | DB 增量 | P1 | 方案 §2.1（沿用） |
| 12 | jarvis_tape_flow_snap | 盘口主体画像快照 | API 拉取 | P2（二期） | 本文 §2.2 |
| 13 | jarvis_sync_state | 同步心跳 | 同步器自写 | **P0** | 方案 §2.1（沿用） |

**沿用表统一修订**：①库名 `jiaweisi`；②表尾显式 `DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci`；③JSON 类型定稿（MySQL 8.4）；④**时区口径**——所有 DATETIME 列统一存东八区（GMT+8）挂钟时间，与 RuoYi druid `serverTimezone=GMT%2B8` 对齐，同步器写入前把源侧 UTC/epoch 换算为东八区（含 jarvis_force_order_min.minute_ts、jarvis_tape_bar.minute_time）。其余照方案 §2.1 原文。

### 2.2 新增表 DDL（P0 四张 + P2 一张）

```sql
-- 9) 推荐点位：twelve 共识综合交易计划快照（API 拉取，内容变化才插新行）
CREATE TABLE jarvis_reco_plan (
  id             BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol         VARCHAR(32)  NOT NULL COMMENT '交易对，如 BTCUSDT',
  source_tf      VARCHAR(8)   DEFAULT NULL COMMENT '计划来源时间框架（MTF 融合选中）',
  side           VARCHAR(8)   DEFAULT NULL COMMENT '方向（long多 short空）',
  entry_lo       DECIMAL(20,8) DEFAULT NULL COMMENT '入场区间下沿',
  entry_hi       DECIMAL(20,8) DEFAULT NULL COMMENT '入场区间上沿',
  stop_loss      DECIMAL(20,8) DEFAULT NULL COMMENT '止损价',
  take_profit_1  DECIMAL(20,8) DEFAULT NULL COMMENT '止盈1',
  take_profit_2  DECIMAL(20,8) DEFAULT NULL COMMENT '止盈2',
  rr             DECIMAL(10,4) DEFAULT NULL COMMENT '盈亏比',
  position_pct   DECIMAL(10,4) DEFAULT NULL COMMENT '建议仓位%（1%权益风险反推）',
  plan_status    VARCHAR(16)  DEFAULT NULL COMMENT '计划状态（ok可执行 watch观望 neutral中性）',
  plan_reason    VARCHAR(512) DEFAULT NULL COMMENT '状态原因',
  basis_json     JSON         COMMENT '贡献系统 slug 列表及口径说明',
  price          DECIMAL(20,8) DEFAULT NULL COMMENT '拉取时价格',
  direction      VARCHAR(16)  DEFAULT NULL COMMENT '共识方向 bullish/bearish/neutral',
  confidence     DECIMAL(10,4) DEFAULT NULL COMMENT '共识置信度 0~1',
  plan_hash      VARCHAR(64)  NOT NULL COMMENT '计划内容哈希（判重）',
  as_of          DATETIME(3)  NOT NULL COMMENT '计划产生时间',
  create_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_hash (symbol, plan_hash),
  KEY idx_sym_asof (symbol, as_of),
  KEY idx_asof (as_of)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯推荐点位（十二系统共识交易计划快照）';

-- 10) 推荐点位：4h 预测（镜像 intraday_predictions，PK 直用源 id）
CREATE TABLE jarvis_intraday_prediction (
  id             BIGINT       NOT NULL COMMENT '源库自增 id（直用，幂等）',
  symbol         VARCHAR(32)  NOT NULL COMMENT '交易对',
  bar_time       DATETIME(3)  NOT NULL COMMENT '预测锚定 4h K 收盘时间',
  src_bar_ts     DECIMAL(16,3) NOT NULL COMMENT '源 bar_ts（epoch 秒，游标列）',
  direction      VARCHAR(16)  DEFAULT NULL COMMENT '预测方向 up/down/sideways',
  prob           DECIMAL(10,4) DEFAULT NULL COMMENT '概率 0~1',
  tradeable      TINYINT      DEFAULT NULL COMMENT '是否可交易（0否 1是）',
  entry          DECIMAL(20,8) DEFAULT NULL COMMENT '入场价（最新收盘）',
  stop           DECIMAL(20,8) DEFAULT NULL COMMENT '止损价（ATR 倍数）',
  take           DECIMAL(20,8) DEFAULT NULL COMMENT '止盈价（ATR 倍数）',
  atr_pct        DECIMAL(10,4) DEFAULT NULL COMMENT 'ATR 百分比',
  oos_hit_rate   DECIMAL(10,4) DEFAULT NULL COMMENT '样本外命中率',
  p_value        DECIMAL(10,6) DEFAULT NULL COMMENT '显著性 p 值',
  reason         VARCHAR(512) DEFAULT NULL COMMENT '预测依据摘要',
  why_text       TEXT         COMMENT '人话解释',
  outcome_ret    DECIMAL(10,4) DEFAULT NULL COMMENT '事后收益%（回填）',
  hit            TINYINT      DEFAULT NULL COMMENT '是否命中（回填，0否 1是）',
  create_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  update_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_bar (symbol, src_bar_ts),
  KEY idx_bar_time (bar_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯4小时预测点位（镜像）';

-- 11) 盘口：tape 分钟聚合（镜像 tape_minute_bars）
CREATE TABLE jarvis_tape_bar (
  id             BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol         VARCHAR(32)  NOT NULL COMMENT '交易对',
  minute_time    DATETIME     NOT NULL COMMENT '分钟桶（东八区 GMT+8，源 minute epoch 秒换算）',
  src_minute     BIGINT       NOT NULL COMMENT '源 minute（epoch 秒，游标列）',
  buy_usd        DECIMAL(20,4) DEFAULT NULL COMMENT '主动买入额 USD',
  sell_usd       DECIMAL(20,4) DEFAULT NULL COMMENT '主动卖出额 USD',
  nr_buy_usd     DECIMAL(20,4) DEFAULT NULL COMMENT '非散户主动买入额 USD',
  nr_sell_usd    DECIMAL(20,4) DEFAULT NULL COMMENT '非散户主动卖出额 USD',
  open_price     DECIMAL(20,8) DEFAULT NULL COMMENT '分钟开盘价',
  close_price    DECIMAL(20,8) DEFAULT NULL COMMENT '分钟收盘价',
  high_price     DECIMAL(20,8) DEFAULT NULL COMMENT '分钟最高价',
  low_price      DECIMAL(20,8) DEFAULT NULL COMMENT '分钟最低价',
  trades_n       INT          DEFAULT NULL COMMENT '成交笔数',
  create_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_minute (symbol, src_minute),
  KEY idx_minute_time (minute_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯盘口分钟聚合（主动买卖/非散户资金流，镜像）';

-- 12) 币种市场情报快照（API 拉取，时序）
CREATE TABLE jarvis_market_snapshot (
  id               BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol           VARCHAR(32)  NOT NULL COMMENT '交易对',
  snap_time        DATETIME(3)  NOT NULL COMMENT '快照时间（分钟对齐）',
  price            DECIMAL(20,8) DEFAULT NULL COMMENT '现价',
  price_chg_24h    DECIMAL(10,4) DEFAULT NULL COMMENT '24h 涨跌幅%',
  funding_rate     DECIMAL(12,8) DEFAULT NULL COMMENT '资金费率（可为空，仅部分币种有源）',
  oi_value         DECIMAL(24,4) DEFAULT NULL COMMENT '持仓量',
  oi_change_pct    DECIMAL(10,4) DEFAULT NULL COMMENT '持仓量变化%',
  long_pct         DECIMAL(10,4) DEFAULT NULL COMMENT '多头占比%',
  short_pct        DECIMAL(10,4) DEFAULT NULL COMMENT '空头占比%',
  ls_ratio         DECIMAL(10,4) DEFAULT NULL COMMENT '多空比',
  fng_value        INT          DEFAULT NULL COMMENT '恐贪指数（全市场）',
  fng_class        VARCHAR(32)  DEFAULT NULL COMMENT '恐贪分级',
  sentiment_score  DECIMAL(10,4) DEFAULT NULL COMMENT '情绪评分 -100~100',
  sentiment_bias   VARCHAR(16)  DEFAULT NULL COMMENT '情绪倾向 bullish/bearish/neutral',
  sentiment_headline VARCHAR(255) DEFAULT NULL COMMENT '情绪一句话结论',
  factors_json     JSON         COMMENT '情绪因子明细',
  create_time      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_snap (symbol, snap_time),
  KEY idx_snap_time (snap_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯币种市场情报快照（资金费率/持仓/多空/情绪）';

-- 13) 二期可选：盘口主体画像快照（/api/tape/flow breakdown）
CREATE TABLE jarvis_tape_flow_snap (
  id             BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol         VARCHAR(32)  NOT NULL COMMENT '交易对',
  snap_time      DATETIME(3)  NOT NULL COMMENT '快照时间',
  window_min     INT          DEFAULT NULL COMMENT '统计窗口分钟数',
  verdict        VARCHAR(255) DEFAULT NULL COMMENT '综合判词',
  actor_json     JSON         COMMENT '各主体统计（retail/mid/inst/maker：usd/net_usd/long_pct/verdict_cn）',
  total_usd      DECIMAL(20,4) DEFAULT NULL COMMENT '窗口总成交额 USD',
  create_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_snap (symbol, snap_time),
  KEY idx_snap_time (snap_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯盘口主体画像快照（二期）';
```

**代码生成器适配说明**：全部表满足——单列 bigint 主键、表 COMMENT 非空、逐列 COMMENT、无 `qrtz_`/`gen_` 前缀。`autoRemovePre=false` 下类名形如 `JarvisRecoPlan`（如需去前缀，生成界面改 tablePrefix，属用户侧选择）。`direction`/`plan_status` 等枚举列如需字典下拉，在生成界面绑 dictType（建议字典：`jarvis_direction`、`jarvis_plan_status`、`jarvis_yes_no`（0否 1是，RuoYi 自带 sys_yes_no 值域 Y/N 与 TINYINT 0/1 列不匹配），初始化 SQL 见 T1 产出物清单）。

### 2.3 授权语句修订（替换方案 §4.1 中库名，并补新表）

```sql
CREATE USER 'jarvis_sync'@'%' IDENTIFIED BY '<32位随机密码>';
-- 13 张 jarvis_* 表逐一：
GRANT SELECT, INSERT, UPDATE ON `jiaweisi`.`jarvis_signal_state` TO 'jarvis_sync'@'%';
-- ……（jarvis_signal_change / jarvis_reco_plan / jarvis_intraday_prediction / jarvis_tape_bar /
--      jarvis_force_order_min / jarvis_market_snapshot / jarvis_snapshot / jarvis_outcome /
--      jarvis_position / jarvis_limit_order / jarvis_tape_flow_snap / jarvis_sync_state 同款）
```

> 本地阶段（localhost 直连）`REQUIRE SSL` 可不加；迁云后按方案 §4.2 选通道并加 `REQUIRE SSL` + 收紧来源 IP。

---

## 3. 同步架构设计

### 3.1 双通道模型

```
┌─ Mac mini ──────────────────────────────────────────────┐
│ 贾维斯 dashboard(7899?)/daemon      PostgreSQL 16 (jarvis)│
│        ▲ HTTP 拉取(通道B)               ▲ 只读增量(通道A)   │
│        │                               │                 │
│      jarvis_sync.py（launchd com.jarvis.sync 常驻）        │
│        │ force_orders.db(SQLite) ──分钟聚合──┤             │
│        ▼ 批量 upsert（本地 3306 → 迁云后 TLS/隧道）          │
│      MySQL `jiaweisi` 库 jarvis_* 镜像表                   │
└─────────────────────────────────────────────────────────┘
```

- **通道 A（DB 只读增量）**：经 `jarvis_db` 兼容层读源库（自动跟随 pg/SQLite 切换），游标增量，覆盖表 1/2/4/5/8/9/10/11；`force_orders` 独立 SQLite 只读聚合 → 表 6。
- **通道 B（HTTP API 快照）**：拉本机 dashboard API，覆盖表 3（/api/twelve/consensus）、表 7（/api/market-intel + /api/sentiment）、表 12（二期 /api/tape/flow）。

### 3.2 通道 B 的「一石二鸟」设计（解决 D4 问题）

同步器每 180s 逐币拉 `/api/twelve/consensus?symbol=X`：
1. 响应中的 `consensus.trade_plan` → 算 plan_hash（side/entry_zone/sl/tp1/tp2/position_pct 序列化后 sha256 截断 16 字节）→ 与该 symbol 最近一行比对，**变化才 INSERT** `jarvis_reco_plan`；
2. 该请求本身触发源侧重算路径 `record_batch` → `twelve_signal_state/changes` 持续有新数据 → 通道 A 的 fast 组随之有增量。**无需改动贾维斯任何代码**即保证"无人看界面也全天有数"。

注意：consensus 内部有 180s 缓存，同步器周期设 ≥180s 时源侧每轮至多重算一次，不产生额外压力（重算成本与用户开一次页面等价）。

### 3.3 频率分组与游标幂等（含新增表）

| 组 | 表 | 周期 | 游标 | 幂等写 |
|---|---|---|---|---|
| fast | signal_change | 5s | id 单调 | INSERT..ON DUP no-op |
| fast | signal_state | 5s | updated_ts + 5s 重叠 | uk 覆盖 upsert |
| mid | tape_bar | 60s | src_minute + 120s 重叠（源 30s flush，可能补写迟到分钟） | uk(symbol,src_minute) 覆盖 |
| mid | intraday_prediction | 60s | src_bar_ts + 重推 `hit IS NULL` 行（outcome/hit 为回填列） | PK=源 id 覆盖 |
| mid | force_order_min | 60s | 源 id 游标 → 本地整分钟桶重算 | uk(symbol,minute) 覆盖 |
| mid | snapshot / outcome / position / limit_order | 60s | 按方案 §2.2 原定 | 同方案 |
| api | reco_plan | 180s | 无游标（hash 判重） | uk(symbol,plan_hash) IGNORE |
| api | market_snapshot | 300s | 无游标（snap_time 分钟对齐） | uk(symbol,snap_time) IGNORE |
| heartbeat | sync_state | 每轮 | — | PK upsert |

其余照方案 §2.2-§2.6 原文执行：游标从不后退、批内失败整批重试、源表暂缺容忍（twelve_* 懒建）、显式列名禁 SELECT *、catch-up 追平模式、指数退避 1s→60s、flock 单实例、长连接+每轮 ping、源侧零写入纪律。

通道 B 附加约束：HTTP 超时 10s；dashboard 不可达（进程重启中）记 warn 心跳、下轮重试，**不算失败不告警**（阈值：连续 10 轮不可达才 error）；响应 `ok=false` 或字段缺失按 NULL 容忍写入（market-intel 的 oi/多空仅 BTCUSDT 有源、funding 仅 4 币，属正常）。

### 3.4 断线缓存补推

- 通道 A 天然断点续传（游标持久化 `~/.vibe-trading/sync/cursors.json`，原子写；恢复路径：本地 json → MySQL 心跳表 → 冷启动全量，同方案 §2.4）。
- 通道 B 快照类数据**断网期间的时点数据本就不可重建**（源侧只有当前态）：断网恢复后从当前时刻继续快照，缺口留白并在 `jarvis_sync_state.last_error` 标注断档区间；`reco_plan` 因 hash 判重，恢复首轮若计划有变自然补一行，无重复风险。
- MySQL 不可达时通道 B 最近一次快照暂存内存（每表 1 条），恢复后先写暂存再继续，避免整段丢失。

---

## 4. 配置项设计（满足"切云库不改代码"硬要求）

`~/.vibe-trading/sync/sync_config.json`（chmod 600，不进 git；模板文件 `sync_config.example.json` 进仓库）：

```json
{
  "mysql": {
    "host": "127.0.0.1", "port": 3306,
    "user": "jarvis_sync", "password": "<密码>",
    "database": "jiaweisi",
    "ssl_mode": "PREFERRED", "connect_timeout": 5
  },
  "dashboard_base_url": "http://127.0.0.1:7899",
  "symbols": null,
  "table_prefix": "jarvis_",
  "groups": {
    "fast": {"enabled": true, "period_s": 5},
    "mid":  {"enabled": true, "period_s": 60},
    "api":  {"enabled": true, "period_s": 180},
    "market_snapshot_period_s": 300
  },
  "batch_size": 5000, "exec_batch": 500,
  "retention_days": {"jarvis_signal_change": 180, "jarvis_tape_bar": 90, "jarvis_market_snapshot": 90, "jarvis_reco_plan": 365},
  "http_timeout_s": 10,
  "log": {"path": "~/.vibe-trading/sync/sync.log", "max_mb": 20, "backups": 3}
}
```

| 项 | 说明 |
|---|---|
| `mysql.*` | **唯一连接真源**。本地→云库切换 = 改 host/port/user/password（+`ssl_mode: REQUIRED`）后 `launchctl kickstart -k` 重启，零代码改动。可选环境变量 `JARVIS_SYNC_MYSQL_URL` 覆盖（调试用） |
| `dashboard_base_url` | 通道 B 基址；端口以 T0 确认结果为准（本机现存 7897/7899 两份日志） |
| `symbols` | null=自动跟随贾维斯 watchlist（读 `jarvis_config`）；亦可显式列表收窄 |
| `groups.*.enabled` | 分组开关：任一组可独立停用（如云库带宽紧张时先停 api 组） |
| `retention_days` | 镜像保留期，供归档任务读取（归档删除本身跑在 RuoYi quartz 或同步器低峰轮，二选一，T7 定稿） |

---

## 5. 分阶段任务拆解（供主控派发）

> 工期为单 agent 净工时估算。**T3 与 T4、T7 三者可并行**；其余按依赖串行。总净工时约 4 人天，双 agent 并行日历约 2.5 天。

| # | 任务 | 产出物 | 依赖 | 可并行 | 建议角色 |
|---|---|---|---|---|---|
| **T0** | 前置确认（0.25d）：①MySQL 可用凭据核实（root CLI 试连被拒，见 D5）＋建 `jarvis_sync` 账号权限的执行凭据；②dashboard 实际端口（7897/7899）；③用户确认"B种数据=币种市场数据"解读；④确认 `jiaweisi` 库允许新建 13 张 jarvis_* 表 | 确认结论清单（回执主控） | — | — | 主控问用户 + 任一 agent 佐证 |
| **T1** | MySQL 初始化脚本（0.5d）：13 张表 DDL 定稿（方案 8 张修订版 + 本文 5 张）＋账号/授权＋可选字典初始化（jarvis_direction/jarvis_plan_status 的 sys_dict SQL）＋执行与回滚说明 | `Vibe-Trading/sql/jarvis_mysql_init.sql`＋`jarvis_mysql_rollback.sql`（DROP 空表） | T0 | 否（后续基座） | 数据库工程（database-engineering） |
| **T2** | 同步器框架（1d）：`jarvis_sync.py` 骨架——config 加载校验、flock 单实例、游标持久化（原子写+三级恢复）、MySQL 长连接+ping+指数退避、分组调度循环、心跳写入、日志轮转；`com.jarvis.sync.plist`（KeepAlive）＋`sync_config.example.json` | 可空转运行的框架（无表接入）＋plist | T1（联调）；开发可与 T1 并行起步 | 与 T1 部分并行 | 后端 Python（backend-engineering + python-development） |
| **T3** | 通道 A 表接入（1d）：fast 组（signal_state/change）＋mid 组（tape_bar、intraday_prediction、snapshot、outcome、position、limit_order）＋force_orders 分钟聚合；逐表游标/幂等按 §3.3 | 8 张表同步模块＋各表拉取 SQL 显式列清单 | T2 | **∥T4 ∥T7** | 后端 Python |
| **T4** | 通道 B 接入（0.75d）：consensus 拉取→plan_hash 判重→reco_plan；market-intel+sentiment 组装→market_snapshot；HTTP 容错（超时/降级/连续失败阈值）；「一石二鸟」联动验证（拉 consensus 后 fast 组确实有增量） | 2 张表 API 同步模块 | T2 | **∥T3 ∥T7** | 后端 Python |
| **T5** | 冒烟测试（0.5d）：`_sync_smoketest.py`——游标推进/重叠窗幂等（连跑两轮行数不变）、kill -9 断点续传、MySQL 拒连退避、源表缺失容忍、plan_hash 判重、分钟桶聚合对账；对齐仓库既有 `_*_smoketest.py` 风格（无 pytest 依赖） | 冒烟脚本全 PASS 输出 | T3+T4 | 部分用例可提前写 | 测试工程（test-engineering） |
| **T6** | 联调验收（0.5d）：真实两侧跑通≥1h；行数对账；断网演练（停 MySQL 10min 恢复追平）；贾维斯无影响验证（同步器启停两态 dashboard P95 差异<5%）；launchd 装载、杀进程自愈 | 验收记录（对账数字+延迟+P95） | T1-T5 | — | 后端 Python + 测试 |
| **T7** | RuoYi 侧使用指引（0.25d，纯文档）：代码生成器导入 13 表步骤（含 tablePrefix/dictType 建议）、只读页面生成要点（去增删改按钮）、quartz 归档任务示例（`jarvisCleanTask` Bean + sys_job 配置 SQL）、心跳徽标查询 SQL | `Vibe-Trading/贾维斯-RuoYi同步-使用指引.md` | T1（表定稿） | **∥T3 ∥T4** | 文档/后端任一 |
| **T8** | code-audit 收口（0.25d）：对 T2-T4 代码按源侧零写入纪律、密钥不入仓、SQL 注入面（表名白名单）、异常路径复查 | 审计意见清单 | T3+T4 | — | 代码审查（code-audit） |

**里程碑**：T1+T2+T3(fast 组) = 12信号可远程查（最小可用）；+T3 全量+T4 = 用户点名四类数据全到位；+T5/T6 = 可交付；T7 后用户可自行出页面。

---

## 6. 验证方案

| 维度 | 方法 | 通过标准 |
|---|---|---|
| 数据正确性 | 逐表对账：源 `SELECT COUNT(*), MAX(游标列)` vs 镜像同款；force_order_min/tape_bar 抽 3 个分钟桶手工 SUM | 行数一致（重叠窗表允许镜像≥源）；聚合值一致 |
| 幂等性 | 清游标重跑一轮全量 | 镜像行数不变、update_time 变 |
| 断点续传 | kill -9 → 重启；删 cursors.json → 重启 | 无缺口无重复；从心跳表恢复成功 |
| 延迟 | 读 `jarvis_sync_state.lag_seconds` 连续 1h 采样 | fast<15s、mid<90s、api<400s（P95） |
| 贾维斯零影响 | 同步器启停两态各采样 20 次 `/api/twelve/signals` 耗时；daemon 日志 diff | P95 差异<5%；daemon 无新增错误 |
| 断网容错 | 停 MySQL 10min / 停 dashboard 5min | 恢复后 2min 内追平；进程不崩、无告警风暴 |
| RuoYi 可用性 | 代码生成器导入 jarvis_signal_state + jarvis_reco_plan 生成代码 | 编译通过、列表页可查、分页正常 |
| 资源占用 | `ps` 采样同步器 RSS/CPU | 常驻 <50MB、空闲 CPU<1% |

---

## 7. 开放问题与风险

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| Q1 | MySQL root 凭据 CLI 试连被拒（D5） | 阻塞 T1 执行 | T0 让用户给出可建表账号或修正密码；同步器自身用 jarvis_sync 专号 |
| Q2 | dashboard 端口 7897/7899 双日志并存 | 通道 B 基址 | T0 从 launchd plist / lsof 确认，写入 config |
| Q3 | "B种数据"语义推定为币种市场数据 | 表 7 设计方向 | 主控向用户一句话确认；若另有所指，快照表模式可平移 |
| Q4 | 云库迁移时间未定 | 传输通道选型（方案 §4.2） | 本地期用 localhost 直连；迁云触发时按方案 §4.2 选 WireGuard/autossh，仅改 config |
| Q5 | 足迹图/主体画像/深度等纯内存数据是否入库 | 二期范围 | 表 12 已预留；建议先跑一期看界面诉求再定 |
| Q6 | reco_plan 在源侧 plan_status=neutral 时无计划 | 快照有空洞 | 正常业务态：neutral 也插一行（side=NULL、plan_status=neutral），界面可区分"无计划"与"没同步" |

---

## 附录：数据源→镜像表映射速查

| 用户点名 | 源（文件:行号） | 镜像表 | 通道 | 周期 |
|---|---|---|---|---|
| 12信号 | twelve_signal_state/changes（`jarvis_signal_history.py:53-101`） | jarvis_signal_state / jarvis_signal_change | A | 5s |
| 推荐点位 | consensus.trade_plan（`jarvis_twelve_systems.py:1011-1133`，API） | jarvis_reco_plan | B | 180s |
| 推荐点位 | intraday_predictions（`jarvis_intraday_trader.py:69-93`） | jarvis_intraday_prediction | A | 60s |
| 推荐点位（辅） | snapshots（`jarvis_journal.py:51-74`） | jarvis_snapshot | A | 60s |
| 盘口 | tape_minute_bars（`jarvis_tape_classify.py:558-571`） | jarvis_tape_bar | A | 60s |
| 盘口 | force_orders（`jarvis_ws_stream.py:177-188`，SQLite） | jarvis_force_order_min | A(聚合) | 60s |
| 盘口（二期） | /api/tape/flow breakdown（纯内存） | jarvis_tape_flow_snap | B | 300s |
| B种/币种 | /api/market-intel + /api/sentiment（纯内存） | jarvis_market_snapshot | B | 300s |
| 其它高价值 | outcomes/paper_positions/limit_orders | jarvis_outcome / jarvis_position / jarvis_limit_order | A | 60s |
