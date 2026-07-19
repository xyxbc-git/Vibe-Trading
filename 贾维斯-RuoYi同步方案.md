# 贾维斯 → RuoYi 数据同步链路方案

> 版本：v1.0（2026-07-19）
> 范围：方案设计文档，不含代码改动。目标是把贾维斯（Vibe-Trading，Python FastAPI）的指标/信号/交易数据单向同步到 RuoYi-Vue 后台（MySQL），对外提供远程监控界面。
> 硬约束：贾维斯本体不对外暴露、**不能影响其正常运行**；RuoYi 界面查询响应速度要求严格；同步链路单向只写不回读。

---

## 0. 现状盘点（只读采集的真实证据）

### 0.1 生效后端：已经是 PostgreSQL，不是 SQLite

任务背景描述"数据在 SQLite"，但实测 **`~/.vibe-trading/db.json` 存在且已于 2026-07-08 切到 pg**：

```
url: postgresql://jarvis:***@127.0.0.1:5432/jarvis
backend: postgres:16.14 @ quantdinger-db (127.0.0.1:5432), db=jarvis, role=jarvis
```

`jarvis_db.connect()` 的分流逻辑：`use_pg() == True` 且路径为默认库时走 `PgConnection`，否则走 SQLite。因此：

- **当前生产数据事实源 = 本机 Docker 里的 PostgreSQL 16.14 `jarvis` 库**；
- 本地 `~/.vibe-trading/jarvis_journal.db`（308KB，7月10日后未更新）是切换前的旧副本，行数已落后（snapshots 436 vs pg 478、paper_positions 0 vs pg 804）；
- **同步器读源必须复用 `jarvis_db` 兼容层**（`import jarvis_db; jarvis_db.connect(DB_PATH)`），自动跟随后端切换，禁止硬编码只读 SQLite 文件——否则同步的是过期数据。

### 0.2 pg 侧表清单与量级（2026-07-19 实测）

| 表 | 行数 | 说明 |
|---|---|---|
| snapshots | 478 | 决策快照，UNIQUE(symbol, as_of_date)，日期跨度 2018-08-20 ~ 2026-07-19（含回填），BTCUSDT 419 行为主 |
| outcomes | 852 | 前向收益回填，PK(snapshot_id, horizon)，horizon ∈ {7,30} |
| paper_positions | 804 | 模拟仓：open 1 / closed 803；signal_tf 分布 15m:612、1h:153、4h:39 |
| intraday_predictions | 266 | 日内预测，UNIQUE(symbol, bar_ts) |
| limit_orders | 1 | 限价挂单 |
| wallet / wallet_ledger | 1 / 18 | 虚拟钱包与流水 |
| executions | 1 | 成交对账 |
| twelve_signal_state / twelve_signal_changes | **尚未建出** | 懒建表：首次 `record_batch()`（dashboard `/api/twelve/signals`、`/api/twelve/consensus` 缓存未命中重算路径）才 `CREATE TABLE IF NOT EXISTS`。同步器必须容忍源表暂缺 |

### 0.3 twelve 信号表结构（来自 `jarvis_signal_history.py` DDL，源码为准）

- `twelve_signal_state`：PK(symbol, tf, system)，列 name_cn/direction/strength/reasoning/levels_json/plan_json/updated_ts/changed_ts。**每次重算全量 upsert**（updated_ts 恒更新，changed_ts 仅实质变更时更新）。
- `twelve_signal_changes`：id 自增 PK，列 ts/symbol/tf/system/name_cn/prev_direction/new_direction/prev_strength/new_strength/change_kinds/summary/prev_json/new_json/price。**只在实质变更时追加**（方向翻转必记；强度绝对变化 ≥0.15；计划价相对变化 >0.2%；关键位仅附带）。索引 (symbol,tf,system,ts) 与 (ts)。
- 源侧自动裁剪：`MAX_CHANGE_ROWS = 50_000`，每小时最多 prune 一次，删最老流水。**同步器的拉取节奏远快于裁剪节奏，正常不会丢数据**；同步器宕机超过"50k 行写满周期"（按峰值 1 万行/天也有 5 天缓冲）才可能产生缺口，需缺口告警（见 §4.6）。

### 0.4 force_orders（强平流水）

- 独立 SQLite `~/.vibe-trading/jarvis_ws_force_orders.db`（**不走 jarvis_db 层，永远是 SQLite**），写入方 `jarvis_ws_stream.py`（币安 forceOrder WS 流，逐条 INSERT+commit）。
- 表结构：id 自增 PK，symbol/side/price/qty/avg_price/status/trade_time(epoch ms)/notional/raw(截断 2000 字符的原始 JSON)。索引 (symbol, trade_time)。
- 当前 0 行（WS 流近期未开或无强平），但行情剧烈时**单日可达数千~数万条**，且 raw 字段大。**结论：不同步原始行，只同步分钟级聚合**（见 §3.2 G7），原始明细留在本地，需要时通过阶段 4 之后再评估阈值明细同步。

### 0.5 运行形态

- launchd 常驻：`com.jarvis.dashboard`（FastAPI 界面/API）、`com.jarvis.daemon`（定时引擎）。同步器将以第三个独立 launchd 服务加入，与两者进程隔离。

---

## 1. 拓扑选型对比（3 案 + 重型方案否决说明）

### 方案① 旁路同步器进程（本地 Python 定时增量拉源库 → 写远程 MySQL）★ 推荐

```
┌─ Mac mini（本地）────────────────────────────┐        ┌─ 云服务器 ─────────────────┐
│ 贾维斯 dashboard/daemon                       │        │  RuoYi-Vue (Spring Boot)   │
│   │ 读写（不感知同步器）                        │        │    │ 只读 jarvis_* 镜像表    │
│   ▼                                          │        │    ▼                       │
│ PostgreSQL 16 (docker, 127.0.0.1:5432)       │        │  MySQL（jarvis_* 镜像表）    │
│   ▲ 只读增量拉取                               │        │    ▲                       │
│ jarvis_sync.py（launchd 常驻，游标增量）────────┼─TLS/隧道┼────┘ 批量 upsert           │
│ force_orders.db (SQLite) ──分钟聚合──┘         │        │  nginx+HTTPS → 公网浏览器   │
└──────────────────────────────────────────────┘        └────────────────────────────┘
```

