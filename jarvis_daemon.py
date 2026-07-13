#!/usr/bin/env python3
"""贾维斯 JARVIS - 7×24 定时引擎（心跳）。

让贾维斯真正"活着"：按固定周期自动跑决策快照落库 + 回填到期战绩，
持续积累诚实的前向准确率，无需人工每天手动敲命令。

每个周期对每个币种执行：
  1) jarvis_journal.record(symbol)   落一条今日决策快照
  2) jarvis_journal.evaluate(symbol) 回填已到期快照的真实收益/命中

设计原则：
  - 永不因单次失败而退出：网络/数据异常只记日志并继续下个周期（会话保护级健壮）。
  - 幂等：同一天重复 record 会更新而非重复落库（journal 自带 UNIQUE 约束）。
  - 可观测：每轮写日志到 ~/.vibe-trading/jarvis_daemon.log，并落一份最新状态 json。

用法：
  python jarvis_daemon.py --once                       # 跑一轮就退出（cron/launchd 友好）
  python jarvis_daemon.py --symbols BTCUSDT,ETHUSDT     # 常驻循环（默认每 24h）
  python jarvis_daemon.py --interval-hours 6            # 自定义周期
  python jarvis_daemon.py --install-launchd             # 生成 macOS launchd plist（每日定时）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

import jarvis_journal as jj
import jarvis_net

jarvis_net.ensure_proxy()   # 大陆网络：探测本地代理，Binance 等出网自动走代理

LOG_DIR = os.path.expanduser("~/.vibe-trading")
LOG_PATH = os.path.join(LOG_DIR, "jarvis_daemon.log")
STATUS_PATH = os.path.join(LOG_DIR, "jarvis_daemon_status.json")
TWELVE_STATE_PATH = os.path.join(LOG_DIR, "jarvis_twelve_state.json")
PLIST_LABEL = "com.jarvis.daemon"


def _log(msg: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 日志失败不应中断主循环
        pass


def _twelve_events(sym: str, prev: dict, cons: dict, strong_conf: float = 0.75) -> list[dict]:
    """由上一轮状态 + 本轮共识判定应上报的洞察事件（纯函数，便于离线测试）。

    规则（防翻转噪音）：
      - bullish ↔ bearish 互翻 → consensus_flip（强共识 critical，否则 warning）
      - neutral → 方向 且新共识为强（conf ≥ strong_conf）→ consensus_established
      - 方向 → neutral 且原共识曾为强（prev_conf ≥ strong_conf）→ consensus_lost
      - 同向置信度首次站上阈值（或首轮即强）→ strong_signal
      - 其余（弱共识间摆动、neutral↔弱方向）一律静默
    """
    cur_dir = cons.get("direction", "neutral")
    cur_conf = float(cons.get("confidence", 0.0) or 0.0)
    prev_dir = (prev or {}).get("direction")
    prev_conf = float((prev or {}).get("confidence", 0.0) or 0.0)
    dir_cn = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}
    reasoning = cons.get("reasoning", "")
    directional = ("bullish", "bearish")

    if prev_dir in directional and cur_dir in directional and cur_dir != prev_dir:
        return [{
            "kind": "consensus_flip",
            "severity": "critical" if cur_conf >= strong_conf else "warning",
            "title": f"{sym} 十二套共识翻转：{dir_cn[prev_dir]} → {dir_cn[cur_dir]}",
            "detail": reasoning,
        }]
    if prev_dir == "neutral" and cur_dir in directional and cur_conf >= strong_conf:
        return [{
            "kind": "consensus_established",
            "severity": "warning",
            "title": f"{sym} 十二套共识建立：中性 → {dir_cn[cur_dir]}（置信度 {cur_conf:.0%}）",
            "detail": reasoning,
        }]
    if prev_dir in directional and cur_dir == "neutral" and prev_conf >= strong_conf:
        return [{
            "kind": "consensus_lost",
            "severity": "warning",
            "title": f"{sym} 十二套共识消失：{dir_cn[prev_dir]} → 中性",
            "detail": reasoning,
        }]
    if (cur_dir in directional and cur_conf >= strong_conf
            and (prev_dir != cur_dir or prev_conf < strong_conf) and prev_dir != "neutral"):
        return [{
            "kind": "strong_signal",
            "severity": "warning",
            "title": f"{sym} 十二套强{dir_cn[cur_dir]}共识（置信度 {cur_conf:.0%}）",
            "detail": reasoning,
        }]
    return []


def twelve_step(symbols: list[str], strong_conf: float = 0.75) -> dict:
    """十二套技术共识巡检（自主意识）：方向翻转 / 强信号时写 insight 并推送。

    永不抛出；K线拉取失败只跳过该币。状态存 TWELVE_STATE_PATH（原子写），
    insight 落库走 jarvis_reasoning（复用 jarvis_db 连接层），
    推送走 jarvis_notify.notify（未配置渠道自动跳过）。
    """
    res: dict = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "symbols": {}}
    try:
        import jarvis_reasoning as jre
        import jarvis_twelve_systems as jts
        try:
            with open(TWELVE_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f) or {}
        except Exception:  # noqa: BLE001 — 状态文件缺失/损坏视为首轮
            state = {}
        for sym in symbols:
            try:
                # [D2] 战绩口径只认已收盘 bar：进行中 bar 会随行情重绘，污染共识翻转判定
                df = jts.fetch_klines_df(sym, "4h", 300, drop_unclosed=True)
                if df is None or len(df) < 30:
                    res["symbols"][sym] = {"skipped": "K线不足或拉取失败"}
                    continue
                out = jts.analyze(df)
                cons = out["consensus"]
                cur_dir, cur_conf = cons["direction"], float(cons["confidence"])
                events = _twelve_events(sym, state.get(sym) or {}, cons, strong_conf)
                for ev in events:
                    wr = jre.add_insight(sym, ev["kind"], ev["title"],
                                         detail=ev["detail"], severity=ev["severity"])
                    if not wr.get("ok"):
                        _log(f"🧠 {sym} insight 落库失败: {wr.get('error')}")
                    try:
                        import jarvis_notify as jn
                        jn.notify(f"🧠 贾维斯洞察\n{ev['title']}\n{ev['detail'][:300]}")
                    except Exception as e:  # noqa: BLE001 — 推送失败不影响巡检
                        _log(f"🧠 {sym} 洞察推送失败（已兜底）: {repr(e)[:150]}")
                    _log(f"🧠 洞察：{ev['title']}")
                state[sym] = {"direction": cur_dir, "confidence": cur_conf,
                              "score": cons["score"], "ts": time.time()}
                res["symbols"][sym] = {"direction": cur_dir, "confidence": cur_conf,
                                       "events": len(events)}
            except Exception as e:  # noqa: BLE001 — 单币失败不拖垮整轮
                res["symbols"][sym] = {"error": repr(e)[:200]}
                _log(f"🧠 {sym} 十二套巡检异常（已兜底）: {repr(e)[:150]}")
        try:
            # 原子写：先落临时文件再 rename，避免进程被杀时留下半截 JSON
            tmp_path = TWELVE_STATE_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, TWELVE_STATE_PATH)
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001 — 模块级失败也不许抛出
        res["error"] = repr(e)[:300]
        _log("🧠 十二套巡检 ❌ 异常（已兜底）: " + repr(e)[:200])
    res["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return res


# ── pg 健康检查（C3）─────────────────────────────────────────────────────────

_PG_ALERT_COOLDOWN_S = 3600.0  # 宕机告警通知冷却，防止每轮重复刷屏
_pg_health_state: dict = {"down_since": None, "last_alert_ts": 0.0}


def pg_health_check() -> bool:
    """每轮开始前探测数据库连通性。返回 True=健康（SQLite 模式恒健康）。

    pg 不可达时：日志告警 + 经 jarvis_notify 推送一次（冷却 1h），
    调用方据此跳过本轮写库操作（降级而非 crash），下轮自动重试。
    恢复时推一条恢复通知并重置冷却。永不抛出。
    """
    try:
        import jarvis_db as jdb
        r = jdb.ping()
    except Exception as e:  # noqa: BLE001 — 探测自身异常按不健康处理
        r = {"ok": False, "backend": "pg", "error": repr(e)[:200]}
    now = time.time()
    if r.get("ok"):
        if _pg_health_state["down_since"] is not None:
            downtime_min = round((now - _pg_health_state["down_since"]) / 60, 1)
            _log(f"✅ 数据库恢复（{r.get('backend')}），宕机约 {downtime_min} 分钟，恢复写库")
            try:
                import jarvis_notify as jn
                jn.notify(f"✅ 贾维斯数据库已恢复（宕机约 {downtime_min} 分钟），已恢复正常写库")
            except Exception:  # noqa: BLE001 — 通知失败不影响主循环
                pass
            _pg_health_state["down_since"] = None
            _pg_health_state["last_alert_ts"] = 0.0
        return True
    if _pg_health_state["down_since"] is None:
        _pg_health_state["down_since"] = now
    _log(f"🛑 数据库不可达：{r.get('error')} → 本轮降级跳过写库，下轮自动重试")
    if now - _pg_health_state["last_alert_ts"] >= _PG_ALERT_COOLDOWN_S:
        _pg_health_state["last_alert_ts"] = now
        try:
            import jarvis_notify as jn
            jn.notify("🛑 贾维斯数据库(PostgreSQL)不可达，daemon 已降级（跳过写库）。\n"
                      "排查：docker ps | grep quantdinger-db；"
                      "手动拉起：docker start quantdinger-db")
        except Exception:  # noqa: BLE001
            pass
    return False


def run_cycle(symbols: list[str], paper_trade: bool = False) -> dict:
    """跑一轮：对每个 symbol record + evaluate，返回本轮汇总。永不抛出。

    paper_trade=True 时，本轮战绩落库后额外跑一次模拟跟盘（撮合 → 盯平仓 → 找开仓）。
    该步惰性导入、异常隔离，失败只记日志，绝不影响 record/evaluate 主心跳。
    """
    cycle = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "symbols": {}}
    for sym in symbols:
        res = {"record": None, "evaluate": None, "error": None}
        try:
            rec = jj.record(sym)
            res["record"] = rec
            if rec.get("ok"):
                _log(f"{sym} record ✅ {rec.get('as_of_date')} 信心 {rec.get('conviction_score')} → {rec.get('direction')}")
            else:
                _log(f"{sym} record ⚠️ 失败: {rec.get('error')}")
        except Exception as e:  # noqa: BLE001
            res["error"] = repr(e)[:300]
            _log(f"{sym} record ❌ 异常: {res['error']}")
        try:
            ev = jj.evaluate(sym)
            res["evaluate"] = ev
            _log(f"{sym} evaluate ✅ 回填 {ev.get('outcomes_filled')} 条, {ev.get('not_due')} 条未到期")
        except Exception as e:  # noqa: BLE001
            res["error"] = (res["error"] or "") + " | eval:" + repr(e)[:200]
            _log(f"{sym} evaluate ❌ 异常: {repr(e)[:200]}")
        cycle["symbols"][sym] = res
    # T-09 组合级熔断周期巡检：主动监控组合健康，持仓中途暴跌也能触发，
    # 不必等到下一次开仓。惰性导入 + 异常隔离，绝不影响 record/evaluate 主心跳。
    try:
        import jarvis_circuit_breaker as _cb
        _ev = _cb.evaluate()
        if _ev.get("should_halt") and not _ev.get("already_tripped"):
            _tr = _cb.trip(_cb._summarize(_ev["triggers"]))
            cycle["circuit_breaker"] = {"tripped": True, "reason": _tr.get("reason"),
                                        "drawdown_pct": _ev.get("drawdown_pct")}
            _log("🛑 熔断触发：" + str(_tr.get("reason")) + "（已取消挂单+告警，后续开仓被阻断）")
        else:
            cycle["circuit_breaker"] = {"tripped": bool(_ev.get("already_tripped")),
                                        "should_halt": bool(_ev.get("should_halt")),
                                        "drawdown_pct": _ev.get("drawdown_pct"),
                                        "equity_usdt": _ev.get("equity_usdt")}
            _log(f"🛡️ 熔断巡检：回撤 {_ev.get('drawdown_pct')}% / 已熔断 {bool(_ev.get('already_tripped'))}")
    except Exception as e:  # noqa: BLE001 — 熔断巡检失败不影响主心跳
        cycle["circuit_breaker"] = {"error": repr(e)[:300]}
        _log("熔断巡检 ❌ 异常（已兜底）: " + repr(e)[:200])
    # 十二套技术共识巡检（自主意识）：方向翻转/强信号 → insight 落库 + 推送。
    # twelve_step 自身永不抛出，此处再兜一层保持主心跳零风险。
    try:
        tw = twelve_step(symbols)
        cycle["twelve"] = tw.get("symbols", {}) if not tw.get("error") else {"error": tw["error"]}
    except Exception as e:  # noqa: BLE001
        cycle["twelve"] = {"error": repr(e)[:300]}
        _log("十二套巡检 ❌ 异常（已兜底）: " + repr(e)[:200])
    if paper_trade:
        try:
            import jarvis_executor as jx
            import jarvis_paper_trader as jpt
            pt = jpt.run_cycle(symbols, jx.load_config(), notify_on_action=True)
            cycle["paper_trade"] = {
                "matched": len(pt.get("matched") or []),
                "closed": len(pt.get("closed") or []),
                "opened": len(pt.get("opened") or []),
                "open_after": pt.get("open_after"),
            }
            p = cycle["paper_trade"]
            _log(f"📈 自动跟盘：撮合 {p['matched']} / 平仓 {p['closed']} / 开仓 {p['opened']} / 持仓 {p['open_after']}")
        except Exception as e:  # noqa: BLE001 — 跟盘失败不影响主心跳
            cycle["paper_trade"] = {"error": repr(e)[:300]}
            _log(f"📈 自动跟盘 ❌ 异常: {repr(e)[:300]}")
    cycle["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(cycle, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return cycle


def grow_step(symbols: list[str], *, apply_retrain: bool = False) -> dict:
    """自进化「总结不足 + 训」一步（低频触发）。永不抛出，失败只记日志。

    - 先产出自评报告（jarvis_forward_report，纯只读）写日志摘要；
    - apply_retrain=True 时再跑 jarvis_retrain --apply 温和调权（OOS+护栏内）。
    默认 apply_retrain=False：只总结不改权重（看趋势，人工决定何时真正采纳）。
    """
    res: dict = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "reports": {}, "retrain": {}}
    try:
        import jarvis_forward_report as jfr
        for sym in symbols:
            rep = jfr.build(sym)
            weak = rep.get("weaknesses", [])
            res["reports"][sym] = {"weaknesses": len(weak), "top": weak[0] if weak else None}
            _log(f"🔬 自评 {sym}：不足 {len(weak)} 条" + (f"｜首要：{weak[0]}" if weak else ""))
    except Exception as e:  # noqa: BLE001
        res["reports"]["error"] = repr(e)[:300]
        _log("🔬 自评 ❌ 异常（已兜底）: " + repr(e)[:200])
    if apply_retrain:
        try:
            import jarvis_retrain as jr
            for sym in symbols:
                rt = jr.run(sym, apply=True)
                res["retrain"][sym] = {"adopted": rt.get("adopted_count"), "version": rt.get("new_version")}
                _log(f"🧠 重训 {sym}：采纳 {rt.get('adopted_count')} 项"
                     + (f"→ 权重 v{rt.get('new_version')}" if rt.get("applied") else "（无采纳，权重不变）"))
        except Exception as e:  # noqa: BLE001
            res["retrain"]["error"] = repr(e)[:300]
            _log("🧠 重训 ❌ 异常（已兜底）: " + repr(e)[:200])
    res["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return res


def morning_step(symbols: list[str], *, notify: bool = True, dry_run: bool = False) -> dict:
    """生成并（可选）推送每日晨报（T-14）。永不抛出，失败只记日志。"""
    res: dict = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        import jarvis_morning_report as jmr
        report = jmr.build(symbols)
        r = report.get("radar", {})
        hits = len(r.get("data", {}).get("actionable", [])) if r.get("ok") else 0
        res["actionable"] = hits
        _log(f"☀️ 晨报生成：达标信号 {hits} 个")
        if notify:
            push = jmr.send(report, dry_run=dry_run)
            res["push"] = push
            _log(f"☀️ 晨报推送：{push}")
    except Exception as e:  # noqa: BLE001 — 晨报失败不影响主心跳
        res["error"] = repr(e)[:300]
        _log("☀️ 晨报 ❌ 异常（已兜底）: " + repr(e)[:200])
    res["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return res


def intraday_step(symbols: list[str]) -> dict:
    """跑一轮 4h 盘中引擎（预测→模拟下单）。永不抛出，失败只记日志。"""
    try:
        import jarvis_intraday_trader as jit
        res = jit.cycle(symbols)
        _log(f"⚡ 4h 轮：预测 {len(res.get('predictions', {}))} 币 / "
             f"开仓 {len(res.get('opened', []))} / 平仓 {len(res.get('closed', []))} / "
             f"回填 {res.get('backfilled', 0)} / 熔断 {'是' if res.get('halted') else '否'}"
             + (f" / 错误 {res['error']}" if res.get("error") else ""))
        return res
    except Exception as e:  # noqa: BLE001 — 盘中引擎失败绝不拖垮主心跳
        _log("⚡ 4h 轮 ❌ 异常（已兜底）: " + repr(e)[:300])
        return {"error": repr(e)[:300]}


def _seconds_to_next_4h_close(now: float | None = None, buffer_s: int = 120) -> float:
    """距下一个 4h K 线收盘（UTC 0/4/8/12/16/20 点）+ 缓冲的秒数。"""
    t = now if now is not None else time.time()
    next_boundary = (int(t // 14400) + 1) * 14400
    return max(60.0, next_boundary + buffer_s - t)


def loop(symbols: list[str], interval_hours: float, paper_trade: bool = False,
         *, auto_grow: bool = False, grow_every: int = 30, auto_retrain_apply: bool = False,
         auto_morning: bool = False, morning_dry_run: bool = False,
         intraday: bool = False) -> int:
    interval = max(60.0, interval_hours * 3600.0)
    _log(f"🤖 贾维斯定时引擎启动：symbols={symbols} 周期={interval_hours}h "
         f"自动跟盘={'开' if paper_trade else '关'} "
         f"4h盘中={'开（对齐 4h 收盘）' if intraday else '关'} "
         f"自进化={'开（每'+str(grow_every)+'轮，重训'+('采纳' if auto_retrain_apply else '仅建议')+'）' if auto_grow else '关'}")
    cycle_count = 0
    last_morning_date = None
    last_daily_date = None
    while True:
        # [C3] 每轮先探测数据库；不可达则本轮跳过所有写库步骤（降级），下轮自动重试。
        # 注意：跳过时不更新 last_daily_date，pg 恢复后当天仍会补跑 record/evaluate（幂等）。
        db_ok = pg_health_check()
        # intraday 模式下：日线 record/evaluate 只在日历日切换时跑一次（幂等，防高频打点）
        today = time.strftime("%Y-%m-%d")
        if db_ok and (not intraday or today != last_daily_date):
            last_daily_date = today
            try:
                run_cycle(symbols, paper_trade=paper_trade)
            except Exception:  # noqa: BLE001 — 兜底，确保循环不死
                _log("本轮异常（已兜底，继续运行）:\n" + traceback.format_exc())
        if intraday and db_ok:
            intraday_step(symbols)
        cycle_count += 1
        # [T-14] 每日晨报：日历日切换即触发一次（避免高频；首轮也会发一封）。
        if auto_morning:
            today = time.strftime("%Y-%m-%d")
            if today != last_morning_date:
                last_morning_date = today
                try:
                    morning_step(symbols, notify=True, dry_run=morning_dry_run)
                except Exception:  # noqa: BLE001 — 晨报失败绝不拖垮主心跳
                    _log("晨报异常（已兜底，继续运行）:\n" + traceback.format_exc())
        if auto_grow and grow_every > 0 and cycle_count % grow_every == 0:
            _log(f"🌱 触发第 {cycle_count} 轮自进化（总结不足"
                 + ("+重训采纳）" if auto_retrain_apply else "，重训仅建议）"))
            try:
                grow_step(symbols, apply_retrain=auto_retrain_apply)
            except Exception:  # noqa: BLE001 — 自进化失败绝不拖垮主心跳
                _log("自进化异常（已兜底，继续运行）:\n" + traceback.format_exc())
        if intraday:
            wait = _seconds_to_next_4h_close()
            _log(f"😴 对齐 4h 收盘，休眠 {round(wait / 3600, 2)}h，下轮 "
                 f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + wait))}")
            time.sleep(wait)
        else:
            _log(f"😴 休眠 {interval_hours}h，下轮 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + interval))}")
            time.sleep(interval)


def install_launchd(symbols: list[str], interval_hours: float, paper_trade: bool = False,
                    intraday: bool = False) -> str:
    """生成 macOS launchd plist（KeepAlive 常驻），返回写入路径。"""
    py = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    if not os.path.exists(py):
        py = sys.executable
    script = os.path.abspath(__file__)
    pt_arg = "\n    <string>--paper-trade</string>" if paper_trade else ""
    if intraday:
        pt_arg += "\n    <string>--intraday</string>"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{script}</string>
    <string>--symbols</string><string>{','.join(symbols)}</string>
    <string>--interval-hours</string><string>{interval_hours}</string>{pt_arg}
  </array>
  <key>WorkingDirectory</key><string>{os.path.dirname(script)}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{LOG_PATH}</string>
  <key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict>
</plist>
"""
    out_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{PLIST_LABEL}.plist")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(plist)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 7×24 定时引擎")
    ap.add_argument("--symbols", default="BTCUSDT", help="逗号分隔，如 BTCUSDT,ETHUSDT")
    # [Sprint0] 默认周期从配置中心读（daemon_interval_hours，默认 24 零回归）；CLI 显式传参仍最高优先。
    try:
        import jarvis_config as _jcfg
        _default_interval = float(_jcfg.get("daemon_interval_hours") or 24.0)
    except Exception:  # noqa: BLE001
        _default_interval = 24.0
    ap.add_argument("--interval-hours", type=float, default=_default_interval,
                    help=f"循环周期（小时），默认 {_default_interval:g}（配置中心 daemon_interval_hours）")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（cron/launchd 友好）")
    ap.add_argument("--paper-trade", action="store_true",
                    help="每轮额外跑一次模拟跟盘（撮合+盯平仓+按决策开仓）；默认关闭")
    ap.add_argument("--intraday", action="store_true",
                    help="开启 4h 盘中引擎：对齐 4h K 线收盘跑 预测→模拟下单；默认关闭")
    ap.add_argument("--auto-grow", action="store_true",
                    help="开启自进化：每 N 轮自动总结不足（+可选重训）；默认关闭")
    ap.add_argument("--grow-every", type=int, default=30,
                    help="自进化触发周期（每多少轮一次），默认 30")
    ap.add_argument("--auto-retrain-apply", action="store_true",
                    help="自进化时不仅总结，还把重训建议写入权重（OOS+护栏内）；默认仅建议不写")
    ap.add_argument("--grow-now", action="store_true",
                    help="立刻跑一次自进化步骤（总结不足）然后退出，便于手动体检")
    ap.add_argument("--auto-morning", action="store_true",
                    help="[T-14] 开启每日晨报：日历日切换自动生成+推送一封；默认关闭")
    ap.add_argument("--morning-now", action="store_true",
                    help="[T-14] 立刻生成并推送一封晨报然后退出，便于手动体检")
    ap.add_argument("--morning-dry-run", action="store_true",
                    help="[T-14] 晨报只打印不真发（联网前演练）")
    ap.add_argument("--install-launchd", action="store_true", help="生成 macOS launchd plist")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        symbols = ["BTCUSDT"]

    if args.install_launchd:
        path = install_launchd(symbols, args.interval_hours, paper_trade=args.paper_trade,
                               intraday=args.intraday)
        print(f"✅ 已生成 launchd 配置: {path}")
        print("加载/启动：")
        print(f"  launchctl load {path}")
        print(f"  launchctl start {PLIST_LABEL}")
        print("停止/卸载：")
        print(f"  launchctl unload {path}")
        return 0

    if args.grow_now:
        grow_step(symbols, apply_retrain=args.auto_retrain_apply)
        _log("单次自进化完成")
        return 0

    if args.morning_now:
        morning_step(symbols, notify=True, dry_run=args.morning_dry_run)
        _log("单次晨报完成")
        return 0

    if args.once:
        cycle = run_cycle(symbols, paper_trade=args.paper_trade)
        if args.intraday:
            intraday_step(symbols)
        ok = all(v.get("record", {}) and v["record"].get("ok") for v in cycle["symbols"].values())
        _log("单轮完成" + ("（全部成功）" if ok else "（含失败，见日志）"))
        return 0

    return loop(symbols, args.interval_hours, paper_trade=args.paper_trade,
                auto_grow=args.auto_grow, grow_every=args.grow_every,
                auto_retrain_apply=args.auto_retrain_apply,
                auto_morning=args.auto_morning, morning_dry_run=args.morning_dry_run,
                intraday=args.intraday)


if __name__ == "__main__":
    raise SystemExit(main())
