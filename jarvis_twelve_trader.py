#!/usr/bin/env python3
"""贾维斯 JARVIS — 十二系统 × 六时间轴 槽位级模拟交易引擎（Vibe-Trading 台账）。

概念：配置币种（如 ETHUSDT）后，每个 tf×system 槽位（6×12=72）开一个独立虚拟
钱包（默认本金 100U），按 `twelve_signal_state` 的当前态信号自动开平仓记台账，
用于统计「哪个信号系统 / 哪个时间轴」胜率最高。

边界（硬约束）：
  - **只读** twelve_signal_state：绝不触发 12 信号重算、绝不改 jarvis_twelve_systems；
  - paper-only：不碰真钱、不打真实交易所，现价复用 jarvis_paper_trader.latest_price
    同款通道（Agent Gateway /price，失败回退 brief 因子价）；
  - 独立进程运行（CLI loop / daemon --twelve-sim），绝不挂进 dashboard 请求路径。

交易规则（每轮 run_cycle，平仓判定优先级 爆仓 > 止损 > 止盈 > 时间止损；
2026-08-06 R3 重构：点位触发才成交 + 信号变更失效留痕 + 已成交仓位独立）：
  1. 盯盘：持仓槽位先用「自开仓以来已收盘 bar 的 high/low」判断 爆仓(liq)/
     止损(sl)/止盈(tp) 盘中触碰（影线也算；同 bar 双触按保守取 SL；结算价=触发位；
     K 线取不到时优雅回退快照现价比对，缺口按更差价结算）；
  2. 已成交仓位独立（R3 规则3）：持仓后同槽位信号再变化（反向/转中性/点位更新）
     **绝不影响已成交仓位**——不 flip 平仓、不跟随更新 SL/TP，仅记 applied=0
     留痕日志；持仓只按自身 liq/sl/tp/timeout 生命周期退出。反向信号作为
     **新的独立计划**评估（同槽位可并存 1 多 + 1 空 + 1 pending）；
     同方向信号视为同一观点延续，不重复建仓；
  3. 时间止损：持仓超过 TF 分档上限（5m/15m:1天 30m:2天 1h:3天 4h:7天 1d:14天）
     → 以现价平仓(exit_reason=timeout)；
  4. 开仓（R3 规则1：点位触发才算成交）：信号 bullish 开多 / bearish 开空；
     neutral 不动；只要 plan 给出有效 entry 点位就必须比对实时价格：
     - entry_type=breakout/pullback：沿用方向性触达口径（见 4.5）；
     - entry_type=market（或缺 entry_type）且带有效 entry：按「点位或更优」限价
       口径（做多 现价≤点位 / 做空 现价≥点位）；已处于可成交侧 → 按现价立即
       成交（现有业务口径）；未触达 → 挂计划(pending) 等触达；
     - 计划缺失 / entry 非法 → 无点位可比，维持现状按现价立即开仓；
     - pending 不建持仓、不动钱包、不进胜率、不写 twelve_sim_trade；
  4.5 计划盯盘（每轮，先于持仓开仓处理）：
     - 触达判定用「自计划创建以来已收盘 bar 的 high/low ∪ 快照现价」（同 _exit_check
       口径）：breakout 多 high≥entry / 空 low≤entry；pullback 多 low≤entry / 空 high≥entry；
       market 按限价口径（多 low≤entry / 空 high≥entry，成交价取点位与现价更优侧）；
     - 触达即「成交」：以 **plan['entry'] 价**转持仓（entry_price=entry、entry_ts=成交
       时刻），SL/TP/杠杆/qty 经 _resolve_entry_params 基于 entry 价合成；
       成交落 twelve_sim_signal_log 留痕（change_kinds=fill）；
     - 未成交期间信号 entry/SL/TP 实质变化（≥0.2%）→ 更新计划点位并记
       twelve_sim_signal_log（position_id 关联 pending 行）；
     - R3 规则2：pending 期间同级别信号变更（反向/转 neutral/计划消失/槽位停用/
       挂单超 TF 超时档）→ **旧计划失效**：行保留 status='canceled' + cancel_reason
       + canceled_ts 留痕（看板可见「已失效」），并落 twelve_sim_signal_log
       （change_kinds=cancel）；失效后价格再触达旧点位也不成交，一切以最新信号
       为准（撤销判定先于触达成交判定）；canceled 行保留 7 天后清理
       （日志表留痕永久）；全程不产生 twelve_sim_trade；
  4.8 开仓门禁链（2026-08-06 亏损止血 S1+）：
     - 信号级前置门禁 _pre_gate（开仓/挂计划/成交时刻都先过）：
       S4 周期门禁——twelve_tf_enabled 停用的 TF 全拒、信号强度低于
       twelve_tf_min_confidence 该 TF 置信档（5m 默认 0.75）拒 'tf_gate'；
       S5 逆势过滤——1h 威科夫 dist-C/D/E 拒多、acc-C/D/E 拒空
       （twelve_trend_filter_enabled，仅 5m/15m/30m 生效，复用 jarvis_wyckoff
       进程内缓存；数据 stale/不可用放行不阻塞）拒 'counter_trend'；
       S3 战绩熔断——
       滚动窗口（twelve_cb_window 笔、恢复时刻后）胜率 < twelve_cb_min_winrate
       且净亏超 twelve_cb_max_loss → 该 信号×周期 熔断（twelve_sim_breaker 表
       持久化，只推信号不开仓）；冷却 twelve_cb_cooldown_hours 期满半开放行
       1 笔试探单，盈利恢复（战绩窗口重起算）/ 否则续熔断重计冷却；
       熔断/半开/恢复事件落 signal_log（change_kinds=breaker）可审计；
     - 参数级门禁 _risk_gate（合成参数后）：止损最小距离（twelve_min_sl_pct
       按 TF 分层）、最小盈亏比（twelve_min_rr）、费用负担
       （twelve_fee_burden_mult × 双边费用）不满足 → 拒单不入场；
     - 两级门禁拒单统一落 status='rejected' + reject_reason 行 + signal_log
       留痕（不静默丢弃，S6 归因报表可按原因聚合）；挂单在成交时刻同样再验一次；
  5. 参数：止损/止盈/杠杆/仓位% 优先用 twelve_sim_config 覆盖
     （优先级 信号级 > tf组级 > 币种级），否则用 plan_json 系统推荐；杠杆双兜底：
     配置/plan 均未给时按止损距离自动推荐（S1 解耦：打到止损亏≈保证金25%
     twelve_auto_lev_loss_frac，夹 [1, TF 分层上限 twelve_max_leverage]；
     显式杠杆尊重显式值但同样夹 TF 上限）；
  6. 逐仓口径：margin = balance × position_pct%，qty = margin × leverage / entry；
     爆仓价 = entry × (1 ∓ 1/leverage)，触发即以爆仓价强平 pnl=-margin；
     开/平双边手续费按名义单边 0.05%（jarvis_config: twelve_sim_fee_pct 可配）
     折进净 pnl；资金费模拟（S7）：持仓每满 8h 按 entry 名义 × twelve_funding_rate
     计提一次（rate>0 多头付/空头收），折进净 pnl 并单列 trade.funding_fee 留痕；
     爆仓不另计费；单笔最大亏损钳到 -margin（不倒欠）。

六张本地表（经 jarvis_db 兼容层懒建，pg 可切）：
  twelve_sim_wallet     槽位虚拟钱包 + 累计战绩（UNIQUE symbol,tf,system）
  twelve_sim_position   计划/在途持仓（status pending计划未成交 / open持仓 /
                        closed已平 / canceled已失效；pending 行 entry_price=计划
                        入场价、entry_ts=计划创建时刻、qty/margin=0 延迟到成交时
                        按 entry 价计算回填；canceled 行带 cancel_reason/canceled_ts）
  twelve_sim_trade      平仓台账流水（exit_reason tp/sl/flip(历史)/timeout/liq；
                        只有「成交→平仓」才写，计划的建/撤/改不落此表）
  twelve_sim_signal_log 信号变更留痕日志（计划建/撤/成交、pending 点位跟随、
                        持仓期间信号漂移 applied=0 留痕；关联 position_id）
  twelve_sim_config     参数配置（scope_tf/scope_system 可 NULL 分层覆盖；
                        内容由 RuoYi 同步链路回读落地，本模块只负责
                        建表 + 读取 + 提供 upsert_config 函数）
  twelve_sim_breaker    信号×周期战绩熔断器状态（S3：tripped/probing/recovered
                        + trip_count/reset_ts，进程重启熔断态不丢）

用法：
  python jarvis_twelve_trader.py config-set ETHUSDT                # 启用币种（币种级配置）
  python jarvis_twelve_trader.py config-set ETHUSDT --tf 1h --leverage 3
  python jarvis_twelve_trader.py config-list
  python jarvis_twelve_trader.py cycle                             # 跑一轮（读配置币种）
  python jarvis_twelve_trader.py cycle --symbols ETH,BTC           # 跑一轮（显式币种）
  python jarvis_twelve_trader.py run --interval-min 5              # 常驻循环（独立进程）
  python jarvis_twelve_trader.py status --symbol ETHUSDT           # 槽位战绩榜
"""

from __future__ import annotations

import argparse
import json
import os
import time

import jarvis_db as jdb

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")
LOG_PATH = os.path.join(DB_DIR, "jarvis_twelve_trader.log")

TFS = ("5m", "15m", "30m", "1h", "4h", "1d")
SYSTEMS = ("turtle", "dow", "elliott", "volatility", "gann", "chanlun",
           "rule123", "gap", "martingale", "oscillator", "triple_rsi", "arbitrage")
NAME_CN = {
    "turtle": "海龟交易", "dow": "道氏理论", "elliott": "艾略特波浪",
    "volatility": "波动率系统", "gann": "江恩时间窗", "chanlun": "缠论",
    "rule123": "123法则", "gap": "跳空缺口", "martingale": "马丁格尔",
    "oscillator": "摆动震荡", "triple_rsi": "三重平滑RSI", "arbitrage": "套利系统",
}

DEFAULT_PRINCIPAL = 100.0     # 槽位默认本金（U）
DEFAULT_POSITION_PCT = 10.0   # 无配置且计划未给时的默认仓位%（占槽位余额）
MIN_BALANCE = 1.0             # 余额低于此视为爆仓槽位，不再开新仓
DEFAULT_FEE_PCT = 0.05        # 单边手续费%（按名义；jarvis_config: twelve_sim_fee_pct 可覆盖）

# 各 TF 时间止损（天），口径对齐 jarvis_paper_trader._TWELVE_TIME_STOP
TF_TIME_STOP_DAYS = {"5m": 1, "15m": 1, "30m": 2, "1h": 3, "4h": 7, "1d": 14}

# 自动杠杆推荐（2026-08-06 亏损止血 S1，杠杆与止损解耦）：打到止损时目标亏损占
# 保证金比例（旧 0.5 顶格制造「窄止损×20×」出血点，现降 0.25），杠杆上限按 TF
# 分层封顶；均可经 jarvis_config（twelve_auto_lev_loss_frac / twelve_max_leverage）
# 热加载覆盖，MAX_AUTO_LEVERAGE 为任何配置都不放行的绝对硬顶。
AUTO_LEV_SL_LOSS_FRAC = 0.25
MAX_AUTO_LEVERAGE = 20.0
MAX_LEVERAGE_BY_TF = {"5m": 5.0, "15m": 8.0, "30m": 10.0,
                      "1h": 12.0, "4h": 15.0, "1d": 20.0}

# 止损最小距离门禁（S1，单位 %，按 TF 分层；jarvis_config: twelve_min_sl_pct）：
# SL 距离低于该 TF 下限 → 拒单 reject_reason='sl_too_tight'——R10 取证 5m 中位
# SL 距离 0.172% 在噪声带内，sl 平仓 228 笔胜率仅 4.4%，窄止损单不再入场。
MIN_SL_PCT_BY_TF = {"5m": 0.5, "15m": 0.7, "30m": 1.0,
                    "1h": 1.2, "4h": 2.0, "1d": 3.0}
# D5 ATR 自适应止损下限：静态档之上叠波动率自适应档——SL 距离 < N×该 TF
# ATR14% → 拒单 'sl_below_atr'（止损埋在噪声带内，扫损概率极高）。
# 0=关闭；ATR 取数失败自动放行（可用性优先，静态档仍兜底）。
SL_ATR_MULT_DEFAULT = 1.5

# 拒单(rejected)留痕行保留天数（与 canceled 同哲学：窗口期可复盘，到期物理清理；
# twelve_sim_signal_log 的 reject 留痕永久保留）
REJECTED_RETENTION_DAYS = 7

# 费用感知期望值门禁（S2）：最小盈亏比 + 止盈须覆盖 N 倍双边费用——R10 取证
# 总费 77.64U = 净亏 44%，止盈太近的单赢了也在喂手续费。
MIN_RR_DEFAULT = 1.5
FEE_BURDEN_MULT_DEFAULT = 3.0

# 信号×周期战绩熔断器（S3）默认：滚动窗口内 胜率<下限 且 净亏超阈值 → 熔断
# 只推信号不开仓；冷却期满半开放行 1 笔试探，赢了恢复（窗口重起算）输了续熔断。
# R10 取证：elliott 胜率 7% 连亏 38 笔不停——弱组合必须自动止血。
CB_WINDOW_DEFAULT = 30
CB_MIN_TRADES_DEFAULT = 10
CB_MIN_WINRATE_DEFAULT = 15.0
CB_MAX_LOSS_DEFAULT = 10.0
CB_COOLDOWN_HOURS_DEFAULT = 24.0

# 周期再平衡（S4）：TF 开关 + 按 TF 信号强度下限——R10 取证 5m 占 202/406 笔
# 亏 -57.7U、费用占该轴亏损 66%；5m 保留验证价值但只放行高置信（strength≥0.75）信号。
TF_ENABLED_DEFAULT = {"5m": 1, "15m": 1, "30m": 1, "1h": 1, "4h": 1, "1d": 1}
TF_MIN_CONF_DEFAULT = {"5m": 0.75, "15m": 0.0, "30m": 0.0,
                       "1h": 0.0, "4h": 0.0, "1d": 0.0}

# 高周期趋势逆势过滤（S5）：1h 威科夫 dist-C/D/E 拒多、acc-C/D/E 拒空，仅短周期
# 生效——R10 取证 short -123.2U vs long -53.1U（取证窗口 ETH 上行，全程逆势做空）。
TREND_FILTER_ENABLED_DEFAULT = True
TREND_FILTER_TFS = ("5m", "15m", "30m")
TREND_FILTER_PHASES = ("C", "D", "E")

# 资金费率模拟（S7）：持仓每满 8h 按 entry 名义计提一次（永续合约口径补齐）；
# rate>0 多头付/空头收，平仓折进净 pnl 并单列 funding_fee 留痕（正=支出 负=收入）。
FUNDING_RATE_DEFAULT = 0.0001
FUNDING_INTERVAL_HOURS = 8.0