独立进程经 `jarvis_db` 兼容层**只读**贾维斯生效后端（当前 pg；若用户删 db.json 回退 SQLite 也自动跟随），按表分组、分频率增量拉取，批量 upsert 写远程 MySQL；游标落本地 JSON 断点续传。

| 维度 | 评估 |
|---|---|
| 不影响贾维斯运行 | ★★★ 进程隔离；源侧只有低频只读 SELECT（pg MVCC 读不阻塞写；SQLite 回退时用 `mode=ro`+WAL 快照读）。同步器崩溃/断网/MySQL 宕机对贾维斯零影响 |
| 断网容错 | ★★★ 游标持久化，断点续传；恢复后分批追平，天然削峰 |
| 实时性 | ★★☆ 秒级（变更流水 5-10s 周期），监控场景足够 |
| 安全 | ★★☆ 需要一个出站 MySQL 通道（TLS/隧道 + 最小权限账号，见 §5）；贾维斯本体不暴露任何入站端口 |
| 运维成本 | ★★☆ 新增 1 个 launchd 服务 + 1 张心跳表监控；单文件脚本，无中间件 |
| 开发量 | 约 1 个脚本（jarvis_sync.py）+ MySQL DDL，RuoYi 侧零后端开发（直接代码生成器出页） |

### 方案② 贾维斯直接切 JARVIS_DB_URL 到云上 PG，RuoYi 读同一库

把 db.json 的 url 指到云上 PostgreSQL，贾维斯所有读写直接落云库；RuoYi 配第二数据源读它。

| 维度 | 评估 |
|---|---|
| 不影响贾维斯运行 | ★☆☆ **致命伤**。`jarvis_db` 是"每次操作开短连接、用完即关"模型（`__exit__` 即 close），跨公网后每次 `_conn()` 都吃一次完整 TCP+TLS+认证握手延迟；公网抖动/断网期间**信号计算、模拟盘、钱包记账全部阻塞或报错**，违反硬约束 |
| 断网容错 | ★☆☆ 断网 = 贾维斯功能不可用（虽有 try/except 静默，但等于数据停摆） |
| 实时性 | ★★★ 零同步延迟（同一份数据） |
| 安全 | ★☆☆ 云 pg 必须对贾维斯所在网络开放入站；交易决策数据的唯一事实源放到公网可达位置，攻击面最大 |
| 运维成本 | ★★☆ 省同步器，但 RuoYi-Vue 全家桶（MyBatis/代码生成器/PageHelper）默认按 MySQL 设计，接 pg 需引 pg 驱动 + dynamic-datasource 双数据源 + 生成器模板不兼容，隐性成本高 |
| 结论 | **否决**：与"不能影响正常运行"直接冲突，且事实源上云不可接受 |

### 方案③ 本地 HTTP 推送到 RuoYi 开放接口

本地推送器把增量打包成 JSON，POST 到 RuoYi 新开发的 `/api/jarvis/ingest/*` 接口（token 鉴权），RuoYi 落 MySQL。

| 维度 | 评估 |
|---|---|
| 不影响贾维斯运行 | ★★★ 同①，旁路进程 |
| 断网容错 | ★★☆ 同①可做游标，但幂等要靠 RuoYi 侧按业务键去重，两端都要写对 |
| 实时性 | ★★☆ 秒级，HTTP 批量开销略高于直写 MySQL |
| 安全 | ★★★ 最优：MySQL 完全不出内网，仅暴露一个受控 HTTPS ingest 端点；服务端可校验/限流/审计 |
| 运维成本 | ★☆☆ **最高**：RuoYi 侧要开发+维护一组 Controller/Service/防重放鉴权/批量落库，本地要维护重试与队列；接口契约变更两端联动 |
| 结论 | 备选。当"MySQL 通道打不通（云厂商不允许 3306/隧道）"或"后续要接多个消费方"时升级到此案；第一期不选，避免两端开发 |

### 重型 CDC（Debezium/Kafka、DataX、Canal）— 一并否决

- Canal 只支持 MySQL 作源，源是 pg/SQLite 用不上；Debezium(pg logical decoding)→Kafka→MySQL sink 链路组件 ≥3 个，单机 Mac mini 上运维成本与故障面完全不成比例；DataX 是批处理定位，秒级增量还得自己包调度。数据量级（日增 <2 万行）用 CDC 属于杀鸡用牛刀。

### 选型结论

**方案①旁路同步器**为推荐案：唯一同时满足"贾维斯零影响 + 断网容错 + 秒级监控 + RuoYi 零后端开发"的拓扑。方案③保留为安全升级路径（同步器的"写 MySQL"模块抽象成 sink，后续可平替为 HTTP sink，游标/增量逻辑全复用）。

---

## 2. 推荐案详细设计

### 2.1 MySQL 目标表 DDL（按 RuoYi 习惯：utf8mb4 / InnoDB / create_time·update_time / 注释齐全）

设计要点：

- 表名前缀 `jarvis_`，与 RuoYi 系统表（sys_*）隔离；单数命名对齐 RuoYi 代码生成器习惯。
- **幂等主键策略**：流水类表直接复用源库自增 id 作 PK（不设 AUTO_INCREMENT，同步天然幂等）；状态类表用业务唯一键 + 本地自增 id（代码生成器偏好单列 bigint 主键）。
- 源库 `system` 列改名 `system_code`（规避关键字与 MyBatis 生成器踩坑）；epoch 秒/毫秒原值保留（`src_*` 列，排序游标与对账用），同时冗余 DATETIME(3) 列供界面直读与索引。
- 价格类 DECIMAL(20,8)，比率/强度 DECIMAL(10,4)，大 JSON 用 JSON 类型（MySQL ≥5.7.8；若盘点结论是 5.7 之前版本则降级 TEXT——待另一 agent 的 RuoYi 版本盘点结果确认，DDL 默认按 MySQL 8.0 写）。
- 时间戳列 `create_time`/`update_time` 由 MySQL 自动维护，表示**镜像行落库/更新时间**（与业务时间区分），天然可做同步延迟审计。

