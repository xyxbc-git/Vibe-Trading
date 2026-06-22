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

LOG_DIR = os.path.expanduser("~/.vibe-trading")
LOG_PATH = os.path.join(LOG_DIR, "jarvis_daemon.log")
STATUS_PATH = os.path.join(LOG_DIR, "jarvis_daemon_status.json")
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


def loop(symbols: list[str], interval_hours: float, paper_trade: bool = False) -> int:
    interval = max(60.0, interval_hours * 3600.0)
    _log(f"🤖 贾维斯定时引擎启动：symbols={symbols} 周期={interval_hours}h 自动跟盘={'开' if paper_trade else '关'}")
    while True:
        try:
            run_cycle(symbols, paper_trade=paper_trade)
        except Exception:  # noqa: BLE001 — 兜底，确保循环不死
            _log("本轮异常（已兜底，继续运行）:\n" + traceback.format_exc())
        _log(f"😴 休眠 {interval_hours}h，下轮 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + interval))}")
        time.sleep(interval)


def install_launchd(symbols: list[str], interval_hours: float, paper_trade: bool = False) -> str:
    """生成 macOS launchd plist（KeepAlive 常驻），返回写入路径。"""
    py = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    if not os.path.exists(py):
        py = sys.executable
    script = os.path.abspath(__file__)
    pt_arg = "\n    <string>--paper-trade</string>" if paper_trade else ""
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
    ap.add_argument("--interval-hours", type=float, default=24.0, help="循环周期（小时），默认 24")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（cron/launchd 友好）")
    ap.add_argument("--paper-trade", action="store_true",
                    help="每轮额外跑一次模拟跟盘（撮合+盯平仓+按决策开仓）；默认关闭")
    ap.add_argument("--install-launchd", action="store_true", help="生成 macOS launchd plist")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        symbols = ["BTCUSDT"]

    if args.install_launchd:
        path = install_launchd(symbols, args.interval_hours, paper_trade=args.paper_trade)
        print(f"✅ 已生成 launchd 配置: {path}")
        print("加载/启动：")
        print(f"  launchctl load {path}")
        print(f"  launchctl start {PLIST_LABEL}")
        print("停止/卸载：")
        print(f"  launchctl unload {path}")
        return 0

    if args.once:
        cycle = run_cycle(symbols, paper_trade=args.paper_trade)
        ok = all(v.get("record", {}) and v["record"].get("ok") for v in cycle["symbols"].values())
        _log("单轮完成" + ("（全部成功）" if ok else "（含失败，见日志）"))
        return 0

    return loop(symbols, args.interval_hours, paper_trade=args.paper_trade)


if __name__ == "__main__":
    raise SystemExit(main())