# 13诊断 D0：开仓时刻市场环境快照（诊断实验场基础设施）。开仓那一刻把市场
# 环境写进 position 行，平仓时原样拷入 trade 行——归因从「哪个信号亏」升级为
# 「哪个信号在什么环境亏」。取数失败落 NULL，绝不阻塞开仓主链路。
# context_tags/size_factor 为 D2-D6 上下文标签/降权系数预留列（D0 只建列：
# tags 恒 NULL、factor 恒 1.0，语义由后续任务接管）。
CTX_SNAPSHOT_ENABLED_DEFAULT = True
CTX_CACHE_TTL_DEFAULT = 300.0          # regime/ATR 取数限频（秒）
CTX_COLUMNS_DDL = (
    ("ctx_regime", "TEXT"),        # 市场状态 trending/ranging/breakout
    ("ctx_regime_dir", "TEXT"),    # 状态方向 bullish/bearish/neutral
    ("ctx_atr_pct", "REAL"),       # 该 TF ATR14 相对收盘价（%）
    ("ctx_vol_bucket", "TEXT"),    # 波动率分档 low/mid/high（窗口内分位）
    ("ctx_wyckoff", "TEXT"),       # 1h 威科夫语境 side-phase（如 acc-C）
    ("ctx_funding", "REAL"),       # 该币最新 8h 资金费率（正=多头付）
    ("ctx_oi_btc_chg", "REAL"),    # BTC OI 变化%（全市场杠杆水位代理口径）
    ("ctx_hour_utc", "INTEGER"),   # 开仓 UTC 小时（0-23，时段归因）
    ("ctx_btc_trend", "TEXT"),     # BTC 1h regime 方向（带动过滤诊断）
    ("context_tags", "TEXT"),      # D2+ 上下文标签（逗号串）
    ("size_factor", "REAL"),       # D2+ 降权系数乘积（1.0=无降权）
)
CTX_FIELDS = tuple(c for c, _ in CTX_COLUMNS_DDL if c not in
                   ("context_tags", "size_factor"))

# 13诊断 D2+：信号侧上下文层（装眼睛+打标签+降权，绝不拒单/关闭——诊断实验场
# 纪律：错误环境降权继续攒数据，稳定亏组合本身就是候选反向 alpha）。
# D2 量能/CVD 突破确认：突破/追价类信号量能不确认 → vol_suspect 降权。
CTX_DEWEIGHT_SUSPECT_DEFAULT = 0.5
CTX_VOL_SYSTEMS_DEFAULT = ("turtle", "rule123", "gap", "dow", "chanlun")
CTX_MIN_SIZE_FACTOR = 0.05    # 降权系数连乘下限：绝不降到 0（=变相关闭断样本）
# D3 多周期趋势/regime 上下文：均值回归系统在趋势市逆势 / 突破系统在震荡市 /
# 短周期逆 1h 威科夫 → 打标降权。S5 逆势过滤增加 mode 开关：默认 deweight
# （打标降权继续跑，本层承接），reject 档回退旧硬拒单行为（零回归通道）。
TREND_FILTER_MODE_DEFAULT = "deweight"
CTX_DEWEIGHT_REGIME_DEFAULT = 0.5
CTX_DEWEIGHT_COUNTER_DEFAULT = 0.5
CTX_MEANREV_SYSTEMS_DEFAULT = ("oscillator", "triple_rsi")
# D4 funding/OI 拥挤度：资金费率热且顺拥挤方向 → crowded_side 降权（拥挤侧遇
# 反向清算级联最受伤）；叠加 OI 24h 激增 → 追加 crowded_hot 再乘一次系数；
# 反拥挤侧 → contrarian_side 纯标记（factor=1，归因对照组）。
CTX_FUNDING_HOT_DEFAULT = 0.0005      # 每 8h 费率绝对值阈值
CTX_OI_SURGE_PCT_DEFAULT = 5.0        # OI 24h 激增阈值（%）
CTX_DEWEIGHT_CROWDED_DEFAULT = 0.6
# D6 S3/S4 门禁模式化：熔断/低置信拒单=样本断流（诊断实验场最怕），默认
# deweight 打标降权继续跑；reject 回退旧硬拒单（零回归通道）。TF 显式停用
# （twelve_tf_enabled=0）是运营指令，两种 mode 下都保持硬拒。
CB_MODE_DEFAULT = "deweight"
TF_GATE_MODE_DEFAULT = "deweight"
CB_DEWEIGHT_DEFAULT = 0.25
TF_DEWEIGHT_DEFAULT = 0.5
CONTEXT_TAG_CN = {
    "vol_suspect": "量能/CVD 不确认突破（假突破嫌疑）",
    "vol_confirmed": "量能/CVD 确认突破",
    "osc_in_trend": "均值回归系统在趋势市逆势开仓（趋势市毒药语境）",
    "breakout_in_range": "突破系统在震荡市开仓（假突破高发语境）",
    "counter_trend": "逆 1h 威科夫高周期趋势（短周期逆势）",
    "crowded_side": "顺资金费拥挤方向开仓（拥挤侧清算级联风险）",
    "crowded_hot": "拥挤侧叠加 OI 激增（杠杆快速堆积）",
    "contrarian_side": "逆资金费拥挤方向开仓（反拥挤侧对照组）",
    "breaker_deweight": "信号×周期战绩熔断中（降权观察继续攒样本）",
    "tf_lowconf": "信号置信低于该周期置信档（降权放行）",
}

# D6 门禁降权标签 → (系数配置键, 默认系数)；_apply_gate_tags 查表打标
GATE_TAG_FACTORS = {
    "breaker_deweight": ("twelve_cb_deweight", CB_DEWEIGHT_DEFAULT),
    "tf_lowconf": ("twelve_tf_deweight", TF_DEWEIGHT_DEFAULT),
}

# 门禁拒单原因 → 中文留痕说明（写进 twelve_sim_signal_log.note，看板/复盘直读）
REJECT_REASON_CN = {
    "sl_too_tight": "止损距离低于该周期下限",
    "sl_below_atr": "止损距离低于 ATR 噪声带（波动率自适应下限）",
    "rr_too_low": "盈亏比低于下限",
    "fee_negative_ev": "止盈不足以覆盖费用负担（负期望）",
    "circuit_breaker": "信号×周期战绩熔断中",
    "tf_gate": "周期门禁（TF 停用或信号置信不足）",
    "counter_trend": "逆 1h 威科夫高周期趋势（短周期不逆势）",
}

# 点位跟随：SL/TP 相对变化 ≥ 此阈值(%)才算实质变更（对齐 jarvis_signal_history
# 计划价 0.2% 变更判定，滤掉浮点噪音与微调抖动）
SLTP_MIN_CHANGE_PCT = 0.2

# 已失效(canceled)计划行保留天数：看板留痕窗口；到期物理清理防表膨胀
# （twelve_sim_signal_log 的 cancel 留痕永久保留，完整审计链在日志表）
CANCELED_RETENTION_DAYS = 7

# 计划失效原因 → 中文留痕说明（写进 twelve_sim_signal_log.note，看板直读）
CANCEL_REASON_CN = {
    "neutral": "信号转中性", "flip": "信号反向", "plan_gone": "信号计划消失",
    "disabled": "槽位停用", "timeout": "挂单超时", "broke": "槽位余额不足",
    "incoherent": "点位与配置不自洽",
}

_INITED = False


def _log(msg: str) -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 日志失败不拖垮交易循环
        pass


def _conn():
    os.makedirs(DB_DIR, exist_ok=True)
    return jdb.connect(DB_PATH)


def init_db() -> None:
    global _INITED
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_wallet (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT NOT NULL,
                tf               TEXT NOT NULL,
                system           TEXT NOT NULL,
                name_cn          TEXT,
                principal        REAL NOT NULL DEFAULT 100,
                balance          REAL NOT NULL DEFAULT 100,
                equity           REAL NOT NULL DEFAULT 100,
                total_trades     INTEGER NOT NULL DEFAULT 0,
                win_trades       INTEGER NOT NULL DEFAULT 0,
                total_pnl        REAL NOT NULL DEFAULT 0,
                win_rate         REAL,
                profit_factor    REAL,
                max_drawdown_pct REAL,
                updated_ts       REAL,
                UNIQUE (symbol, tf, system)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_position (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT NOT NULL,
                tf             TEXT NOT NULL,
                system         TEXT NOT NULL,
                direction      TEXT NOT NULL,
                entry_price    REAL NOT NULL,
                entry_ts       REAL NOT NULL,
                qty            REAL NOT NULL,
                margin         REAL NOT NULL,
                leverage       REAL NOT NULL DEFAULT 1,
                position_pct   REAL,
                stop_loss      REAL,
                take_profit    REAL,
                cur_price      REAL,
                unrealized_pnl REAL,
                status         TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tsp_sym_status "
            "ON twelve_sim_position(symbol, status)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_trade (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                tf              TEXT NOT NULL,
                system          TEXT NOT NULL,
                name_cn         TEXT,
                direction       TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                entry_ts        REAL NOT NULL,
                exit_price      REAL NOT NULL,
                exit_ts         REAL NOT NULL,
                qty             REAL NOT NULL,
                margin          REAL NOT NULL,
                leverage        REAL NOT NULL DEFAULT 1,
                stop_loss       REAL,
                take_profit     REAL,
                exit_reason     TEXT NOT NULL,
                pnl             REAL NOT NULL,
                pnl_pct         REAL,
                rr              REAL,
                balance_after   REAL,
                holding_minutes REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tst_slot "
            "ON twelve_sim_trade(symbol, tf, system, exit_ts)"
        )
        # 旧库升级：R3 失效留痕列 + S7 资金费列 + D0 环境快照列（SQLite 无
        # IF NOT EXISTS，重复加列抛错=已升级过；jarvis_db 兼容层对 pg 自动翻译
        # ADD COLUMN IF NOT EXISTS 幂等）。必须放在相关 CREATE TABLE 之后：
        # 曾放在 twelve_sim_trade CREATE 之前，全新库单次 init 时 ALTER 因表
        # 不存在被吞、CREATE 又不含新列 → 缺列（冒烟调两次 init 侥幸掩盖）。
        _upgrades = ["ALTER TABLE twelve_sim_position ADD COLUMN cancel_reason TEXT",
                     "ALTER TABLE twelve_sim_position ADD COLUMN canceled_ts REAL",
                     "ALTER TABLE twelve_sim_position ADD COLUMN reject_reason TEXT",
                     "ALTER TABLE twelve_sim_trade ADD COLUMN funding_fee REAL"]
        _upgrades += [f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_typ}"
                      for _tbl in ("twelve_sim_position", "twelve_sim_trade")
                      for _col, _typ in CTX_COLUMNS_DDL]
        for _ddl in _upgrades:
            try:
                conn.execute(_ddl)
            except Exception:  # noqa: BLE001 — duplicate column = 已升级过
                pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_config (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                scope_tf        TEXT,
                scope_system    TEXT,
                principal       REAL DEFAULT 100,
                leverage        REAL,
                position_pct    REAL,
                stop_loss_pct   REAL,
                take_profit_pct REAL,
                enabled         INTEGER NOT NULL DEFAULT 1,
                UNIQUE (symbol, scope_tf, scope_system)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_signal_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                symbol       TEXT NOT NULL,
                tf           TEXT NOT NULL,
                system       TEXT NOT NULL,
                name_cn      TEXT,
                position_id  INTEGER,
                prev_entry   REAL,
                prev_sl      REAL,
                prev_tp      REAL,
                new_entry    REAL,
                new_sl       REAL,
                new_tp       REAL,
                price        REAL,
                change_kinds TEXT,
                applied      INTEGER NOT NULL DEFAULT 1,
                note         TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tssl_slot "
            "ON twelve_sim_signal_log(symbol, tf, system, ts)"
        )
        # S3 信号×周期战绩熔断器状态（落库持久化，进程重启不丢）：
        # state: tripped=熔断中 / probing=半开试探单在途 / recovered=已恢复；
        # reset_ts=战绩窗口起点（恢复时刻起算，避免旧亏损战绩立刻再触发）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_breaker (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol            TEXT NOT NULL,
                tf                TEXT NOT NULL,
                system            TEXT NOT NULL,
                state             TEXT NOT NULL,
                tripped_ts        REAL,
                probe_position_id INTEGER,
                reset_ts          REAL NOT NULL DEFAULT 0,
                trip_count        INTEGER NOT NULL DEFAULT 0,
                updated_ts        REAL,
                UNIQUE (symbol, tf, system)
            )
            """
        )
    _INITED = True


def _ensure_init() -> None:
    if not _INITED:
        init_db()


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return s if s.endswith("USDT") else s + "USDT"


# ─────────────────────────── 现价通道 ───────────────────────────

def latest_price(cfg: dict, symbol: str) -> float | None:
    """现价：复用 jarvis_paper_trader.latest_price 同款通道（惰性导入）。

    冒烟测试直接对本函数打桩（jtt.latest_price = lambda ...），不触发导入。
    """
    try:
        import jarvis_paper_trader as jpt
        return jpt.latest_price(cfg, symbol)
    except Exception as exc:  # noqa: BLE001 — 取价失败降级为无价，本轮跳过该币
        _log(f"⚠️ {symbol} 取现价失败: {exc!r}"[:160])
        return None


def mark_price_of(cfg: dict, symbol: str) -> float | None:
    """标记价（爆仓判定口径，币安合约强平按 markPrice 触发）：惰性导入 jcd。

    冒烟测试直接对本函数打桩（jtt.mark_price_of = lambda ...）；返回 None 时
    爆仓判定回退最新成交价（与旧行为一致）。
    """
    try:
        import jarvis_crypto_data as jcd
        return jcd.fetch_mark_price(symbol)
    except Exception as exc:  # noqa: BLE001 — 标记价缺失不阻塞盯盘，回退成交价
        _log(f"⚠️ {symbol} 取标记价失败: {exc!r}"[:160])
        return None


def _load_cfg() -> dict:
    """执行手配置（gateway_base/agent_token 等，供取价用）；失败回空 dict。"""
    try:
        import jarvis_executor as jx
        return jx.load_config()
    except Exception:  # noqa: BLE001
        return {}


def _fee_pct() -> float:
    """单边手续费%（按名义）：jarvis_config twelve_sim_fee_pct > 内置默认 0.05。"""
    try:
        import jarvis_config as jc
        v = jc.get("twelve_sim_fee_pct")
        return max(0.0, float(v)) if v is not None else DEFAULT_FEE_PCT
    except Exception:  # noqa: BLE001 — 配置层异常回退默认，不拖垮交易循环
        return DEFAULT_FEE_PCT


def _gate_cfg(key: str, default):
    """读 jarvis_config 门禁键（YAML 热加载即生效）；异常/缺失回退默认值。

    开仓门禁链（S1-S5/S7）全部经由本函数取参——冒烟测试对本函数打桩即可
    整体控制门禁口径；配置层任何异常绝不拖垮交易循环。
    """
    try:
        import jarvis_config as jc
        v = jc.get(key)
        return default if v is None else v
    except Exception:  # noqa: BLE001 — 配置层异常回退默认
        return default


def _gate_num(key: str, default: float) -> float:
    """数值门禁键：类型异常回退默认。"""
    try:
        return float(_gate_cfg(key, default))
    except (TypeError, ValueError):
        return float(default)


def _tf_gate_num(key: str, tf: str, defaults: dict, fallback: float = 0.0) -> float:
    """按 TF 分层的数值门禁键：配置(dict / JSON 串) > 内置分层默认 > fallback。"""
    raw = _gate_cfg(key, None)
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if isinstance(raw, dict) and raw.get(tf) is not None:
        try:
            return float(raw[tf])
        except (TypeError, ValueError):
            pass
    try:
        return float(defaults.get(tf, fallback))
    except (TypeError, ValueError):
        return float(fallback)


def _fetch_bars(symbol: str, tf: str) -> list[dict] | None:
    """取该 TF 最近的已收盘 K 线（[{time(ms), high, low}]），供影线盘中触发判定。

    只拉数据不算信号（不触发 12 信号重算）；失败返回 None → 调用方
    优雅回退快照现价比对。冒烟测试直接对本函数打桩。
    """
    try:
        import jarvis_twelve_systems as jts
        df = jts.fetch_klines_df(symbol, tf, 300, drop_unclosed=True)
        if df is None or len(df) == 0:
            return None
        return df[["time", "high", "low"]].to_dict("records")
    except Exception as exc:  # noqa: BLE001 — 取数失败降级为快照比对
        _log(f"⚠️ {symbol} [{tf}] 取K线失败（回退现价比对）: {exc!r}"[:160])
        return None


# ─────────────────────────── 配置：upsert / 读取 / 合并 ───────────────────────────

_CFG_FIELDS = ("principal", "leverage", "position_pct",
               "stop_loss_pct", "take_profit_pct", "enabled")


def upsert_config(symbol: str, scope_tf: str | None = None,
                  scope_system: str | None = None, *,
                  principal: float | None = None,
                  leverage: float | None = None,
                  position_pct: float | None = None,
                  stop_loss_pct: float | None = None,
                  take_profit_pct: float | None = None,
                  enabled: bool | int | None = None) -> dict:
    """写入/更新一条配置（供 RuoYi 回读链路与 CLI 调用）。

    scope_tf/scope_system 为 NULL 时 UNIQUE 约束不生效（SQL NULL 语义），
    故此处显式先查后改，保证同 scope 只有一行。只更新显式传入的字段。
    """
    _ensure_init()
    sym = _norm_symbol(symbol)
    stf = scope_tf or None
    ssys = scope_system or None
    if stf is not None and stf not in TFS:
        return {"ok": False, "error": f"scope_tf 非法: {stf}（可选 {'/'.join(TFS)}）"}
    if ssys is not None and ssys not in SYSTEMS:
        return {"ok": False, "error": f"scope_system 非法: {ssys}"}
    fields = {"principal": principal, "leverage": leverage,
              "position_pct": position_pct, "stop_loss_pct": stop_loss_pct,
              "take_profit_pct": take_profit_pct,
              "enabled": (None if enabled is None else int(bool(enabled)))}
    with _conn() as conn:
        cond = ["symbol=?"]
        args: list = [sym]
        for col, val in (("scope_tf", stf), ("scope_system", ssys)):
            if val is None:
                cond.append(f"{col} IS NULL")
            else:
                cond.append(f"{col}=?")
                args.append(val)
        row = conn.execute(
            "SELECT id FROM twelve_sim_config WHERE " + " AND ".join(cond),
            args).fetchone()
        if row:
            sets = {k: v for k, v in fields.items() if v is not None}
            if sets:
                conn.execute(
                    "UPDATE twelve_sim_config SET "
                    + ", ".join(f"{k}=?" for k in sets)
                    + " WHERE id=?", (*sets.values(), row["id"]))
            return {"ok": True, "id": row["id"], "updated": sorted(sets)}
        cur = conn.execute(
            """
            INSERT INTO twelve_sim_config
              (symbol, scope_tf, scope_system, principal, leverage,
               position_pct, stop_loss_pct, take_profit_pct, enabled)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (sym, stf, ssys,
             fields["principal"] if fields["principal"] is not None else DEFAULT_PRINCIPAL,
             fields["leverage"], fields["position_pct"],
             fields["stop_loss_pct"], fields["take_profit_pct"],
             fields["enabled"] if fields["enabled"] is not None else 1))
        return {"ok": True, "id": cur.lastrowid, "created": True}