```sql
-- 1) 信号当前态（720 行级别小表：symbols × tfs × 12 systems，全量 upsert）
CREATE TABLE jarvis_signal_state (
  id             BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol         VARCHAR(32)  NOT NULL COMMENT '交易对，如 BTCUSDT',
  tf             VARCHAR(8)   NOT NULL COMMENT '时间框架 5m/15m/30m/1h/4h/1d',
  system_code    VARCHAR(32)  NOT NULL COMMENT '信号系统标识（源列 system）',
  name_cn        VARCHAR(64)  DEFAULT NULL COMMENT '系统中文名',
  direction      VARCHAR(16)  DEFAULT NULL COMMENT '方向 bullish/bearish/neutral',
  strength       DECIMAL(10,4) DEFAULT NULL COMMENT '强度 0~1',
  reasoning      TEXT         COMMENT '推理说明',
  levels_json    JSON         COMMENT '关键位快照',
  plan_json      JSON         COMMENT '交易计划快照',
  src_updated_ts DECIMAL(16,3) DEFAULT NULL COMMENT '源 updated_ts（epoch 秒）',
  src_changed_ts DECIMAL(16,3) DEFAULT NULL COMMENT '源 changed_ts（epoch 秒）',
  updated_at     DATETIME(3)  DEFAULT NULL COMMENT '信号最近计算时间（源 updated_ts 换算）',
  changed_at     DATETIME(3)  DEFAULT NULL COMMENT '信号最近实质变更时间',
  create_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像首次落库时间',
  update_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '镜像更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_tf_sys (symbol, tf, system_code),
  KEY idx_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯十二系统信号当前态（镜像）';

-- 2) 信号变更流水（追加型，PK 直用源 id，幂等）
CREATE TABLE jarvis_signal_change (
  id             BIGINT       NOT NULL COMMENT '源库自增 id（直用，保证幂等）',
  signal_time    DATETIME(3)  NOT NULL COMMENT '变更时间（源 ts 换算）',
  src_ts         DECIMAL(16,3) NOT NULL COMMENT '源 ts（epoch 秒，游标列）',
  symbol         VARCHAR(32)  NOT NULL COMMENT '交易对',
  tf             VARCHAR(8)   NOT NULL COMMENT '时间框架',
  system_code    VARCHAR(32)  NOT NULL COMMENT '信号系统标识',
  name_cn        VARCHAR(64)  DEFAULT NULL COMMENT '系统中文名',
  prev_direction VARCHAR(16)  DEFAULT NULL COMMENT '变更前方向',
  new_direction  VARCHAR(16)  DEFAULT NULL COMMENT '变更后方向',
  prev_strength  DECIMAL(10,4) DEFAULT NULL COMMENT '变更前强度',
  new_strength   DECIMAL(10,4) DEFAULT NULL COMMENT '变更后强度',
  change_kinds   VARCHAR(128) DEFAULT NULL COMMENT '变更类型 JSON 数组字符串 direction/strength/plan/levels',
  summary        VARCHAR(512) DEFAULT NULL COMMENT '一句话摘要',
  prev_json      JSON         COMMENT '变更前完整快照',
  new_json       JSON         COMMENT '变更后完整快照',
  price          DECIMAL(20,8) DEFAULT NULL COMMENT '变更时价格',
  create_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '镜像落库时间',
  PRIMARY KEY (id),
  KEY idx_sym_tf_sys_time (symbol, tf, system_code, signal_time),
  KEY idx_signal_time (signal_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯信号变更流水（镜像，保留期长于源库 50k 裁剪）';

-- 3) 决策快照（PK 直用源 id；同日重录会 DO UPDATE，须整行覆盖）
CREATE TABLE jarvis_snapshot (
  id               BIGINT       NOT NULL COMMENT '源库自增 id',
  symbol           VARCHAR(32)  NOT NULL COMMENT '交易对',
  as_of_date       DATE         NOT NULL COMMENT '决策日',
  generated_at_utc VARCHAR(32)  DEFAULT NULL COMMENT '生成时间（UTC 字符串，保持源值）',
  price            DECIMAL(20,8) DEFAULT NULL COMMENT '决策时价格',
  conviction_score DECIMAL(10,4) DEFAULT NULL COMMENT '信心分',
  direction        VARCHAR(32)  DEFAULT NULL COMMENT '方向结论',
  position_pct     DECIMAL(10,4) DEFAULT NULL COMMENT '建议仓位%',
  dd_pct           DECIMAL(10,4) DEFAULT NULL COMMENT '回撤%',
  fng              INT          DEFAULT NULL COMMENT '恐贪指数',
  above_ma200      TINYINT      DEFAULT NULL COMMENT '是否在 MA200 上方',
  dd30_active      TINYINT      DEFAULT NULL COMMENT '30日回撤保护是否触发',
  stop_loss        DECIMAL(20,8) DEFAULT NULL COMMENT '止损价',
  take_profit      DECIMAL(20,8) DEFAULT NULL COMMENT '止盈价',
  decision_json    JSON         COMMENT '完整决策 JSON',
  src_created_ts   DECIMAL(16,3) DEFAULT NULL COMMENT '源 created_ts（游标列）',
  create_time      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  update_time      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_date (symbol, as_of_date),
  KEY idx_date (as_of_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯决策快照（镜像）';

-- 4) 前向收益（复合业务键，本地自增 id）
CREATE TABLE jarvis_outcome (
  id           BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  snapshot_id  BIGINT       NOT NULL COMMENT '快照 id（对应 jarvis_snapshot.id）',
  horizon      INT          NOT NULL COMMENT '前向窗口天数 7/30',
  fwd_date     DATE         DEFAULT NULL COMMENT '前向到期日',
  fwd_price    DECIMAL(20,8) DEFAULT NULL COMMENT '到期价格',
  fwd_ret_pct  DECIMAL(10,4) DEFAULT NULL COMMENT '前向收益%',
  correct      TINYINT      DEFAULT NULL COMMENT '方向是否踩对 1/0/NULL',
  src_evaluated_ts DECIMAL(16,3) DEFAULT NULL COMMENT '源 evaluated_ts（游标列，重评会更新）',
  create_time  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  update_time  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_snap_horizon (snapshot_id, horizon)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯前向收益追踪（镜像）';

-- 5) 模拟仓（PK 直用源 id；生命周期会变更 open→closed，整行覆盖）
CREATE TABLE jarvis_position (
  id                BIGINT       NOT NULL COMMENT '源库自增 id',
  symbol            VARCHAR(32)  NOT NULL COMMENT '交易对',
  status            VARCHAR(16)  NOT NULL COMMENT 'open/closed',
  side              VARCHAR(8)   NOT NULL COMMENT 'buy/sell',
  qty               DECIMAL(24,10) DEFAULT NULL COMMENT '数量',
  signal_tf         VARCHAR(8)   DEFAULT NULL COMMENT '触发信号时间框架',
  entry_date        DATE         DEFAULT NULL COMMENT '开仓日',
  entry_price       DECIMAL(20,8) DEFAULT NULL COMMENT '开仓价',
  stop_loss         DECIMAL(20,8) DEFAULT NULL COMMENT '止损价',
  take_profit       DECIMAL(20,8) DEFAULT NULL COMMENT '止盈价',
  time_stop_days    INT          DEFAULT NULL COMMENT '时间止损天数',
  conviction_score  DECIMAL(10,4) DEFAULT NULL COMMENT '开仓信心分',
  exit_date         DATE         DEFAULT NULL COMMENT '平仓日',
  exit_price        DECIMAL(20,8) DEFAULT NULL COMMENT '平仓价',
  exit_reason       VARCHAR(32)  DEFAULT NULL COMMENT '平仓原因 stop/take/time/signal/manual',
  realized_pnl_usdt DECIMAL(20,8) DEFAULT NULL COMMENT '已实现盈亏 USDT',
  realized_pnl_pct  DECIMAL(10,4) DEFAULT NULL COMMENT '已实现盈亏%',
  opened_at         DATETIME(3)  DEFAULT NULL COMMENT '开仓时间',
  closed_at         DATETIME(3)  DEFAULT NULL COMMENT '平仓时间',
  src_opened_ts     DECIMAL(16,3) DEFAULT NULL COMMENT '源 opened_ts（游标列）',
  src_closed_ts     DECIMAL(16,3) DEFAULT NULL COMMENT '源 closed_ts（游标列）',
  create_time       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  update_time       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_status (status),
  KEY idx_sym_opened (symbol, opened_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯模拟仓（镜像）';

-- 6) 限价挂单（PK 直用源 id；状态会流转 pending→filled/cancelled）
CREATE TABLE jarvis_limit_order (
  id             BIGINT       NOT NULL COMMENT '源库自增 id',
  symbol         VARCHAR(32)  NOT NULL,
  side           VARCHAR(8)   NOT NULL,
  limit_price    DECIMAL(20,8) NOT NULL COMMENT '限价',
  qty            DECIMAL(24,10) NOT NULL,
  notional_usdt  DECIMAL(20,8) DEFAULT NULL,
  status         VARCHAR(16)  NOT NULL COMMENT 'pending/filled/cancelled',
  stop_loss      DECIMAL(20,8) DEFAULT NULL,
  take_profit    DECIMAL(20,8) DEFAULT NULL,
  time_stop_days INT          DEFAULT NULL,
  created_date   DATE         DEFAULT NULL,
  filled_price   DECIMAL(20,8) DEFAULT NULL,
  filled_at      DATETIME(3)  DEFAULT NULL,
  cancelled_at   DATETIME(3)  DEFAULT NULL,
  position_id    BIGINT       DEFAULT NULL COMMENT '成交后关联 jarvis_position.id',
  note           VARCHAR(255) DEFAULT NULL,
  src_created_ts DECIMAL(16,3) DEFAULT NULL COMMENT '源 created_ts（游标列）',
  create_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  update_time    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯限价挂单（镜像）';

-- 7) 强平分钟聚合（不同步原始 force_orders，见性能预算）
CREATE TABLE jarvis_force_order_min (
  id            BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  symbol        VARCHAR(32) NOT NULL COMMENT '交易对',
  minute_ts     DATETIME    NOT NULL COMMENT '分钟桶（UTC，源 trade_time 向下取整）',
  order_cnt     INT         NOT NULL DEFAULT 0 COMMENT '强平笔数',
  buy_cnt       INT         NOT NULL DEFAULT 0 COMMENT '买方向笔数（空头被强平）',
  sell_cnt      INT         NOT NULL DEFAULT 0 COMMENT '卖方向笔数（多头被强平）',
  qty_sum       DECIMAL(24,10) DEFAULT NULL COMMENT '数量合计',
  notional_sum  DECIMAL(20,4) DEFAULT NULL COMMENT '名义价值合计 USDT',
  notional_max  DECIMAL(20,4) DEFAULT NULL COMMENT '单笔最大名义价值',
  create_time   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  update_time   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_sym_minute (symbol, minute_ts),
  KEY idx_minute (minute_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='强平流水分钟聚合（镜像）';

-- 8) 同步心跳/游标监控表（同步器唯一"回读"自己写的表，用于断点自愈与界面延迟徽标）
CREATE TABLE jarvis_sync_state (
  table_name    VARCHAR(64) NOT NULL COMMENT '逻辑表名',
  cursor_value  VARCHAR(64) DEFAULT NULL COMMENT '当前游标（id 或 epoch 秒）',
  last_run_at   DATETIME(3) DEFAULT NULL COMMENT '最近一次同步尝试',
  last_ok_at    DATETIME(3) DEFAULT NULL COMMENT '最近一次成功',
  last_error    VARCHAR(512) DEFAULT NULL COMMENT '最近错误（截断）',
  rows_total    BIGINT      NOT NULL DEFAULT 0 COMMENT '累计推送行数',
  lag_seconds   DECIMAL(12,3) DEFAULT NULL COMMENT '估算延迟：now - 已同步的最大业务时间',
  update_time   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (table_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='贾维斯同步链路心跳';
```

