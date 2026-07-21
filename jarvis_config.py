#!/usr/bin/env python3
"""贾维斯 JARVIS - 统一交易配置中心（T-15 / Sprint0 配置中心化）。

把原先散落在各脚本里的「交易运行参数」集中成一份可改不改码的配置：
币种池、信心阈值、仓位上限、组合风险红线、止损/止盈/时间止损规则、
入场区间带、sizing 方式、熔断阈值、守护进程周期、服务端口、轮询间隔。

与 `jarvis_weights.py` 的分工：
  - jarvis_weights：**因子权重 + 方向阈值**（由 jarvis_retrain 自学习覆盖）。
  - jarvis_config ：**交易执行/风控/系统旋钮 + 币种池**（人工运营调参，改配置即生效）。

默认值（DEFAULTS）= 各脚本改造前的硬编码原值，因此「配置文件不存在 /
损坏 / 缺键」时行为与改造前**完全一致**（零回归、可一键回退）。

存储（Sprint0 起首选 YAML 分组文件，旧 JSON 只读兼容）：
  ~/.vibe-trading/config.yaml          # 首选：分组 trading/risk/signal/data/notify/system
  ~/.vibe-trading/jarvis_config.json   # 兼容：旧扁平 JSON，仍会读取（YAML 优先覆盖）

三层覆盖（后者覆盖前者）：
  内置 DEFAULTS → 旧 JSON → config.yaml → 环境变量 JARVIS_CFG_<KEY大写>

热加载：load() 内置 mtime 缓存——文件被外部修改后下一次调用即读到新值，
无需重启进程；mtime 未变时直接用缓存（省去每次磁盘 IO + 解析）。

设计原则（与 jarvis_weights 一致）：
  - 永不抛出：任何读取异常都回退内置默认（决策链不能被配置拖垮）。
  - 缺键补全：只覆盖配置里出现的键，其余用默认，便于增量演进。
  - 护栏夹紧：写入时把关键风控旋钮夹到安全区间，非法值回退默认。

用法：
  python jarvis_config.py show              # 查看当前生效配置（含来源 default/file/yaml）
  python jarvis_config.py get watchlist     # 取单个键
  python jarvis_config.py set min_conviction 0.85    # 改单个键（自动夹护栏）
  python jarvis_config.py diff              # 对比当前 vs 内置默认
  python jarvis_config.py init              # 生成带注释的 config.yaml 默认模板
  python jarvis_config.py reset             # 删除配置，恢复内置默认
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:  # pragma: no cover — venv 已带 PyYAML，此兜底仅防裸环境
    _HAS_YAML = False

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
CONFIG_PATH = os.path.join(CONFIG_DIR, "jarvis_config.json")
YAML_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")
ENV_PREFIX = "JARVIS_CFG_"

# ── 内置默认 = 各脚本改造前的硬编码原值（改这里会直接改变运行口径）────────────
DEFAULTS: dict = {
    # 币种池（与 jarvis_radar.DEFAULT_WATCHLIST 原值一致）
    "watchlist": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"],
    # 执行 / 雷达共享旋钮
    "min_conviction": 0.8,            # 偏多达标信心阈值（executor 护栏 + radar）
    "max_position_pct": 40.0,         # 单笔仓位上限%（与 brief 弱因子上限一致）
    "max_portfolio_risk_pct": 1.5,    # 组合最大风险红线%（仓位×止损幅度）
    "max_effective_pct": 50.0,        # 多币相关性有效敞口上限%（T-10）
    "account_equity_usdt": 1000.0,    # 账户权益（sizing 用）
    # 止损 / 止盈 / 时间止损规则（brief 原硬编码 0.90 / 1.08 / 30）
    "stop_loss_drop_pct": 10.0,       # 硬止损：入场价 ×(1-10%)
    "take_profit_pct": 8.0,           # 参考止盈：入场价 ×(1+8%)
    "time_stop_days": 30,             # 时间止损（因子是 30 天均值回归）
    # 入场区间带（brief 原 price*0.985 ~ price*1.005）
    "entry_band_below_pct": 1.5,      # 区间下沿：低于现价的百分比
    "entry_band_above_pct": 0.5,      # 区间上沿：高于现价的百分比
    # sizing（T-11 动态仓位）
    "sizing_method": "fixed",         # fixed=固定比例（默认，零回归）| kelly=分数凯利
    "kelly_fraction": 0.5,            # 分数凯利系数（0~1，越小越保守）
    # ── 合约仓位计算器（Task #3：小本金高杠杆风控建议，jarvis_position_calc）──
    # [Sprint1 T1.1] 杠杆默认 100→10（安全基线）、保证金默认 100%→10%（告别全押）；
    # 存量用户已保存的自定义值不受影响（文件层覆盖默认）。
    "poscalc_capital_usdt": 130.0,    # 合约本金（用户实际可亏总额）
    "poscalc_leverage": 10.0,         # 目标杠杆（1~125；默认 10x 安全基线）
    "poscalc_risk_pct": 1.0,          # 单笔风险占本金%（风险法 legacy 口径，默认 1）
    "poscalc_margin_pct": 10.0,       # 保证金占本金%（保证金法：名义=本金×该%×杠杆）
    # ── 4h 盘中引擎（jarvis_intraday_trader，扁平键便于 clamp 护栏）──────────
    "intraday_enabled": True,             # 总开关（关=心跳里跳过 4h 轮）
    "intraday_min_prob": 0.60,            # 开仓最低预测概率
    "intraday_risk_pct_per_trade": 1.0,   # 单笔风险占权益%（按止损距离反推仓位）
    "intraday_max_open_positions": 3,     # 同时最多持仓数
    "intraday_stop_atr_mult": 1.2,        # 止损 = 入场 ∓ 1.2×ATR
    "intraday_take_atr_mult": 1.8,        # 止盈 = 入场 ± 1.8×ATR
    "intraday_time_stop_bars": 6,         # 时间止损（6 根 4h = 24h）
    "intraday_cooldown_bars": 1,          # 平仓后同币冷却根数
    "intraday_max_consecutive_losses": 3, # 连亏 N 笔熔断停开仓
    # ── T-18 TradingAgents 辩论增强层（jarvis_debate，默认关=零回归）──────────
    "debate_enabled": False,              # 总开关（关=executor 完全跳过辩论层）
    "debate_mode": "warn",                # warn=只警示不拦单 | veto=反向结论否决下单
    "debate_timeout_sec": 300,            # 单次辩论子进程超时（LLM 多轮，量级分钟）
    # ── D1 回测成本口径（jarvis_factor_backtest 换仓单边滑点成本，bps）─────────
    # 10bps 与 jarvis_slippage.FALLBACK_BPS 同口径（保守兜底）；显式传 0 可复现旧零成本结果。
    "backtest_cost_bps": 10.0,
    # ── 共识交易计划 RR 门槛（jarvis_twelve_systems._aggregate_trade_plan）───────
    # 止盈按 RR ≥ 该值推导/验证；结构目标撑不起门槛时输出观望不硬造计划。
    # 主流风控最低 1:2；激进可调 3.0（=用户口径「止损 1/2~1/3」）。
    "plan_min_rr": 2.0,
    # ── M2 s5 大单流监控 whale tape（jarvis_whale_tape，aggTrade 分层聚合）──────
    "whale_tier1_usd": 100000.0,      # 大单一档：单笔成交额 ≥ 该值计入大单统计
    "whale_tier2_usd": 1000000.0,     # 巨单二档：单笔 ≥ 该值触发「超大单」异常事件
    "whale_window_min": 15,           # 滚动统计窗口（分钟）：净流/买卖比/占比口径
    "whale_seatbelt_enabled": True,   # 安全带可选因子：开仓方向逆大单净流时提醒
    # ── T1.6 大户 vs 全网多空比背离因子（jarvis_sentiment.score_top_divergence）──
    # 大户与全网多头占比反向（一方>50% 一方<50%）且占比差超该阈值（小数，
    # 0.15=15 个百分点）时，情绪层向大户方向加分（聪明钱背离信号）。
    "divergence_threshold": 0.15,
    # ── M2 s5 爆仓流实时面板（jarvis_liquidation，消费 forceOrder 流+历史库）────
    "liq_window_min": 60,             # 滚动统计窗口（分钟）
    "liq_large_usd": 50000.0,         # 大额爆仓事件阈值（单笔名义 USDT）
    "liq_cluster_window_s": 180,      # 爆仓簇检测滑窗（秒）：短时同向密集=行情加速
    "liq_cluster_min_count": 5,       # 滑窗内同向爆仓 ≥ 该笔数判定为簇
    # ── M2 s7 Delta 面板 AI 解读卡（jarvis_delta_explain）──────────────────────
    "ai_explain_enabled": True,       # 总开关（关=接口返回 disabled，前端隐藏入口）
    "ai_explain_cache_min": 5,        # 同 symbol+tf 解读结果缓存（分钟），防重复烧 token
    # ── T1.7 持仓陪伴条：止损接近度预警阈值（/api/positions 陪伴字段）─────────
    # 现价到止损的剩余距离占「入场→止损总距离」比例低于该百分比时预警变色。
    "sl_proximity_warn_pct": 30.0,
    # ── T1.3 手动下单计划确认层（desktop Trading 页，不阻断只提示）──────────────
    "min_rr_warning": 1.5,            # 手动下单盈亏比低于该值黄色警告（不拦单）
    "plan_confirm_enabled": True,     # 下单前确认层总开关（RR 校验 + 逆信号提示）
    # ── T1.4 平仓复盘行为标签集（desktop 平仓打标弹窗选项，可增删自定义）─────────
    "journal_tags": ["按计划止盈", "按计划止损", "恐慌割肉", "追高被套", "贪婪不止盈", "其它"],
    # ── Sprint0 新收编：组合级熔断（jarvis_circuit_breaker 原 DEFAULTS 原值）────
    "cb_drawdown_halt_pct": 20.0,     # 组合回撤熔断阈值%（权益从峰值回撤）
    "cb_position_loss_halt_pct": 25.0,  # 单仓亏损熔断阈值%
    "cb_flash_crash_24h_pct": 15.0,   # 持仓币 24h 闪崩熔断阈值%
    "cb_depeg_deviation_pct": 35.0,   # 稳定币脱锚熔断阈值%
    # ── Sprint0 新收编：守护进程 / 系统（原 argparse default 原值）──────────────
    "daemon_interval_hours": 24.0,    # jarvis_daemon 循环周期（小时）
    "dashboard_host": "127.0.0.1",    # jarvis_dashboard 监听地址
    "dashboard_port": 7899,           # jarvis_dashboard 监听端口（Electron 同步读取）
    # ── Sprint0 新收编：通知超时（jarvis_notify 原 request_timeout_s 原值）──────
    "notify_timeout_s": 15,           # TG/飞书等通知渠道 HTTP 超时（秒）
    # ── Sprint1 T1.1 杠杆安全化 ─────────────────────────────────────────────
    "default_leverage": 10.0,         # 下单链路默认杠杆（安全基线 ≤10x）
    "max_leverage_no_confirm": 20.0,  # 超过该杠杆前端必须二次确认（明示爆仓亏损额）
    # ── Sprint1 T1.2 止损隐蔽化（避开整数关口/摆动点扫单区）──────────────────
    "sl_avoid_round_levels": True,    # 开关：系统默认 SL 自动避开整数关口/前高前低
    "sl_atr_buffer_mult": 0.3,        # 避让缓冲 = 该系数 × ATR（方向远离扫单区）
    # ── Sprint1 T1.5 熔断冷静期 ─────────────────────────────────────────────
    "cooldown_hours": 4.0,            # 熔断触发后锁单时长（小时）；0=禁用冷静期
    # ── M2 s5 清算/止损密集区估计器（jarvis_liq_map）─────────────────────────
    # 杠杆档位权重："L:w" 逗号分隔；反映市场上各杠杆倍数的持仓占比经验分布
    "liq_leverage_weights": "5:0.1,10:0.3,25:0.3,50:0.2,100:0.1",
    "liq_magnet_warn_pct": 1.5,       # 现价距强磁吸位小于该% → 插针风险提醒
    "liq_map_seatbelt_enabled": True,  # 磁吸位提醒因子并入 seatbelt 输出
    # ── M2 s4 Binance WebSocket 实时数据地基（jarvis_ws_stream）──────────────
    "ws_enabled": True,               # 总开关：dashboard 启动时是否拉起 WS 客户端
    "ws_stream_kline": True,          # 订阅 K 线增量流
    "ws_stream_aggtrade": True,       # 订阅逐笔归集成交流（whale tape 依赖）
    "ws_stream_forceorder": True,     # 订阅强平单流（爆仓面板/清算校准依赖）
    "ws_stream_depth": True,          # 订阅盘口深度增量流（幌骗检测依赖）
    "ws_kline_interval": "1m",        # kline 流周期
    "ws_depth_speed": "250ms",        # depth 流推送频率
    "ws_buffer_size": 1000,           # 每流每币种环形缓冲容量（条）
    "ws_reconnect_base_s": 1.0,       # 重连指数退避起始秒
    "ws_reconnect_max_s": 60.0,       # 重连退避封顶秒
    "ws_force_order_persist": True,   # forceOrder 是否落 SQLite 保留历史
    # ── [风控篇 P0-2] 模拟盘成交摩擦（jarvis_paper_trader，双边生效，0=关闭）────
    "paper_fee_pct": 0.05,            # 单边手续费%（taker 近似；开/平各收一次）
    "paper_slippage_pct": 0.02,       # 单边滑点%（市价单成交价按方向变差；限价单不加）
    # ── [风控篇 P0-3] 平仓通知兜底 ─────────────────────────────────────────
    "notify_all_closes": True,        # True=所有平仓原因都发邮件（time/signal/manual 等）
    # ── [风控篇 P0-4] 12 系统自动跟盘红线 ──────────────────────────────────
    "twelve_max_open_positions": 4,   # 总持仓数上限（含全部未平仓，非仅 twelve 来源）
    "twelve_reopen_cooldown_min": 60, # 同币平仓后再开仓冷却（分钟；0=关闭）
}

# ── YAML 分组 schema：key → 组名（trading/risk/signal/data/notify/system）────────
# config.yaml 按组嵌套呈现；load 时拍平回扁平键，代码侧取值方式不变。
GROUPS: dict[str, str] = {
    # trading——交易执行旋钮
    "watchlist": "trading",
    "min_conviction": "trading",
    "max_position_pct": "trading",
    "account_equity_usdt": "trading",
    "entry_band_below_pct": "trading",
    "entry_band_above_pct": "trading",
    "sizing_method": "trading",
    "kelly_fraction": "trading",
    "poscalc_capital_usdt": "trading",
    "poscalc_leverage": "trading",
    "poscalc_risk_pct": "trading",
    "poscalc_margin_pct": "trading",
    "intraday_enabled": "trading",
    "intraday_max_open_positions": "trading",
    "intraday_cooldown_bars": "trading",
    "default_leverage": "trading",
    "max_leverage_no_confirm": "trading",
    "plan_confirm_enabled": "trading",
    "journal_tags": "trading",
    "paper_fee_pct": "trading",
    "paper_slippage_pct": "trading",
    "twelve_max_open_positions": "trading",
    "twelve_reopen_cooldown_min": "trading",
    # risk——风控红线
    "max_portfolio_risk_pct": "risk",
    "max_effective_pct": "risk",
    "stop_loss_drop_pct": "risk",
    "take_profit_pct": "risk",
    "time_stop_days": "risk",
    "intraday_risk_pct_per_trade": "risk",
    "intraday_stop_atr_mult": "risk",
    "intraday_take_atr_mult": "risk",
    "intraday_time_stop_bars": "risk",
    "intraday_max_consecutive_losses": "risk",
    "cb_drawdown_halt_pct": "risk",
    "cb_position_loss_halt_pct": "risk",
    "cb_flash_crash_24h_pct": "risk",
    "cb_depeg_deviation_pct": "risk",
    "plan_min_rr": "risk",
    "min_rr_warning": "risk",
    "sl_avoid_round_levels": "risk",
    "sl_atr_buffer_mult": "risk",
    "cooldown_hours": "risk",
    "sl_proximity_warn_pct": "risk",
    # signal——信号/决策层
    "intraday_min_prob": "signal",
    "debate_enabled": "signal",
    "debate_mode": "signal",
    "debate_timeout_sec": "signal",
    "divergence_threshold": "signal",
    "liq_window_min": "signal",
    "liq_large_usd": "signal",
    "liq_cluster_window_s": "signal",
    "liq_cluster_min_count": "signal",
    "ai_explain_enabled": "signal",
    "ai_explain_cache_min": "signal",
    "liq_leverage_weights": "signal",
    "liq_magnet_warn_pct": "signal",
    "liq_map_seatbelt_enabled": "signal",
    "whale_tier1_usd": "signal",
    "whale_tier2_usd": "signal",
    "whale_window_min": "signal",
    "whale_seatbelt_enabled": "signal",
    # data——数据/回测口径
    "backtest_cost_bps": "data",
    "ws_stream_kline": "data",
    "ws_stream_aggtrade": "data",
    "ws_stream_forceorder": "data",
    "ws_stream_depth": "data",
    "ws_kline_interval": "data",
    "ws_depth_speed": "data",
    "ws_buffer_size": "data",
    "ws_reconnect_base_s": "data",
    "ws_reconnect_max_s": "data",
    "ws_force_order_persist": "data",
    # notify——通知
    "notify_timeout_s": "notify",
    "notify_all_closes": "notify",
    # system——进程/端口
    "daemon_interval_hours": "system",
    "dashboard_host": "system",
    "dashboard_port": "system",
    "ws_enabled": "system",
}

# 组内注释（init 模板用；也是 Settings 页分组展示的口径说明）。
GROUP_COMMENTS: dict[str, str] = {
    "trading": "交易执行：币种池 / 信心阈值 / 仓位 / 入场带 / sizing / 4h 引擎开关",
    "risk": "风控红线：组合风险 / 止损止盈 / 时间止损 / 熔断阈值 / RR 门槛",
    "signal": "信号与决策：4h 开仓概率门槛 / TradingAgents 辩论层",
    "data": "数据与回测：回测滑点成本等口径",
    "notify": "通知：渠道超时（token/webhook 走 notify_config.json 或环境变量，不放这里）",
    "system": "系统：守护进程周期 / 仪表盘监听地址端口",
}

# 关键风控旋钮的安全区间（写入时夹紧；未列的键不夹）。
BOUNDS: dict[str, tuple[float, float]] = {
    "min_conviction": (0.0, 2.0),
    "max_position_pct": (0.0, 100.0),
    "max_portfolio_risk_pct": (0.1, 20.0),
    "max_effective_pct": (1.0, 100.0),
    "account_equity_usdt": (1.0, 1e9),
    "stop_loss_drop_pct": (0.5, 90.0),
    "take_profit_pct": (0.5, 500.0),
    "time_stop_days": (1, 3650),
    "entry_band_below_pct": (0.0, 50.0),
    "entry_band_above_pct": (0.0, 50.0),
    "kelly_fraction": (0.0, 1.0),
    "poscalc_capital_usdt": (1.0, 1e9),
    "poscalc_leverage": (1.0, 125.0),
    "poscalc_risk_pct": (0.1, 10.0),
    "poscalc_margin_pct": (1.0, 100.0),
    "intraday_min_prob": (0.5, 0.99),
    "intraday_risk_pct_per_trade": (0.1, 5.0),
    "intraday_max_open_positions": (1, 10),
    "intraday_stop_atr_mult": (0.3, 5.0),
    "intraday_take_atr_mult": (0.3, 10.0),
    "intraday_time_stop_bars": (1, 42),
    "intraday_cooldown_bars": (0, 12),
    "intraday_max_consecutive_losses": (1, 20),
    "debate_timeout_sec": (30, 1800),
    "backtest_cost_bps": (0.0, 800.0),  # 上限对齐 jarvis_slippage.MAX_ONE_WAY_BPS
    "plan_min_rr": (1.0, 10.0),         # RR 门槛安全区间（<1 无意义，>10 不现实）
    "min_rr_warning": (0.5, 10.0),      # 手动下单 RR 警告线（低于 0.5 无警示意义）
    "cb_drawdown_halt_pct": (1.0, 95.0),
    "cb_position_loss_halt_pct": (1.0, 95.0),
    "cb_flash_crash_24h_pct": (1.0, 95.0),
    "cb_depeg_deviation_pct": (1.0, 95.0),
    "daemon_interval_hours": (0.05, 168.0),  # 3 分钟 ~ 7 天
    "dashboard_port": (1024, 65535),
    "notify_timeout_s": (3, 120),
    "default_leverage": (1.0, 125.0),
    "max_leverage_no_confirm": (1.0, 125.0),
    "sl_atr_buffer_mult": (0.0, 2.0),
    "cooldown_hours": (0.0, 72.0),
    "divergence_threshold": (0.01, 0.60),   # 大户背离占比差阈值（0.15=15 个百分点）
    "whale_tier1_usd": (1000.0, 1e8),       # 大单一档下限：过低失去「大单」意义
    "whale_tier2_usd": (10000.0, 1e9),      # 巨单二档下限
    "whale_window_min": (1, 240),           # 滚动窗口 1 分钟 ~ 4 小时
    "sl_proximity_warn_pct": (5.0, 90.0),   # 止损接近度预警线（剩余距离占比%）
    "liq_window_min": (1, 1440),            # 爆仓统计窗口 1 分钟 ~ 24 小时
    "liq_large_usd": (100.0, 1e8),          # 大额爆仓阈值（单笔名义 USDT）
    "liq_cluster_window_s": (10, 3600),     # 簇检测滑窗（秒）
    "liq_cluster_min_count": (2, 100),      # 簇判定最小同向笔数
    "ai_explain_cache_min": (1, 120),       # AI 解读缓存 1 分钟 ~ 2 小时
    "ws_buffer_size": (100, 100_000),
    "ws_reconnect_base_s": (0.5, 30.0),
    "ws_reconnect_max_s": (5.0, 600.0),
    "liq_magnet_warn_pct": (0.1, 10.0),
    "paper_fee_pct": (0.0, 1.0),             # 单边费率%（0=关闭；>1% 不现实）
    "paper_slippage_pct": (0.0, 2.0),        # 单边滑点%（0=关闭）
    "twelve_max_open_positions": (1, 20),
    "twelve_reopen_cooldown_min": (0, 1440),  # 0=关闭 ~ 24 小时
}

# 允许的枚举键。
ENUMS: dict[str, tuple[str, ...]] = {
    "sizing_method": ("fixed", "kelly"),
    "debate_mode": ("warn", "veto"),
    "ws_kline_interval": ("1m", "3m", "5m", "15m", "1h"),
    "ws_depth_speed": ("100ms", "250ms", "500ms"),
}


def default_config() -> dict:
    """返回内置默认配置的深拷贝（外部可安全修改）。"""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["meta"] = {"version": 0, "updated_at": None, "source": "builtin-default", "note": ""}
    return cfg


# 保留原文大小写的 list 键（自由文本标签）；watchlist 等币种列表仍统一大写。
_TEXT_LIST_KEYS = {"journal_tags"}


def _coerce(key: str, value):
    """把值按默认键的类型温和转换；失败则原样返回（由调用方决定取舍）。"""
    dv = DEFAULTS.get(key)
    try:
        if isinstance(dv, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "开")
            return bool(value)
        if isinstance(dv, int) and not isinstance(dv, bool):
            return int(value)
        if isinstance(dv, float):
            return float(value)
        if isinstance(dv, list):
            keep_case = key in _TEXT_LIST_KEYS
            if isinstance(value, list):
                items = [str(x).strip() for x in value if str(x).strip()]
            else:
                items = [s.strip() for s in str(value).split(",") if s.strip()]
            return items if keep_case else [s.upper() for s in items]
        return value
    except Exception:  # noqa: BLE001
        return value


def clamp(key: str, value):
    """把数值键夹到安全区间；枚举键校验；其余原样返回。永不抛出。"""
    v = _coerce(key, value)
    try:
        if key in ENUMS:
            return v if v in ENUMS[key] else DEFAULTS.get(key)
        if key in BOUNDS and isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = BOUNDS[key]
            v2 = max(lo, min(hi, v))
            return int(v2) if isinstance(DEFAULTS.get(key), int) else round(float(v2), 6)
    except Exception:  # noqa: BLE001
        return DEFAULTS.get(key, value)
    return v


def _flatten_yaml(raw: dict) -> dict:
    """把 config.yaml 的分组嵌套结构拍平成扁平键。

    支持两种形态混用：
      - 分组：{"risk": {"stop_loss_drop_pct": 12}}   （标准形态）
      - 扁平：{"stop_loss_drop_pct": 12}             （容错：手写省略分组也认）
    未知组名下的键与顶层未知键均原样保留（前向兼容）。
    """
    flat: dict = {}
    group_names = set(GROUPS.values())
    for k, v in (raw or {}).items():
        if k == "meta":
            continue
        if k in group_names and isinstance(v, dict):
            flat.update(v)
        else:
            flat[k] = v
    return flat


def _apply_env_overrides(cfg: dict) -> list[str]:
    """环境变量覆盖：JARVIS_CFG_<KEY大写>=value（如 JARVIS_CFG_MIN_CONVICTION=0.9）。

    只认 DEFAULTS 中存在的键；返回被覆盖的键列表（供 meta.source 标注）。
    """
    hit: list[str] = []
    for k in DEFAULTS:
        env_val = os.environ.get(ENV_PREFIX + k.upper())
        if env_val is not None and env_val != "":
            cfg[k] = clamp(k, env_val)
            hit.append(k)
    return hit


# mtime 缓存：{(json_path, yaml_path): (json_mtime, yaml_mtime, env_sig, cfg)}
_LOAD_CACHE: dict[tuple, tuple] = {}


def _mtime(p: str) -> float:
    try:
        return os.stat(p).st_mtime
    except OSError:
        return -1.0


def load(path: str | None = None, yaml_path: str | None = None,
         apply_env: bool = True) -> dict:
    """读取生效配置；缺失/损坏/缺键回退默认。永不抛出。

    - `load()`：三层覆盖 DEFAULTS → 旧 JSON → config.yaml → 环境变量（生产口径）。
    - `load(某路径)`：**严格模式**——只读指定 JSON 文件，不叠加全局 YAML/env
      （显式指定路径 = 调用方要的就是这个文件；smoketest/工具场景依赖此语义）。
    - `apply_env=False`：跳过环境变量层（写盘前合并用，避免把 env 固化进文件）。
    热加载：按文件 mtime 缓存，外部改文件后下一次调用即读到新值；
    mtime 未变时直接返回缓存深拷贝（调用方可安全修改返回值）。
    返回结构恒含全部默认键 + meta；文件里多出的未知键保留（前向兼容）。
    """
    strict = path is not None and yaml_path is None
    p = path or CONFIG_PATH
    yp = "" if strict else (yaml_path or YAML_CONFIG_PATH)
    use_env = apply_env and not strict
    env_sig = () if not use_env else tuple(sorted(
        (k, os.environ.get(ENV_PREFIX + k.upper(), "")) for k in DEFAULTS
        if os.environ.get(ENV_PREFIX + k.upper())
    ))
    cache_key = (p, yp, use_env)
    cached = _LOAD_CACHE.get(cache_key)
    jm, ym = _mtime(p), (_mtime(yp) if yp else -1.0)
    if cached and cached[0] == jm and cached[1] == ym and cached[2] == env_sig:
        return copy.deepcopy(cached[3])

    cfg = default_config()
    sources: list[str] = []

    # 第 1 层：旧 JSON（兼容存量部署）
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k == "meta":
                        if isinstance(v, dict):
                            cfg["meta"].update(v)
                        continue
                    cfg[k] = clamp(k, v) if k in DEFAULTS else v
                sources.append("json")
            else:
                cfg["meta"]["source"] = "default(bad-shape)"
    except Exception:  # noqa: BLE001 — 配置异常绝不能拖垮决策
        cfg["meta"]["source"] = "default(load-error)"

    # 第 2 层：config.yaml（Sprint0 首选，覆盖 JSON；严格模式跳过）
    try:
        if yp and _HAS_YAML and os.path.exists(yp):
            with open(yp, "r", encoding="utf-8") as f:
                raw_y = _yaml.safe_load(f)
            if isinstance(raw_y, dict):
                if isinstance(raw_y.get("meta"), dict):
                    cfg["meta"].update(raw_y["meta"])
                for k, v in _flatten_yaml(raw_y).items():
                    cfg[k] = clamp(k, v) if k in DEFAULTS else v
                sources.append("yaml")
    except Exception:  # noqa: BLE001 — YAML 损坏时保留 JSON/默认，不拖垮
        sources.append("yaml-error")

    # 第 3 层：环境变量（最高优先级，便于容器/一次性实验覆盖；严格/写盘模式跳过）
    if use_env:
        env_hit = _apply_env_overrides(cfg)
        if env_hit:
            sources.append("env:" + ",".join(env_hit))

    if sources:
        cfg["meta"]["source"] = "+".join(sources)

    _LOAD_CACHE[cache_key] = (jm, ym, env_sig, copy.deepcopy(cfg))
    return cfg


def get(key: str, default=None, path: str | None = None):
    """取单个配置项；缺失时回退（显式 default > 内置默认）。"""
    cfg = load(path)
    if key in cfg:
        return cfg[key]
    return default if default is not None else DEFAULTS.get(key)


def get_all(path: str | None = None) -> dict:
    """取全部生效配置（含 meta）。"""
    return load(path)


def save(updates: dict, *, source: str = "manual", note: str = "", path: str | None = None) -> dict:
    """合并写入若干配置项：自动夹护栏 + 累加 version + 记录来源/时间。原子落盘。

    Sprint0 起：若 config.yaml 已存在（或系统装有 PyYAML 且未显式传 path），
    写入走 YAML 分组文件，保证「单一事实来源」；显式传 path 时维持旧 JSON
    行为（smoketest 与存量调用兼容）。
    """
    if path is None and _HAS_YAML:
        return save_yaml(updates, source=source, note=note)
    p = path or CONFIG_PATH
    prev = load(p)  # 显式 path=严格模式：只读该文件，不叠加全局 YAML/env
    cfg = {k: v for k, v in prev.items() if k != "meta"}
    for k, v in (updates or {}).items():
        cfg[k] = clamp(k, v) if k in DEFAULTS else v
    cfg["meta"] = {
        "version": int(prev.get("meta", {}).get("version", 0) or 0) + 1,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "note": note,
    }
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)  # 原子替换，避免半写坏配置
    _LOAD_CACHE.clear()
    return cfg


def to_grouped(flat: dict | None = None) -> dict:
    """把扁平配置转成分组嵌套结构（Settings 页 / config.yaml 的呈现形态）。"""
    cfg = flat if flat is not None else load()
    grouped: dict = {g: {} for g in sorted(set(GROUPS.values()))}
    for k, v in cfg.items():
        if k == "meta":
            continue
        g = GROUPS.get(k)
        if g:
            grouped[g][k] = v
        else:
            grouped.setdefault("misc", {})[k] = v  # 未知键归 misc，保留不丢
    grouped["meta"] = cfg.get("meta", {})
    return grouped


def save_yaml(updates: dict, *, source: str = "yaml", note: str = "",
              yaml_path: str | None = None) -> dict:
    """合并写入 config.yaml（分组结构、原子落盘、夹护栏、version 累加）。"""
    yp = yaml_path or YAML_CONFIG_PATH
    prev = load(yaml_path=yp, apply_env=False)  # env 只在运行时生效，不固化落盘
    cfg = {k: v for k, v in prev.items() if k != "meta"}
    for k, v in (updates or {}).items():
        cfg[k] = clamp(k, v) if k in DEFAULTS else v
    meta = {
        "version": int(prev.get("meta", {}).get("version", 0) or 0) + 1,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "note": note,
    }
    grouped = to_grouped({**cfg, "meta": meta})
    os.makedirs(os.path.dirname(yp), exist_ok=True)
    tmp = yp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _yaml.dump(grouped, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp, yp)
    _LOAD_CACHE.clear()
    return {**cfg, "meta": meta}


def init_yaml_template(yaml_path: str | None = None, *, force: bool = False) -> str:
    """生成带注释的 config.yaml 模板（已存在且未 force 时不覆盖）。

    值 = 当前生效值（含存量 jarvis_config.json 的自定义项，迁移无损）；
    注释 = 内置默认 + 安全范围/枚举，便于对照回退。
    """
    yp = yaml_path or YAML_CONFIG_PATH
    if os.path.exists(yp) and not force:
        return yp
    cur = load(apply_env=False)  # 收编存量 JSON 值；env 只在运行时生效不落盘
    lines: list[str] = [
        "# 贾维斯统一配置中心（Sprint0）",
        "# 分组：trading / risk / signal / data / notify / system",
        "# 改保存即热生效（后端 mtime 检测，无需重启）；非法值自动夹到安全区间。",
        "# 覆盖优先级：内置默认 < 旧 jarvis_config.json < 本文件 < 环境变量 JARVIS_CFG_<KEY大写>",
        "",
    ]
    for g in ("trading", "risk", "signal", "data", "notify", "system"):
        lines.append(f"# ── {GROUP_COMMENTS.get(g, g)}")
        lines.append(f"{g}:")
        for k, grp in GROUPS.items():
            if grp != g:
                continue
            dv = DEFAULTS[k]
            cv = cur.get(k, dv)
            hint_parts = []
            if cv != dv:
                hint_parts.append(f"默认 {json.dumps(dv, ensure_ascii=False)}")
            if k in BOUNDS:
                lo, hi = BOUNDS[k]
                hint_parts.append(f"范围 {lo}~{hi}")
            elif k in ENUMS:
                hint_parts.append(f"可选 {' | '.join(ENUMS[k])}")
            hint = f"  # {'；'.join(hint_parts)}" if hint_parts else ""
            if isinstance(dv, list):
                lines.append(f"  {k}: {json.dumps(cv if isinstance(cv, list) else dv)}{hint}")
            elif isinstance(dv, bool):
                lines.append(f"  {k}: {str(bool(cv)).lower()}{hint}")
            elif isinstance(dv, str):
                lines.append(f"  {k}: \"{cv}\"{hint}")
            else:
                lines.append(f"  {k}: {cv}{hint}")
        lines.append("")
    os.makedirs(os.path.dirname(yp), exist_ok=True)
    tmp = yp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, yp)
    _LOAD_CACHE.clear()
    return yp


def reset(path: str | None = None) -> bool:
    """删除配置文件（JSON + YAML），恢复内置默认。返回是否真的删了文件。"""
    removed = False
    for p in (path or CONFIG_PATH, YAML_CONFIG_PATH if path is None else None):
        if p and os.path.exists(p):
            os.remove(p)
            removed = True
    _LOAD_CACHE.clear()
    return removed


def diff_from_default(path: str | None = None) -> dict:
    """当前生效配置相对内置默认的差异（只列变化项），用于审计。"""
    cur = load(path)
    out: dict = {}
    for k, dv in DEFAULTS.items():
        cv = cur.get(k, dv)
        if cv != dv:
            out[k] = {"default": dv, "current": cv}
    out["meta"] = cur.get("meta")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯统一交易配置中心")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="查看当前生效配置")
    g = sub.add_parser("get", help="取单个键")
    g.add_argument("key")
    s = sub.add_parser("set", help="改单个键（自动夹护栏）")
    s.add_argument("key")
    s.add_argument("value")
    sub.add_parser("diff", help="对比当前 vs 内置默认")
    ini = sub.add_parser("init", help="生成带注释的 config.yaml 默认模板")
    ini.add_argument("--force", action="store_true", help="已存在也覆盖重建")
    sub.add_parser("reset", help="删除配置，恢复内置默认")
    args = ap.parse_args()

    if args.cmd == "show":
        print(json.dumps(load(), ensure_ascii=False, indent=2))
    elif args.cmd == "get":
        print(json.dumps(get(args.key), ensure_ascii=False))
    elif args.cmd == "set":
        if args.key not in DEFAULTS:
            print(f"⚠️ 未知配置键 '{args.key}'（仍写入，但不在内置默认表）")
        cfg = save({args.key: args.value}, source="cli-set", note=f"set {args.key}")
        print(f"✅ 已写入 {args.key} = {cfg.get(args.key)}（version {cfg['meta']['version']}）")
    elif args.cmd == "diff":
        print(json.dumps(diff_from_default(), ensure_ascii=False, indent=2))
    elif args.cmd == "init":
        if not _HAS_YAML:
            print("⚠️ PyYAML 未安装，无法生成 config.yaml")
            return 1
        existed = os.path.exists(YAML_CONFIG_PATH)
        out = init_yaml_template(force=args.force)
        if existed and not args.force:
            print(f"（config.yaml 已存在，未覆盖：{out}；--force 可重建）")
        else:
            print(f"✅ 已生成带注释的默认模板: {out}")
    elif args.cmd == "reset":
        print("✅ 已删除配置，恢复内置默认" if reset() else "（配置本就不存在，当前即内置默认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