def list_configs(symbol: str | None = None) -> list[dict]:
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_config WHERE symbol=? "
                "ORDER BY scope_tf, scope_system", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_config ORDER BY symbol, scope_tf, scope_system")
        return [dict(r) for r in cur.fetchall()]


def configured_symbols() -> list[str]:
    """已启用的配置币种（币种级行 scope 全 NULL 且 enabled=1）。"""
    _ensure_init()
    with _conn() as conn:
        cur = conn.execute(
            "SELECT DISTINCT symbol FROM twelve_sim_config "
            "WHERE scope_tf IS NULL AND scope_system IS NULL AND enabled=1 "
            "ORDER BY symbol")
        return [str(r["symbol"]) for r in cur.fetchall()]


def effective_config(symbol: str, tf: str, system: str,
                     rows: list[dict] | None = None) -> dict:
    """槽位生效配置：逐字段按 信号级 > tf组级 > 币种级 取第一个非 NULL。

    返回 {principal, leverage, position_pct, stop_loss_pct, take_profit_pct, enabled}；
    leverage/position_pct/sl/tp 可能为 None（= 交给 plan_json / 默认值兜底）。
    """
    sym = _norm_symbol(symbol)
    if rows is None:
        rows = [r for r in list_configs(sym)]
    levels: list[dict | None] = [None, None, None]
    for r in rows:
        stf, ssys = r.get("scope_tf") or None, r.get("scope_system") or None
        if stf == tf and ssys == system:
            levels[0] = r
        elif stf == tf and ssys is None:
            levels[1] = r
        elif stf is None and ssys is None:
            levels[2] = r
    out: dict = {"principal": DEFAULT_PRINCIPAL, "leverage": None,
                 "position_pct": None, "stop_loss_pct": None,
                 "take_profit_pct": None, "enabled": True}
    for field in _CFG_FIELDS:
        for lv in levels:
            if lv is not None and lv.get(field) is not None:
                out[field] = (bool(lv[field]) if field == "enabled"
                              else float(lv[field]))
                break
    return out


# ─────────────────────────── 钱包 / 持仓 / 台账 读取 ───────────────────────────

def ensure_wallets(symbol: str, cfg_rows: list[dict] | None = None) -> int:
    """给该币种补齐 72 个槽位钱包（缺哪个建哪个，幂等）；返回新建数量。"""
    _ensure_init()
    sym = _norm_symbol(symbol)
    rows = cfg_rows if cfg_rows is not None else list_configs(sym)
    created = 0
    now = time.time()
    with _conn() as conn:
        cur = conn.execute(
            "SELECT tf, system FROM twelve_sim_wallet WHERE symbol=?", (sym,))
        have = {(r["tf"], r["system"]) for r in cur.fetchall()}
        for tf in TFS:
            for system in SYSTEMS:
                if (tf, system) in have:
                    continue
                principal = float(effective_config(sym, tf, system, rows)["principal"])
                conn.execute(
                    """
                    INSERT INTO twelve_sim_wallet
                      (symbol, tf, system, name_cn, principal, balance, equity,
                       total_trades, win_trades, total_pnl, updated_ts)
                    VALUES (?,?,?,?,?,?,?,0,0,0,?)
                    """,
                    (sym, tf, system, NAME_CN.get(system, system),
                     principal, principal, principal, now))
                created += 1
    return created


def get_wallets(symbol: str | None = None) -> list[dict]:
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_wallet WHERE symbol=? ORDER BY tf, system",
                (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_wallet ORDER BY symbol, tf, system")
        return [dict(r) for r in cur.fetchall()]


def open_positions(symbol: str | None = None) -> list[dict]:
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='open' AND symbol=? "
                "ORDER BY tf, system", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='open' "
                "ORDER BY symbol, tf, system")
        return [dict(r) for r in cur.fetchall()]


def pending_positions(symbol: str | None = None) -> list[dict]:
    """计划(pending)行查询：已挂未成交的入场计划（CLI / 冒烟 / 调试用）。"""
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='pending' AND symbol=? "
                "ORDER BY tf, system", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='pending' "
                "ORDER BY symbol, tf, system")
        return [dict(r) for r in cur.fetchall()]


def canceled_positions(symbol: str | None = None) -> list[dict]:
    """已失效(canceled)计划行查询：R3 规则2 留痕（保留 7 天，看板/冒烟/调试用）。"""
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='canceled' AND symbol=? "
                "ORDER BY canceled_ts DESC, id DESC", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='canceled' "
                "ORDER BY canceled_ts DESC, id DESC")
        return [dict(r) for r in cur.fetchall()]


def trades(symbol: str | None = None, tf: str | None = None,
           system: str | None = None, limit: int = 200) -> list[dict]:
    _ensure_init()
    cond, args = [], []
    if symbol:
        cond.append("symbol=?")
        args.append(_norm_symbol(symbol))
    if tf:
        cond.append("tf=?")
        args.append(tf)
    if system:
        cond.append("system=?")
        args.append(system)
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    with _conn() as conn:
        cur = conn.execute(
            f"SELECT * FROM twelve_sim_trade{where} "
            f"ORDER BY exit_ts DESC, id DESC LIMIT ?", (*args, max(1, int(limit))))
        return [dict(r) for r in cur.fetchall()]


# ─────────────────────────── 信号读取（只读 twelve_signal_state） ───────────────────────────

def read_signals(symbol: str) -> dict[tuple[str, str], dict]:
    """读该币全部槽位当前态信号 → {(tf, system): {direction, strength, plan}}。

    只 SELECT，绝不触发重算；plan_json 解析失败按无计划处理。
    """
    _ensure_init()
    sym = _norm_symbol(symbol)
    out: dict[tuple[str, str], dict] = {}
    with _conn() as conn:
        cur = conn.execute(
            "SELECT tf, system, name_cn, direction, strength, plan_json, updated_ts "
            "FROM twelve_signal_state WHERE symbol=?", (sym,))
        for r in cur.fetchall():
            try:
                plan = json.loads(r["plan_json"] or "null")
            except (TypeError, ValueError):
                plan = None
            out[(str(r["tf"]), str(r["system"]))] = {
                "direction": str(r["direction"] or "neutral"),
                "strength": float(r["strength"] or 0.0),
                "name_cn": r["name_cn"],
                "plan": plan if isinstance(plan, dict) else None,
                "updated_ts": r["updated_ts"],
            }
    return out


# ─────────────────────────── 开仓参数解析 ───────────────────────────

def _tf_max_leverage(tf: str | None) -> float:
    """TF 分层杠杆上限（S1）：配置 twelve_max_leverage > 内置分层默认；
    任何来源都不越过 MAX_AUTO_LEVERAGE 绝对硬顶。tf 未知回退硬顶。"""
    if not tf:
        return MAX_AUTO_LEVERAGE
    cap = _tf_gate_num("twelve_max_leverage", tf, MAX_LEVERAGE_BY_TF, MAX_AUTO_LEVERAGE)
    return max(1.0, min(MAX_AUTO_LEVERAGE, cap))


def _auto_leverage(entry: float, stop_loss: float, tf: str | None = None) -> float:
    """按止损距离反推推荐杠杆（S1 解耦版）：打到止损亏损 ≈ 保证金
    twelve_auto_lev_loss_frac（默认 25%），夹到 [1, TF 分层上限]。

    旧口径（0.5 / 顶格 20×）与窄止损强耦合——止损越窄杠杆越顶格，等于专挑
    噪声带下最大注（R10 取证 402/406 笔全 20×）；现降目标亏损比例并按 TF 封顶。
    """
    import math
    try:
        dist = abs(float(entry) - float(stop_loss)) / float(entry)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    if not math.isfinite(dist) or dist <= 0:
        return 1.0
    frac = _gate_num("twelve_auto_lev_loss_frac", AUTO_LEV_SL_LOSS_FRAC)
    return float(max(1.0, min(_tf_max_leverage(tf), math.floor(frac / dist))))


def _risk_gate(tf: str, entry: float, params: dict,
               sym: str | None = None) -> str | None:
    """开仓风控门禁链（合成参数后的最终校验）→ reject_reason 或 None（放行）。

    S1 止损最小距离：SL 距离(%) < 该 TF 下限 → 'sl_too_tight'；
    D5 ATR 自适应档：SL 距离(%) < twelve_sl_atr_mult × 该 TF ATR14% →
       'sl_below_atr'（sym 缺省 / ATR 取数失败 / mult=0 → 跳过，静态档兜底）；
    S2 最小盈亏比：TP距离/SL距离 < twelve_min_rr → 'rr_too_low'；
    S2 费用负担：单笔止盈收益(占保证金%) < twelve_fee_burden_mult ×
       双边费用(占保证金% = 单边费率×2×杠杆) → 'fee_negative_ev'。
    被拦信号不静默丢弃——调用方负责落 status='rejected' + reject_reason 留痕。
    """
    try:
        sl_dist = abs(entry - float(params["stop_loss"])) / entry * 100.0
        tp_dist = abs(float(params["take_profit"]) - entry) / entry * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if sl_dist < _tf_gate_num("twelve_min_sl_pct", tf, MIN_SL_PCT_BY_TF, 0.0):
        return "sl_too_tight"
    # D5：波动率自适应档（复用 D0 环境快照的 _ctx_atr TTL 缓存，零新增出网）
    mult = _gate_num("twelve_sl_atr_mult", SL_ATR_MULT_DEFAULT)
    if sym and mult > 0:
        try:
            atr_pct, _bucket = _ctx_atr(sym, tf)
        except Exception:  # noqa: BLE001 — 眼睛坏了=放行，静态档仍兜底
            atr_pct = None
        if atr_pct and sl_dist < mult * float(atr_pct):
            return "sl_below_atr"
    if sl_dist <= 0 or tp_dist / sl_dist < _gate_num("twelve_min_rr", MIN_RR_DEFAULT):
        return "rr_too_low"
    lev = float(params.get("leverage") or 1.0)
    fee_of_margin = _fee_pct() * 2.0 * lev   # 双边费用占保证金%（名义≈入场名义）
    if (fee_of_margin > 0
            and tp_dist * lev < _gate_num("twelve_fee_burden_mult",
                                          FEE_BURDEN_MULT_DEFAULT) * fee_of_margin):
        return "fee_negative_ev"
    return None


def _resolve_entry_params(direction: str, price: float, eff: dict,
                          plan: dict | None, tf: str | None = None) -> dict | None:
    """合成一笔开仓参数：配置覆盖 > plan_json 推荐 > 默认/自动推荐。

    杠杆兜底顺序：配置 > plan_json > 按止损距离自动推荐（S1：显式杠杆尊重
    显式值但夹 TF 分层上限；自动推荐按 twelve_auto_lev_loss_frac 解耦）。
    返回 {stop_loss, take_profit, leverage, position_pct} 或 None（点位不自洽，
    如现价已越过计划止损/止盈 → 宁缺毋滥不硬开）。
    """
    plan = plan or {}
    pos_pct = eff.get("position_pct")
    if pos_pct is None:
        pos_pct = plan.get("position_pct") or DEFAULT_POSITION_PCT
    pos_pct = max(0.1, min(100.0, float(pos_pct)))

    long_side = direction == "long"
    sl_pct, tp_pct = eff.get("stop_loss_pct"), eff.get("take_profit_pct")
    sl = (price * (1 - sl_pct / 100.0) if long_side else price * (1 + sl_pct / 100.0)) \
        if sl_pct else None
    tp = (price * (1 + tp_pct / 100.0) if long_side else price * (1 - tp_pct / 100.0)) \
        if tp_pct else None
    if sl is None:
        sl = plan.get("stop_loss")
    if tp is None:
        tp = plan.get("take_profit") or plan.get("take_profit_1")
    try:
        sl = float(sl) if sl is not None else None
        tp = float(tp) if tp is not None else None
    except (TypeError, ValueError):
        return None
    if sl is None or tp is None:
        return None   # 无止损/止盈点位不开仓（宁缺毋滥）
    # 方向自洽校验：多单 SL < 现价 < TP；空单 TP < 现价 < SL（行情跑掉不硬开）
    if long_side and not (sl < price < tp):
        return None
    if not long_side and not (tp < price < sl):
        return None

    lev = eff.get("leverage")
    if lev is None:
        lev = plan.get("leverage")
    if lev:
        # 显式杠杆（配置/plan）尊重显式值，只夹 TF 分层上限（S1）
        lev = min(max(1.0, float(lev)), _tf_max_leverage(tf))
    else:
        lev = _auto_leverage(price, sl, tf)
    return {"stop_loss": sl, "take_profit": tp,
            "leverage": lev, "position_pct": pos_pct}