> wallet / wallet_ledger / executions / intraday_predictions 属低价值监控项，第一期不建镜像表（阶段 3 视界面需求增补，模式与上述一致：ledger 用 id 游标、wallet 单行 upsert）。

### 2.2 增量拉取策略（按表逐一定义游标与幂等）

| 镜像表 | 源表 | 游标 | 拉取 SQL 形态（源侧只读） | MySQL 幂等写 |
|---|---|---|---|---|
| jarvis_signal_change | twelve_signal_changes | **id 游标**（自增/BIGSERIAL 单调） | `WHERE id > :cur ORDER BY id LIMIT :batch` | `INSERT ... ON DUPLICATE KEY UPDATE id=id`（追加型 no-op） |
| jarvis_signal_state | twelve_signal_state | **updated_ts 游标 + 5s 重叠窗** | `WHERE updated_ts >= :cur - 5 ORDER BY updated_ts LIMIT :batch` | uk(symbol,tf,system_code) `ON DUPLICATE KEY UPDATE` 全值列覆盖 |
| jarvis_snapshot | snapshots | **created_ts 游标 + 近 7 天窗口重刷**（同日 DO UPDATE 的行无独立更新时间列，用短窗重刷兜底） | `WHERE created_ts > :cur - 604800 ORDER BY created_ts LIMIT :batch` | PK=源 id `ON DUPLICATE KEY UPDATE` 全值列覆盖 |
| jarvis_outcome | outcomes | **evaluated_ts 游标 + 60s 重叠**（重评会刷新 evaluated_ts，游标天然覆盖更新） | `WHERE evaluated_ts > :cur - 60` | uk(snapshot_id,horizon) 覆盖 |
| jarvis_position | paper_positions | **双游标 opened_ts / closed_ts + 全量重推 open 态**（open 行个位数，行会原地变更） | `WHERE opened_ts > :c1 - 60 OR closed_ts > :c2 - 60 OR status='open'` | PK=源 id 覆盖 |
| jarvis_limit_order | limit_orders | **created_ts 游标 + 全量重推非终态**（pending 行状态会流转） | `WHERE created_ts > :cur - 60 OR status='pending'` | PK=源 id 覆盖 |
| jarvis_force_order_min | force_orders（独立 SQLite，只读 URI 连接） | **id 游标**，读原始行在本地按 (symbol, minute) 聚合后写 | `WHERE id > :cur ORDER BY id LIMIT :batch` → 内存聚合 | uk(symbol,minute_ts) `ON DUPLICATE KEY UPDATE cnt=cnt+VALUES(...)` 不可行——**改为整分钟桶重算覆盖**：只聚合已完整过去的分钟（trade_time < 当前整分），同一分钟桶重复计算结果一致，直接覆盖，保证幂等 |