def _max_drawdown_pct(principal: float, balance_series: list[float]) -> float | None:
    """由 本金 + 逐笔 balance_after 序列 算最大回撤%（峰谷口径）。"""
    peak = principal
    mdd = 0.0
    for b in balance_series:
        peak = max(peak, b)
        if peak > 0:
            mdd = max(mdd, (peak - b) / peak * 100.0)
    return round(mdd, 2)


# ─────────────────────────── 开仓 / 平仓（内部，同一连接内执行） ───────────────────────────

# ─────────────── 13诊断 D0：开仓时刻市场环境快照（装眼睛第一步：先落数据） ───────────────

_CTX_REGIME_CACHE: dict = {}   # sym → (ts, RegimeResult|None)：classify 拉 3×200 根 K 线必须限频
_CTX_ATR_CACHE: dict = {}      # (sym, tf) → (ts, atr_pct|None, bucket|None)


def _ctx_ttl() -> float:
    return max(30.0, _gate_num("twelve_ctx_cache_ttl_s", CTX_CACHE_TTL_DEFAULT))


def _ctx_regime_of(sym: str):
    """市场状态分类（TTL 缓存，镜像 jarvis_paper_trader._REGIME_CACHE 先例）；
    失败也缓存 None——防每轮 72 槽位对故障源重试风暴。"""
    hit = _CTX_REGIME_CACHE.get(sym)
    if hit and time.time() - hit[0] < _ctx_ttl():
        return hit[1]
    res = None
    try:
        import jarvis_regime_classifier as jrc
        res = jrc.classify(sym)
    except Exception:  # noqa: BLE001 — 快照失败=字段落 NULL，绝不拖垮开仓
        res = None
    _CTX_REGIME_CACHE[sym] = (time.time(), res)
    return res


def _ctx_atr(sym: str, tf: str) -> tuple[float | None, str | None]:
    """该 TF 的 ATR14%（相对最新收盘价）与波动率分档（TTL 缓存）。

    分档口径：80 根窗口内滚动 ATR% 序列的分位——当前值 <30 分位 low、
    >70 分位 high、其余 mid（自适应各币/各 TF 的波动率基准，无需绝对阈值）。
    """
    key = (sym, tf)
    hit = _CTX_ATR_CACHE.get(key)
    if hit and time.time() - hit[0] < _ctx_ttl():
        return hit[1], hit[2]
    atr_pct = bucket = None
    try:
        import jarvis_delta_flow as jdf
        bars = jdf.fetch_bars(sym, tf, 80)
        if bars and len(bars) >= 20:
            trs, prev_close = [], None
            for b in bars:
                h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
                tr = (h - l) if prev_close is None else max(
                    h - l, abs(h - prev_close), abs(l - prev_close))
                trs.append(tr / c * 100.0 if c > 0 else 0.0)
                prev_close = c
            series = [sum(trs[i - 14:i]) / 14.0 for i in range(14, len(trs) + 1)]
            atr_pct = round(series[-1], 4)
            rank = sum(1 for x in series if x < series[-1]) / len(series) * 100.0
            bucket = "low" if rank < 30 else ("high" if rank > 70 else "mid")
    except Exception:  # noqa: BLE001 — 同上：落 NULL 不阻塞
        atr_pct = bucket = None
    _CTX_ATR_CACHE[key] = (time.time(), atr_pct, bucket)
    return atr_pct, bucket


def _market_context(sym: str, tf: str, now: float) -> dict:
    """开仓时刻市场环境快照 → {ctx_* 字段: 值|None}（字段口径见 CTX_COLUMNS_DDL）。

    四路取数全部带缓存 + 独立容错：regime/BTC 趋势走 _ctx_regime_of（TTL）、
    ATR 走 _ctx_atr（TTL）、威科夫复用 _trend_context（指纹缓存）、funding/OI
    走 jarvis_market_intel.get_intel()（模块级 TTL + 后台刷新）。零新增出网端点；
    任一路失败对应字段落 None，绝不阻塞开仓主链路（seatbelt 哲学）。
    """
    ctx: dict = dict.fromkeys(CTX_FIELDS)
    try:
        res = _ctx_regime_of(sym)
        regime = getattr(res, "regime", None)
        if regime in ("trending", "ranging", "breakout"):
            ctx["ctx_regime"] = regime
        d = getattr(res, "direction", None)
        if d in ("bullish", "bearish", "neutral"):
            ctx["ctx_regime_dir"] = d
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx["ctx_atr_pct"], ctx["ctx_vol_bucket"] = _ctx_atr(sym, tf)
    except Exception:  # noqa: BLE001
        pass
    try:
        side, phase = _trend_context(sym)
        if side:
            ctx["ctx_wyckoff"] = f"{side}-{phase}" if phase else side
    except Exception:  # noqa: BLE001
        pass
    try:
        import jarvis_market_intel as jmi
        intel = jmi.get_intel()
        rates = intel.get("funding_rate") or {}
        if rates.get(sym) is not None:
            ctx["ctx_funding"] = float(rates[sym])
        oi = intel.get("oi") or {}
        if oi.get("change_pct") is not None:
            ctx["ctx_oi_btc_chg"] = float(oi["change_pct"])
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx["ctx_hour_utc"] = int(time.gmtime(now).tm_hour)
    except Exception:  # noqa: BLE001
        pass
    try:
        if sym == "BTCUSDT":
            ctx["ctx_btc_trend"] = ctx.get("ctx_regime_dir")
        else:
            btc = _ctx_regime_of("BTCUSDT")
            d = getattr(btc, "direction", None)
            if d in ("bullish", "bearish", "neutral"):
                ctx["ctx_btc_trend"] = d
    except Exception:  # noqa: BLE001
        pass
    return ctx


def _ctx_snapshot(sym: str, tf: str, now: float) -> dict:
    """快照总闸：twelve_ctx_snapshot_enabled 关闭时不调 provider（零出网增量）；
    provider 任何异常返回空 dict（全字段落 NULL）。开仓路径唯一入口。"""
    if _gate_num("twelve_ctx_snapshot_enabled",
                 float(CTX_SNAPSHOT_ENABLED_DEFAULT)) < 0.5:
        return {}
    try:
        out = _market_context(sym, tf, now)
        return out if isinstance(out, dict) else {}
    except Exception:  # noqa: BLE001 — 眼睛坏了不能把交易引擎卡死
        return {}


# ─────────── 13诊断 D2+：信号侧上下文层（打标签+降权公共设施，绝不拒单） ───────────

_CTX_VOL_CACHE: dict = {}   # (sym, tf) → (ts, verdict_dict|None)


def _apply_context(params: dict, tag: str, factor: float, note: str = "") -> dict:
    """上下文打标 + 降权（D2-D6 公共设施）：tag 追加进 params['context_tags']、
    factor 连乘进 params['size_factor']（下限 CTX_MIN_SIZE_FACTOR，绝不到 0）。

    只降仓位不拒单——信号继续跑、样本继续攒，标签落库供 D1 按环境切片归因。
    同 tag 幂等（重复打标不重复降权）。
    """
    tags = params.setdefault("context_tags", [])
    if any(t["tag"] == tag for t in tags):
        return params
    tags.append({"tag": tag, "factor": float(factor),
                 "note": note or CONTEXT_TAG_CN.get(tag, tag)})
    params["size_factor"] = round(
        max(CTX_MIN_SIZE_FACTOR,
            float(params.get("size_factor", 1.0)) * float(factor)), 6)
    return params


def _ctx_tags_str(params: dict) -> str | None:
    """params 的上下文标签 → 逗号串（落库口径）；无标签 → None。"""
    tags = params.get("context_tags") or []
    return ",".join(t["tag"] for t in tags) or None


def _apply_gate_tags(params: dict, tags: list[str] | None) -> dict:
    """D6：_pre_gate 返回的门禁降权标签落进 params（查 GATE_TAG_FACTORS 取
    系数，复用 _apply_context 幂等连乘 + 留痕链路）。未知标签忽略。"""
    for t in tags or []:
        key, dft = GATE_TAG_FACTORS.get(t, (None, None))
        if key:
            _apply_context(params, t, _gate_num(key, dft))
    return params


def _log_context(conn, sym: str, tf: str, system: str, position_id: int,
                 price: float, now: float, params: dict) -> None:
    """上下文打标留痕（change_kinds='context'）：不静默丢弃降权原因，
    D1 报表/看板可复盘「为什么这单只有半仓」。日志失败不拖垮开仓。"""
    tags = params.get("context_tags") or []
    if not tags:
        return
    try:
        note = "；".join(f"{t['tag']}×{t['factor']:g}：{t['note']}" for t in tags)
        conn.execute(
            """
            INSERT INTO twelve_sim_signal_log
              (ts, symbol, tf, system, name_cn, position_id,
               price, change_kinds, applied, note)
            VALUES (?,?,?,?,?,?,?,'context',1,?)
            """,
            (now, sym, tf, system, NAME_CN.get(system, system), position_id,
             price, f"[上下文降权 size_factor={params.get('size_factor', 1.0):g}] {note}"[:500]))
    except Exception:  # noqa: BLE001 — 留痕失败不阻塞开仓
        pass


def _volume_context(sym: str, tf: str) -> dict | None:
    """量能/CVD 突破核验（TTL 缓存）→ {active, direction, verdict, reasons} 或 None。

    取数三件套：jarvis_delta_flow.fetch_bars（含 taker_buy 的已收盘 bar）→
    compute_delta_cvd → jarvis_supply_demand.breakout_check(bars, cvd, None, None)。
    trap/whale 传 None：二者是 dashboard 进程 WS 内存态，trader 独立进程恒空，
    breakout_check 已容错判空。取数失败返回 None（不打标放行）。
    """
    key = (sym, tf)
    hit = _CTX_VOL_CACHE.get(key)
    if hit and time.time() - hit[0] < _ctx_ttl():
        return hit[1]
    out = None
    try:
        import jarvis_delta_flow as jdf
        import jarvis_supply_demand as jsd
        bars = jdf.fetch_bars(sym, tf, 80)
        if bars and len(bars) >= 25:
            rows = jdf.compute_delta_cvd(bars)
            chk = jsd.breakout_check(bars, {"rows": rows}, None, None)
            if isinstance(chk, dict):
                out = chk
    except Exception:  # noqa: BLE001 — 眼睛坏了=不打标，绝不阻塞
        out = None
    _CTX_VOL_CACHE[key] = (time.time(), out)
    return out


def _crowd_context(sym: str) -> tuple[float | None, float | None]:
    """D4 拥挤度取数 → (该币 funding 费率, OI 24h 变化率%)；任一不可得落 None。

    复用 jarvis_market_intel.get_intel()（模块级 TTL + 后台刷新，与 D0 快照
    同一数据源，零新增出网端点）。OI 变化率是 intel 的 BTC 口径（市场杠杆
    温度计代理指标），与 ctx_oi_btc_chg 落库字段同源同义。
    """
    funding = oi_chg = None
    try:
        import jarvis_market_intel as jmi
        intel = jmi.get_intel()
        rates = intel.get("funding_rate") or {}
        if rates.get(sym) is not None:
            funding = float(rates[sym])
        oi = intel.get("oi") or {}
        if oi.get("change_pct") is not None:
            oi_chg = float(oi["change_pct"])
    except Exception:  # noqa: BLE001 — 眼睛坏了=不打标，绝不阻塞
        return None, None
    return funding, oi_chg


def _context_layers(sym: str, tf: str, system: str, direction: str,
                    params: dict, now: float) -> dict:
    """信号侧上下文层总装（开仓/成交前对 params 打标降权；逐层独立容错）。

    D2 量能/CVD 突破确认层；D3 趋势/regime 层、D4 拥挤度层在此追加。
    任何一层异常 = 该层不打标放行，绝不拒单、绝不阻塞开仓主链路。
    """
    # D2：突破/追价类系统 × 量能核验（同向突破才有核验意义）
    try:
        raw = _gate_cfg("twelve_ctx_vol_systems", list(CTX_VOL_SYSTEMS_DEFAULT))
        if isinstance(raw, str):
            raw = [s.strip() for s in raw.split(",") if s.strip()]
        vol_systems = {str(s).lower() for s in (raw or [])}
        if system.lower() in vol_systems:
            vc = _volume_context(sym, tf)
            if vc and vc.get("active"):
                same = ((direction == "long" and vc.get("direction") == "up")
                        or (direction == "short" and vc.get("direction") == "down"))
                if same and vc.get("verdict") == "suspect":
                    why = "；".join(map(str, vc.get("reasons") or []))[:200]
                    _apply_context(
                        params, "vol_suspect",
                        _gate_num("twelve_ctx_deweight_suspect",
                                  CTX_DEWEIGHT_SUSPECT_DEFAULT),
                        f"{CONTEXT_TAG_CN['vol_suspect']}：{why}" if why else "")
                elif same and vc.get("verdict") == "confirmed":
                    # 纯标记（factor=1 不降权）：归因侧对照「确认组 vs 嫌疑组」用
                    _apply_context(params, "vol_confirmed", 1.0)
    except Exception:  # noqa: BLE001
        pass
    # D3-1/2：市场状态（regime）× 系统性格错配——oscillator 自述「趋势市毒药」，
    # 突破系在震荡市假突破高发；错配不禁止，打标降权继续攒对照样本
    try:
        res = _ctx_regime_of(sym)
        regime = getattr(res, "regime", None)
        rdir = getattr(res, "direction", None)
        if regime == "trending":
            raw = _gate_cfg("twelve_ctx_meanrev_systems",
                            list(CTX_MEANREV_SYSTEMS_DEFAULT))
            if isinstance(raw, str):
                raw = [s.strip() for s in raw.split(",") if s.strip()]
            if (system.lower() in {str(s).lower() for s in (raw or [])}
                    and ((rdir == "bullish" and direction == "short")
                         or (rdir == "bearish" and direction == "long"))):
                _apply_context(params, "osc_in_trend",
                               _gate_num("twelve_ctx_deweight_regime",
                                         CTX_DEWEIGHT_REGIME_DEFAULT))
        elif regime == "ranging":
            raw = _gate_cfg("twelve_ctx_vol_systems", list(CTX_VOL_SYSTEMS_DEFAULT))
            if isinstance(raw, str):
                raw = [s.strip() for s in raw.split(",") if s.strip()]
            if system.lower() in {str(s).lower() for s in (raw or [])}:
                _apply_context(params, "breakout_in_range",
                               _gate_num("twelve_ctx_deweight_regime",
                                         CTX_DEWEIGHT_REGIME_DEFAULT))
    except Exception:  # noqa: BLE001
        pass
    # D3-3：1h 威科夫逆势（S5 同判据）——mode=deweight 时由本层打标降权；
    # mode=reject 时 _pre_gate 已拒单，到不了这里（互斥不双罚）
    try:
        if (tf in TREND_FILTER_TFS
                and _gate_num("twelve_trend_filter_enabled",
                              float(TREND_FILTER_ENABLED_DEFAULT)) >= 0.5
                and str(_gate_cfg("twelve_trend_filter_mode",
                                  TREND_FILTER_MODE_DEFAULT)).lower() == "deweight"):
            side, phase = _trend_context(sym)
            if phase in TREND_FILTER_PHASES and (
                    (side == "dist" and direction == "long")
                    or (side == "acc" and direction == "short")):
                _apply_context(params, "counter_trend",
                               _gate_num("twelve_ctx_deweight_counter",
                                         CTX_DEWEIGHT_COUNTER_DEFAULT),
                               f"{CONTEXT_TAG_CN['counter_trend']}：1h {side}-{phase}")
    except Exception:  # noqa: BLE001
        pass
    # D4：funding/OI 拥挤度——资金费率热时，顺拥挤方向（funding>0 做多 /
    # funding<0 做空）打 crowded_side 降权；叠加 OI 24h 激增追加 crowded_hot
    # 再乘一次系数；反拥挤侧打 contrarian_side 纯标记（factor=1 对照组）。
    # funding 不热 / 取数失败 → 本层零动作。
    try:
        funding, oi_chg = _crowd_context(sym)
        hot = _gate_num("twelve_ctx_funding_hot", CTX_FUNDING_HOT_DEFAULT)
        if funding is not None and hot > 0 and abs(funding) >= hot:
            crowded_dir = "long" if funding > 0 else "short"
            if direction == crowded_dir:
                dw = _gate_num("twelve_ctx_deweight_crowded",
                               CTX_DEWEIGHT_CROWDED_DEFAULT)
                _apply_context(
                    params, "crowded_side", dw,
                    f"{CONTEXT_TAG_CN['crowded_side']}：funding={funding:+.4%}")
                if (oi_chg is not None
                        and oi_chg >= _gate_num("twelve_ctx_oi_surge_pct",
                                                CTX_OI_SURGE_PCT_DEFAULT)):
                    _apply_context(
                        params, "crowded_hot", dw,
                        f"{CONTEXT_TAG_CN['crowded_hot']}：OI 24h {oi_chg:+.1f}%")
            else:
                _apply_context(params, "contrarian_side", 1.0,
                               f"{CONTEXT_TAG_CN['contrarian_side']}"
                               f"：funding={funding:+.4%}")
    except Exception:  # noqa: BLE001
        pass
    return params


def _do_open(conn, sym: str, tf: str, system: str,
             direction: str, price: float, balance: float,
             params: dict, now: float) -> dict:
    _context_layers(sym, tf, system, direction, params, now)   # D2+：打标降权
    sf = float(params.get("size_factor") or 1.0)
    margin = round(min(balance, balance * params["position_pct"] / 100.0 * sf), 8)
    qty = round(margin * params["leverage"] / price, 8)
    ctx = _ctx_snapshot(sym, tf, now)   # D0：开仓时刻环境快照（失败=全 NULL）
    cur = conn.execute(
        """
        INSERT INTO twelve_sim_position
          (symbol, tf, system, direction, entry_price, entry_ts, qty, margin,
           leverage, position_pct, stop_loss, take_profit, cur_price,
           unrealized_pnl, status,
           ctx_regime, ctx_regime_dir, ctx_atr_pct, ctx_vol_bucket, ctx_wyckoff,
           ctx_funding, ctx_oi_btc_chg, ctx_hour_utc, ctx_btc_trend,
           context_tags, size_factor)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,'open',?,?,?,?,?,?,?,?,?,?,?)
        """,
        (sym, tf, system, direction, price, now, qty, margin,
         params["leverage"], params["position_pct"],
         params["stop_loss"], params["take_profit"], price,
         *(ctx.get(f) for f in CTX_FIELDS), _ctx_tags_str(params), sf))
    _log_context(conn, sym, tf, system, int(cur.lastrowid or 0), price, now, params)
    return {"symbol": sym, "tf": tf, "system": system, "direction": direction,
            "entry_price": price, "qty": qty, "margin": margin,
            "leverage": params["leverage"], "stop_loss": params["stop_loss"],
            "take_profit": params["take_profit"], "position_id": cur.lastrowid,
            "context_tags": _ctx_tags_str(params), "size_factor": sf}


def _do_close(conn, pos: dict, exit_price: float, reason: str, now: float) -> dict:
    """平仓：净 PnL = 毛盈亏 − 开/平双边手续费（按名义，出场名义=qty×出场价，
    多空同式无方向 bug）；爆仓(liq) 直接亏光保证金、不再另计费；
    逐仓亏损钳到 -margin → 记台账 → 更新钱包战绩。"""
    sym, tf, system = pos["symbol"], pos["tf"], pos["system"]
    entry = float(pos["entry_price"])
    qty = float(pos["qty"])
    margin = float(pos["margin"])
    sign = 1.0 if pos["direction"] == "long" else -1.0
    if reason == "liq":
        pnl = -margin   # 爆仓：损失以保证金为上限，手续费/资金费不再另计
        funding = 0.0
    else:
        gross = (exit_price - entry) * qty * sign
        fee = (entry * qty + exit_price * qty) * _fee_pct() / 100.0
        # S7 资金费：持仓每满 8h 按 entry 名义计提一次；rate>0 多头付/空头收
        # （正=支出 负=收入），折进净 pnl 并单列 funding_fee 留痕
        periods = int((now - float(pos["entry_ts"]))
                      // (FUNDING_INTERVAL_HOURS * 3600.0))
        funding = round(periods
                        * _gate_num("twelve_funding_rate", FUNDING_RATE_DEFAULT)
                        * entry * qty * sign, 8)
        pnl = max(gross - fee - funding, -margin)   # 逐仓：最大亏损=保证金，不倒欠
    pnl = round(pnl, 8)
    pnl_pct = round(pnl / margin * 100.0, 2) if margin > 0 else None
    sl, tp = pos.get("stop_loss"), pos.get("take_profit")
    rr = None
    if sl is not None and tp is not None:
        risk = abs(entry - float(sl))
        if risk > 0:
            rr = round(abs(float(tp) - entry) / risk, 2)
    holding_min = round((now - float(pos["entry_ts"])) / 60.0, 1)

    conn.execute("UPDATE twelve_sim_position SET status='closed', cur_price=?, "
                 "unrealized_pnl=0 WHERE id=?", (exit_price, pos["id"]))

    wal = conn.execute(
        "SELECT * FROM twelve_sim_wallet WHERE symbol=? AND tf=? AND system=?",
        (sym, tf, system)).fetchone()
    wal = dict(wal) if wal else {"principal": DEFAULT_PRINCIPAL,
                                 "balance": DEFAULT_PRINCIPAL}
    balance_after = round(float(wal["balance"]) + pnl, 8)

    conn.execute(
        """
        INSERT INTO twelve_sim_trade
          (symbol, tf, system, name_cn, direction, entry_price, entry_ts,
           exit_price, exit_ts, qty, margin, leverage, stop_loss, take_profit,
           exit_reason, pnl, pnl_pct, rr, balance_after, holding_minutes,
           funding_fee,
           ctx_regime, ctx_regime_dir, ctx_atr_pct, ctx_vol_bucket, ctx_wyckoff,
           ctx_funding, ctx_oi_btc_chg, ctx_hour_utc, ctx_btc_trend,
           context_tags, size_factor)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?)
        """,
        (sym, tf, system, NAME_CN.get(system, system), pos["direction"],
         entry, pos["entry_ts"], exit_price, now, qty, margin,
         pos.get("leverage") or 1.0, sl, tp, reason, pnl, pnl_pct, rr,
         balance_after, holding_min, funding,
         # D0：开仓时刻环境快照原样拷入台账（pos 来自 SELECT *，新列已就位）
         *(pos.get(f) for f in CTX_FIELDS),
         pos.get("context_tags"), pos.get("size_factor")))

    # 钱包战绩重算（该槽位全量台账，72 槽位×小样本，代价可忽略）
    rows = conn.execute(
        "SELECT pnl, balance_after FROM twelve_sim_trade "
        "WHERE symbol=? AND tf=? AND system=? ORDER BY exit_ts, id",
        (sym, tf, system)).fetchall()
    pnls = [float(r["pnl"]) for r in rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    principal = float(wal.get("principal") or DEFAULT_PRINCIPAL)
    conn.execute(
        """
        UPDATE twelve_sim_wallet
        SET balance=?, equity=?, total_trades=?, win_trades=?, total_pnl=?,
            win_rate=?, profit_factor=?, max_drawdown_pct=?, updated_ts=?
        WHERE symbol=? AND tf=? AND system=?
        """,
        (balance_after, balance_after, len(pnls), len(wins),
         round(sum(pnls), 8),
         round(100.0 * len(wins) / len(pnls), 1) if pnls else None,
         round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
         _max_drawdown_pct(principal, [float(r["balance_after"]) for r in rows]),
         now, sym, tf, system))
    # S3 熔断器簿记：试探单结算（恢复/继续熔断）或滚动战绩评估触发熔断
    _breaker_on_close(conn, pos, pnl, exit_price, now)
    return {"symbol": sym, "tf": tf, "system": system,
            "direction": pos["direction"], "entry_price": entry,
            "exit_price": exit_price, "exit_reason": reason, "pnl": pnl,
            "pnl_pct": pnl_pct, "balance_after": balance_after,
            "holding_minutes": holding_min}


def _liq_price(entry: float, direction: str, leverage: float) -> float | None:
    """逐仓爆仓价：逆向变动 = 1/杠杆（近似口径，忽略维持保证金率）；1 倍无爆仓价。"""
    lev = float(leverage or 1.0)
    if lev <= 1.0:
        return None
    return entry * (1.0 - 1.0 / lev) if direction == "long" else entry * (1.0 + 1.0 / lev)


def _exit_check(pos: dict, price: float,
                bars: list[dict] | None,
                mark_price: float | None = None) -> tuple[str, float] | None:
    """爆仓/止损/止盈 盘中触发判定 → (reason, 结算价) 或 None。

    bars=自开仓以来已收盘 bar（含影线 high/low）；None/为空 → 回退快照现价。
    优先级 liq > sl > tp（同 bar 双触按保守取 SL）；结算价=触发位本身，
    缺口行情（快照已越过触发位）按更差价结算。
    mark_price=合约标记价（2026-08-05 口径切换：爆仓按 markPrice 判定，与币安
    强平规则一致）；None 时回退 price（成交价），sl/tp 始终按成交价口径。
    """
    entry = float(pos["entry_price"])
    long_side = pos["direction"] == "long"
    hi = lo = None
    if bars:
        entry_ms = float(pos["entry_ts"]) * 1000.0
        rel = [b for b in bars if float(b["time"]) > entry_ms]
        if rel:
            hi = max(float(b["high"]) for b in rel)
            lo = min(float(b["low"]) for b in rel)
    # 有效触及范围 = 已收盘 bar 影线 ∪ 快照现价（快照可能领先未收盘 bar）
    eff_hi = max(hi, price) if hi is not None else price
    eff_lo = min(lo, price) if lo is not None else price

    liq = _liq_price(entry, pos["direction"], pos.get("leverage") or 1.0)
    if liq is not None:
        mark = float(mark_price) if mark_price else price
        m_hi = max(hi, mark) if hi is not None else mark
        m_lo = min(lo, mark) if lo is not None else mark
        if long_side and m_lo <= liq:
            return ("liq", liq)
        if not long_side and m_hi >= liq:
            return ("liq", liq)

    sl, tp = pos.get("stop_loss"), pos.get("take_profit")
    if sl is not None:
        sl = float(sl)
        if long_side and eff_lo <= sl:
            return ("sl", min(sl, price))    # 缺口向下：按更差的快照价结算
        if not long_side and eff_hi >= sl:
            return ("sl", max(sl, price))
    if tp is not None:
        tp = float(tp)
        if long_side and eff_hi >= tp:
            return ("tp", tp)
        if not long_side and eff_lo <= tp:
            return ("tp", tp)
    return None


def _timeout_due(pos: dict, now: float) -> bool:
    """TF 分档时间止损：持仓时长 ≥ 上限（5m/15m:1天 30m:2天 1h:3天 4h:7天 1d:14天）。

    对 pending 计划行同口径复用：entry_ts=计划创建时刻 → 挂单超时撤销分档。
    """
    days = float(TF_TIME_STOP_DAYS.get(str(pos["tf"]), 7))
    return (now - float(pos["entry_ts"])) >= days * 86400.0


# ─────────────────────────── 计划(pending)：挂单 / 触达成交 / 跟随 / 撤销 ───────────────────────────

def _plan_points(plan: dict | None) -> dict | None:
    """从信号 plan 提取计划点位与入场方式；entry 缺失/非法返回 None（无计划）。

    返回 {entry, entry_type, stop_loss, take_profit}；sl/tp 允许 None
    （成交时经 _resolve_entry_params 再校验，宁缺毋滥）。
    """
    if not isinstance(plan, dict) or not plan:
        return None
    try:
        entry = float(plan["entry"]) if plan.get("entry") is not None else None
        sl = float(plan["stop_loss"]) if plan.get("stop_loss") is not None else None
        tp_raw = plan.get("take_profit") or plan.get("take_profit_1")
        tp = float(tp_raw) if tp_raw is not None else None
    except (TypeError, ValueError):
        return None
    if entry is None or entry <= 0:
        return None
    et = str(plan.get("entry_type") or "market")
    if et not in ("breakout", "pullback", "market"):
        et = "market"
    return {"entry": entry, "entry_type": et, "stop_loss": sl, "take_profit": tp}


def _entry_touched(direction: str, entry_type: str, entry: float, price: float,
                   bars: list[dict] | None, since_ts: float) -> bool:
    """计划入场价是否已触达：自 since_ts 以来已收盘 bar 影线 ∪ 快照现价（同
    _exit_check 有效触及口径；bars 取不到时优雅回退快照现价比对）。

    breakout：多 high≥entry / 空 low≤entry；pullback：多 low≤entry / 空 high≥entry；
    market（带有效点位，R3 规则1）：按「点位或更优」限价口径 = 多 low≤entry /
    空 high≥entry（与任务边界约定一致：做多触发 现价≤点位 / 做空触发 现价≥点位）。
    """
    hi = lo = None
    if bars:
        since_ms = float(since_ts) * 1000.0
        rel = [b for b in bars if float(b["time"]) > since_ms]
        if rel:
            hi = max(float(b["high"]) for b in rel)
            lo = min(float(b["low"]) for b in rel)
    eff_hi = max(hi, price) if hi is not None else price
    eff_lo = min(lo, price) if lo is not None else price
    long_side = direction == "long"
    if entry_type in ("pullback", "market"):
        return (eff_lo <= entry) if long_side else (eff_hi >= entry)
    return (eff_hi >= entry) if long_side else (eff_lo <= entry)


def _do_plan(conn, sym: str, tf: str, system: str, direction: str,
             pts: dict, eff: dict, price: float, now: float) -> dict:
    """建一条计划(pending)：不建持仓、不动钱包、不进胜率、不写 twelve_sim_trade。

    entry_price=计划入场价、entry_ts=计划创建时刻；qty/margin=0 延迟到成交时
    按 entry 价计算回填（leverage 占位 1，成交时重算）。
    计划创建落 twelve_sim_signal_log 留痕（change_kinds=plan，新计划替换可追溯）。
    """
    cur = conn.execute(
        """
        INSERT INTO twelve_sim_position
          (symbol, tf, system, direction, entry_price, entry_ts, qty, margin,
           leverage, position_pct, stop_loss, take_profit, cur_price,
           unrealized_pnl, status)
        VALUES (?,?,?,?,?,?,0,0,1,?,?,?,?,0,'pending')
        """,
        (sym, tf, system, direction, pts["entry"], now,
         eff.get("position_pct"), pts["stop_loss"], pts["take_profit"], price))
    pid = cur.lastrowid
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,'plan',1,?)
        """,
        (now, sym, tf, system, NAME_CN.get(system, system), pid,
         pts["entry"], pts["stop_loss"], pts["take_profit"], price,
         f"新计划创建（挂单等触达，{'做多' if direction == 'long' else '做空'}"
         f"@{pts['entry']}）"))
    return {"symbol": sym, "tf": tf, "system": system, "direction": direction,
            "entry": pts["entry"], "entry_type": pts["entry_type"],
            "position_id": pid}


def _cancel_plan(conn, pen: dict, reason: str, price: float, now: float) -> dict:
    """计划失效（R3 规则2）：pending 行保留为 status='canceled' + 失效原因/时刻，
    并落 twelve_sim_signal_log 留痕（不产生 twelve_sim_trade，不影响钱包/胜率）。

    失效后该计划永不再参与触达成交判定（触达查询只认 status='pending'）。
    """
    conn.execute(
        "UPDATE twelve_sim_position SET status='canceled', cancel_reason=?, "
        "canceled_ts=?, cur_price=? WHERE id=? AND status='pending'",
        (reason, now, price, pen["id"]))
    old_sl = float(pen["stop_loss"]) if pen.get("stop_loss") is not None else None
    old_tp = float(pen["take_profit"]) if pen.get("take_profit") is not None else None
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,'cancel',1,?)
        """,
        (now, pen["symbol"], pen["tf"], pen["system"],
         NAME_CN.get(str(pen["system"]), str(pen["system"])), pen["id"],
         float(pen["entry_price"]), old_sl, old_tp, price,
         f"计划已失效（{CANCEL_REASON_CN.get(reason, reason)}），"
         f"之后价格触达旧点位也不成交"))
    return {"symbol": pen["symbol"], "tf": pen["tf"], "system": pen["system"],
            "direction": pen["direction"], "entry": float(pen["entry_price"]),
            "reason": reason}