设计规则说明：

1. **单调 id 游标优先**（无更新语义的追加流水）；有"行会更新"的表一律"时间游标 + 重叠窗 + 全值列覆盖 upsert"，重叠窗把时钟毛刺、同秒并发、事务提交乱序都吸收掉，重复推送靠 upsert 幂等消化。
2. 游标**从不后退**：每批成功写入 MySQL 并 commit 后才推进游标；批内失败整批重试。
3. 源表暂缺（twelve_* 懒建）→ 捕获"表不存在"，记 warn、下轮重试，不算失败。
4. 源列显式点名（`SELECT id, ts, symbol, ...`），**禁止 SELECT ***：源侧未来 ALTER ADD COLUMN 不会打断同步；新列纳入同步时同步器与 DDL 同步升级。

### 2.3 同步周期（分表分频）

| 分组 | 表 | 周期 | 理由 |
|---|---|---|---|
| fast | signal_change、signal_state | **5s**（可配 5-10s） | 监控核心是"信号变了没有"；源侧 dashboard 缓存 60-120s，5s 轮询延迟对用户不可感知且源压力可忽略（两条索引命中的增量 SELECT） |
| slow | snapshot、outcome、position、limit_order | **60s** | 日级/小时级数据，60s 足够 |
| agg | force_order_min | **60s** | 只聚合完整分钟，60s 一轮天然对齐 |
| heartbeat | jarvis_sync_state | 每轮顺带 | 每组每轮结束 upsert 心跳 |

追平模式（catch-up）：某组积压 > batch 上限时，连续拉批（批间 sleep 50ms）直到批不满，再回归正常周期。断网数小时后恢复也能在分钟级追平且不冲击两侧。

### 2.4 断点续传

- 游标持久化到 `~/.vibe-trading/sync/cursors.json`，**原子写**（写临时文件 + `os.replace`），每次游标推进即落盘。
- 进程重启：读 cursors.json 恢复；文件缺失/损坏 → 从 `jarvis_sync_state.cursor_value`（MySQL 心跳表，同步器自己写的数据）恢复；两者皆无 → 冷启动全量：按游标零值分批拉全表（幂等 upsert，重复无害）。
- 这是对"单向只写不回读"的唯一豁免：**只读自己写的心跳表游标字段**，不读任何 RuoYi 业务数据；若要严格零回读，config 可关闭该恢复路径，改用"回看窗口重推"（代价：游标文件丢失时重推最近 N 天）。

### 2.5 失败重试 / 限流 / 资源约束

- 每组独立 try/except，单组失败不影响其他组；MySQL 连接失败按指数退避 1s→2s→4s→…→60s 封顶，恢复即归零。
- 每轮每组行数上限（默认 5000）+ executemany 批 500 行 + 批间 sleep，写侧限流；源侧读永远走索引增量，单查询毫秒级。
- 同步器进程加 `flock` 单实例锁（防 launchd 重复拉起叠加实例）；日志按大小截断轮转。
- MySQL 连接生命周期：每轮建连、轮末关闭（分钟级轮次）或保持长连接 + ping 重连均可，推荐**长连接 + 每轮 ping**，减少 TLS 握手开销。

### 2.6 单向性声明

- 同步账号无 DELETE/DROP/ALTER 权限（见 §4），代码中不出现任何对源库的 INSERT/UPDATE/DELETE——源侧连接以只读语义使用（pg 侧可再加 `default_transaction_read_only=on` 的账号级保险，见 §4.4）。
- RuoYi 侧对 jarvis_* 镜像表只读（页面查询），不写；镜像表数据修正的唯一途径是同步器重推。

---

## 3. 性能预算

### 3.1 日增量估算（按当前证据 + 放大系数）