def _do_reject(conn, sym: str, tf: str, system: str, direction: str,
               reason: str, entry: float, sl: float | None, tp: float | None,
               price: float, now: float) -> dict | None:
    """门禁拒单留痕（公共纪律：不静默丢弃）：落 status='rejected' + reject_reason
    行 + twelve_sim_signal_log（change_kinds=reject），S6 归因报表可按原因聚合。

    防重：该槽位同方向最近一条 rejected 行原因相同且点位无实质变化
    （SLTP_MIN_CHANGE_PCT 同阈值）→ 不重复落行（信号每轮常驻，防行数爆炸）；
    返回 None 表示防重跳过。rejected 行 qty/margin=0，不动钱包/胜率/台账。
    """
    last = conn.execute(
        "SELECT reject_reason, entry_price, stop_loss, take_profit "
        "FROM twelve_sim_position WHERE symbol=? AND tf=? AND system=? "
        "AND direction=? AND status='rejected' ORDER BY id DESC LIMIT 1",
        (sym, tf, system, direction)).fetchone()
    if last is not None and str(last["reject_reason"] or "") == reason:
        old_sl = float(last["stop_loss"]) if last["stop_loss"] is not None else None
        old_tp = float(last["take_profit"]) if last["take_profit"] is not None else None
        if (not _sltp_changed(float(last["entry_price"]), entry)
                and not _sltp_changed(old_sl, sl)
                and not _sltp_changed(old_tp, tp)):
            return None
    cur = conn.execute(
        """
        INSERT INTO twelve_sim_position
          (symbol, tf, system, direction, entry_price, entry_ts, qty, margin,
           leverage, position_pct, stop_loss, take_profit, cur_price,
           unrealized_pnl, status, reject_reason)
        VALUES (?,?,?,?,?,?,0,0,1,NULL,?,?,?,0,'rejected',?)
        """,
        (sym, tf, system, direction, entry, now, sl, tp, price, reason))
    pid = cur.lastrowid
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,'reject',0,?)
        """,
        (now, sym, tf, system, NAME_CN.get(system, system), pid,
         entry, sl, tp, price,
         f"开仓被门禁拦截（{REJECT_REASON_CN.get(reason, reason)}），"
         f"只推送信号不开仓"))
    return {"symbol": sym, "tf": tf, "system": system, "direction": direction,
            "reason": reason, "entry": entry, "position_id": pid}


def _reject_plan(conn, pen: dict, reason: str, price: float, now: float) -> dict:
    """挂单(pending)在成交时刻被门禁拦截：行保留为 status='rejected' +
    reject_reason（同 _cancel_plan 留痕哲学，用 canceled_ts 记终态时刻），
    并落 twelve_sim_signal_log；不产生持仓/台账，不影响钱包。"""
    conn.execute(
        "UPDATE twelve_sim_position SET status='rejected', reject_reason=?, "
        "canceled_ts=?, cur_price=? WHERE id=? AND status='pending'",
        (reason, now, price, pen["id"]))
    old_sl = float(pen["stop_loss"]) if pen.get("stop_loss") is not None else None
    old_tp = float(pen["take_profit"]) if pen.get("take_profit") is not None else None
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,'reject',0,?)
        """,
        (now, pen["symbol"], pen["tf"], pen["system"],
         NAME_CN.get(str(pen["system"]), str(pen["system"])), pen["id"],
         float(pen["entry_price"]), old_sl, old_tp, price,
         f"计划触达但被门禁拦截未成交（{REJECT_REASON_CN.get(reason, reason)}）"))
    return {"symbol": pen["symbol"], "tf": pen["tf"], "system": pen["system"],
            "direction": pen["direction"], "reason": reason,
            "entry": float(pen["entry_price"]), "position_id": pen["id"]}


def rejected_positions(symbol: str | None = None, tf: str | None = None,
                       system: str | None = None, limit: int = 200) -> list[dict]:
    """门禁拒单(rejected)留痕行查询（保留 7 天；看板/归因/冒烟/调试用）。"""
    _ensure_init()
    cond, args = ["status='rejected'"], []
    if symbol:
        cond.append("symbol=?")
        args.append(_norm_symbol(symbol))
    if tf:
        cond.append("tf=?")
        args.append(tf)
    if system:
        cond.append("system=?")
        args.append(system)
    with _conn() as conn:
        cur = conn.execute(
            "SELECT * FROM twelve_sim_position WHERE " + " AND ".join(cond)
            + " ORDER BY entry_ts DESC, id DESC LIMIT ?",
            (*args, max(1, int(limit))))
        return [dict(r) for r in cur.fetchall()]


# ─────────────────────────── S3 信号×周期战绩熔断器 ───────────────────────────

def _breaker_row(conn, sym: str, tf: str, system: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM twelve_sim_breaker WHERE symbol=? AND tf=? AND system=?",
        (sym, tf, system)).fetchone()
    return dict(r) if r else None


def _breaker_log(conn, sym: str, tf: str, system: str, note: str,
                 price: float, now: float, position_id: int | None = None) -> None:
    """熔断/半开/恢复事件写 twelve_sim_signal_log（change_kinds=breaker）可审计。"""
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,?,'breaker',1,?)
        """,
        (now, sym, tf, system, NAME_CN.get(system, system), position_id,
         price, note))


def _breaker_gate(conn, sym: str, tf: str, system: str,
                  now: float) -> tuple[str | None, bool]:
    """熔断门禁 → (reject_reason|None, 半开试探标记)。

    tripped 且冷却未满 / probing（试探单在途）→ 拒 'circuit_breaker'；
    tripped 且冷却期满 → 放行并标记「本次开仓为试探单」（由调用方
    _breaker_mark_probe 落状态）；无记录 / recovered → 正常放行。
    """
    row = _breaker_row(conn, sym, tf, system)
    if row is None or row["state"] == "recovered":
        return None, False
    if row["state"] == "probing":
        return "circuit_breaker", False
    cd_s = _gate_num("twelve_cb_cooldown_hours", CB_COOLDOWN_HOURS_DEFAULT) * 3600.0
    if now - float(row["tripped_ts"] or 0.0) >= cd_s:
        return None, True   # 冷却期满：半开，放行 1 笔试探
    return "circuit_breaker", False


def _breaker_mark_probe(conn, sym: str, tf: str, system: str,
                        position_id: int, price: float, now: float) -> None:
    """半开试探单已开仓：熔断状态 tripped → probing（试探期间其余信号仍拒）。"""
    conn.execute(
        "UPDATE twelve_sim_breaker SET state='probing', probe_position_id=?, "
        "updated_ts=? WHERE symbol=? AND tf=? AND system=?",
        (position_id, now, sym, tf, system))
    _breaker_log(conn, sym, tf, system,
                 "熔断冷却期满半开：放行 1 笔试探单（赢了恢复，输了继续熔断）",
                 price, now, position_id)


def _breaker_on_close(conn, pos: dict, pnl: float, exit_price: float,
                      now: float) -> None:
    """每笔平仓后的熔断器簿记（_do_close 内调用，同一连接）。

    1) 平的是试探单 → 盈利恢复（reset_ts=now 战绩窗口重起算）/ 否则继续熔断
       （重新计冷却，trip_count+1）；
    2) 无激活熔断 → 评估滚动窗口（最近 twelve_cb_window 笔、恢复时刻之后）：
       样本 ≥ twelve_cb_min_trades 且 胜率 < 下限 且 净亏 < -阈值 → 熔断。
    """
    sym, tf, system = str(pos["symbol"]), str(pos["tf"]), str(pos["system"])
    row = _breaker_row(conn, sym, tf, system)
    if (row and row["state"] == "probing"
            and row.get("probe_position_id") == pos["id"]):
        if pnl > 0:
            conn.execute(
                "UPDATE twelve_sim_breaker SET state='recovered', reset_ts=?, "
                "probe_position_id=NULL, updated_ts=? WHERE id=?",
                (now, now, row["id"]))
            _breaker_log(conn, sym, tf, system,
                         f"试探单盈利 {round(pnl, 4)}U：熔断解除，战绩窗口重新起算",
                         exit_price, now, pos["id"])
        else:
            conn.execute(
                "UPDATE twelve_sim_breaker SET state='tripped', tripped_ts=?, "
                "probe_position_id=NULL, trip_count=trip_count+1, updated_ts=? "
                "WHERE id=?", (now, now, row["id"]))
            _breaker_log(conn, sym, tf, system,
                         f"试探单未盈利（{round(pnl, 4)}U）：继续熔断，重新计冷却",
                         exit_price, now, pos["id"])
        return
    if row and row["state"] in ("tripped", "probing"):
        return   # 熔断中（非试探平仓，如存量持仓自然退出）：不重复评估
    window = max(1, int(_gate_num("twelve_cb_window", CB_WINDOW_DEFAULT)))
    min_trades = max(1, int(_gate_num("twelve_cb_min_trades", CB_MIN_TRADES_DEFAULT)))
    reset_ts = float(row["reset_ts"] or 0.0) if row else 0.0
    rows = conn.execute(
        "SELECT pnl FROM twelve_sim_trade WHERE symbol=? AND tf=? AND system=? "
        "AND exit_ts>? ORDER BY exit_ts DESC, id DESC LIMIT ?",
        (sym, tf, system, reset_ts, window)).fetchall()
    pnls = [float(r["pnl"]) for r in rows]
    if len(pnls) < min_trades:
        return
    win_rate = 100.0 * sum(1 for x in pnls if x > 0) / len(pnls)
    net = sum(pnls)
    min_wr = _gate_num("twelve_cb_min_winrate", CB_MIN_WINRATE_DEFAULT)
    max_loss = _gate_num("twelve_cb_max_loss", CB_MAX_LOSS_DEFAULT)
    if win_rate < min_wr and net < -max_loss:
        cd_h = _gate_num("twelve_cb_cooldown_hours", CB_COOLDOWN_HOURS_DEFAULT)
        if row:
            conn.execute(
                "UPDATE twelve_sim_breaker SET state='tripped', tripped_ts=?, "
                "probe_position_id=NULL, trip_count=trip_count+1, updated_ts=? "
                "WHERE id=?", (now, now, row["id"]))
        else:
            conn.execute(
                "INSERT INTO twelve_sim_breaker (symbol, tf, system, state, "
                "tripped_ts, probe_position_id, reset_ts, trip_count, updated_ts) "
                "VALUES (?,?,?,'tripped',?,NULL,?,1,?)",
                (sym, tf, system, now, reset_ts, now))
        _breaker_log(conn, sym, tf, system,
                     f"信号×周期熔断触发：近 {len(pnls)} 笔胜率 {win_rate:.1f}% "
                     f"净亏 {net:.2f}U（阈值 胜率<{min_wr:g}% 且 净亏<-{max_loss:g}U），"
                     f"只推信号不开仓，{cd_h:g}h 后半开试探", exit_price, now)


def breaker_states(symbol: str | None = None) -> list[dict]:
    """熔断器状态查询（看板/归因/冒烟/调试用）。"""
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_breaker WHERE symbol=? "
                "ORDER BY tf, system", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_breaker ORDER BY symbol, tf, system")
        return [dict(r) for r in cur.fetchall()]


def _trend_context(sym: str) -> tuple[str | None, str | None]:
    """1h 威科夫趋势语境 → (side, phase)；复用 jarvis_wyckoff.analyze 的进程内
    指纹缓存（新 1h bar 才重算，不新增出网压力）。

    数据不可用 / ok:false / stale → (None, None)：S5 放行不阻塞
    （可用性优先，与 seatbelt 同哲学——过滤器坏了不能把交易引擎卡死）。
    """
    try:
        import jarvis_wyckoff as jw
        out = jw.analyze(sym, "1h")
        if not isinstance(out, dict) or not out.get("ok") or out.get("stale"):
            return None, None
        st = out.get("state") or {}
        return st.get("side"), st.get("phase")
    except Exception:  # noqa: BLE001 — 过滤器故障=放行，绝不拖垮开仓主链路
        return None, None


def _pre_gate(conn, sym: str, tf: str, system: str, direction: str,
              strength: float, now: float) -> tuple[str | None, bool, list[str]]:
    """信号级门禁链（参数无关，开仓/挂计划/成交前置）
    → (reject_reason|None, 试探标记, 门禁降权标签列表)。

    链序：S4 周期门禁（TF 开关 + 置信档）→ S5 高周期逆势过滤 → S3 战绩熔断。
    D6 起 S4 置信档 / S3 熔断默认 mode=deweight：不拒单，返回标签由调用方经
    _apply_gate_tags 打标降权（tf_lowconf×0.5 / breaker_deweight×0.25），信号
    继续跑、样本继续攒；mode=reject 回退旧硬拒单（零回归通道，行为与 D6 前
    字节级一致）。TF 显式停用（twelve_tf_enabled=0）是运营指令，两种 mode
    下都保持硬拒。deweight 模式下熔断半开试探自动旁路（一直在跑无需试探），
    _breaker_on_close 战绩簿记与 trip/recover 状态机原样保留。
    """
    tags: list[str] = []
    # S4 周期再平衡：TF 停用 → 拒 'tf_gate'（mode 无关）；置信不足 → 按 mode
    if _tf_gate_num("twelve_tf_enabled", tf, TF_ENABLED_DEFAULT, 1.0) < 0.5:
        return "tf_gate", False, []
    if strength < _tf_gate_num("twelve_tf_min_confidence", tf,
                               TF_MIN_CONF_DEFAULT, 0.0):
        if str(_gate_cfg("twelve_tf_gate_mode",
                         TF_GATE_MODE_DEFAULT)).lower() == "reject":
            return "tf_gate", False, []
        tags.append("tf_lowconf")
    # S5 高周期趋势逆势过滤：仅短周期生效；1h 威科夫 dist-C/D/E 逆多、acc-C/D/E 逆空。
    # D3 起默认 mode=deweight——本处不再拒单，改由 _context_layers 打标降权继续跑
    # （诊断实验场纪律：不关信号只降权）；mode=reject 回退旧硬拒单（零回归通道）。
    if (tf in TREND_FILTER_TFS
            and _gate_num("twelve_trend_filter_enabled",
                          float(TREND_FILTER_ENABLED_DEFAULT)) >= 0.5
            and str(_gate_cfg("twelve_trend_filter_mode",
                              TREND_FILTER_MODE_DEFAULT)).lower() == "reject"):
        side, phase = _trend_context(sym)
        if phase in TREND_FILTER_PHASES and (
                (side == "dist" and direction == "long")
                or (side == "acc" and direction == "short")):
            return "counter_trend", False, []
    # S3 战绩熔断：mode=reject 走旧 _breaker_gate（拒单/冷却半开试探）；
    # mode=deweight 熔断中（tripped/probing）打标降权继续跑，试探机制旁路
    if str(_gate_cfg("twelve_cb_mode", CB_MODE_DEFAULT)).lower() == "reject":
        reason, probe = _breaker_gate(conn, sym, tf, system, now)
        return reason, probe, ([] if reason else tags)
    row = _breaker_row(conn, sym, tf, system)
    if row and str(row["state"]) in ("tripped", "probing"):
        tags.append("breaker_deweight")
    return None, False, tags


def _maybe_update_plan(conn, pen: dict, pts: dict, price: float,
                       now: float) -> dict | None:
    """计划未成交期间信号点位跟随：entry/SL/TP 实质变化（≥0.2% 同阈值）
    → 更新 pending 行点位并记 twelve_sim_signal_log（applied=1）。"""
    old_entry = float(pen["entry_price"])
    old_sl = float(pen["stop_loss"]) if pen.get("stop_loss") is not None else None
    old_tp = float(pen["take_profit"]) if pen.get("take_profit") is not None else None

    upd_entry = _sltp_changed(old_entry, pts["entry"])
    upd_sl = _sltp_changed(old_sl, pts["stop_loss"])
    upd_tp = _sltp_changed(old_tp, pts["take_profit"])
    if not (upd_entry or upd_sl or upd_tp):
        return None

    new_entry = pts["entry"] if upd_entry else old_entry
    new_sl = pts["stop_loss"] if upd_sl else old_sl
    new_tp = pts["take_profit"] if upd_tp else old_tp
    conn.execute(
        "UPDATE twelve_sim_position SET entry_price=?, stop_loss=?, take_profit=?, "
        "cur_price=? WHERE id=?",
        (new_entry, new_sl, new_tp, price, pen["id"]))
    pen["entry_price"], pen["stop_loss"], pen["take_profit"] = new_entry, new_sl, new_tp

    kinds = ",".join(k for k, u in (("entry", upd_entry), ("sl", upd_sl),
                                    ("tp", upd_tp)) if u)
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
        """,
        (now, pen["symbol"], pen["tf"], pen["system"],
         NAME_CN.get(str(pen["system"]), str(pen["system"])), pen["id"],
         old_entry, old_sl, old_tp, new_entry, new_sl, new_tp,
         price, kinds, "计划(pending)点位跟随"))
    return {"symbol": pen["symbol"], "tf": pen["tf"], "system": pen["system"],
            "position_id": pen["id"], "change_kinds": kinds, "applied": True,
            "entry": new_entry, "stop_loss": new_sl, "take_profit": new_tp,
            "pending": True}


def _fill_plan(conn, pen: dict, plan: dict | None, eff: dict, balance: float,
               price: float, now: float,
               pre_tags: list[str] | None = None) -> dict | None:
    """计划触达成交：以计划 entry 价转正式持仓（entry_ts=成交时刻），
    SL/TP/杠杆/qty 经 _resolve_entry_params 基于 entry 价合成回填；
    成交落 twelve_sim_signal_log 留痕（change_kinds=fill）。

    点位相对 entry 不自洽（配置/信号漂移）→ 返回 None，由调用方撤销计划；
    风控门禁（_risk_gate）拦截 → 返回 {"rejected": True, "reason": ...}，
    由调用方 _reject_plan 留痕（挂单期间配置可能已收紧，成交时刻再验一次）。
    """
    entry = float(pen["entry_price"])
    tf = str(pen["tf"])
    params = _resolve_entry_params(str(pen["direction"]), entry, eff, plan, tf)
    if params is None:
        return None
    risk = _risk_gate(tf, entry, params, str(pen["symbol"]))
    if risk:
        return {"rejected": True, "reason": risk}
    _context_layers(str(pen["symbol"]), tf, str(pen["system"]),
                    str(pen["direction"]), params, now)   # D2+：成交时刻打标降权
    _apply_gate_tags(params, pre_tags)   # D6：成交时刻门禁降权标签（同时刻重评）
    sf = float(params.get("size_factor") or 1.0)
    margin = round(min(balance, balance * params["position_pct"] / 100.0 * sf), 8)
    qty = round(margin * params["leverage"] / entry, 8)
    ctx = _ctx_snapshot(str(pen["symbol"]), tf, now)   # D0：成交时刻=开仓时刻快照
    conn.execute(
        """
        UPDATE twelve_sim_position
        SET entry_ts=?, qty=?, margin=?, leverage=?, position_pct=?,
            stop_loss=?, take_profit=?, cur_price=?, unrealized_pnl=0,
            status='open',
            ctx_regime=?, ctx_regime_dir=?, ctx_atr_pct=?, ctx_vol_bucket=?,
            ctx_wyckoff=?, ctx_funding=?, ctx_oi_btc_chg=?, ctx_hour_utc=?,
            ctx_btc_trend=?, context_tags=?, size_factor=?
        WHERE id=? AND status='pending'
        """,
        (now, qty, margin, params["leverage"], params["position_pct"],
         params["stop_loss"], params["take_profit"], price,
         *(ctx.get(f) for f in CTX_FIELDS), _ctx_tags_str(params), sf, pen["id"]))
    _log_context(conn, str(pen["symbol"]), tf, str(pen["system"]),
                 int(pen["id"]), price, now, params)
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,'fill',1,?)
        """,
        (now, pen["symbol"], pen["tf"], pen["system"],
         NAME_CN.get(str(pen["system"]), str(pen["system"])), pen["id"],
         entry, entry, params["stop_loss"], params["take_profit"], price,
         "计划触达成交（按计划价成交，成交后独立于后续信号变化）"))
    return {"symbol": pen["symbol"], "tf": pen["tf"], "system": pen["system"],
            "direction": pen["direction"], "entry_price": entry, "qty": qty,
            "margin": margin, "leverage": params["leverage"],
            "stop_loss": params["stop_loss"], "take_profit": params["take_profit"],
            "position_id": pen["id"],
            "context_tags": _ctx_tags_str(params), "size_factor": sf}


# ─────────────────────────── 持仓点位跟随（信号更新 → 同步 SL/TP + 变更日志） ───────────────────────────

def _sltp_changed(old: float | None, new: float | None) -> bool:
    """新点位是否构成实质变更：旧值缺失时有新值即算；否则相对变化 ≥ 阈值。"""
    if new is None or new <= 0:
        return False
    if old is None or old <= 0:
        return True
    return abs(new - old) / old * 100.0 >= SLTP_MIN_CHANGE_PCT


def _log_signal_drift(conn, pos: dict, sig: dict | None,
                      price: float, now: float) -> dict | None:
    """持仓期间同级别信号点位漂移留痕（R3 规则3：已成交仓位不受后续信号影响）。

    已开仓位的 SL/TP **绝不**跟随信号更新（成交即独立，只按自身
    liq/sl/tp/timeout 生命周期退出）；此处仅在同方向信号的 plan 点位相对持仓
    当前风控点位实质变化（≥ SLTP_MIN_CHANGE_PCT%）时记 applied=0 留痕，
    供看板追溯「信号变了但仓位保持独立」。

    防重：与该持仓最近一条日志的 new 值一致（同阈值口径）则不重复记录。
    返回日志摘要 dict（applied 恒为 False），无实质变化返回 None。
    """
    plan = (sig or {}).get("plan") or {}
    if not isinstance(plan, dict) or not plan:
        return None
    try:
        new_sl = float(plan["stop_loss"]) if plan.get("stop_loss") is not None else None
        tp_raw = plan.get("take_profit") or plan.get("take_profit_1")
        new_tp = float(tp_raw) if tp_raw is not None else None
        new_entry = float(plan["entry"]) if plan.get("entry") is not None else None
    except (TypeError, ValueError):
        return None
    old_sl = float(pos["stop_loss"]) if pos.get("stop_loss") is not None else None
    old_tp = float(pos["take_profit"]) if pos.get("take_profit") is not None else None

    drift_sl = _sltp_changed(old_sl, new_sl)
    drift_tp = _sltp_changed(old_tp, new_tp)
    if not (drift_sl or drift_tp):
        return None

    # 防重：信号维持同一组点位时每轮都会走到这里，与最近一条日志一致则跳过
    last = conn.execute(
        "SELECT new_sl, new_tp FROM twelve_sim_signal_log "
        "WHERE position_id=? ORDER BY id DESC LIMIT 1",
        (pos["id"],)).fetchone()
    if last is not None:
        last_sl = float(last["new_sl"]) if last["new_sl"] is not None else None
        last_tp = float(last["new_tp"]) if last["new_tp"] is not None else None
        if (not _sltp_changed(last_sl, new_sl)
                and not _sltp_changed(last_tp, new_tp)):
            return None

    kinds = ",".join(k for k, u in (("sl", drift_sl), ("tp", drift_tp)) if u)
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
        """,
        (now, pos["symbol"], pos["tf"], pos["system"],
         NAME_CN.get(str(pos["system"]), str(pos["system"])), pos["id"],
         float(pos["entry_price"]), old_sl, old_tp,
         new_entry, new_sl, new_tp, price, kinds,
         "信号点位已更新，但已成交仓位保持独立不跟随（新信号另行计算计划）"))
    return {"symbol": pos["symbol"], "tf": pos["tf"], "system": pos["system"],
            "position_id": pos["id"], "change_kinds": kinds,
            "applied": False, "stop_loss": old_sl, "take_profit": old_tp}


def signal_logs(symbol: str | None = None, tf: str | None = None,
                system: str | None = None, limit: int = 200) -> list[dict]:
    """点位跟随变更日志查询（CLI / 同步器 / 调试用）。"""
    _ensure_init()
    cond, args = [], []
    if symbol:
        cond.append("symbol=?")
        args.append(_norm_symbol(symbol))
    if tf:
        cond.append("tf=?")
        args.append(tf)
    if system:
        cond.append("system=?")
        args.append(system)
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    with _conn() as conn:
        cur = conn.execute(
            f"SELECT * FROM twelve_sim_signal_log{where} "
            f"ORDER BY ts DESC, id DESC LIMIT ?", (*args, max(1, int(limit))))
        return [dict(r) for r in cur.fetchall()]


# ─────────────────────────── 主循环：一轮 ───────────────────────────

_DIR_OF_SIGNAL = {"bullish": "long", "bearish": "short"}


def run_cycle(symbols: list[str] | None = None, cfg: dict | None = None,
              now: float | None = None) -> dict:
    """跑一轮：每个配置币种 → 计划盯盘（失效留痕/点位跟随/触达成交）→ 持仓盯盘
    （liq/sl/tp/timeout，含影线；已成交仓位独立，不受信号变更影响）
    → 开仓/挂计划（有点位未触达只挂 pending；反向信号=新独立计划）。

    symbols=None 时用 twelve_sim_config 里已启用的币种；cfg 为取价配置
    （None 自动加载执行手配置）。单币失败只记日志跳过，永不抛出。
    """
    _ensure_init()
    syms = ([_norm_symbol(s) for s in symbols] if symbols
            else configured_symbols())
    out: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "symbols": {}}
    if not syms:
        out["note"] = "无已启用币种（先 config-set 配置币种级行）"
        return out
    if cfg is None:
        cfg = _load_cfg()
    for sym in syms:
        try:
            out["symbols"][sym] = _cycle_symbol(sym, cfg, now)
        except Exception as exc:  # noqa: BLE001 — 单币异常绝不拖垮整轮
            out["symbols"][sym] = {"error": repr(exc)[:200]}
            _log(f"❌ {sym} 本轮异常（已兜底）: {exc!r}"[:200])
    return out