| 表 | 当前证据 | 日增量估算（常态 / 峰值） | 年量级（峰值持续） |
|---|---|---|---|
| twelve_signal_changes | 表未建，源码变更判定阈值较严（方向翻转/强度≥0.15/计划价>0.2%） | 常态 200~2,000 行（≈7 symbols × 5-6 tf × 12 系统，仅实质变更）；剧烈行情峰值 ~1 万 | ≤365 万，实际远低 |
| twelve_signal_state | ≤ symbols×tf×12 ≈ 500~900 行**存量恒定**，只有 upsert 无增长 | 行数不增长，更新频次 = dashboard 重算频次 | 恒定小表 |
| snapshots | 478 行（含 2018 起回填） | 每 symbol 每日 1 行，≈7~10 行/日 | ~3,650 |
| outcomes | 852 行 | ≈2×snapshot 日增 | ~7,300 |
| paper_positions | 804 行（15m 策略贡献 612） | 常态 10~50 行/日 | ~1.8 万 |
| limit_orders / wallet_ledger | 1 / 18 行 | 个位数/日 | 忽略 |
| force_orders（原始，**不同步**） | 本地当前 0 行，WS 开启+行情剧烈时可日增数千~数万，raw 字段大 | 镜像侧只落分钟聚合：≤ 1440×symbols 行/日，实际只有有强平的分钟才有行，常态 <500 行/日 | 聚合 ≤18 万 |

**结论：MySQL 侧年数据量 < 500 万行 / < 2GB（含 JSON），单实例完全无压力。** 瓶颈永远不在容量，而在查询路径是否走索引。

### 3.2 索引 / 分区 / 归档策略

- G1 上述 DDL 的每张表索引都对着界面查询路径设计：流水页 `(symbol, tf, system_code, signal_time)` 前缀匹配 + `(signal_time)` 全局时间轴；仓位页 `(status)`、`(symbol, opened_at)`。**上线后跑一轮 EXPLAIN 验证**（实施清单 P3），出现 filesort/全表扫再调，不预加多余索引（写放大不划算）。
- G2 第一期**不做分区**：signal_change 年增 ≤365 万行远够不到分区收益线；预留策略——若未来 >500 万行且时间轴查询变慢，按月 RANGE 分区（PK 需改 (id, signal_time) 复合，属破坏性变更，届时用 expand-contract：建新分区表→双写→切读）。
- G3 归档：镜像表保留期默认 **180 天**（源侧 changes 仅 50k 行滚动，镜像已经是更长的历史），RuoYi 定时任务（或同步器顺带）每日低峰 `DELETE ... WHERE signal_time < NOW() - INTERVAL 180 DAY LIMIT 5000` 分批删；如需永久留档，先 `INSERT INTO ..._hist SELECT`。
- G4 force_orders：**只同步聚合的决定就是性能预算本身**——原始行含 2KB raw JSON，剧烈行情日数万行 × 公网传输 × MySQL 写放大，换来的只是界面上根本展示不完的明细；分钟聚合把它压缩 2~3 个数量级且完全满足"强平热力/爆仓脉冲"类监控可视化。确需明细时（阶段 4 后评估）再加"名义价值 ≥ $50k 才同步原始行"的阈值通道。
- G5 RuoYi 端查询规范：所有列表页强制分页（PageHelper limit ≤100）；时间范围条件默认带（近 7 天），杜绝无条件全表 COUNT；聚合看板（当日变更数、方向分布）如果慢，加 Redis 5~10s TTL 缓存（RuoYi 自带 RedisCache 工具类）——数据本身 5s 延迟，缓存 5s 不损失时效。
- G6 界面响应目标：列表页 P95 < 200ms（limit 20、索引命中、单表无 JOIN）；心跳徽标查询 jarvis_sync_state 单行 PK 读 <5ms。
- G7 同步器自身资源：常驻内存 <50MB、CPU 空闲期 <1%、fast 组单轮源查询 <10ms（索引增量）+ 一次批量 upsert 网络往返；对贾维斯宿主机影响可忽略。

### 3.3 源侧（贾维斯）保护

- pg 源：增量 SELECT 全部索引命中（changes 主键、state 无索引但表 <1000 行全扫也 <1ms；如 state 表未来变大，加 `(updated_ts)` 索引属于源侧改动，列为可选优化不在本方案强制）。MVCC 下只读查询不阻塞 dashboard/daemon 写入。
- SQLite 回退场景：同步器以 `sqlite3.connect("file:...?mode=ro", uri=True)` + `PRAGMA busy_timeout=2000` 打开，WAL 模式下读写互不阻塞；即便源库回退也零影响。
- force_orders SQLite：同样 ro URI 打开；WS 写入方是逐条 commit 的短事务，分钟聚合读只碰已完整分钟，无锁竞争窗口。

---

## 4. 安全设计

### 4.1 MySQL 最小权限账号

```sql
-- DBA 一次性执行（DDL 由 DBA/root 建，同步账号无 DDL 权限）
CREATE USER 'jarvis_sync'@'%' IDENTIFIED BY '<32位随机密码>' REQUIRE SSL;
-- MySQL 无表名通配符授权，逐表显式授予（仅 SELECT/INSERT/UPDATE，无 DELETE/DROP/ALTER）
GRANT SELECT, INSERT, UPDATE ON `ry-vue`.`jarvis_signal_state`    TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `ry-vue`.`jarvis_signal_change`   TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `ry-vue`.`jarvis_snapshot`        TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `ry-vue`.`jarvis_outcome`         TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `ry-vue`.`jarvis_position`        TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `ry-vue`.`jarvis_limit_order`     TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `ry-vue`.`jarvis_force_order_min` TO 'jarvis_sync'@'%';
GRANT SELECT, INSERT, UPDATE ON `ry-vue`.`jarvis_sync_state`      TO 'jarvis_sync'@'%';
```

- 库名以 RuoYi 实际库为准（另一 agent 盘点结论出来后替换 `ry-vue`）；SELECT 仅用于 upsert 语义与心跳恢复，若采用"零回读"模式可收窄为仅 jarvis_sync_state 有 SELECT。
- 归档 DELETE 由 RuoYi 侧定时任务用**另一个**运维账号执行，与同步账号隔离。
- `'%'` 建议收紧为固定出口 IP 或隧道内网段（见 4.2）。

### 4.2 传输通道（按优先级选一）

1. **WireGuard 隧道（推荐）**：Mac mini ↔ 云服务器建点对点 WG，MySQL 只监听 WG 内网地址（如 10.0.0.2:3306），公网 3306 完全不开。配置轻、断线自愈、内核级性能。
2. autossh 反向/正向 SSH 隧道：`autossh -L 13306:127.0.0.1:3306 云主机`，同步器连 127.0.0.1:13306。零新组件，依赖 sshd 稳定性。
3. 公网 3306 + TLS：仅当 1/2 均不可行。必须 `require_secure_transport=ON` + 账号 REQUIRE SSL + 防火墙白名单本地出口 IP + fail2ban。

任一通道下连接串都开 TLS（`ssl_mode=REQUIRED`），隧道内叠加 TLS 成本可忽略，纵深防御。

### 4.3 密钥存放

- `~/.vibe-trading/sync/mysql.json`（host/port/user/password/db/ssl 配置），`chmod 600`，属主 jolly；**不进 git 仓库、不写环境变量、不写 launchd plist**（plist 内容任何进程可读）。
- 与既有约定一致（db.json 同目录同模式）；密码 90 天轮换（DBA `ALTER USER ... IDENTIFIED BY`，同步器改配置重启，分钟级操作）。

### 4.4 源侧保险（可选加固）

- pg 侧为同步器单建只读角色：`CREATE ROLE jarvis_ro LOGIN PASSWORD '...' ; GRANT SELECT ON ALL TABLES IN SCHEMA public TO jarvis_ro; ALTER ROLE jarvis_ro SET default_transaction_read_only = on;`——同步器用它连 pg，代码 bug 也写不进源库。此项涉及源侧 DBA 操作，列为推荐项非阻塞项（第一期可直接复用 jarvis 账号 + 代码只读纪律）。

### 4.5 RuoYi 对外暴露要点

- **nginx 反代 + HTTPS**（Let's Encrypt/acme.sh 自动续期），仅开 443；HTTP 301 跳 HTTPS；HSTS。
- 上游只代理 RuoYi 前端静态资源与 `/prod-api/`（或实际 context-path）；**禁止**把 druid 监控页（`/druid/*`）、actuator、swagger 暴露公网（nginx location 直接 return 403，RuoYi 配置同时关闭）。
- RuoYi 自身：改默认 admin 密码为强密码、开验证码、登录失败锁定（自带）；token 有效期收敛（如 120min）；jarvis 监控页面按角色授权（建只读角色"监控访客"，仅授 jarvis 菜单查询权限，不给任何 sys_* 管理菜单）。
- nginx 加基础限速（`limit_req` 每 IP 10r/s burst 20）与 fail2ban 盯 401/404 爆破日志；后台管理路径可选 IP 白名单二道门。
- 数据敏感性评估：镜像数据是**模拟盘**信号与虚拟仓位，无真实资金账号/密钥，泄露风险等级中低；但决策信号有策略价值，仍按上述标准处理。

### 4.6 监控与告警

- 界面徽标：RuoYi 首页读 jarvis_sync_state，`lag_seconds > 300` 显示红色"同步延迟"。
- 缺口检测：同步器每轮校验 signal_change 游标连续性（源 `MIN(id) WHERE id > cursor` 与上批末 id 的差值 >1 且源侧 prune 已越过游标时记 error 心跳），提示可能存在被裁剪未同步的区间。
- 本地日志 `~/.vibe-trading/sync/sync.log`（含每轮各组行数/耗时/游标），大小轮转。

---

## 5. 实施清单（分阶段，每步带验证与回滚）

> 依赖注记：P0 的 MySQL 版本/库名/字符集以另一 agent 的 RuoYi 盘点结论为准；若 MySQL < 5.7.8，JSON 列降级 TEXT（DDL 已注明）。各阶段串行推进，任一阶段回滚不影响已上线的前序阶段。

### P0 通道与库表准备（0.5 天）

| 步骤 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| 0-1 | 建传输通道（WireGuard 或 autossh，见 §4.2） | 本地 `nc -z <隧道地址> 3306` 通；断开重连自愈 | 拆隧道，无任何业务影响 |
| 0-2 | DBA 在 RuoYi 库执行 §2.1 全部 DDL | `SHOW CREATE TABLE` 逐表核对；`SELECT` 空表成功 | `DROP TABLE jarvis_*`（空表零风险） |
| 0-3 | 建 `jarvis_sync` 账号 + §4.1 授权 | `SHOW GRANTS`；用该账号 TLS 连接执行 `INSERT`+`UPDATE` 试写 jarvis_sync_state 后清理；执行 `DELETE` 应被拒绝 | `DROP USER` |
| 0-4 | 本地写 `~/.vibe-trading/sync/mysql.json`（chmod 600） | `ls -l` 权限 600；测试脚本连通 | 删文件 |

### P1 同步器 MVP：信号两表（1 天）

| 步骤 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| 1-1 | 开发 `jarvis_sync.py`（fast 组：signal_state/signal_change；游标/退避/心跳/单实例锁/日志） | 冒烟：手动跑一轮，源行数 = 镜像行数；重复跑无重复行（幂等）；kill -9 后重启从游标续传 | 停脚本即回滚，两侧无残留影响 |
| 1-2 | 触发源表生成：访问一次 dashboard `/api/twelve/signals?symbol=BTCUSDT`（twelve_* 懒建表落地 pg） | pg `\dt twelve*` 两表存在且 state 有行 | 无需回滚（源侧正常业务行为） |
| 1-3 | launchd 装 `com.jarvis.sync`（KeepAlive、日志重定向、WorkingDirectory=Vibe-Trading、.venv python） | `launchctl list | grep jarvis.sync` 存活；杀进程 5s 内自动拉起；连续运行 1h 心跳 lag_seconds < 15 | `launchctl unload` + 删 plist |
| 1-4 | 贾维斯无影响验证 | 对照同步器启停两种状态下 dashboard `/api/twelve/signals` 响应耗时（各采样 20 次），P95 差异 < 5%；daemon 日志无新增错误 | — |

### P2 全量表接入（0.5 天）