def _cycle_symbol(sym: str, cfg: dict, now: float | None = None) -> dict:
    ts = float(now if now is not None else time.time())
    price = latest_price(cfg, sym)
    if price is None or price <= 0:
        return {"skipped": "无现价"}
    mark = mark_price_of(cfg, sym)   # 爆仓判定用标记价；None 时 _exit_check 回退成交价
    cfg_rows = list_configs(sym)
    ensure_wallets(sym, cfg_rows)
    signals = read_signals(sym)
    res = {"price": price, "closed": [], "opened": [], "holds": 0,
           "sltp_updates": [], "planned": [], "filled": [], "canceled": [],
           "plan_updates": [], "rejected": []}

    with _conn() as conn:
        pos_rows = [dict(p) for p in conn.execute(
            "SELECT * FROM twelve_sim_position WHERE status='open' AND symbol=?",
            (sym,)).fetchall()]
        pend_rows = conn.execute(
            "SELECT * FROM twelve_sim_position WHERE status='pending' AND symbol=?",
            (sym,)).fetchall()
        pendings = {(str(p["tf"]), str(p["system"])): dict(p) for p in pend_rows}
        wal_rows = conn.execute(
            "SELECT tf, system, balance FROM twelve_sim_wallet WHERE symbol=?",
            (sym,)).fetchall()
        balances = {(str(w["tf"]), str(w["system"])): float(w["balance"])
                    for w in wal_rows}
        bars_cache: dict[str, list[dict] | None] = {}   # 每 TF 只拉一次 K 线
        # 本轮成交槽位 → 成交方向（成交后同槽位同方向本轮不再评估新计划）
        filled_dirs: dict[tuple[str, str], set[str]] = {}
        # 超时撤销的槽位本轮不再重挂（否则同轮重建=无限续期）；下一轮信号仍在则重新评估
        timeout_slots: set[tuple[str, str]] = set()

        # 0) 计划(pending)盯盘：失效判定 → 点位跟随 → 触达成交
        #    （R3 规则2：失效判定先于触达成交——信号已变更时，价格触达旧点位也不成交）
        for slot, pen in list(pendings.items()):
            tf_, system_ = slot
            sig = signals.get(slot)
            want = _DIR_OF_SIGNAL.get((sig or {}).get("direction") or "")
            pts = _plan_points((sig or {}).get("plan"))
            eff = effective_config(sym, tf_, system_, cfg_rows)

            reason = None
            if want is None:
                reason = "neutral"           # 信号转中性 → 旧计划失效
            elif want != str(pen["direction"]):
                reason = "flip"              # 信号反向 → 旧计划失效
            elif pts is None:
                reason = "plan_gone"         # 计划消失/entry 失效 → 失效
            elif not eff["enabled"]:
                reason = "disabled"          # 槽位停用 → 失效
            elif _timeout_due(pen, ts):
                reason = "timeout"           # 挂单超过 TF 超时档 → 失效
            if reason:
                res["canceled"].append(_cancel_plan(conn, pen, reason, price, ts))
                pendings.pop(slot)
                if reason == "timeout":
                    timeout_slots.add(slot)
                continue

            # 点位跟随（同方向信号点位更新 → pending 计划以最新信号为准；
            # 先更新再判触达，触达判定用最新 entry）
            upd = _maybe_update_plan(conn, pen, pts, price, ts)
            if upd:
                res["plan_updates"].append(upd)

            if tf_ not in bars_cache:
                bars_cache[tf_] = _fetch_bars(sym, tf_)
            if _entry_touched(str(pen["direction"]), pts["entry_type"],
                              float(pen["entry_price"]), price,
                              bars_cache[tf_], float(pen["entry_ts"])):
                balance = balances.get(slot, DEFAULT_PRINCIPAL)
                if balance < MIN_BALANCE:
                    res["canceled"].append(_cancel_plan(conn, pen, "broke", price, ts))
                    pendings.pop(slot)
                    continue
                # S3+：成交时刻再过一次信号级门禁（挂单期间战绩/配置可能已恶化）
                gate, probe, gtags = _pre_gate(
                    conn, sym, tf_, system_, str(pen["direction"]),
                    float((sig or {}).get("strength") or 0.0), ts)
                if gate:
                    res["rejected"].append(_reject_plan(conn, pen, gate, price, ts))
                    pendings.pop(slot)
                    continue
                filled = _fill_plan(conn, pen, (sig or {}).get("plan"),
                                    eff, balance, price, ts, pre_tags=gtags)
                if filled is None:
                    # 点位相对 entry 不自洽（配置/信号漂移）→ 宁缺毋滥失效
                    res["canceled"].append(
                        _cancel_plan(conn, pen, "incoherent", price, ts))
                    pendings.pop(slot)
                    continue
                if filled.get("rejected"):
                    # 成交时刻风控门禁拦截（S1+）→ 拒单留痕，不成交
                    res["rejected"].append(
                        _reject_plan(conn, pen, filled["reason"], price, ts))
                    pendings.pop(slot)
                    continue
                if probe:
                    _breaker_mark_probe(conn, sym, tf_, system_,
                                        int(filled["position_id"]), price, ts)
                res["filled"].append(filled)
                filled_dirs.setdefault(slot, set()).add(str(pen["direction"]))
                pendings.pop(slot)
                continue
            # 未触达继续挂：仅刷新展示用现价
            conn.execute("UPDATE twelve_sim_position SET cur_price=? WHERE id=?",
                         (price, pen["id"]))

        # 1) 持仓盯盘：优先级 liq > sl > tp > timeout。
        #    R3 规则3：已成交仓位独立——信号反向不再 flip 平仓、SL/TP 不跟随信号，
        #    只按自身生命周期退出；同方向信号点位漂移仅记 applied=0 留痕。
        #    同槽位可能并存多笔仓位（1多+1空），浮盈按槽位累计后更新钱包 equity。
        open_dirs: dict[tuple[str, str], set[str]] = {}
        upnl_by_slot: dict[tuple[str, str], float] = {}
        for pos in pos_rows:
            slot = (str(pos["tf"]), str(pos["system"]))
            tf_ = slot[0]
            if tf_ not in bars_cache:
                bars_cache[tf_] = _fetch_bars(sym, tf_)
            hit = _exit_check(pos, price, bars_cache[tf_], mark_price=mark)
            if hit:
                reason, exit_px = hit
                res["closed"].append(_do_close(conn, pos, exit_px, reason, ts))
                continue
            if _timeout_due(pos, ts):
                # TF 分档持仓超时：以现价平仓
                res["closed"].append(_do_close(conn, pos, price, "timeout", ts))
                continue
            # 持有（本轮不平仓）：同方向信号点位漂移留痕（不应用），刷新现价与浮盈
            sig = signals.get(slot)
            want = _DIR_OF_SIGNAL.get((sig or {}).get("direction") or "")
            if want == str(pos["direction"]):
                upd = _log_signal_drift(conn, pos, sig, price, ts)
                if upd:
                    res["sltp_updates"].append(upd)
            sign = 1.0 if pos["direction"] == "long" else -1.0
            upnl = round(max((price - float(pos["entry_price"]))
                             * float(pos["qty"]) * sign,
                             -float(pos["margin"])), 8)
            conn.execute(
                "UPDATE twelve_sim_position SET cur_price=?, unrealized_pnl=? "
                "WHERE id=?", (price, upnl, pos["id"]))
            open_dirs.setdefault(slot, set()).add(str(pos["direction"]))
            upnl_by_slot[slot] = upnl_by_slot.get(slot, 0.0) + upnl
            res["holds"] += 1
        for slot, upnl in upnl_by_slot.items():
            conn.execute(
                "UPDATE twelve_sim_wallet SET equity=balance+?, updated_ts=? "
                "WHERE symbol=? AND tf=? AND system=?",
                (round(upnl, 8), ts, sym, slot[0], slot[1]))

        # 2) 开仓/挂计划（R3 规则1+3）：
        #    - 同槽位已有**同方向**持仓 → 视为同一信号观点延续，不重复建仓；
        #    - 反向信号即使有持仓也作为**新的独立计划**评估（不影响已成交仓位）；
        #    - 有有效点位必须触达才成交，未触达一律挂 pending。
        wal_rows = conn.execute(
            "SELECT tf, system, balance FROM twelve_sim_wallet WHERE symbol=?",
            (sym,)).fetchall()
        balances = {(str(w["tf"]), str(w["system"])): float(w["balance"])
                    for w in wal_rows}
        for slot, sig in signals.items():
            tf, system = slot
            if (slot in pendings or slot in timeout_slots
                    or tf not in TFS or system not in SYSTEMS):
                continue
            direction = _DIR_OF_SIGNAL.get(sig["direction"])
            if not direction:
                continue   # neutral 不动
            occupied = open_dirs.get(slot, set()) | filled_dirs.get(slot, set())
            if direction in occupied:
                continue   # 同方向已有持仓 → 不重复建仓
            eff = effective_config(sym, tf, system, cfg_rows)
            if not eff["enabled"]:
                continue
            balance = balances.get(slot, DEFAULT_PRINCIPAL)
            if balance < MIN_BALANCE:
                continue   # 槽位已爆仓，停开
            plan = sig.get("plan")
            pts = _plan_points(plan)
            # S3+ 信号级门禁链（战绩熔断等）：开仓与挂计划都在此前置拦截。
            # D6：gtags 门禁降权标签——立即开仓在下方落 params；挂计划(pending)
            # 不落（成交时刻 _pre_gate 重评，用成交时刻的门禁状态打标）
            gate, probe, gtags = _pre_gate(conn, sym, tf, system, direction,
                                           float(sig.get("strength") or 0.0), ts)
            if gate:
                rej = _do_reject(conn, sym, tf, system, direction, gate,
                                 float((pts or {}).get("entry") or price),
                                 (pts or {}).get("stop_loss"),
                                 (pts or {}).get("take_profit"), price, ts)
                if rej:
                    res["rejected"].append(rej)
                continue
            if (pts and not _entry_touched(direction, pts["entry_type"],
                                           pts["entry"], price, None, ts)):
                # 有点位且现价未触达 → 只挂计划(pending)，价到才成交（规则1）
                res["planned"].append(
                    _do_plan(conn, sym, tf, system, direction, pts, eff, price, ts))
                continue
            # 计划缺失（无点位可比）/ 现价已处于可成交侧 → 按现价立即成交（现有口径）
            params = _resolve_entry_params(direction, price, eff, plan, tf)
            if params is None:
                continue   # 点位缺失或不自洽，宁缺毋滥
            risk = _risk_gate(tf, price, params, sym)
            if risk:
                # 风控门禁拦截（S1+）→ 拒单留痕，不静默丢弃
                rej = _do_reject(conn, sym, tf, system, direction, risk,
                                 price, params["stop_loss"],
                                 params["take_profit"], price, ts)
                if rej:
                    res["rejected"].append(rej)
                continue
            _apply_gate_tags(params, gtags)   # D6：门禁降权标签落 params
            opened = _do_open(conn, sym, tf, system,
                              direction, price, balance, params, ts)
            if probe:
                _breaker_mark_probe(conn, sym, tf, system,
                                    int(opened["position_id"]), price, ts)
            res["opened"].append(opened)

        # 3) 已失效(canceled)/已拒单(rejected)留痕行到期清理
        #    （日志表留痕永久，行级留痕仅保窗口期）
        conn.execute(
            "DELETE FROM twelve_sim_position WHERE symbol=? AND ("
            "(status='canceled' AND canceled_ts IS NOT NULL AND canceled_ts < ?) "
            "OR (status='rejected' AND COALESCE(canceled_ts, entry_ts) < ?))",
            (sym, ts - CANCELED_RETENTION_DAYS * 86400.0,
             ts - REJECTED_RETENTION_DAYS * 86400.0))

    if (res["closed"] or res["opened"] or res["sltp_updates"] or res["planned"]
            or res["filled"] or res["canceled"] or res["plan_updates"]
            or res["rejected"]):
        _log(f"🧭 {sym} 槽位轮：价 {price} / 平 {len(res['closed'])} "
             f"/ 开 {len(res['opened'])} / 持 {res['holds']} "
             f"/ 计划 +{len(res['planned'])} 成交 {len(res['filled'])} "
             f"撤 {len(res['canceled'])} 拒 {len(res['rejected'])} "
             f"/ 点位跟随 {len(res['sltp_updates']) + len(res['plan_updates'])}")
    return res


# ─────────────────────────── 状态汇总 ───────────────────────────

def status(symbol: str | None = None) -> dict:
    """槽位战绩汇总（钱包 + 在途持仓数 + 计划中数），按累计盈亏降序。"""
    wallets = get_wallets(symbol)
    opens = open_positions(symbol)
    pends = pending_positions(symbol)
    open_slots = {(p["symbol"], p["tf"], p["system"]) for p in opens}
    rows = []
    for w in wallets:
        rows.append({**w, "has_open": (w["symbol"], w["tf"], w["system"]) in open_slots})
    rows.sort(key=lambda x: -(x.get("total_pnl") or 0.0))
    return {"wallets": rows, "open_positions": len(opens),
            "pending_plans": len(pends)}


def to_markdown(st: dict, top: int = 20) -> str:
    lines = ["# 十二系统 × 六时间轴 模拟台账", "",
             f"- 在途持仓 {st['open_positions']} 笔 | 计划中 "
             f"{st.get('pending_plans', 0)} 笔 | 槽位钱包 {len(st['wallets'])} 个",
             "",
             "| 币种 | 时间轴 | 系统 | 余额U | 笔数 | 胜率% | 累计盈亏U | PF | 最大回撤% | 在途 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for w in st["wallets"][:top]:
        lines.append(
            f"| {w['symbol']} | {w['tf']} | {w.get('name_cn') or w['system']} "
            f"| {round(w['balance'], 2)} | {w['total_trades']} "
            f"| {w['win_rate'] if w['win_rate'] is not None else '—'} "
            f"| {round(w['total_pnl'], 2)} "
            f"| {w['profit_factor'] if w['profit_factor'] is not None else '—'} "
            f"| {w['max_drawdown_pct'] if w['max_drawdown_pct'] is not None else '—'} "
            f"| {'●' if w['has_open'] else ''} |")
    return "\n".join(lines)


# ─────────────────────────── CLI（独立进程，绝不挂 dashboard） ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="十二系统×六时间轴槽位级模拟交易引擎（paper-only 独立进程）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cyc = sub.add_parser("cycle", help="跑一轮（默认读配置币种）")
    p_cyc.add_argument("--symbols", default=None, help="逗号分隔，如 ETH,BTC；缺省读配置")
    p_cyc.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="常驻循环（独立进程模式）")
    p_run.add_argument("--symbols", default=None)
    p_run.add_argument("--interval-min", type=float, default=5.0,
                       help="轮询间隔（分钟），默认 5")

    p_st = sub.add_parser("status", help="槽位战绩榜")
    p_st.add_argument("--symbol", default=None)
    p_st.add_argument("--json", action="store_true")

    p_cs = sub.add_parser("config-set", help="写一条配置（币种级/tf组级/信号级）")
    p_cs.add_argument("symbol")
    p_cs.add_argument("--tf", default=None, help=f"tf 组级/信号级 scope（{'/'.join(TFS)}）")
    p_cs.add_argument("--system", default=None, help="信号级 scope（system 编码）")
    p_cs.add_argument("--principal", type=float, default=None)
    p_cs.add_argument("--leverage", type=float, default=None)
    p_cs.add_argument("--position-pct", type=float, default=None)
    p_cs.add_argument("--stop-loss-pct", type=float, default=None)
    p_cs.add_argument("--take-profit-pct", type=float, default=None)
    p_cs.add_argument("--disable", action="store_true", help="停用该 scope")
    p_cs.add_argument("--enable", action="store_true", help="启用该 scope")

    p_cl = sub.add_parser("config-list", help="列配置")
    p_cl.add_argument("--symbol", default=None)

    args = ap.parse_args()

    if args.cmd == "cycle":
        syms = ([s.strip() for s in args.symbols.split(",") if s.strip()]
                if args.symbols else None)
        out = run_cycle(syms)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for sym, r in out.get("symbols", {}).items():
                if r.get("skipped") or r.get("error"):
                    print(f"{sym}: {r.get('skipped') or r.get('error')}")
                else:
                    print(f"{sym}: 价 {r['price']} / 平 {len(r['closed'])} "
                          f"/ 开 {len(r['opened'])} / 持 {r['holds']} "
                          f"/ 计划 +{len(r.get('planned') or [])} "
                          f"成交 {len(r.get('filled') or [])} "
                          f"撤 {len(r.get('canceled') or [])} "
                          f"拒 {len(r.get('rejected') or [])} "
                          f"/ 点位跟随 {len(r.get('sltp_updates') or []) + len(r.get('plan_updates') or [])}")
            if out.get("note"):
                print(out["note"])
    elif args.cmd == "run":
        syms = ([s.strip() for s in args.symbols.split(",") if s.strip()]
                if args.symbols else None)
        interval = max(30.0, args.interval_min * 60.0)
        _log(f"▶️ 槽位模拟引擎启动：symbols={syms or '（配置币种）'} "
             f"间隔 {args.interval_min}min")
        while True:
            try:
                run_cycle(syms)
            except Exception as exc:  # noqa: BLE001 — 循环永不退出
                _log(f"❌ 槽位轮异常（继续）: {exc!r}"[:200])
            time.sleep(interval)
    elif args.cmd == "status":
        st = status(args.symbol)
        print(json.dumps(st, ensure_ascii=False, indent=2) if args.json
              else to_markdown(st))
    elif args.cmd == "config-set":
        enabled = True if args.enable else (False if args.disable else None)
        r = upsert_config(args.symbol, args.tf, args.system,
                          principal=args.principal, leverage=args.leverage,
                          position_pct=args.position_pct,
                          stop_loss_pct=args.stop_loss_pct,
                          take_profit_pct=args.take_profit_pct,
                          enabled=enabled)
        print(json.dumps(r, ensure_ascii=False))
    elif args.cmd == "config-list":
        for row in list_configs(args.symbol):
            print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