| 步骤 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| 2-1 | slow 组接入：snapshot/outcome/position/limit_order | 各表源目标行数一致；改一条源侧 open 仓（等真实平仓事件）后 60s 内镜像状态翻转 | config 中禁用对应表分组 |
| 2-2 | agg 组接入：force_order_min（含 WS 流开启时的真实数据验证；WS 未开则用历史文件回放验证聚合正确性） | 抽 3 个分钟桶手工 SUM 对账 | 同上 |
| 2-3 | 断网演练：拔隧道 10 分钟再恢复 | 恢复后 2 分钟内追平（心跳 lag 归位）；贾维斯全程无感知 | — |

### P3 RuoYi 界面（1 天，RuoYi 侧）

| 步骤 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| 3-1 | 代码生成器导入 jarvis_* 表，生成只读查询页（列表+详情+时间/符号筛选），去掉增删改按钮与权限 | 页面可查；生成的 Controller 无写接口 | 删生成模块 |
| 3-2 | 首页加同步心跳徽标（读 jarvis_sync_state） | lag 正常绿色；手动停同步器 5 分钟变红 | 删组件 |
| 3-3 | 性能验收：列表页 EXPLAIN 全部索引命中；P95 < 200ms（limit 20）；慢的接口加 Redis 5~10s 缓存 | 压测 50 并发浏览无 >500ms | — |
| 3-4 | 建"监控访客"只读角色，jarvis 菜单最小授权 | 该角色登录仅见监控页，无系统管理入口 | 删角色 |

### P4 对外暴露（0.5 天，云服务器）

| 步骤 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| 4-1 | nginx + HTTPS（acme 证书 + 自动续期），仅开 443，反代 RuoYi；封 druid/actuator/swagger 路径 | SSL Labs A 级；访问 `/druid/` 得 403；80 仅跳转 | 关站点配置 |
| 4-2 | limit_req + fail2ban + 云安全组仅放行 443（及隧道端口） | 爆破模拟触发封禁；安全组核对 | 还原安全组 |
| 4-3 | 验收演练：外网手机访问监控页，信号变更 ≤15s 可见；同时确认贾维斯本体无任何公网可达端口（`nmap` 外部扫描核对） | 全链路 OK | 4-1/4-2 逐项回退 |

### 里程碑汇总

- P0+P1 完成 = 核心监控数据（信号态+变更史）已可远程消费（哪怕先用任意 MySQL 客户端看）。
- P2 完成 = 数据面全量。P3 完成 = 可视化。P4 完成 = 对外可用。总工期估算 3.5 人天。

---

## 6. 风险清单与开放问题

| # | 风险/开放问题 | 应对 |
|---|---|---|
| R1 | RuoYi MySQL 具体版本未定（另一 agent 盘点中） | DDL 按 8.0 写，5.7 需去 DATETIME(3) 之外无阻塞项（5.7 支持 fsp）；<5.7.8 JSON→TEXT。拿到版本后过一遍 DDL 即可 |
| R2 | 源侧 twelve_* 表结构未来演进（源码 ALTER ADD COLUMN 幂等模式） | 同步器显式列名 SELECT，新列不破坏既有链路；纳入同步走"先加镜像列（nullable）→ 升级同步器"两步 |
| R3 | 源库后端再次切换（pg ⇄ SQLite） | 同步器经 jarvis_db 层读，自动跟随；游标语义两端一致（id/epoch 秒），切换后首轮重叠窗自动对齐 |
| R4 | 同步器长时间宕机 + 源侧 changes 裁剪（50k 上限）导致缺口 | §4.6 缺口检测告警；峰值 1 万行/天也有 ≥5 天缓冲，配合 launchd KeepAlive 实际风险极低 |
| R5 | 时钟漂移影响时间游标 | 游标全部取**源库业务时间列**（updated_ts/evaluated_ts 等，由写入方进程赋值），不依赖两机时钟对齐；重叠窗 ≥5s 吸收毛刺 |
| R6 | 未来要同步 executions/wallet/intraday_predictions | 模式已覆盖（id 游标或 uk upsert），加表 = 配置 + DDL，不动架构 |

---

## 附录 A：同步器最小骨架（伪代码，实施阶段落地为 jarvis_sync.py）

```python
GROUPS = {
  "fast": {"period": 5,  "tables": ["signal_change", "signal_state"]},
  "slow": {"period": 60, "tables": ["snapshot", "outcome", "position", "limit_order"]},
  "agg":  {"period": 60, "tables": ["force_order_min"]},
}

def main_loop():
    acquire_flock_or_exit()                  # 单实例
    cursors = load_cursors_json_or_recover() # 本地 json → MySQL 心跳表 → 零游标冷启动
    mysql = connect_mysql_tls()              # 长连接，每轮 ping，断连指数退避重连
    while True:
        for group in due_groups(now()):
            for table in group.tables:
                try:
                    rows = pull_incremental(table, cursors[table])   # jarvis_db.connect() 只读；显式列名
                    if rows:
                        upsert_batch(mysql, table, rows, batch=500)  # ON DUPLICATE KEY UPDATE
                        cursors[table] = advance(rows)
                        save_cursors_atomic(cursors)
                    heartbeat(mysql, table, cursors[table], lag(rows))
                except SourceTableMissing:
                    warn_once(table)          # twelve_* 懒建前的正常状态
                except MySQLError as e:
                    backoff_and_reconnect(e)  # 游标不动，下轮重推
        sleep_until_next_due()
```

## 附录 B：本方案引用的源码/数据证据

- `jarvis_db.py`：后端分流（db.json / JARVIS_DB_URL / 默认 SQLite）、短连接语义、SQL 方言翻译。
- `jarvis_signal_history.py`：twelve 两表 DDL、变更判定阈值、50k 裁剪、写入路径。
- `jarvis_dashboard.py` L3638-3700：`/api/twelve/signals`（缓存 60/120s）与 `/api/twelve/consensus`（5 TF 逐一落库）为唯一写入点。
- `jarvis_journal.py`：snapshots/outcomes DDL 与 ON CONFLICT 更新语义。
- `jarvis_ws_stream.py` L171-230：force_orders 独立 SQLite、写入与读取路径。
- `jarvis_migrate_to_pg.py`：既有 SQLite→pg 迁移工具（佐证 pg 为当前事实源）。
- 实测：pg 16.14 表清单/行数/符号分布（§0.2）、本地两 .db 文件规模与新鲜度（§0.1/§0.4）。
