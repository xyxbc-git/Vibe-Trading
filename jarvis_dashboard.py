#!/usr/bin/env python3
"""贾维斯 JARVIS - 可视化仪表盘（独立 FastAPI 应用）。

把前面跑通的 4 个脚本（真实数据 / 因子回测 / 样本外验证 / 闭环简报）
封装成一个网页仪表盘，浏览器里一眼看清：
  - 实时决策简报（信心分 / 仓位 / 止损 / 时间止损）
  - 真实数据卡片（资金费率/OI/多空比/情绪/链上/市场结构）
  - 恐慌贪婪 & 回撤历史曲线（echarts）
  - 已验证因子事件研究表（含 P4 过拟合警示）

独立端口运行，不影响 Vibe-Trading 主服务。
用法：
  ./.venv/bin/python jarvis_dashboard.py            # 默认 127.0.0.1:7899
  ./.venv/bin/python jarvis_dashboard.py --port 7899
"""

from __future__ import annotations

import argparse
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import jarvis_brief as jb
import jarvis_crypto_data as jcd
import jarvis_net as _jnet

_jnet.ensure_proxy()   # 大陆网络：探测本地代理，Binance 等出网自动走代理
import jarvis_alert_center as jac
import jarvis_executor as jx
import jarvis_journal as jj
import jarvis_lessons as jl
import jarvis_order_notify as jon
import jarvis_paper_trader as jpt
import jarvis_price_alert as jpa
import jarvis_wallet as jw
from jarvis_factor_backtest import _build_series, event_study, fetch_fng_all, fetch_price_daily

def _load_env_file() -> None:
    """零依赖加载脚本同目录下的 .env（KEY=VALUE，每行一条，# 开头为注释）。
    已存在于真实环境变量的不覆盖；让用户有一个文件可填 DeepSeek/OpenAI key。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_env_file()

app = FastAPI(title="贾维斯仪表盘")

# ============ 后端日志采集（供前端「终端」页面实时查看）============
# 把 uvicorn 日志、应用 logging、以及各 jarvis 模块的 print 输出，统一收进一个
# 内存环形缓冲，并通过 SSE 实时广播给前端「终端」页面，便于排查接口卡顿/报错。
import logging as _logging
import queue as _queue
import sys as _sys
import threading as _threading
from collections import deque as _deque

_LOG_BUFFER: "_deque[dict]" = _deque(maxlen=4000)
_LOG_LOCK = _threading.Lock()
_LOG_SUBSCRIBERS: "list[_queue.Queue]" = []
_LOG_SEQ = 0


def _log_emit(text: str, level: str = "info", source: str = "app") -> None:
    """写入一行日志到环形缓冲并广播给所有 SSE 订阅者。"""
    global _LOG_SEQ
    for raw in str(text).rstrip("\n").split("\n"):
        if raw == "":
            continue
        with _LOG_LOCK:
            _LOG_SEQ += 1
            item = {
                "seq": _LOG_SEQ,
                "ts": time.strftime("%H:%M:%S"),
                "level": level,
                "source": source,
                "text": raw,
            }
            _LOG_BUFFER.append(item)
            dead = []
            for q in _LOG_SUBSCRIBERS:
                try:
                    q.put_nowait(item)
                except _queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    _LOG_SUBSCRIBERS.remove(q)
                except ValueError:
                    pass


class _BufferLogHandler(_logging.Handler):
    """把标准 logging 记录转发进日志缓冲。"""

    def emit(self, record: "_logging.LogRecord") -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001
            return
        if record.levelno >= _logging.ERROR:
            level = "error"
        elif record.levelno >= _logging.WARNING:
            level = "warn"
        else:
            level = "info"
        _log_emit(msg, level=level, source=(record.name.split(".")[0] or "log"))


class _TeeStream:
    """把 print 输出同时送到原始流和日志缓冲（按行切分）。"""

    def __init__(self, original, level: str):
        self._orig = original
        self._level = level
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            self._orig.write(s)
        except Exception:  # noqa: BLE001
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                _log_emit(line, level=self._level, source="stdout")
        return len(s)

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        return False


def _install_log_capture() -> None:
    # 只挂到 root logger：uvicorn 在 run() 时会重配自己的 logger（会清掉外部
    # handler），但其默认 StreamHandler 写的是 sys.stderr —— 已被下面的 Tee 接管，
    # 故 uvicorn 的启动/错误日志仍会进入缓冲，无需也不应重复挂到 uvicorn.* 上。
    handler = _BufferLogHandler()
    handler.setFormatter(_logging.Formatter("%(message)s"))
    root = _logging.getLogger("")
    root.addHandler(handler)
    if root.level == _logging.NOTSET:
        root.setLevel(_logging.INFO)
    # 捕获 jarvis 模块里大量的 print 输出（stdout=信息，stderr=警告）
    _sys.stdout = _TeeStream(_sys.__stdout__, "info")
    _sys.stderr = _TeeStream(_sys.__stderr__, "warn")


_install_log_capture()


@app.middleware("http")
async def _access_timing(request: "Request", call_next):
    """为每个 API 请求记录耗时与状态，便于定位「点了没反应」的慢接口。"""
    path = request.url.path
    # 不记录日志接口自身，避免噪声与自我递归
    skip = path.startswith("/api/logs")
    start = time.time()
    try:
        resp = await call_next(request)
    except Exception as e:  # noqa: BLE001
        if not skip:
            _log_emit(
                f"{request.method} {path} → 异常 {e} ({(time.time() - start) * 1000:.0f}ms)",
                "error",
                "http",
            )
        raise
    if not skip:
        dur = (time.time() - start) * 1000
        level = "warn" if dur > 3000 else "info"
        _log_emit(f"{request.method} {path} → {resp.status_code} ({dur:.0f}ms)", level, "http")
    return resp


# 简单内存缓存：{key: (ts, value)}，TTL 控制刷新频率
_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: int, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


# ============ 后端日志接口（前端「终端」页面消费）============

@app.get("/api/logs")
def api_logs(limit: int = 500):
    """拉取最近 N 行后端日志（前端首次加载或 SSE 不可用时的兜底）。"""
    n = max(1, min(int(limit), 4000))
    with _LOG_LOCK:
        items = list(_LOG_BUFFER)[-n:]
    return JSONResponse({"lines": items, "total": len(items)})


@app.post("/api/logs/clear")
def api_logs_clear():
    """清空后端日志缓冲。"""
    with _LOG_LOCK:
        _LOG_BUFFER.clear()
    _log_emit("日志已清空", "info", "app")
    return JSONResponse({"ok": True})


@app.get("/api/logs/stream")
def api_logs_stream():
    """SSE 实时推送后端日志。"""

    def gen():
        q: "_queue.Queue" = _queue.Queue(maxsize=2000)
        with _LOG_LOCK:
            backlog = list(_LOG_BUFFER)[-300:]
            _LOG_SUBSCRIBERS.append(q)
        try:
            for item in backlog:
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            while True:
                try:
                    item = q.get(timeout=15)
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except _queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _LOG_LOCK:
                try:
                    _LOG_SUBSCRIBERS.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


_DAEMON_STATUS_PATH = os.path.join(os.path.expanduser("~/.vibe-trading"), "jarvis_daemon_status.json")
_VIBE_TRADING_DIR = os.path.expanduser("~/.vibe-trading")

_BACKUP_FILES = (
    "jarvis_config.json",
    "jarvis_weights.json",
    "executor_config.json",
    "notify_config.json",
    "data_keys.json",
    "scalper_backtest_config.json",
    "circuit_breaker.json",
    "horizon_cache.json",
    "scalper_graveyard.json",
    "scalper_hall_of_fame.json",
    "structured_lessons.json",
    "combo_hall_of_fame.json",
    "intraday_halt.json",
    "intraday_resume.json",
    "jarvis_daemon_status.json",
    "jarvis_executor_status.json",
    "jarvis_journal.db",
    "price_alert_config.json.bak",
)


def _read_daemon_status() -> dict:
    if not os.path.exists(_DAEMON_STATUS_PATH):
        return {"available": False, "reason": "尚无 daemon 运行记录"}
    try:
        with open(_DAEMON_STATUS_PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
        return {"available": True, **data}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": repr(exc)[:200]}


@app.get("/api/health")
def api_health():
    """聚合健康检查：daemon / journal / 价位监控 / QD 网关可达性。"""
    checks: dict = {}
    try:
        jj.init_db()
        with jj._conn() as conn:
            snaps = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]
            outs = conn.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"]
        checks["journal"] = {
            "ok": os.path.exists(jj.DB_PATH),
            "reachable": True,
            "db": jj.DB_PATH,
            "snapshots": snaps,
            "outcomes": outs,
        }
    except Exception as exc:  # noqa: BLE001
        checks["journal"] = {"ok": False, "reachable": False, "error": repr(exc)[:200]}

    try:
        jpa.init_price_alert_db()
        mon = jpa.monitor_status()
        pub = jpa.public_config()
        with jj._conn() as conn:
            plans = conn.execute("SELECT COUNT(*) AS n FROM price_alert_plans").fetchone()["n"]
        checks["price_alert_monitor"] = {
            "ok": True,
            "reachable": True,
            "running": bool(mon.get("running")),
            "last_run": mon.get("last_run"),
            "last_error": mon.get("last_error"),
            "plans": plans,
            "has_smtp_password": pub.get("smtp", {}).get("has_password"),
        }
    except Exception as exc:  # noqa: BLE001
        checks["price_alert_monitor"] = {"ok": False, "reachable": False, "error": repr(exc)[:200]}
    # desktop 前端（HealthStatusCard）按 price_alert 键消费，保留旧键兼容其它调用方
    checks["price_alert"] = checks["price_alert_monitor"]

    daemon = _read_daemon_status()
    age_h = None
    finished = daemon.get("finished_at") or daemon.get("started_at") or ""
    if finished:
        try:
            ts = time.mktime(time.strptime(finished, "%Y-%m-%d %H:%M:%S"))
            age_h = round((time.time() - ts) / 3600, 2)
        except ValueError:
            pass
    daemon_ok = daemon.get("available", False) and (age_h is None or age_h <= 48)
    checks["daemon"] = {
        "ok": daemon_ok,
        # available 必须保留：desktop 前端据此区分「末轮 xx」与「未运行」
        "available": daemon.get("available", False),
        "reachable": daemon.get("available", False),
        "age_hours": age_h,
        "last_finished_at": finished,
        "paper_trade": daemon.get("paper_trade"),
        "symbols": list((daemon.get("symbols") or {}).keys()),
        **{k: v for k, v in daemon.items() if k not in ("available",)},
    }

    try:
        import jarvis_circuit_breaker as _cb
        cb_ev = _cb.evaluate(jx.load_config())
        cb_st = _cb._read_state()  # noqa: SLF001 — 只读展示
        tripped = bool(cb_st.get("tripped"))
        checks["circuit_breaker"] = {
            "ok": bool(cb_ev.get("ok")) and not tripped and not cb_ev.get("should_halt"),
            "reachable": bool(cb_ev.get("ok")),
            "tripped": tripped,
            "should_halt": bool(cb_ev.get("should_halt")),
            "equity_usdt": cb_ev.get("equity_usdt"),
            "peak_equity": cb_ev.get("peak_equity"),
            "drawdown_pct": cb_ev.get("drawdown_pct"),
            "reason": cb_st.get("reason"),
            "ts": cb_st.get("ts"),
        }
    except Exception as exc:  # noqa: BLE001
        checks["circuit_breaker"] = {"ok": False, "reachable": False, "error": repr(exc)[:200]}

    try:
        import jarvis_scalper_backtest as jbt
        qd_h = jbt.check_qd_health()
        qd_t = jbt.check_token()
        healthy = bool(qd_h.get("healthy"))
        valid = bool(qd_t.get("valid"))
        checks["qd_gateway"] = {
            "ok": healthy and valid,
            "reachable": healthy,
            "healthy": healthy,
            "token_valid": valid,
            "health_error": qd_h.get("error"),
            "token_error": qd_t.get("error"),
        }
    except Exception as exc:  # noqa: BLE001
        checks["qd_gateway"] = {"ok": False, "reachable": False, "error": repr(exc)[:200]}

    core_ok = all(
        checks.get(k, {}).get("ok")
        for k in ("journal", "price_alert_monitor", "daemon")
    )
    return JSONResponse({
        "ok": core_ok,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checks": checks,
        "log_buffer_size": len(_LOG_BUFFER),
    })


@app.get("/api/health/migrations")
def api_health_migrations():
    """价位提醒 SQLite 迁移验收：表存在性 + 元数据 + smoketest 命令。"""
    try:
        jpa.init_price_alert_db()
        tables = (
            "price_alert_smtp",
            "price_alert_settings",
            "price_alert_contacts",
            "price_alert_plans",
            "price_alert_meta",
        )
        status: dict[str, dict] = {}
        with jj._conn() as conn:
            for name in tables:
                try:
                    n = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
                    status[name] = {"ok": True, "rows": n}
                except Exception as exc:  # noqa: BLE001
                    status[name] = {"ok": False, "error": str(exc)}
            meta = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM price_alert_meta").fetchall()
            }
        return JSONResponse({
            "ok": all(v.get("ok") for v in status.values()),
            "tables": status,
            "meta": meta,
            "smoketest_cmd": "cd Vibe-Trading && python _price_alert_db_smoketest.py",
        })
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


@app.get("/api/config/backup")
def api_config_backup():
    """打包 ~/.vibe-trading 关键配置与数据库为 zip 下载。"""
    buf = io.BytesIO()
    included: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in _BACKUP_FILES:
            path = os.path.join(_VIBE_TRADING_DIR, name)
            if os.path.isfile(path):
                zf.write(path, arcname=name)
                included.append(name)
        try:
            cfg = jpa.load_config()
            smtp = dict(cfg.get("smtp") or {})
            if smtp.get("password"):
                smtp["password"] = "***"
            export_cfg = {**cfg, "smtp": smtp}
            zf.writestr(
                "price_alert_export.json",
                json.dumps(export_cfg, ensure_ascii=False, indent=2),
            )
            included.append("price_alert_export.json")
        except Exception:  # noqa: BLE001
            pass
        zf.writestr(
            "backup_manifest.json",
            json.dumps(
                {
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source_dir": _VIBE_TRADING_DIR,
                    "included": included,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    buf.seek(0)
    fname = f"vibe-trading-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/trust/chain")
def api_trust_chain(symbol: str = "BTCUSDT"):
    """Brief → Journal record → Evaluate 一键链路，附 evaluate 命中率指标。"""
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    try:
        brief = jb.build(sym)
        rec = jj.record(spot)
        ev = jj.evaluate(spot)
        rep = jj.report(spot)
        _CACHE.pop(f"track:{sym}", None)
        hit_rates: dict[str, float | None] = {}
        for hk, hv in (rep.get("by_horizon") or {}).items():
            hit_rates[hk] = (hv.get("overall") or {}).get("hit_rate_pct")
        dec = brief.get("decision") or {}
        return JSONResponse({
            "ok": bool(rec.get("ok")),
            "symbol": spot,
            "brief": {
                "direction": dec.get("direction"),
                "conviction_score": dec.get("conviction_score"),
                "suggested_position_pct": dec.get("suggested_position_pct"),
            },
            "record": rec,
            "evaluate": ev,
            "hit_rates": hit_rates,
            "report_summary": {
                "total_snapshots": rep.get("total_snapshots"),
                "evaluated_outcomes": rep.get("evaluated_outcomes"),
                "by_horizon": rep.get("by_horizon"),
            },
        })
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


@app.get("/api/circuit-breaker")
def api_circuit_breaker_get():
    try:
        import jarvis_circuit_breaker as _cb
        cfg = jx.load_config()
        ev = _cb.evaluate(cfg)
        st = _cb._read_state()  # noqa: SLF001 — dashboard 只读展示
        return JSONResponse({"ok": True, "evaluation": ev, "state": st})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:300]}, status_code=500)


@app.post("/api/circuit-breaker/reset")
def api_circuit_breaker_reset():
    try:
        import jarvis_circuit_breaker as _cb
        out = _cb.reset()
        _log_emit("熔断已手动复位", "warn", "circuit-breaker")
        return JSONResponse({"ok": True, "result": out})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:300]}, status_code=500)


@app.post("/api/actions/kill-switch")
def api_action_kill_switch():
    """急停：撤 QD 模拟挂单 + 取消本地 pending 限价单。"""
    cfg = jx.load_config()
    try:
        out = jx.kill_switch(cfg)
        local_cancelled = []
        for o in jw.pending_orders():
            oid = o.get("id")
            if oid is not None:
                local_cancelled.append(jw.cancel_limit_order(int(oid)))
        _log_emit("kill-switch 已触发", "warn", "executor")
        return JSONResponse({"ok": True, "qd": out, "local_cancelled": local_cancelled})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:300]}, status_code=500)


@app.get("/api/trading-config")
def api_trading_config_get():
    import jarvis_config as jc_mod
    keys = (
        "max_position_pct", "max_portfolio_risk_pct", "account_equity_usdt",
        "min_conviction", "intraday_enabled", "intraday_max_open_positions",
    )
    return JSONResponse({k: jc_mod.get(k) for k in keys})


@app.put("/api/trading-config")
def api_trading_config_put(data: dict):
    import jarvis_config as jc_mod
    allowed = {
        "max_position_pct", "max_portfolio_risk_pct", "account_equity_usdt",
        "min_conviction", "intraday_enabled", "intraday_max_open_positions",
    }
    patch = {k: v for k, v in (data or {}).items() if k in allowed}
    if not patch:
        return JSONResponse({"ok": False, "reason": "无有效字段"}, status_code=400)
    jc_mod.save(patch, source="desktop-settings", note="trading-config UI")
    return JSONResponse({"ok": True, "config": {k: jc_mod.get(k) for k in allowed}})


@app.get("/api/snapshot")
def snapshot(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    data = _cached(f"snap:{sym}", 300, lambda: jb.build(sym))
    return JSONResponse(data)


@app.get("/api/series")
def series(symbol: str = "BTCUSDT", days: int = 365):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        prices = fetch_price_daily(spot)
        fng = fetch_fng_all()
        dates = sorted(prices)
        closes, _ma, dd = _build_series(dates, prices)
        sl = slice(max(0, len(dates) - days), len(dates))
        ds = dates[sl]
        return {
            "dates": ds,
            "close": [round(c, 2) for c in closes[sl]],
            "drawdown_pct": [round(x * 100, 2) for x in dd[sl]],
            "fng": [fng.get(d) for d in ds],
        }

    return JSONResponse(_cached(f"series:{sym}:{days}", 600, _calc))


@app.get("/api/kline")
def kline(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 200):
    """Binance 公开 K线（免 Key），供 echarts 自绘蜡烛图 + 叠加决策信号。"""
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    iv = interval if interval in allowed else "1h"
    lim = max(20, min(int(limit), 500))

    def _calc():
        raw = jcd._get(jcd.SPOT_API + "/api/v3/klines", {"symbol": spot, "interval": iv, "limit": lim})
        if isinstance(raw, dict):
            return {"error": raw.get("_error", "kline fetch failed"), "rows": []}
        fmt = "%m-%d" if iv in ("1d",) else "%m-%d %H:%M"
        rows = []
        for k in raw:
            ts = time.localtime(k[0] / 1000)
            rows.append({
                "t": time.strftime(fmt, ts),
                "ts": int(k[0]),
                "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                "c": float(k[4]), "v": round(float(k[5]), 2),
            })
        return {"symbol": spot, "interval": iv, "rows": rows}

    return JSONResponse(_cached(f"kline:{spot}:{iv}:{lim}", 60, _calc))


@app.get("/api/factor")
def factor(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        prices = fetch_price_daily(spot)
        fng = fetch_fng_all()
        dates = sorted(set(prices) & set(fng))
        return event_study(dates, prices, fng)

    return JSONResponse(_cached(f"factor:{sym}", 1800, _calc))


@app.get("/api/calibration")
def calibration(symbol: str = "BTCUSDT", horizon: int = 30):
    """置信度校准：贾维斯的信心 vs 实际兑现率 + Brier 分。

    口径（透明可复核）：
      - 把信心分 score 的绝对值映射为「方向判断把握度」implied_prob = 0.5 + min(|score|,2)/2 * 0.45（0.5~0.95）。
      - 只统计已评估且方向可判定（偏多/偏空）的快照；中性观望不计入。
      - 实际命中 y = correct(1/0)。Brier = mean((p-y)^2)，越低越好；
        基线 Brier = mean((0.5-y)^2)；Brier 技巧分 BSS = 1 - Brier/基线（>0 说明比瞎猜强）。
    """
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    h = 7 if int(horizon) == 7 else 30
    ckey, rkey = (f"c{h}", f"r{h}")

    def _calc():
        rows = jj.list_recent(spot, 100000)
        pts = []  # (implied_prob, y, score)
        for r in rows:
            score = r.get("conviction_score")
            y = r.get(ckey)
            if score is None or y is None:
                continue
            conf = min(2.0, abs(float(score))) / 2.0
            p = round(0.5 + conf * 0.45, 4)
            pts.append((p, int(y), float(score)))
        n = len(pts)
        if n == 0:
            return {"symbol": spot, "horizon": h, "n": 0, "buckets": [], "brier": None,
                    "brier_baseline": None, "bss": None}
        edges = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]
        labels = ["50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]
        buckets = []
        for i in range(len(labels)):
            lo, hi = edges[i], edges[i + 1]
            grp = [t for t in pts if lo <= t[0] < hi]
            if not grp:
                continue
            buckets.append({
                "label": labels[i],
                "n": len(grp),
                "pred_pct": round(100 * sum(t[0] for t in grp) / len(grp), 1),
                "actual_pct": round(100 * sum(t[1] for t in grp) / len(grp), 1),
            })
        brier = sum((p - y) ** 2 for p, y, _ in pts) / n
        base = sum((0.5 - y) ** 2 for _, y, _ in pts) / n
        bss = (1 - brier / base) if base > 0 else None
        return {
            "symbol": spot, "horizon": h, "n": n,
            "overall_hit_pct": round(100 * sum(y for _, y, _ in pts) / n, 1),
            "buckets": buckets,
            "brier": round(brier, 4),
            "brier_baseline": round(base, 4),
            "bss": round(bss, 3) if bss is not None else None,
        }

    return JSONResponse(_cached(f"calib:{spot}:{h}", 120, _calc))


@app.get("/api/track")
def track(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        return {
            "report": jj.report(spot),
            "recent": jj.list_recent(spot, 30),
        }

    return JSONResponse(_cached(f"track:{sym}", 300, _calc))


@app.post("/api/track/record")
def track_record(symbol: str = "BTCUSDT"):
    """落一条今日快照并回填到期收益，然后清缓存。"""
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    rec = jj.record(spot)
    ev = jj.evaluate(spot)
    _CACHE.pop(f"track:{sym}", None)
    return JSONResponse({"record": rec, "evaluate": ev})


# ─────────────────────────── 模拟交易台账 API ───────────────────────────

def _trader_cfg():
    return jx.load_config()


@app.get("/api/wallet")
def api_wallet():
    cfg = _trader_cfg()
    return JSONResponse(jw.ensure_account(cfg.get("account_equity_usdt", 1000.0)))


@app.get("/api/trader/status")
def api_trader_status(symbol: str | None = None):
    """跟盘账户总览：钱包总权益 + 盈亏比 + 持仓明细。"""
    return JSONResponse(jpt.stats(_trader_cfg(), symbol))


@app.get("/api/positions")
def api_positions(symbol: str | None = None, status: str = "all", limit: int = 0,
                  include_replay: bool = False):
    rows = jpt.all_positions(symbol)
    if not include_replay:
        # 历史回放样本默认不进台账/交易记录（只在信号胜率统计 source=replay 可见）
        rows = [r for r in rows if (r.get("signal_source") or "") != "replay"]
    if status == "open":
        rows = [r for r in rows if r["status"] == "open"]
    elif status == "closed":
        rows = [r for r in rows if r["status"] == "closed"]
    if limit and limit > 0:
        rows = rows[:limit]   # all_positions 已按 opened_ts 倒序，截断即取最近 N 笔

    # 自创单（保存交易计划）在 limit_orders.note 里带完整计划快照（kind='trade-plan'，
    # 含杠杆/保证金/名义仓位），成交后经 position_id 关联持仓。这里反查附加成
    # plan_leverage / plan_margin_usdt / plan_notional_usdt 列，供前端卡片展示
    # 杠杆与保证金口径；无快照的系统/手动单由前端按现货全额 1x 回退。
    open_ids = [r["id"] for r in rows if r.get("status") == "open" and r.get("id") is not None]
    plan_map: dict = {}
    if open_ids:
        try:
            with jj._conn() as conn:
                marks = ",".join("?" * len(open_ids))
                for o in conn.execute(
                    f"SELECT position_id, note FROM limit_orders "
                    f"WHERE status='filled' AND note IS NOT NULL AND position_id IN ({marks})",
                    open_ids,
                ).fetchall():
                    try:
                        meta = json.loads(o["note"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(meta, dict) and meta.get("kind") == "trade-plan":
                        plan_map[o["position_id"]] = meta
        except Exception:  # noqa: BLE001 — 快照缺失只影响卡片增强列，不阻塞持仓主数据
            plan_map = {}
    for r in rows:
        meta = plan_map.get(r.get("id"))
        if meta:
            r["plan_leverage"] = meta.get("leverage")
            r["plan_margin_usdt"] = meta.get("margin_usdt")
            r["plan_notional_usdt"] = meta.get("notional_usdt")

    # 给所有行补 direction（前端按 long/short 渲染，DB 里只有 buy/sell）
    # 同时给 open 持仓补 current_price / pnl_pct / pnl_usdt
    # （latest_price 走 60s 缓存，避免每次轮询都打外部 API；取价失败 → 字段为 None，前端按缺失态显示）。
    cfg = _trader_cfg()
    for r in rows:
        side = (r.get("side") or "buy").lower()
        r["direction"] = "long" if side == "buy" else "short"
        if r.get("status") != "open":
            continue
        sym = r.get("symbol")
        entry = r.get("entry_price")
        if not sym or entry in (None, 0):
            continue
        try:
            price = _cached(f"pos_price:{sym}", 3, lambda s=sym: jpt.latest_price(cfg, s))
        except Exception:  # noqa: BLE001
            price = None
        if price is None:
            continue
        sign = 1 if side == "buy" else -1
        qty = r.get("qty") or 0
        r["current_price"] = round(float(price), 8)
        r["pnl_pct"] = round((price / entry - 1.0) * 100 * sign, 2)
        r["pnl_usdt"] = round((price - entry) * qty * sign, 4)
    return JSONResponse(rows)


@app.get("/api/orders")
def api_orders(symbol: str | None = None, all: bool = False):
    return JSONResponse(jw.all_limit_orders(symbol) if all else jw.pending_orders(symbol))


@app.get("/api/ledger")
def api_ledger(limit: int = 50, symbol: str | None = None):
    return JSONResponse(jw.ledger(limit, symbol))


@app.post("/api/wallet/deposit")
def api_deposit(amount: float):
    if amount <= 0:
        return JSONResponse({"ok": False, "reason": "金额必须 > 0"}, status_code=400)
    return JSONResponse(jw.deposit(amount))


@app.post("/api/orders/place")
def api_place_order(symbol: str, side: str, price: float, qty: float,
                    stop_loss: float | None = None, take_profit: float | None = None,
                    source: str = "system", note: str | None = None):
    cfg = _trader_cfg()
    jw.ensure_account(cfg.get("account_equity_usdt", 1000.0))
    return JSONResponse(jw.place_limit_order(symbol, side, price, qty,
                                             stop_loss=stop_loss, take_profit=take_profit,
                                             note=note, source=source))


@app.post("/api/orders/cancel")
def api_cancel_order(order_id: int):
    return JSONResponse(jw.cancel_limit_order(order_id))


@app.post("/api/orders/match")
def api_match_orders():
    return JSONResponse({"matched": jpt.match_limit_orders(_trader_cfg())})


@app.post("/api/positions/close")
def api_close_position(symbol: str):
    cfg = _trader_cfg()
    out = []
    for p in jpt.open_positions(symbol):
        price = jpt.latest_price(cfg, p["symbol"]) or p.get("entry_price")
        out.append(jpt._close_position(p, price, "manual", cfg))
    return JSONResponse({"closed": out})


@app.post("/api/trader/cycle")
def api_trader_cycle(symbols: str = "BTC,ETH,SOL", dry_run: bool = False):
    """跑一轮自动跟盘：撮合限价单 → 盯平仓 → 按决策找开仓。"""
    cfg = _trader_cfg()
    syms = [s.strip() for s in symbols.split(",") if s.strip()] or ["BTC"]
    return JSONResponse(jpt.run_cycle(syms, cfg, dry_run=dry_run))


# ─────────────────────────── 快捷操作 API ───────────────────────────

@app.post("/api/actions/brief")
def api_action_brief(symbol: str = "BTCUSDT"):
    """一键生成决策简报。"""
    try:
        result = jb.build(symbol)
        return JSONResponse({"ok": True, "data": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": repr(exc)[:300]})


@app.post("/api/actions/execute")
def api_action_execute(symbol: str = "BTCUSDT", dry_run: bool = True):
    """一键执行（默认 dry-run 演练，不真下单）。"""
    cfg = _trader_cfg()
    try:
        result = jx.execute(symbol, cfg, dry_run=dry_run)
        return JSONResponse({"ok": True, "data": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": repr(exc)[:300]})


@app.post("/api/actions/radar")
def api_action_radar(symbols: str | None = None, min_conviction: float = 0.8):
    """一键雷达扫描。"""
    try:
        import jarvis_radar as jr
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else None
        result = jr.scan(symbols=syms, min_conviction=min_conviction)
        return JSONResponse({"ok": True, "data": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": repr(exc)[:300]})


@app.post("/api/actions/open")
def api_action_open(symbol: str = "BTCUSDT", dry_run: bool = False):
    """一键按决策开仓。"""
    cfg = _trader_cfg()
    try:
        result = jpt.open_from_decision(symbol, cfg, dry_run=dry_run)
        return JSONResponse({"ok": True, "data": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": repr(exc)[:300]})


# ─────────────────────────── 事件流 + 智能问答 API ───────────────────────────

_EXIT_REASON_CN = {
    "stop_loss": "触发止损离场",
    "take_profit": "触发止盈落袋",
    "time_stop": "持有到期离场",
    "signal_flip": "信号反转离场",
    "manual": "手动平仓",
}


def _market_events(spot: str) -> list[dict]:
    """从真实行情数据（恐慌贪婪/资金费率/持仓量/多空比/市值/动量）+ K 线急涨急跌，
    生成真实市场事件，而非模拟盘动作。全部基于 jcd.collect / kline 实拉数据，不编造。"""
    evs: list[dict] = []
    now = time.time()

    def _add(ts, title, detail, tone):
        evs.append({"ts": float(ts), "kind": "market", "symbol": spot,
                    "title": title, "detail": detail, "tone": tone})

    # 1) 市场状态事件（来自 snapshot 实时真实数据，时间戳=快照生成时刻）
    try:
        snap = _cached(f"snap:{spot}", 300, lambda: jb.build(spot))
        rd = snap.get("real_data", {}) or {}
        fac = snap.get("factor_state", {}) or {}
        fng = rd.get("fear_greed", {}) or {}
        fund = rd.get("funding", {}) or {}
        oi = rd.get("open_interest", {}) or {}
        ls = rd.get("long_short", {}) or {}
        ms = rd.get("market_structure", {}) or {}

        v = fng.get("fng_value")
        if v is not None:
            tone = "loss" if v <= 25 else ("win" if v >= 75 else "info")
            _add(now, f"恐慌贪婪指数 {v} · {fng.get('fng_class', '')}",
                 "市场情绪极端，常是反向参考" if (v <= 25 or v >= 75) else "市场情绪中性", tone)

        fr = fund.get("last_funding_rate_8h_pct")
        if fr is not None:
            reg = fund.get("funding_regime", "")
            tone = "loss" if fr >= 0.05 else ("win" if fr <= -0.01 else "info")
            _add(now, f"资金费率 {fr}% / 8h · {reg}",
                 "多头拥挤·留意回调" if fr >= 0.05 else ("空头付费·偏多有利" if fr <= -0.01 else "费率平稳"), tone)

        oic = oi.get("oi_change_24h_pct")
        if oic is not None:
            tone = "info"
            _add(now, f"持仓量 24h {'+' if oic >= 0 else ''}{oic}%",
                 "杠杆增加·波动可能放大" if oic >= 5 else ("杠杆撤离·去风险" if oic <= -5 else "持仓平稳"),
                 "loss" if oic >= 8 else tone)

        lsr = ls.get("global_long_short_ratio")
        if lsr is not None:
            _add(now, f"全网多空比 {lsr}",
                 "散户偏多·注意反向" if lsr >= 1.5 else ("散户偏空·注意反向" if lsr <= 0.7 else "多空均衡"),
                 "info")

        mcc = ms.get("market_cap_change_24h_pct")
        if mcc is not None:
            _add(now, f"总市值 24h {'+' if mcc >= 0 else ''}{mcc}%",
                 "大盘整体走强" if mcc >= 2 else ("大盘整体走弱" if mcc <= -2 else "大盘平稳"),
                 "win" if mcc >= 2 else ("loss" if mcc <= -2 else "info"))

        dd = fac.get("drawdown_from_ath_pct")
        if dd is not None and dd <= -25:
            _add(now, f"距历史高点回撤 {dd}%", "进入深度回撤区·弱抄底因子关注", "win")

        mom = fac.get("momentum_30d_pct")
        if mom is not None:
            if mom >= 10:
                _add(now, f"30 日动量 +{mom}%", "中期趋势偏强", "win")
            elif mom <= -10:
                _add(now, f"30 日动量 {mom}%", "中期趋势偏弱", "loss")
    except Exception:  # noqa: BLE001
        pass

    # 2) 价格异动事件（来自真实 1h K 线，单根 |涨跌| ≥ 2% 记一条，用真实 bar 时间戳）
    try:
        kraw = jcd._get(jcd.SPOT_API + "/api/v3/klines",
                        {"symbol": spot, "interval": "1h", "limit": 120})
        if not isinstance(kraw, dict):
            cnt = 0
            for k in reversed(kraw[-48:]):
                o, c = float(k[1]), float(k[4])
                if o <= 0:
                    continue
                chg = (c - o) / o * 100
                if abs(chg) >= 2.0:
                    cnt += 1
                    _add(k[0] / 1000,
                         f"{'急涨' if chg > 0 else '急跌'} {'+' if chg > 0 else ''}{round(chg, 2)}%（1h）",
                         f"收于 {round(c, 2)}", "win" if chg > 0 else "loss")
                if cnt >= 8:
                    break
    except Exception:  # noqa: BLE001
        pass

    return evs


@app.get("/api/events")
def api_events(symbol: str | None = None, limit: int = 40):
    """聚合模拟盘开仓/平仓/止盈止损/信号反转 + 真实市场事件，按时间倒序滚动给前端。"""
    sym = None
    if symbol:
        s = symbol.upper().replace("-", "").replace("/", "")
        sym = s if s.endswith("USDT") else s + "USDT"

    def _calc():
        evs: list[dict] = []
        if sym:
            evs.extend(_market_events(sym))
        for p in jpt.all_positions(sym):
            side_cn = "开多" if (p.get("side") or "buy") == "buy" else "开空"
            if p.get("opened_ts"):
                evs.append({
                    "ts": float(p["opened_ts"]),
                    "kind": "open",
                    "symbol": p["symbol"],
                    "title": f"{side_cn} {p['symbol']}",
                    "detail": f"入场 {p.get('entry_price','—')} · 数量 {p.get('qty','—')}"
                              + (f" · 止损 {p['stop_loss']}" if p.get("stop_loss") else "")
                              + (f" · 止盈 {p['take_profit']}" if p.get("take_profit") else ""),
                    "tone": "buy",
                })
            if p.get("status") == "closed" and p.get("closed_ts"):
                pnl = p.get("realized_pnl_usdt")
                pnl_pct = p.get("realized_pnl_pct")
                reason = _EXIT_REASON_CN.get(p.get("exit_reason"), p.get("exit_reason") or "平仓")
                tone = "win" if (pnl or 0) > 0 else ("loss" if (pnl or 0) < 0 else "flat")
                pnl_str = ""
                if pnl is not None:
                    pnl_str = f"{'+' if pnl >= 0 else ''}{round(pnl, 2)}U"
                    if pnl_pct is not None:
                        pnl_str += f"（{'+' if pnl_pct >= 0 else ''}{round(pnl_pct, 2)}%）"
                evs.append({
                    "ts": float(p["closed_ts"]),
                    "kind": "close",
                    "symbol": p["symbol"],
                    "title": f"平仓 {p['symbol']} · {reason}",
                    "detail": f"出场 {p.get('exit_price','—')}"
                              + (f" · 盈亏 {pnl_str}" if pnl_str else ""),
                    "tone": tone,
                })
        evs.sort(key=lambda e: e["ts"], reverse=True)
        for e in evs:
            e["time"] = time.strftime("%m-%d %H:%M", time.localtime(e["ts"]))
        return {"events": evs[: max(1, min(int(limit), 200))]}

    return JSONResponse(_cached(f"events:{sym}:{limit}", 20, _calc))


def _answer_question(q: str, snap: dict, st: dict) -> str:
    """根据当前决策快照 + 模拟盘战绩，把问题用人话回答（无需外部 LLM）。"""
    d = (snap or {}).get("decision", {}) or {}
    fac = (snap or {}).get("factor_state", {}) or {}
    sym = (snap or {}).get("symbol", "该币")
    if "_error" in d or "_error" in fac:
        return f"现在 {sym} 取数暂时不可用（{d.get('_error') or fac.get('_error')}），稍后再问我一次。"

    score = d.get("conviction_score", 0) or 0
    pos = d.get("suggested_position_pct", 0) or 0
    price = fac.get("price")
    direction = d.get("direction", "中性观望")
    entry = d.get("entry_zone")
    sl = d.get("stop_loss")
    tp = d.get("take_profit_ref")
    days = d.get("time_stop_days")
    reasons = d.get("reasons", []) or []
    ql = (q or "").lower()

    is_short_dir = direction.startswith("偏空")

    def buy_line():
        if pos > 0 and is_short_dir:
            return (f"现在 {sym} 偏空（信心分 {score}），可以小仓试空，建议仓位约 {pos}%。"
                    f"入场区间 {entry}，进场前先把止损 {sl} 设好（涨破止损）。")
        if pos > 0:
            return (f"现在 {sym} 偏多（信心分 {score}），可以小仓试多，建议仓位约 {pos}%。"
                    f"入场区间 {entry}，进场前先把止损 {sl} 设好。")
        if score <= -0.6:
            return f"现在 {sym} 偏空（信心分 {score}），可考虑做空或空仓观望，等信号确认再操作。"
        return f"现在 {sym} 信号偏中性（信心分 {score}），建议先观望，不值得为了买而买。"

    # 关键词路由
    if any(k in q for k in ("止盈", "卖", "出货", "落袋", "什么时候卖")):
        if pos > 0 and tp and is_short_dir:
            return f"{sym} 做空参考止盈在 {tp}（约 -8%），价格跌到就可以分批平仓落袋；最多持有 {days} 天，到期没走也建议离场。"
        if pos > 0 and tp:
            return f"{sym} 参考止盈在 {tp}（约 +8%），价格到了就可以分批落袋；最多持有 {days} 天，到期没走也建议离场。"
        return f"现在 {sym} 没有建议持仓，谈不上卖点；等出现偏多或偏空信号、开了仓再设止盈。"
    if any(k in q for k in ("止损", "风险", "亏", "守不住", "跌", "涨")):
        if pos > 0 and sl and is_short_dir:
            return f"{sym} 做空硬止损设在 {sl}（约 +10%），涨破就无条件平仓离场，别扛单。当前组合最大风险敞口约 {d.get('max_risk_pct','—')}%。"
        if pos > 0 and sl:
            return f"{sym} 硬止损设在 {sl}（约 -10%），跌破就无条件离场，别扛单。当前组合最大风险敞口约 {d.get('max_risk_pct','—')}%。"
        return f"现在 {sym} 没有建议持仓，先不用设止损；真要开仓，记住硬止损是纪律，不设不进场。"
    if any(k in q for k in ("仓位", "买多少", "投多少", "多少钱", "多少仓")):
        if pos > 0:
            side_hint = "做空" if is_short_dir else "做多"
            return f"{sym} 建议{side_hint}仓位约 {pos}%（弱因子刻意保守）。入场 {entry}，止损 {sl}，止盈 {tp}。"
        return f"现在 {sym} 建议仓位 0%——信号不够强，空仓也是一种持仓。"
    if any(k in q for k in ("为什么", "原因", "理由", "凭什么", "依据")):
        rs = reasons[:3]
        body = "；".join(rs) if rs else "当前因子偏中性，没有特别强的方向依据。"
        return f"{sym} 之所以判 {direction}（信心分 {score}），主要因为：{body}。"
    if any(k in q for k in ("现价", "价格", "多少钱一个", "报价")):
        return f"{sym} 现价约 {price}。距历史高点回撤 {fac.get('drawdown_from_ath_pct','—')}%，30 日动量 {fac.get('momentum_30d_pct','—')}%。"
    if any(k in q for k in ("战绩", "胜率", "赚", "盈亏", "表现", "准不准")):
        if st and st.get("closed_trades"):
            return (f"模拟盘到目前：已平仓 {st['closed_trades']} 笔，胜率 {st.get('win_rate_pct','—')}%，"
                    f"盈亏比 {st.get('profit_factor','—')}，账户总权益 {st.get('equity_usdt','—')}U"
                    f"（较起始 {st.get('equity_change_pct','—')}%）。还在攒样本，多跑一阵更有说服力。")
        return "模拟盘还没有已平仓记录，先让它自动跟盘跑一阵子，再回来看胜率和盈亏比。"
    if any(k in q for k in ("买", "该买", "能买", "进场", "入场", "做多", "操作", "怎么办", "建议")):
        return buy_line()

    # 默认：给一份一句话总览
    base = buy_line()
    if pos > 0:
        base += f" 止盈 {tp}、止损 {sl}，最多持有 {days} 天。"
    return base


def _llm_config() -> dict | None:
    """读取 LLM 配置；未配置返回 None（此时走规则问答兜底）。
    统一走 jarvis_llm_config：桌面端「设置 → 大模型」保存的 llm_config.json 优先，
    环境变量（DEEPSEEK_API_KEY / JARVIS_LLM_API_KEY / OPENAI_API_KEY + BASE_URL/MODEL）兜底。"""
    import jarvis_llm_config as _jlc

    return _jlc.get_llm_config()


def _gather_lessons(snap: dict, sym: str) -> list[dict]:
    """从经验教训库取出当前行情下应主动援引的教训（静态硬经验 + journal 动态挖掘）。

    动态挖掘读 SQLite 失败时不能拖垮问答，整体兜底成空列表。
    """
    d = (snap or {}).get("decision", {}) or {}
    fac = (snap or {}).get("factor_state", {}) or {}
    rd = (snap or {}).get("real_data", {}) or {}
    try:
        return jl.applicable_lessons(fac, rd, d.get("direction"), symbol=sym)
    except Exception:  # noqa: BLE001
        return []


# 内置问答 system prompt（设置页 system_prompt_extra 会追加在其后，借鉴 QD 可配置化思路）。
# 人设定位：并肩看盘的老战友，说人话、有温度；禁模板腔/AI 腔/满屏列表/重复免责声明。
_ASK_SYSTEM = (
    "你是贾维斯，用户的贴身交易搭档——像陪他并肩看盘好几年的老战友，不是客服机器人。\n"
    "\n"
    "说话方式：\n"
    "- 说人话：口语、自然、有温度，偶尔可以调侃一句或自嘲一下，但别尬、别油。\n"
    "- 长短看问题：打招呼、闲聊、简单确认，一两句话就完事；真问行情、要不要动手，再认真展开。\n"
    "- 别套模板：不要固定开场白和收尾（比如「好的」「关于您的问题」「希望对您有帮助」），"
    "严禁「作为AI助手」「作为一个人工智能」这类话。\n"
    "- 少用列表：能用一段自然的话说清楚就别拆条目；只有真在对比好几个要点时才用列表。\n"
    "- 上一轮聊过的东西别从头复述，接着话头往下说。\n"
    "\n"
    "专业底线（这些不许松）：\n"
    "- 所有数字和结论只能来自 context 数据（决策快照、持仓、挂单、战绩、经验教训），"
    "绝不编造价位；手上没有的数据就直说「这个我这没数」。\n"
    "- 用户问持仓、挂单、盈亏，直接引用 open_positions / pending_orders 里的真实数字，"
    "像老朋友替他盯着账户那样自然带出来。\n"
    "- 命中 lessons（经验教训）时，用「我以前在这上面栽过跟头，所以这次…」的口吻自然引出，别生硬罗列。\n"
    "- 真给出操作建议（开仓/止损/止盈价位）时，顺口带一句这是模拟盘研究、最终他自己拿主意——"
    "轻轻一句就够，别每条都念免责声明。\n"
    "\n"
    "全程简体中文。"
)

_ASK_HISTORY_MAX = 8          # 多轮上下文最多携带的历史消息条数
_ASK_HISTORY_ITEM_MAX = 1500  # 单条历史消息截断长度


def _ask_live_context(spot: str) -> dict:
    """持仓 + 挂单实时摘要（问答上下文注入）；任何失败降级为空，绝不拖垮问答。"""
    out: dict = {"open_positions": [], "pending_orders": []}
    try:
        for p in jpt.all_positions(spot):
            if p.get("status") != "open":
                continue
            side = (p.get("side") or "buy").lower()
            out["open_positions"].append({
                "symbol": p.get("symbol"),
                "direction": "long" if side == "buy" else "short",
                "qty": p.get("qty"),
                "entry_price": p.get("entry_price"),
                "stop_loss": p.get("stop_loss"),
                "take_profit": p.get("take_profit"),
                "opened_date": p.get("entry_date"),
            })
    except Exception:  # noqa: BLE001
        pass
    try:
        out["pending_orders"] = [
            {"symbol": o.get("symbol"), "side": o.get("side"),
             "price": o.get("price"), "qty": o.get("qty")}
            for o in jw.pending_orders(spot)[:5]
        ]
    except Exception:  # noqa: BLE001
        pass
    return out


def _build_ask_context(snap: dict, st: dict, sym: str,
                       lessons: list[dict] | None) -> dict:
    """聚合问答上下文 JSON（决策快照 + 教训 + 战绩 + 持仓/挂单）。"""
    d = (snap or {}).get("decision", {}) or {}
    fac = (snap or {}).get("factor_state", {}) or {}
    rd = (snap or {}).get("real_data", {}) or {}
    lesson_brief = [
        {
            "title": l.get("title"),
            "advice": l.get("advice"),
            "severity": l.get("severity"),
            "source": l.get("source"),
            "evidence": l.get("evidence"),
        }
        for l in (lessons or [])[:5]
    ]
    context = {
        "symbol": sym,
        "price": fac.get("price"),
        "conviction_score": d.get("conviction_score"),
        "direction": d.get("direction"),
        "suggested_position_pct": d.get("suggested_position_pct"),
        "entry_zone": d.get("entry_zone"),
        "stop_loss": d.get("stop_loss"),
        "take_profit_ref": d.get("take_profit_ref"),
        "time_stop_days": d.get("time_stop_days"),
        "reasons": d.get("reasons", []),
        "expected_value": d.get("expected_value"),
        "drawdown_from_ath_pct": fac.get("drawdown_from_ath_pct"),
        "momentum_30d_pct": fac.get("momentum_30d_pct"),
        "fear_greed": rd.get("fear_greed"),
        "funding": rd.get("funding"),
        "lessons": lesson_brief,
        "paper_stats": {
            "win_rate_pct": st.get("win_rate_pct"),
            "profit_factor": st.get("profit_factor"),
            "closed_trades": st.get("closed_trades"),
            "equity_usdt": st.get("equity_usdt"),
        } if st else {},
    }
    context.update(_ask_live_context(sym))
    return context


def _build_ask_messages(question: str, snap: dict, st: dict, sym: str,
                        lessons: list[dict] | None = None,
                        history: list[dict] | None = None) -> list[dict]:
    """组装多轮 messages：system（内置+用户附加）→ 历史对话 → 当前问题（带上下文 JSON）。

    上下文 JSON 只拼在最后一条 user 消息里，保证历史轮不携带过期行情数据。
    """
    import jarvis_llm_config as jlc

    system = _ASK_SYSTEM
    try:
        extra = jlc.get_params().get("system_prompt_extra") or ""
        if extra:
            system += "\n用户偏好补充（优先级低于以上规则）：" + extra
    except Exception:  # noqa: BLE001
        pass
    messages: list[dict] = [{"role": "system", "content": system}]
    for m in (history or [])[-_ASK_HISTORY_MAX:]:
        role = str((m or {}).get("role", "")).lower()
        content = str((m or {}).get("content", "") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:_ASK_HISTORY_ITEM_MAX]})
    context = _build_ask_context(snap, st, sym, lessons)
    messages.append({
        "role": "user",
        "content": f"决策与数据(JSON)：{json.dumps(context, ensure_ascii=False)}\n\n用户问题：{question}",
    })
    return messages


def _llm_answer(
    question: str, snap: dict, st: dict, sym: str,
    lessons: list[dict] | None = None, history: list[dict] | None = None,
) -> str | None:
    """接入真实 LLM 回答问题；失败/未配置返回 None 让上层走规则兜底。

    除当前决策快照外，还注入 lessons（经验教训）、模拟盘持仓/挂单，
    并支持多轮 history——让贾维斯能"接着上一句聊"而不是每问都失忆。
    """
    import jarvis_llm_config as jlc

    messages = _build_ask_messages(question, snap, st, sym, lessons, history)
    return jlc.chat(messages, timeout=30, module="ask")


def _parse_ask_body(symbol: str, q: str, data: dict | None) -> tuple[str, str, list[dict]]:
    """统一解析问答入参：返回 (spot, question, history)。"""
    history: list[dict] = []
    if isinstance(data, dict) and data:
        q = str(data.get("question") or data.get("q") or q or "")
        symbol = str(data.get("symbol") or symbol)
        raw_hist = data.get("history")
        if isinstance(raw_hist, list):
            history = [m for m in raw_hist if isinstance(m, dict)]
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    return spot, (q or "").strip(), history


def _ask_gather(spot: str) -> tuple[dict, dict, list[dict]]:
    """聚合问答依赖数据：快照（5min 缓存）+ 模拟盘战绩 + 经验教训。"""
    snap = _cached(f"snap:{spot}", 300, lambda: jb.build(spot))
    try:
        st = jpt.stats(_trader_cfg(), spot)
    except Exception:  # noqa: BLE001
        st = {}
    lessons = _gather_lessons(snap, spot)
    return snap, st, lessons


@app.post("/api/ask")
def api_ask(symbol: str = "BTCUSDT", q: str = "", data: dict | None = None):
    """小白问答：优先接真实 LLM（设置页/环境变量配置），失败/未配置则用规则问答兜底。

    入参兼容两种形态（桌面端发 JSON body，网页端发 query 参数）：
      - JSON body：{"question"|"q": "...", "symbol": "...", "history": [{role,content}...]}
      - query：?symbol=BTCUSDT&q=...
    history 为前端携带的多轮上下文（最多取最近 8 条），实现连续对话。
    """
    spot, question, history = _parse_ask_body(symbol, q, data)
    if not question:
        return JSONResponse({"ok": False, "answer": "你想问点啥？比如「现在该买吗」「止盈止损在哪」「最近战绩怎样」。"})
    snap, st, lessons = _ask_gather(spot)
    llm = _llm_answer(question, snap, st, spot, lessons, history)
    answer = llm if llm else _answer_question(question, snap, st)
    return JSONResponse({"ok": True, "symbol": spot, "question": question,
                         "answer": answer, "engine": "llm" if llm else "rule",
                         "lessons_cited": len(lessons) if llm else 0})


@app.post("/api/ask/stream")
def api_ask_stream(data: dict | None = None):
    """流式问答（借鉴 QD 的 SSE 输出）：token 级增量推送，前端边收边渲染。

    body: {"question": "...", "symbol": "BTCUSDT", "history": [{role,content}...]}
    SSE 事件（data: JSON）：
      {"type":"meta","engine":"llm"|"rule","model":...}   —— 首包
      {"type":"delta","content":"..."}                    —— 增量文本（rule 模式为整段一次）
      {"type":"done","lessons_cited":N}                   —— 收尾
      {"type":"error","message":"..."}                    —— 异常（已推送的内容仍有效）
    未配置 LLM / 首包前失败自动降级规则问答（engine=rule），保证永远有回答。
    """
    import jarvis_llm_config as jlc

    spot, question, history = _parse_ask_body("BTCUSDT", "", data)
    if not question:
        return JSONResponse({"ok": False, "answer": "你想问点啥？"}, status_code=400)

    def _sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def gen():
        try:
            snap, st, lessons = _ask_gather(spot)
        except Exception as exc:  # noqa: BLE001 — 数据聚合失败也要给出可读回复
            yield _sse({"type": "meta", "engine": "rule", "model": None})
            yield _sse({"type": "delta", "content": f"数据获取失败（{repr(exc)[:120]}），稍后再试。"})
            yield _sse({"type": "done", "lessons_cited": 0})
            return
        cfg = jlc.get_llm_config()
        if cfg:
            messages = _build_ask_messages(question, snap, st, spot, lessons, history)
            try:
                stream = jlc.chat_stream(messages, timeout=90, module="ask")
                yield _sse({"type": "meta", "engine": "llm", "model": cfg.get("model")})
                got_any = False
                for delta in stream:
                    got_any = True
                    yield _sse({"type": "delta", "content": delta})
                if got_any:
                    yield _sse({"type": "done", "lessons_cited": len(lessons)})
                    return
                # 流成功建立但一个字没吐：走规则兜底
            except (jlc.LLMNotConfigured, jlc.LLMCallError) as exc:
                _log_emit(f"ask/stream LLM 失败转规则兜底: {exc}", "warn", "ask")
        # 规则兜底（未配置 / 首包前失败 / 空流）
        answer = _answer_question(question, snap, st)
        yield _sse({"type": "meta", "engine": "rule", "model": None})
        yield _sse({"type": "delta", "content": answer})
        yield _sse({"type": "done", "lessons_cited": 0})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─────────────────────────── AI 交易复盘 API（借鉴 QD 策略复盘）───────────────────────────

_REVIEW_TTL = 600  # 复盘结果缓存 10 分钟（同参数重复点击不重复烧 token）


def _review_rule_stats(closed: list[dict]) -> dict:
    """已平仓交易的规则统计（不依赖 LLM，永远可用）。"""
    wins = [p for p in closed if (p.get("realized_pnl_usdt") or 0) > 0]
    losses = [p for p in closed if (p.get("realized_pnl_usdt") or 0) < 0]
    gross_profit = sum(p["realized_pnl_usdt"] for p in wins) if wins else 0.0
    gross_loss = sum(p["realized_pnl_usdt"] for p in losses) if losses else 0.0
    exit_dist: dict[str, int] = {}
    hold_days: list[float] = []
    max_consec_loss = consec = 0
    for p in sorted(closed, key=lambda x: x.get("closed_ts") or 0):
        reason = str(p.get("exit_reason") or "unknown")
        exit_dist[reason] = exit_dist.get(reason, 0) + 1
        if p.get("opened_ts") and p.get("closed_ts"):
            hold_days.append((p["closed_ts"] - p["opened_ts"]) / 86400)
        if (p.get("realized_pnl_usdt") or 0) < 0:
            consec += 1
            max_consec_loss = max(max_consec_loss, consec)
        else:
            consec = 0
    by_side: dict[str, dict] = {}
    for side_key, side_name in (("buy", "long"), ("sell", "short")):
        rows = [p for p in closed if (p.get("side") or "buy").lower() == side_key]
        if rows:
            side_wins = [p for p in rows if (p.get("realized_pnl_usdt") or 0) > 0]
            by_side[side_name] = {
                "trades": len(rows),
                "win_rate_pct": round(100 * len(side_wins) / len(rows), 1),
                "pnl_usdt": round(sum(p.get("realized_pnl_usdt") or 0 for p in rows), 2),
            }
    best = max(closed, key=lambda p: p.get("realized_pnl_usdt") or 0, default=None)
    worst = min(closed, key=lambda p: p.get("realized_pnl_usdt") or 0, default=None)

    def _trade_brief(p: dict | None) -> dict | None:
        if not p:
            return None
        return {"symbol": p.get("symbol"), "side": p.get("side"),
                "pnl_usdt": round(p.get("realized_pnl_usdt") or 0, 2),
                "pnl_pct": p.get("realized_pnl_pct"),
                "exit_reason": p.get("exit_reason")}

    return {
        "closed_trades": len(closed),
        "win_rate_pct": round(100 * len(wins) / len(closed), 1) if closed else None,
        "profit_factor": round(gross_profit / abs(gross_loss), 3) if gross_loss else None,
        "total_pnl_usdt": round(gross_profit + gross_loss, 2),
        "avg_win_usdt": round(gross_profit / len(wins), 2) if wins else None,
        "avg_loss_usdt": round(gross_loss / len(losses), 2) if losses else None,
        "avg_hold_days": round(sum(hold_days) / len(hold_days), 1) if hold_days else None,
        "max_consecutive_losses": max_consec_loss,
        "exit_reason_dist": exit_dist,
        "by_side": by_side,
        "best_trade": _trade_brief(best),
        "worst_trade": _trade_brief(worst),
    }


def _review_rules_only(stats: dict) -> dict:
    """无 LLM 时的规则复盘（阈值化诊断，保证功能可用）。"""
    diagnosis, recommendations = [], []
    wr = stats.get("win_rate_pct")
    pf = stats.get("profit_factor")
    aw, al = stats.get("avg_win_usdt"), stats.get("avg_loss_usdt")
    if wr is not None and wr < 40:
        diagnosis.append(f"胜率 {wr}% 偏低，入场信号质量或时机需要复查")
        recommendations.append("提高开仓信心阈值，减少弱信号出手")
    if pf is not None and pf < 1:
        diagnosis.append(f"盈亏比 {pf} 小于 1，整体在亏损")
        recommendations.append("检查止损/止盈比例设置，砍掉期望值为负的交易模式")
    if aw is not None and al is not None and abs(al) > abs(aw):
        diagnosis.append(f"平均亏损 {al}U 大于平均盈利 {aw}U，存在「亏大赚小」")
        recommendations.append("止损更果断、让盈利多跑一段（收紧止损或放宽止盈）")
    if (stats.get("max_consecutive_losses") or 0) >= 3:
        diagnosis.append(f"最大连亏 {stats['max_consecutive_losses']} 笔，注意情绪化连续开仓")
        recommendations.append("连亏 2 笔后暂停开仓、复查市场环境是否变化")
    dist = stats.get("exit_reason_dist") or {}
    if dist.get("stop_loss", 0) > dist.get("take_profit", 0):
        diagnosis.append("止损离场多于止盈离场，方向判断或入场点位有系统性偏差")
    if not diagnosis:
        diagnosis.append("样本内未发现显著问题，继续积累样本")
    return {
        "summary": (f"已平仓 {stats.get('closed_trades', 0)} 笔，胜率 {wr if wr is not None else '—'}%，"
                    f"盈亏比 {pf if pf is not None else '—'}，累计盈亏 {stats.get('total_pnl_usdt', 0)}U。"),
        "diagnosis": diagnosis,
        "recommendations": recommendations or ["保持当前纪律，样本到 30 笔后再做深度复盘"],
        "cautions": ["模拟盘研究，不构成投资建议"],
    }


_REVIEW_SYSTEM = (
    "你是专业量化交易复盘员。基于给定的模拟盘统计 JSON（胜率/盈亏比/离场原因分布/"
    "多空表现/最佳最差交易/连亏等），输出严格 JSON："
    '{"summary":"一段话总结","diagnosis":["问题1",…],"recommendations":["建议1",…],"cautions":["提醒1",…]}。'
    "要求：① 每条诊断必须引用统计里的具体数字；② 不得编造统计之外的交易；"
    "③ diagnosis/recommendations 各 2~5 条，cautions 1~3 条且必含「模拟盘研究，不构成投资建议」；"
    "④ 简体中文；⑤ 只输出 JSON。"
)


@app.post("/api/jarvis/review")
def api_jarvis_review(data: dict | None = None):
    """AI 交易复盘（借鉴 QD 策略复盘）：模拟盘已平仓交易 → 规则统计 + LLM 诊断建议。

    body: {"symbol": "BTCUSDT" | 空=全部, "limit": 50}
    响应: {ok, symbol, stats, review:{summary,diagnosis,recommendations,cautions},
           source:"llm"|"rules", cached}
    无已平仓交易时 ok:false；无 LLM 配置自动降级规则复盘（source=rules）。
    """
    import jarvis_llm_config as jlc

    d = data or {}
    sym_raw = str(d.get("symbol") or "").upper().replace("-", "").replace("/", "").strip()
    spot = (sym_raw if sym_raw.endswith("USDT") else sym_raw + "USDT") if sym_raw else None
    limit = max(5, min(int(d.get("limit") or 50), 200))
    cache_key = f"review:{spot or 'ALL'}:{limit}"
    hit = _CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _REVIEW_TTL:
        return JSONResponse({**hit[1], "cached": True})

    try:
        closed = [p for p in jpt.all_positions(spot)
                  if p.get("status") == "closed" and p.get("realized_pnl_usdt") is not None]
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"读取台账失败: {repr(exc)[:200]}"})
    closed = closed[:limit]
    if not closed:
        return JSONResponse({"ok": False,
                             "error": "还没有已平仓的模拟盘交易，先让贾维斯跑一阵再来复盘。"})

    stats = _review_rule_stats(closed)
    review, source = None, "rules"
    raw = jlc.chat(
        [{"role": "system", "content": _REVIEW_SYSTEM},
         {"role": "user", "content": json.dumps(stats, ensure_ascii=False)}],
        temperature=0.3, max_tokens=1200, json_mode=True, timeout=45,
        module="review",
    )
    if raw:
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("summary"):
                review = {
                    "summary": str(obj.get("summary", ""))[:600],
                    "diagnosis": [str(x)[:200] for x in (obj.get("diagnosis") or [])][:6],
                    "recommendations": [str(x)[:200] for x in (obj.get("recommendations") or [])][:6],
                    "cautions": [str(x)[:200] for x in (obj.get("cautions") or [])][:4],
                }
                source = "llm"
        except ValueError:
            review = None
    if review is None:
        review = _review_rules_only(stats)
    result = {"ok": True, "symbol": spot or "ALL", "stats": stats,
              "review": review, "source": source}
    _CACHE[cache_key] = (time.time(), result)
    return JSONResponse({**result, "cached": False})


# ─────────────────────────── 短线自动交易 API ───────────────────────────

try:
    import jarvis_scalper_trader as jst
    _HAS_SCALPER_TRADER = True
except ImportError:
    _HAS_SCALPER_TRADER = False

try:
    import jarvis_scalper_evolve as jse
    _HAS_SCALPER_EVOLVE = True
except ImportError:
    _HAS_SCALPER_EVOLVE = False

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

_SCALPER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scalper_config.yaml")


# ============ 进化引擎进程管理（桌面端可见：开始/过程/结束/结果）============
import re as _re
import subprocess as _subprocess

_DASH_ROOT = os.path.dirname(os.path.abspath(__file__))
_EVOLVE_LOCK = _threading.Lock()
_EVOLVE_PROC: "_subprocess.Popen | None" = None
_EVOLVE_STATE: dict = {
    "running": False,
    "mode": None,
    "symbol": None,
    "timeframe": None,
    "rounds": None,
    "current_round": 0,
    "started_at": 0.0,
    "finished_at": 0.0,
    "pid": None,
    "last_line": "",
    "exit_code": None,
}
_EVOLVE_ROUND_RX = _re.compile(r"第\s*(\d+)\s*/\s*(\d+)\s*轮")


def _evolve_reader(proc: "_subprocess.Popen") -> None:
    """读取进化子进程输出：逐行进日志缓冲（前端「终端」页可见）并解析轮次进度。"""
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            _log_emit(line, "info", "evolve")
            with _EVOLVE_LOCK:
                _EVOLVE_STATE["last_line"] = line
                m = _EVOLVE_ROUND_RX.search(line)
                if m:
                    _EVOLVE_STATE["current_round"] = int(m.group(1))
                    _EVOLVE_STATE["rounds"] = int(m.group(2))
    except Exception as e:  # noqa: BLE001
        _log_emit(f"读取进化输出异常: {e}", "error", "evolve")
    finally:
        try:
            proc.wait()
        except Exception:  # noqa: BLE001
            pass
        with _EVOLVE_LOCK:
            _EVOLVE_STATE["running"] = False
            _EVOLVE_STATE["finished_at"] = time.time()
            _EVOLVE_STATE["exit_code"] = proc.returncode
        ok = proc.returncode == 0
        _log_emit(
            f"{'✓' if ok else '✗'} 进化进程结束 (code={proc.returncode})",
            "info" if ok else "error",
            "evolve",
        )


# ============ 短线交易引擎进程管理（桌面端一键启动/停止，模拟盘）============
_SCALPER_LOCK = _threading.Lock()
_SCALPER_PROC: "_subprocess.Popen | None" = None
_SCALPER_STATE_PATH = os.path.expanduser("~/.vibe-trading/scalper_trader_state.json")


def _scalper_reader(proc: "_subprocess.Popen") -> None:
    """读取短线交易子进程输出，逐行进日志缓冲（前端「终端」页可见）。"""
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line.strip():
                _log_emit(line, "info", "scalper")
    except Exception as e:  # noqa: BLE001
        _log_emit(f"读取短线交易输出异常: {e}", "error", "scalper")
    finally:
        try:
            proc.wait()
        except Exception:  # noqa: BLE001
            pass
        _log_emit(
            f"{'✓' if proc.returncode == 0 else '✗'} 短线交易进程结束 (code={proc.returncode})",
            "info" if proc.returncode == 0 else "error",
            "scalper",
        )


def _scalper_heartbeat() -> tuple[bool, float | None]:
    """通过状态文件心跳判断短线引擎是否存活（覆盖命令行手动启动的场景）。

    仅认 run 永续循环写入的 loop_ts + loop_pid（dry-run 单轮不写不误报）：
    loop_ts 在两个 15m 周期内 且 loop_pid 进程仍存活 → 引擎在跑。

    Returns:
        (alive, last_cycle_ts)
    """
    try:
        with open(_SCALPER_STATE_PATH, encoding="utf-8") as f:
            st = json.load(f) or {}
        last_cycle_ts = float(st.get("last_cycle_ts") or 0) or None
        loop_ts = float(st.get("loop_ts") or 0)
        loop_pid = int(st.get("loop_pid") or 0)
    except Exception:  # noqa: BLE001
        return False, None
    if loop_ts <= 0 or loop_pid <= 0:
        return False, last_cycle_ts
    if (time.time() - loop_ts) >= 1800:
        return False, last_cycle_ts
    try:
        os.kill(loop_pid, 0)  # 信号 0 仅探测进程存在
    except OSError:
        return False, last_cycle_ts
    return True, last_cycle_ts


def _scalper_proc_running() -> bool:
    with _SCALPER_LOCK:
        return _SCALPER_PROC is not None and _SCALPER_PROC.poll() is None


@app.get("/api/scalper/status")
def api_scalper_status():
    if not _HAS_SCALPER_TRADER:
        return JSONResponse({"error": "jarvis_scalper_trader 模块未安装"}, status_code=503)
    try:
        report = jst.get_report()
        cfg = jst.load_config()
        best = None
        if _HAS_SCALPER_EVOLVE:
            best = jse.get_best_strategy()
        hb_alive, hb_ts = _scalper_heartbeat()
        running = _scalper_proc_running() or hb_alive
        return JSONResponse({
            "running": running,
            "managed_by_dashboard": _scalper_proc_running(),
            "last_cycle_ts": hb_ts,
            "strategy": best.get("name") if best else None,
            "symbol": cfg.get("symbol", "BTCUSDT"),
            "timeframe": cfg.get("timeframe", "15m"),
            "report": report,
            "config": {
                "confidence_threshold": cfg.get("trading", {}).get("confidence_threshold", 0.6),
                "max_positions": cfg.get("risk", {}).get("max_concurrent_positions", 3),
                "aggressive_mode": cfg.get("trading", {}).get("aggressive_mode", False),
            },
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/scalper/report")
def api_scalper_report():
    if not _HAS_SCALPER_TRADER:
        return JSONResponse({"error": "jarvis_scalper_trader 模块未安装"}, status_code=503)
    try:
        return JSONResponse(jst.get_report())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/scalper/log")
def api_scalper_log(limit: int = 50):
    log_path = os.path.expanduser("~/.vibe-trading/jarvis_scalper_trader.log")
    if not os.path.exists(log_path):
        return JSONResponse({"lines": [], "total": 0})
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        lines = [l.rstrip() for l in lines if l.strip()]
        total = len(lines)
        return JSONResponse({"lines": lines[-min(limit, total):], "total": total})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/scalper/start")
def api_scalper_start(symbol: str = "BTCUSDT"):
    """后台启动短线交易永续循环（模拟盘）；输出实时进「终端」页。"""
    global _SCALPER_PROC
    if not _HAS_SCALPER_TRADER:
        return JSONResponse({"ok": False, "reason": "jarvis_scalper_trader 模块未安装"}, status_code=503)
    if _scalper_proc_running():
        return JSONResponse({"ok": False, "reason": "短线交易已在运行中"})
    hb_alive, _ = _scalper_heartbeat()
    if hb_alive:
        return JSONResponse({"ok": False, "reason": "检测到命令行启动的短线交易正在运行，请先停止它"})
    sym = symbol.upper().replace("-", "").replace("/", "")
    cmd = [_sys.executable, "jarvis_scalper_trader.py", "run", "--symbol", sym]
    try:
        proc = _subprocess.Popen(
            cmd,
            cwd=_DASH_ROOT,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:  # noqa: BLE001
        _log_emit(f"✗ 短线交易启动失败: {e}", "error", "scalper")
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)
    with _SCALPER_LOCK:
        _SCALPER_PROC = proc
    _log_emit(f"▶ 短线交易启动 symbol={sym} (pid={proc.pid}，模拟盘)", "info", "scalper")
    _threading.Thread(target=_scalper_reader, args=(proc,), daemon=True).start()
    return JSONResponse({"ok": True, "pid": proc.pid, "symbol": sym})


@app.post("/api/scalper/stop")
def api_scalper_stop():
    global _SCALPER_PROC
    with _SCALPER_LOCK:
        proc = _SCALPER_PROC
    if proc is None or proc.poll() is not None:
        hb_alive, _ = _scalper_heartbeat()
        if hb_alive:
            return JSONResponse({"ok": False, "reason": "短线交易由命令行启动，请在对应终端 Ctrl+C 停止"})
        return JSONResponse({"ok": False, "reason": "当前没有运行中的短线交易"})
    try:
        proc.terminate()
        _log_emit("■ 已请求停止短线交易进程", "warn", "scalper")
        return JSONResponse({"ok": True})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


# ─────────────────────────── 进化引擎 API ───────────────────────────

@app.get("/api/evolve/status")
def api_evolve_status():
    with _EVOLVE_LOCK:
        st = dict(_EVOLVE_STATE)
    elapsed = 0.0
    if st["started_at"]:
        end = st["finished_at"] or time.time()
        elapsed = max(0.0, end - st["started_at"])
    resp: dict[str, Any] = {
        "running": st["running"],
        "mode": st["mode"],
        "symbol": st["symbol"],
        "current_round": st["current_round"],
        "total_rounds": st["rounds"] or 0,
        "elapsed_seconds": round(elapsed, 1),
        "last_line": st["last_line"],
        "exit_code": st["exit_code"],
        "status": "running" if st["running"] else ("done" if st["finished_at"] else "idle"),
    }
    if _HAS_SCALPER_EVOLVE:
        try:
            gy = jse.load_graveyard()
            hof = jse.load_hall_of_fame()
            best = jse.get_best_strategy()
            resp.update({
                "graveyard_count": len(gy),
                "hall_of_fame_count": len(hof),
                "best_strategy": best.get("name") if best else None,
                "best_win_rate": best.get("win_rate_pct") if best else None,
                "best_profit_factor": best.get("profit_factor") if best else None,
            })
        except Exception as e:  # noqa: BLE001
            resp["counts_error"] = str(e)
    return JSONResponse(resp)


@app.post("/api/evolve/start")
def api_evolve_start(rounds: int = 10, symbol: str = "BTCUSDT", timeframe: str = "15m", mode: str = "evolve"):
    """真正后台启动进化引擎；过程输出实时进「终端」页，结果落名人堂/墓地。"""
    global _EVOLVE_PROC
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"ok": False, "error": "jarvis_scalper_evolve 模块未安装"}, status_code=503)
    with _EVOLVE_LOCK:
        if _EVOLVE_STATE["running"]:
            return JSONResponse({"ok": False, "error": "进化已在运行中"})
    rounds = max(1, min(int(rounds), 100))
    sym = symbol.upper().replace("-", "").replace("/", "")
    cmd_name = "evolve-combo" if mode == "combo" else "evolve"
    cmd = [
        _sys.executable, "jarvis_scalper_evolve.py", cmd_name,
        "--symbol", sym, "--rounds", str(rounds), "--timeframe", timeframe,
    ]
    try:
        proc = _subprocess.Popen(
            cmd,
            cwd=_DASH_ROOT,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:  # noqa: BLE001
        _log_emit(f"✗ 进化启动失败: {e}", "error", "evolve")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    with _EVOLVE_LOCK:
        _EVOLVE_PROC = proc
        _EVOLVE_STATE.update({
            "running": True,
            "mode": cmd_name,
            "symbol": sym,
            "timeframe": timeframe,
            "rounds": rounds,
            "current_round": 0,
            "started_at": time.time(),
            "finished_at": 0.0,
            "pid": proc.pid,
            "last_line": "",
            "exit_code": None,
        })
    _log_emit(
        f"▶ 进化开始 [{cmd_name}] symbol={sym} timeframe={timeframe} rounds={rounds} (pid={proc.pid})",
        "info",
        "evolve",
    )
    _threading.Thread(target=_evolve_reader, args=(proc,), daemon=True).start()
    return JSONResponse({"ok": True, "pid": proc.pid, "symbol": sym, "rounds": rounds, "mode": cmd_name})


@app.post("/api/evolve/stop")
def api_evolve_stop():
    global _EVOLVE_PROC
    with _EVOLVE_LOCK:
        proc = _EVOLVE_PROC
        running = _EVOLVE_STATE["running"]
    if not running or proc is None:
        return JSONResponse({"ok": False, "error": "当前没有运行中的进化"})
    try:
        proc.terminate()
        _log_emit("■ 已请求停止进化进程", "warn", "evolve")
        return JSONResponse({"ok": True})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ============ 单次回测（QD 同款明细：资金曲线 + 逐笔成交 + 指标）============
from pydantic import BaseModel

try:
    import jarvis_scalper_backtest as jbt
    _HAS_BACKTEST = True
except ImportError:
    _HAS_BACKTEST = False

# 回测历史持久化（backtest_runs 表 + /api/backtest/history 端点，见独立模块）
try:
    import jarvis_backtest_history as _jbh
    if _jbh.router is not None:
        app.include_router(_jbh.router)
    _HAS_BT_HISTORY = True
except ImportError:
    _HAS_BT_HISTORY = False


class BacktestReq(BaseModel):
    name: str = ""
    code: str = ""
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"
    start: str = "2025-01-01"
    end: str = "2026-06-01"
    capital: float = 10000

_BT_LOCK = _threading.Lock()
_BT_STATE: dict = {
    "running": False,
    "started_at": 0.0,
    "finished_at": 0.0,
    "params": None,
    "result": None,
    "error": None,
}


def _find_hof_code(name: str) -> "str | None":
    if not _HAS_SCALPER_EVOLVE:
        return None
    try:
        for e in jse.load_hall_of_fame():
            if e.get("name") == name:
                return e.get("code")
    except Exception:  # noqa: BLE001
        return None
    return None


def _backtest_worker(code, symbol, timeframe, start, end, capital, label):
    _log_emit(
        f"▶ 回测开始 [{label}] {symbol} {timeframe} {start}~{end} 本金={capital}",
        "info",
        "backtest",
    )
    t0 = time.time()
    try:
        res = jbt.run_backtest(
            code=code,
            symbol=symbol,
            timeframe=timeframe,
            start_date=start,
            end_date=end,
            initial_capital=capital,
        )
        ok = res.get("status") == "succeeded"
        with _BT_LOCK:
            _BT_STATE["result"] = res
            _BT_STATE["error"] = None if ok else res.get("error", res.get("status"))
        if _HAS_BT_HISTORY:
            rid = _jbh.record_run(
                {"name": label, "symbol": symbol, "timeframe": timeframe,
                 "start": start, "end": end, "capital": capital},
                res,
            )
            if rid:
                _log_emit(f"回测历史已保存 #{rid}", "info", "backtest")
        if ok:
            _log_emit(
                f"✓ 回测完成 收益={res.get('total_return_pct', 0):.2f}% "
                f"胜率={res.get('win_rate', 0):.1f}% 成交={res.get('total_trades', 0)} "
                f"({time.time() - t0:.1f}s)",
                "info",
                "backtest",
            )
        else:
            _log_emit(f"✗ 回测未成功: {res.get('status')} {res.get('error', '')}", "error", "backtest")
    except Exception as e:  # noqa: BLE001
        with _BT_LOCK:
            _BT_STATE["error"] = str(e)
            _BT_STATE["result"] = None
        if _HAS_BT_HISTORY:
            _jbh.record_run(
                {"name": label, "symbol": symbol, "timeframe": timeframe,
                 "start": start, "end": end, "capital": capital},
                None, error=str(e),
            )
        _log_emit(f"✗ 回测异常: {e}", "error", "backtest")
    finally:
        with _BT_LOCK:
            _BT_STATE["running"] = False
            _BT_STATE["finished_at"] = time.time()


@app.post("/api/backtest/run")
def api_backtest_run(req: BacktestReq):
    """跑一次 QD 回测。可直接传 code（编辑后的代码），或传 name 从名人堂取代码。
    过程进「终端」页，结果含逐笔成交。"""
    if not _HAS_BACKTEST:
        return JSONResponse({"ok": False, "error": "jarvis_scalper_backtest 模块未安装"}, status_code=503)
    with _BT_LOCK:
        if _BT_STATE["running"]:
            return JSONResponse({"ok": False, "error": "已有回测在运行中"})
    code = req.code.strip() if req.code else ""
    label = req.name or "custom"
    if not code:
        if not req.name:
            return JSONResponse({"ok": False, "error": "请提供策略代码或策略名"}, status_code=400)
        code = _find_hof_code(req.name) or ""
        if not code:
            return JSONResponse({"ok": False, "error": f"名人堂未找到策略代码: {req.name}"}, status_code=404)
    qd_symbol = req.symbol.upper().replace("-", "").replace("/", "")
    if "USDT" in qd_symbol:
        qd_symbol = qd_symbol.replace("USDT", "/USDT")
    with _BT_LOCK:
        _BT_STATE.update({
            "running": True,
            "started_at": time.time(),
            "finished_at": 0.0,
            "params": {
                "name": req.name, "symbol": req.symbol, "timeframe": req.timeframe,
                "start": req.start, "end": req.end, "capital": req.capital,
            },
            "result": None,
            "error": None,
        })
    _threading.Thread(
        target=_backtest_worker,
        args=(code, qd_symbol, req.timeframe, req.start, req.end, req.capital, label),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True})


@app.get("/api/backtest/result")
def api_backtest_result():
    with _BT_LOCK:
        st = dict(_BT_STATE)
    elapsed = 0.0
    if st["started_at"]:
        endt = st["finished_at"] or time.time()
        elapsed = max(0.0, endt - st["started_at"])
    st["elapsed_seconds"] = round(elapsed, 1)
    return JSONResponse(st)


@app.get("/api/backtest/code")
def api_backtest_code(name: str):
    code = _find_hof_code(name)
    if code is None:
        return JSONResponse({"name": name, "code": "", "error": "未找到该策略代码"}, status_code=404)
    return JSONResponse({"name": name, "code": code})


@app.get("/api/evolve/graveyard")
def api_evolve_graveyard():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"error": "jarvis_scalper_evolve 模块未安装"}, status_code=503)
    try:
        return JSONResponse(jse.load_graveyard())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/evolve/graveyard/clear")
def api_evolve_graveyard_clear():
    """清空策略墓地（scalper_graveyard.json 置空）。"""
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"ok": False, "reason": "jarvis_scalper_evolve 模块未安装"}, status_code=503)
    try:
        before = len(jse.load_graveyard())
        jse.save_graveyard([])
        return JSONResponse({"ok": True, "cleared": before})
    except Exception as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


@app.get("/api/evolve/hall-of-fame")
def api_evolve_hall_of_fame():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"error": "jarvis_scalper_evolve 模块未安装"}, status_code=503)
    try:
        return JSONResponse(jse.load_hall_of_fame())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─────────────────────────── 成长进度 API ───────────────────────────

@app.get("/api/growth/timeline")
def api_growth_timeline():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse([])
    try:
        gy = jse.load_graveyard()
        hof = jse.load_hall_of_fame()
        events = []
        for entry in gy:
            events.append({
                "time": entry.get("buried_at", ""),
                "event": f"策略「{entry.get('name', '?')}」失败入墓",
                "result": "fail",
                "detail": entry.get("failure_reason", "未达标"),
                "metrics": {
                    "win_rate": entry.get("win_rate_pct"),
                    "profit_factor": entry.get("profit_factor"),
                },
            })
        for entry in hof:
            events.append({
                "time": entry.get("promoted_at", ""),
                "event": f"策略「{entry.get('name', '?')}」达标入榜",
                "result": "success",
                "detail": f"胜率 {entry.get('win_rate_pct', '?')}%，盈亏比 {entry.get('profit_factor', '?')}",
                "metrics": {
                    "win_rate": entry.get("win_rate_pct"),
                    "profit_factor": entry.get("profit_factor"),
                },
            })
        events.sort(key=lambda e: e["time"], reverse=True)
        return JSONResponse(events)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/growth/milestones")
def api_growth_milestones():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse([])
    try:
        gy = jse.load_graveyard()
        hof = jse.load_hall_of_fame()
        milestones = []
        total_strategies = len(gy) + len(hof)
        if total_strategies > 0:
            milestones.append({"title": "第一个策略", "achieved": True, "detail": f"已尝试 {total_strategies} 个策略"})
        if len(hof) > 0:
            milestones.append({"title": "第一个达标策略", "achieved": True, "detail": f"名人堂已有 {len(hof)} 个策略"})
        else:
            milestones.append({"title": "第一个达标策略", "achieved": False, "detail": "仍在进化中..."})
        if _HAS_SCALPER_TRADER:
            report = jst.get_report()
            trades = report.get("total_trades", 0)
            if trades >= 10:
                milestones.append({"title": f"累计交易 {trades} 笔", "achieved": True, "detail": f"胜率 {report.get('win_rate_pct', 0)}%"})
            if report.get("win_rate_pct", 0) >= 55:
                milestones.append({"title": "胜率突破 55%", "achieved": True, "detail": f"当前胜率 {report['win_rate_pct']}%"})
        if total_strategies >= 10:
            milestones.append({"title": "试错 10 个策略", "achieved": True, "detail": f"已尝试 {total_strategies} 个"})
        if total_strategies >= 50:
            milestones.append({"title": "试错 50 个策略", "achieved": True, "detail": f"已尝试 {total_strategies} 个"})
        elif total_strategies >= 10:
            milestones.append({"title": "试错 50 个策略", "achieved": False, "detail": f"进度 {total_strategies}/50"})
        return JSONResponse(milestones)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/growth/stats")
def api_growth_stats():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"dimensions": [], "values": []})
    try:
        gy = jse.load_graveyard()
        hof = jse.load_hall_of_fame()
        all_strategies = gy + hof
        total = len(all_strategies)

        trend_score = 50
        risk_score = 50
        timing_score = 50
        position_score = 50
        learning_score = 50

        if total > 0:
            win_rates = [s.get("win_rate_pct", 0) for s in all_strategies if s.get("win_rate_pct")]
            if win_rates:
                trend_score = min(100, max(0, int(sum(win_rates) / len(win_rates))))
            pfs = [s.get("profit_factor", 0) for s in all_strategies if s.get("profit_factor")]
            if pfs:
                risk_score = min(100, max(0, int(sum(pfs) / len(pfs) * 40)))
            if len(hof) > 0:
                timing_score = min(100, int(len(hof) / max(1, total) * 200))
            position_score = min(100, int(total * 3))
            learning_score = min(100, int((1 - len(gy) / max(1, total * 2)) * 100 + len(hof) * 20))

        dimensions = ["趋势识别", "风控能力", "择时精准", "仓位管理", "学习速度"]
        values = [trend_score, risk_score, timing_score, position_score, learning_score]

        failure_reasons = {}
        for s in gy:
            reason = s.get("failure_reason", "未知")
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

        return JSONResponse({
            "dimensions": dimensions,
            "values": values,
            "total_strategies": total,
            "success_count": len(hof),
            "failure_count": len(gy),
            "failure_reasons": failure_reasons,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─────────────────────────── 配置 API ───────────────────────────

@app.get("/api/config")
def api_config_get():
    if _HAS_YAML and os.path.exists(_SCALPER_CONFIG_PATH):
        try:
            with open(_SCALPER_CONFIG_PATH, encoding="utf-8") as f:
                cfg = _yaml.safe_load(f)
            return JSONResponse(cfg or {})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({})


@app.put("/api/config")
def api_config_put(data: dict):
    if not _HAS_YAML:
        return JSONResponse({"ok": False, "reason": "PyYAML 未安装"}, status_code=503)
    try:
        with open(_SCALPER_CONFIG_PATH, "w", encoding="utf-8") as f:
            _yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


# ─────────────────────────── 价位邮件提醒 API ───────────────────────────


@app.get("/api/alerts/config")
def api_alerts_config_get():
    """读取价位提醒全局配置（SMTP 脱敏、收件人、轮询、监控状态）。"""
    try:
        return JSONResponse(jpa.public_config())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.put("/api/alerts/config")
def api_alerts_config_put(data: dict):
    """更新价位提醒全局配置：smtp / recipients / poll_interval_s（按需提供）。"""
    try:
        if isinstance(data.get("smtp"), dict):
            jpa.update_smtp(data["smtp"])
        if "contacts" in data:
            jpa.set_contacts(data.get("contacts") or [])
        elif "recipients" in data:
            jpa.set_recipients(data.get("recipients") or [])
        if data.get("poll_interval_s") is not None:
            jpa.set_poll_interval(int(data["poll_interval_s"]))
        return JSONResponse({"ok": True, "config": jpa.public_config()})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


@app.post("/api/alerts/test-email")
def api_alerts_test_email(data: dict):
    """发送一封测试邮件，校验 SMTP 配置是否可用。

    收件人优先级：请求显式指定 > 全局默认收件人 > SMTP 账号本人（便于配完即自测）。
    始终返回 HTTP 200，失败原因放在 body.reason，避免前端只看到笼统的 400。
    """
    cfg = jpa.load_config()
    recips = data.get("recipients") or cfg.get("recipients") or []
    if not recips:
        self_addr = cfg.get("smtp", {}).get("username")
        if self_addr:
            recips = [self_addr]  # 没配收件人时默认发给自己
    out = jpa.send_email(
        "【贾维斯价位提醒】测试邮件",
        "这是一封测试邮件，收到说明 SMTP 配置正确，价位提醒可正常推送。",
        recips,
        cfg=cfg,
        dry_run=bool(data.get("dry_run")),
    )
    return JSONResponse(out)


@app.get("/api/alerts/plans")
def api_alerts_plans_get():
    try:
        return JSONResponse(jpa.list_plans())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/alerts/plans")
def api_alerts_plans_create(data: dict):
    out = jpa.add_plan(data)
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@app.put("/api/alerts/plans/{plan_id}")
def api_alerts_plans_update(plan_id: str, data: dict):
    out = jpa.update_plan(plan_id, data)
    return JSONResponse(out, status_code=200 if out.get("ok") else 404)


@app.delete("/api/alerts/plans/{plan_id}")
def api_alerts_plans_delete(plan_id: str):
    out = jpa.delete_plan(plan_id)
    return JSONResponse(out, status_code=200 if out.get("ok") else 404)


@app.post("/api/alerts/check")
def api_alerts_check(data: dict | None = None):
    """立即执行一轮价位检查（dry_run 时只判定不发信）。"""
    dry = bool((data or {}).get("dry_run"))
    try:
        return JSONResponse(jpa.evaluate_all(dry_run=dry))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/alerts/price")
def api_alerts_price(symbol: str = "BTCUSDT"):
    """取某币种现价，供前端设定目标价位时参考。"""
    price = jpa.current_price(symbol)
    return JSONResponse({"symbol": jpa._normalize_symbol(symbol), "price": price})


# ─────────────────── 交易订单邮件提醒（按笔配置） ───────────────────

@app.get("/api/order-notify")
def api_order_notify_list():
    """全部订单通知配置（前端按 order_id 建索引，渲染各单的配置状态）。"""
    try:
        return JSONResponse(jon.list_configs())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/order-notify/{order_id}")
def api_order_notify_get(order_id: str):
    cfg = jon.get_config(order_id)
    return JSONResponse({"ok": True, "config": cfg})


@app.put("/api/order-notify/{order_id}")
def api_order_notify_put(order_id: str, data: dict):
    """新建/更新某笔订单的通知配置：{email, notify_take_profit, notify_stop_loss}。"""
    out = jon.set_config(
        order_id,
        str(data.get("email") or ""),
        notify_take_profit=bool(data.get("notify_take_profit", True)),
        notify_stop_loss=bool(data.get("notify_stop_loss", True)),
    )
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@app.delete("/api/order-notify/{order_id}")
def api_order_notify_delete(order_id: str):
    return JSONResponse(jon.delete_config(order_id))


@app.post("/api/order-notify/{order_id}/test")
def api_order_notify_test(order_id: str, data: dict | None = None):
    """对某笔配置发送测试邮件（dry_run 时只组装不发信）。始终 HTTP 200，失败原因在 body.reason。"""
    out = jon.send_test_email(order_id, dry_run=bool((data or {}).get("dry_run")))
    return JSONResponse(out)


@app.on_event("startup")
def _start_price_alert_monitor():
    """随 dashboard 启动后台价位轮询线程。"""
    try:
        jpa.start_monitor()
        _log_emit("价位提醒后台监控已启动", level="info", source="price-alert")
    except Exception as e:  # noqa: BLE001
        _log_emit(f"价位提醒监控启动失败: {e}", level="error", source="price-alert")


# ─────────────────────────── 主动提醒中心 API ───────────────────────────
# 信号反转 / 价格关键位 / 割肉后回升监控 + 页内通知中心 + 浏览器通知（SSE 推送）。


@app.get("/api/alert-center/rules")
def api_ac_rules_get(symbol: str | None = None):
    try:
        return JSONResponse({"ok": True, "rules": jac.list_rules(symbol)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=500)


@app.post("/api/alert-center/rules")
def api_ac_rules_create(data: dict):
    """新建提醒规则：{kind: signal_flip|price_level|reentry, symbol, ...kind 参数}。"""
    out = jac.add_rule(data or {})
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@app.put("/api/alert-center/rules/{rule_id}")
def api_ac_rules_update(rule_id: str, data: dict):
    out = jac.update_rule(rule_id, data or {})
    return JSONResponse(out, status_code=200 if out.get("ok") else 404)


@app.delete("/api/alert-center/rules/{rule_id}")
def api_ac_rules_delete(rule_id: str):
    out = jac.delete_rule(rule_id)
    return JSONResponse(out, status_code=200 if out.get("ok") else 404)


@app.get("/api/alert-center/events")
def api_ac_events(limit: int = 50, unread_only: bool = False,
                  symbol: str | None = None):
    """提醒历史（页内通知中心数据源），新→旧。"""
    try:
        return JSONResponse({"ok": True,
                             "events": jac.list_events(limit, unread_only, symbol),
                             "unread": jac.unread_count()})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=500)


@app.get("/api/alert-center/unread-count")
def api_ac_unread():
    try:
        return JSONResponse({"ok": True, "unread": jac.unread_count()})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=500)


@app.post("/api/alert-center/events/read")
def api_ac_events_read(data: dict | None = None):
    """标记已读：{"all": true} 或 {"ids": [1,2,3]}。"""
    d = data or {}
    out = jac.mark_read(ids=d.get("ids"), mark_all=bool(d.get("all")))
    return JSONResponse(out)


@app.get("/api/alert-center/settings")
def api_ac_settings_get():
    try:
        return JSONResponse({"ok": True, "settings": jac.get_settings(),
                             "monitor": jac.monitor_status()})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=500)


@app.put("/api/alert-center/settings")
def api_ac_settings_put(data: dict):
    try:
        return JSONResponse({"ok": True, "settings": jac.update_settings(data or {})})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=500)


@app.post("/api/alert-center/check")
def api_ac_check(data: dict | None = None):
    """立即执行一轮巡检（信号规则跳过节流；dry_run 时外发渠道只演练）。"""
    d = data or {}
    try:
        return JSONResponse(jac.evaluate_all(dry_run=bool(d.get("dry_run")),
                                             force_signal=True))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=500)


@app.get("/api/alert-center/key-levels")
def api_ac_key_levels(symbol: str = "BTCUSDT", tf: str = "4h"):
    """系统关键支撑/阻力位建议（十二套共识聚合），供前端一键填入价位提醒。"""
    try:
        return JSONResponse(jac.key_levels(symbol, tf))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=500)


@app.get("/api/alert-center/status")
def api_ac_status():
    return JSONResponse({"ok": True, "monitor": jac.monitor_status()})


@app.get("/api/alert-center/stream")
def api_ac_stream():
    """SSE 实时推送新提醒事件（前端弹页内 toast + 浏览器系统通知）。"""

    def gen():
        q = jac.subscribe()
        try:
            # 握手帧：带当前未读数，前端连上即可刷新角标
            yield ("data: " + json.dumps({"type": "hello", "unread": jac.unread_count()},
                                         ensure_ascii=False) + "\n\n")
            while True:
                try:
                    item = q.get(timeout=15)
                    yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                except _queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            jac.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.on_event("startup")
def _start_alert_center_monitor():
    """随 dashboard 启动主动提醒中心后台巡检线程。"""
    try:
        jac.start_monitor()
        _log_emit("主动提醒中心后台监控已启动", level="info", source="alert-center")
    except Exception as e:  # noqa: BLE001
        _log_emit(f"提醒中心监控启动失败: {e}", level="error", source="alert-center")


def _twelve_auto_trader_loop():
    """12 系统信号矩阵自动模拟跟盘循环（daemon 线程）。

    每 twelve_auto_interval_min 分钟对 watchlist 跑一轮 run_twelve_cycle：
    共识方向明确即自动模拟开仓（记录信号归因），持仓按 SL/TP/到期/共识反转平仓。
    配置开关 twelve_auto_trade=false 时本轮跳过（循环保持存活，改配置无需重启）。
    """
    import jarvis_config as jc_mod
    time.sleep(60)   # 等服务与网络就绪，避免启动风暴
    while True:
        interval_min = 15.0
        try:
            if bool(jc_mod.get("twelve_auto_trade", True)):
                interval_min = float(jc_mod.get("twelve_auto_interval_min", 15) or 15)
                syms = list(jc_mod.get("watchlist") or ["BTCUSDT"])
                out = jpt.run_twelve_cycle(syms, _trader_cfg(), notify_on_action=True)
                if out.get("opened") or out.get("closed"):
                    _log_emit(
                        f"12系统自动跟盘：开仓 {len(out.get('opened') or [])} / "
                        f"平仓 {len(out.get('closed') or [])} / 持仓 {out.get('open_after')}",
                        level="info", source="twelve-trader")
        except Exception as e:  # noqa: BLE001 — 单轮失败不退出循环
            _log_emit(f"12系统自动跟盘异常（下轮重试）: {repr(e)[:200]}",
                      level="warn", source="twelve-trader")
        time.sleep(max(60.0, interval_min * 60.0))


@app.on_event("startup")
def _start_twelve_auto_trader():
    """随 dashboard 启动 12 系统自动模拟跟盘线程（开关由配置 twelve_auto_trade 控制）。"""
    try:
        _threading.Thread(target=_twelve_auto_trader_loop, daemon=True,
                          name="twelve-auto-trader").start()
        _log_emit("12系统信号自动跟盘线程已启动（twelve_auto_trade 可关）",
                  level="info", source="twelve-trader")
    except Exception as e:  # noqa: BLE001
        _log_emit(f"12系统自动跟盘启动失败: {e}", level="error", source="twelve-trader")


# ─────────────────────────── QD 网关配置 API ───────────────────────────
# 回测子进程 jarvis_scalper_backtest.py 每次运行时重读该文件，故无需重启 dashboard。
_QD_CONFIG_PATH = os.path.join(os.path.expanduser("~/.vibe-trading"), "scalper_backtest_config.json")
_QD_DEFAULT_GATEWAY = "http://localhost:8888"


def _mask_token(tok: str) -> str:
    """脱敏 Agent Token：保留前 4 后 4，中间固定 6 个圆点。"""
    if not tok:
        return ""
    if len(tok) <= 8:
        return "•" * len(tok)
    return f"{tok[:4]}{'•' * 6}{tok[-4:]}"


def _read_qd_config() -> dict:
    if os.path.exists(_QD_CONFIG_PATH):
        with open(_QD_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    return {}


@app.get("/api/qd-config")
def api_qd_config_get():
    """读取 QD 网关配置，token 仅返回脱敏值，绝不回传明文。"""
    try:
        cfg = _read_qd_config()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    token = str(cfg.get("agent_token", "") or "")
    env_token = os.getenv("QUANTDINGER_AGENT_TOKEN", "")
    env_base = os.getenv("QUANTDINGER_GATEWAY_BASE", "")
    return JSONResponse({
        "gateway_base": cfg.get("gateway_base", _QD_DEFAULT_GATEWAY),
        "agent_token_masked": _mask_token(token),
        "has_token": bool(token),
        # 环境变量优先级高于配置文件，若已设置则前端需提示用户文件值会被覆盖
        "env_token_active": bool(env_token),
        "env_base_active": bool(env_base),
    })


@app.put("/api/qd-config")
def api_qd_config_put(data: dict):
    """合并写入 QD 网关配置：token 留空表示保持原值（不清空）。"""
    try:
        try:
            cfg = _read_qd_config()
        except Exception:
            cfg = {}
        gateway_base = data.get("gateway_base")
        if isinstance(gateway_base, str) and gateway_base.strip():
            cfg["gateway_base"] = gateway_base.strip()
        token = data.get("agent_token")
        if isinstance(token, str) and token.strip():
            cfg["agent_token"] = token.strip()
        os.makedirs(os.path.dirname(_QD_CONFIG_PATH), exist_ok=True)
        with open(_QD_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


@app.post("/api/qd-config/test")
def api_qd_config_test():
    """连接测试：复用回测模块按生效配置（文件+env）探测网关健康与 token 有效性。"""
    if not _HAS_BACKTEST:
        return JSONResponse(
            {"ok": False, "reason": "jarvis_scalper_backtest 模块未安装"},
            status_code=503,
        )
    try:
        health = jbt.check_qd_health()
        token = jbt.check_token()
    except Exception as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)
    healthy = bool(health.get("healthy"))
    valid = bool(token.get("valid"))
    return JSONResponse({
        "ok": healthy and valid,
        "healthy": healthy,
        "token_valid": valid,
        "health_error": health.get("error"),
        "token_error": token.get("error"),
        "whoami": token.get("data") if valid else None,
    })


@app.post("/api/qd-config/issue-token")
def api_qd_config_issue_token(data: dict):
    """用 QD 账号密码登录并自动签发 agent token（默认 scope R,B、paper_only），写入配置文件。"""
    username = str(data.get("username") or "quantdinger").strip()
    password = str(data.get("password") or "").strip()
    scopes = str(data.get("scopes") or "R,B").strip()
    name = str(data.get("name") or "jarvis-backtest").strip()
    if not password:
        return JSONResponse({"ok": False, "reason": "缺少 QD 账号密码"}, status_code=400)
    try:
        cfg = _read_qd_config()
    except Exception:
        cfg = {}
    gateway = str(
        data.get("gateway_base") or cfg.get("gateway_base") or _QD_DEFAULT_GATEWAY
    ).strip().rstrip("/")
    try:
        import requests
    except Exception as e:
        return JSONResponse({"ok": False, "reason": f"requests 模块不可用: {e}"}, status_code=500)
    try:
        login = requests.post(
            f"{gateway}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        if login.status_code != 200:
            return JSONResponse(
                {"ok": False, "reason": f"登录失败 HTTP {login.status_code}: {login.text[:200]}"},
                status_code=502,
            )
        jwt = ((login.json() or {}).get("data") or {}).get("token")
        if not jwt:
            return JSONResponse({"ok": False, "reason": "登录响应中无 JWT"}, status_code=502)

        issued = requests.post(
            f"{gateway}/api/agent/v1/me/tokens",
            headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            json={
                "name": name,
                "scopes": scopes,
                "paper_only": True,
                "rate_limit_per_min": 120,
            },
            timeout=15,
        )
        if issued.status_code not in (200, 201):
            return JSONResponse(
                {"ok": False, "reason": f"签发失败 HTTP {issued.status_code}: {issued.text[:200]}"},
                status_code=502,
            )
        agent_token = ((issued.json() or {}).get("data") or {}).get("token")
        if not agent_token:
            return JSONResponse({"ok": False, "reason": "签发响应中无 token"}, status_code=502)

        cfg["gateway_base"] = gateway
        cfg["agent_token"] = agent_token
        os.makedirs(os.path.dirname(_QD_CONFIG_PATH), exist_ok=True)
        with open(_QD_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return JSONResponse({
            "ok": True,
            "agent_token_masked": _mask_token(agent_token),
            "scopes": scopes,
            "gateway_base": gateway,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


# ─────────────────────────── 大模型 (LLM) 配置 API ───────────────────────────
# 桌面端「设置 → 大模型」读写 ~/.vibe-trading/llm_config.json；
# evolve / reasoning / ask / AI 策略工坊统一经 jarvis_llm_config 读取生效配置。

try:
    import jarvis_llm_config as jlc
    _HAS_LLM_CONFIG = True
except ImportError:
    _HAS_LLM_CONFIG = False


@app.get("/api/llm-config")
def api_llm_config_get():
    """读取 LLM 配置：key 只回脱敏值，并标注当前生效来源（file/env/none）。"""
    if not _HAS_LLM_CONFIG:
        return JSONResponse({"error": "jarvis_llm_config 模块未安装"}, status_code=503)
    try:
        return JSONResponse(jlc.read_config_masked())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.put("/api/llm-config")
def api_llm_config_put(data: dict):
    """保存 LLM 配置（api_key 留空表示不修改；clear_key=True 清空回退环境变量）。"""
    if not _HAS_LLM_CONFIG:
        return JSONResponse({"ok": False, "reason": "jarvis_llm_config 模块未安装"}, status_code=503)
    try:
        cfg = jlc.save_config(data or {})
        return JSONResponse({"ok": True, "config": cfg})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


@app.post("/api/llm-config/test")
def api_llm_config_test():
    """真实调用一次模型验证连通性（按当前生效配置）。"""
    if not _HAS_LLM_CONFIG:
        return JSONResponse({"ok": False, "error": "jarvis_llm_config 模块未安装"}, status_code=503)
    try:
        return JSONResponse(jlc.test_connection())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/llm/usage")
def api_llm_usage(days: int = 30, recent: int = 20,
                  module: str | None = None, offset: int = 0):
    """LLM 用量/成本聚合：今日/本月/窗口合计 + 按日/模块/模型分布 + 最近明细。

    module/offset 只筛选与分页 recent 明细；明细不带大文本（has_content 标记），
    完整发送/返回内容走 /api/llm/usage/detail?id=。
    数据来自 llm_usage 表（jarvis_db：PG 或本地 SQLite）与 jsonl 降级记录合并。
    """
    try:
        import jarvis_llm_usage as jlu

        return JSONResponse({"ok": True, **jlu.query_usage(
            days=days, recent=recent, module=(module or None), offset=offset)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:200]})


@app.get("/api/llm/usage/detail")
def api_llm_usage_detail(id: int):  # noqa: A002 — 对齐前端参数名
    """按 id 取单条 LLM 调用完整日志（发送 messages JSON + 返回文本 + 元信息）。"""
    try:
        import jarvis_llm_usage as jlu

        row = jlu.get_detail(int(id))
        if not row:
            return JSONResponse({"ok": False, "error": "记录不存在或内容已过保留期"})
        return JSONResponse({"ok": True, "record": row})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(e)[:200]})


# ─────────────────────────── AI 策略工坊 API ───────────────────────────
# 自然语言 → LLM 生成规则 → 校验 → 拼装 QD 代码；结果可直接送 /api/backtest/run。
# LLM 生成耗时 10~60s，走后台线程 + 轮询状态（与 _BT_STATE 同款模式）。

try:
    import jarvis_strategy_gen as jsg
    _HAS_STRATEGY_GEN = True
except ImportError:
    _HAS_STRATEGY_GEN = False


class StrategyGenReq(BaseModel):
    description: str = ""
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"


_SG_LOCK = _threading.Lock()
_SG_STATE: dict = {
    "running": False,
    "started_at": 0.0,
    "finished_at": 0.0,
    "params": None,
    "result": None,
    "error": None,
}


def _strategy_gen_worker(description: str, symbol: str, timeframe: str) -> None:
    _log_emit(f"▶ AI 策略生成开始: {description[:60]}", "info", "strategy-gen")
    t0 = time.time()
    try:
        res = jsg.generate_from_description(description, symbol=symbol, timeframe=timeframe)
        with _SG_LOCK:
            _SG_STATE["result"] = res
            _SG_STATE["error"] = None if res.get("ok") else res.get("error", "生成失败")
        if res.get("ok"):
            _log_emit(
                f"✓ AI 策略生成完成: {res.get('name')} "
                f"因子={[f['id'] for f in res.get('summary', {}).get('factors', [])]} "
                f"({time.time() - t0:.1f}s)",
                "info",
                "strategy-gen",
            )
        else:
            _log_emit(f"✗ AI 策略生成失败: {res.get('error')}", "error", "strategy-gen")
    except Exception as e:  # noqa: BLE001
        with _SG_LOCK:
            _SG_STATE["error"] = str(e)
            _SG_STATE["result"] = None
        _log_emit(f"✗ AI 策略生成异常: {e}", "error", "strategy-gen")
    finally:
        with _SG_LOCK:
            _SG_STATE["running"] = False
            _SG_STATE["finished_at"] = time.time()


@app.post("/api/strategy/generate")
def api_strategy_generate(req: StrategyGenReq):
    """启动一次「自然语言 → 可回测策略」生成（后台线程，轮询 result 取结果）。"""
    if not _HAS_STRATEGY_GEN:
        return JSONResponse({"ok": False, "error": "jarvis_strategy_gen 模块未安装"}, status_code=503)
    description = (req.description or "").strip()
    if not description:
        return JSONResponse({"ok": False, "error": "请先描述你的策略想法"}, status_code=400)
    if _HAS_LLM_CONFIG and not jlc.get_llm_config():
        return JSONResponse(
            {"ok": False, "error": "未配置大模型，请先到「设置 → 大模型 (LLM)」填入 API Key"},
            status_code=400,
        )
    with _SG_LOCK:
        if _SG_STATE["running"]:
            return JSONResponse({"ok": False, "error": "已有生成任务在运行中"})
        _SG_STATE.update({
            "running": True,
            "started_at": time.time(),
            "finished_at": 0.0,
            "params": {
                "description": description,
                "symbol": req.symbol,
                "timeframe": req.timeframe,
            },
            "result": None,
            "error": None,
        })
    _threading.Thread(
        target=_strategy_gen_worker,
        args=(description, req.symbol, req.timeframe),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True})


@app.get("/api/strategy/generate/result")
def api_strategy_generate_result():
    with _SG_LOCK:
        st = dict(_SG_STATE)
    elapsed = 0.0
    if st["started_at"]:
        endt = st["finished_at"] or time.time()
        elapsed = max(0.0, endt - st["started_at"])
    st["elapsed_seconds"] = round(elapsed, 1)
    return JSONResponse(st)


@app.post("/api/strategy/save-to-hall")
def api_strategy_save_to_hall(data: dict):
    """把 AI 工坊生成并回测满意的策略存入名人堂，供实盘引擎与回测页复用。"""
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"ok": False, "error": "jarvis_scalper_evolve 模块未安装"}, status_code=503)
    name = str(data.get("name", "") or "").strip()
    code = str(data.get("code", "") or "")
    rule = data.get("rule") or {}
    result = data.get("result") or {}
    if not name or not code:
        return JSONResponse({"ok": False, "error": "缺少策略名或代码"}, status_code=400)
    try:
        existing = [e.get("name") for e in jse.load_hall_of_fame()]
        if name in existing:
            return JSONResponse({"ok": False, "error": f"名人堂已存在同名策略: {name}"})
        jse.add_to_hall_of_fame({
            "name": name,
            "rule": rule,
            "result": result,
            "code": code,
            "reasoning": str(data.get("reasoning", "") or ""),
            "source": "ai_workshop",
        })
        return JSONResponse({"ok": True, "name": name})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────── 策略自动进化 API（路由定义在独立模块） ───────────────────────────
try:
    import jarvis_strategy_evolve_llm as _jsel
    app.include_router(_jsel.router)
except ImportError:
    pass


# ─────────────────────────── 市场概览 API ───────────────────────────

@app.get("/api/market/overview")
def api_market_overview():
    try:
        snap = _cached("snap:BTCUSDT", 300, lambda: jb.build("BTCUSDT"))
        rd = snap.get("real_data", {}) or {}
        fac = snap.get("factor_state", {}) or {}
        return JSONResponse({
            "btc_price": fac.get("price"),
            "fear_greed": rd.get("fear_greed", {}),
            "funding": rd.get("funding", {}),
            "market_structure": rd.get("market_structure", {}),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/intraday/stats")
def api_intraday_stats():
    """4h 引擎看板：前向命中率 + 持仓 + 最新预测 + 熔断状态。"""
    try:
        import jarvis_intraday_trader as jit
        return JSONResponse(jit.stats())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": repr(e)[:300]}, status_code=500)


@app.get("/api/intraday/hits")
def api_intraday_hits(symbol: str = "BTCUSDT", limit: int = 120):
    """4h 引擎逐 bar 预测命中历史（驾驶舱多周期卡展开的命中曲线数据源）。

    返回按时间升序的 [{bar_ts, direction, prob, hit, outcome_ret}]，
    并附滚动 20 次命中率序列 rolling（hit 未回填的条目不参与分母）。
    """
    sym = symbol.upper()
    limit = max(10, min(int(limit), 500))
    try:
        import jarvis_intraday_trader as jit
        jit.ensure_db()
        with jit._conn() as conn:  # noqa: SLF001
            rows = [dict(r) for r in conn.execute(
                "SELECT bar_ts, direction, prob, tradeable, hit, outcome_ret "
                "FROM intraday_predictions WHERE symbol=? "
                "ORDER BY bar_ts DESC LIMIT ?", (sym, limit)).fetchall()]
        rows.reverse()
        window: list[int] = []
        rolling: list[dict] = []
        for r in rows:
            if r.get("hit") is not None:
                window.append(int(r["hit"]))
                if len(window) > 20:
                    window.pop(0)
                rolling.append({
                    "bar_ts": r["bar_ts"],
                    "rate": round(sum(window) / len(window), 4),
                    "n": len(window),
                })
        evaluated = [r for r in rows if r.get("hit") is not None]
        n_eval = len(evaluated)
        n_hit = sum(1 for r in evaluated if r["hit"] == 1)
        return JSONResponse({
            "symbol": sym,
            "predictions": rows,
            "rolling": rolling,
            "n_evaluated": n_eval,
            "hit_rate": round(n_hit / n_eval, 4) if n_eval else None,
        })
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": repr(e)[:300]}, status_code=500)


# ── 中长线多周期预测（15/30 天）：后台线程计算 + 内存/磁盘双缓存 ────────────
# 单次计算含 walk-forward + 置换检验，量级分钟；同步阻塞会拖死接口，故：
# 命中缓存直接返回；未命中返回 computing 并后台起线程算，前端轮询到齐。
_HORIZON_TTL = 6 * 3600
_HORIZON_FAIL_TTL = 600      # 失败结果（数据源不可达等）只缓存 10 分钟，便于网络恢复后重算
_HORIZON_CACHE_PATH = os.path.expanduser("~/.vibe-trading/horizon_cache.json")
_HORIZON_LOCK = _threading.Lock()
_HORIZON_JOBS: set[str] = set()


def _horizon_disk_load() -> dict:
    try:
        with open(_HORIZON_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _horizon_disk_save(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_HORIZON_CACHE_PATH), exist_ok=True)
        with open(_HORIZON_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _horizon_compute(sym: str, h: int) -> None:
    key = f"{sym}:{h}"
    try:
        import jarvis_horizon as jh
        r = jh.predict_horizon(sym, h)
        with _HORIZON_LOCK:
            cache = _horizon_disk_load()
            cache[key] = {"ts": time.time(), "data": r}
            _horizon_disk_save(cache)
    finally:
        with _HORIZON_LOCK:
            _HORIZON_JOBS.discard(key)


@app.get("/api/horizons")
def api_horizons(symbol: str = "BTCUSDT"):
    """15/30 天预测点位：{horizons:{"15":{...}|null,"30":...}, computing:[...]}。"""
    import jarvis_horizon as jh
    sym = symbol.upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    out: dict = {"symbol": sym, "horizons": {}, "computing": []}
    now = time.time()
    with _HORIZON_LOCK:
        cache = _horizon_disk_load()
        for h in jh.HORIZONS:
            key = f"{sym}:{h}"
            hit = cache.get(key)
            # direction 为空视为失败结果（数据不足/异常），用短 TTL 尽快重试
            ttl = (_HORIZON_TTL if hit and hit.get("data", {}).get("direction")
                   else _HORIZON_FAIL_TTL)
            if hit and now - hit.get("ts", 0) < ttl:
                out["horizons"][str(h)] = hit["data"]
                continue
            out["horizons"][str(h)] = hit["data"] if hit else None  # 过期先给旧值
            if key not in _HORIZON_JOBS:
                _HORIZON_JOBS.add(key)
                _threading.Thread(target=_horizon_compute, args=(sym, h),
                                  daemon=True).start()
            out["computing"].append(str(h))
    return JSONResponse(out)


def _why_local_summary(sym: str, ip: dict | None, hz: dict, dec: dict) -> str:
    """无 LLM 时的本地多维总结：把三周期归因 + 30天决策理由拼成通顺中文。"""
    parts = []
    if ip and ip.get("direction"):
        seg = f"4小时看{ip['direction']}"
        if ip.get("prob") is not None:
            seg += f"（{round(ip['prob'] * 100)}%）"
        if ip.get("why_text"):
            seg += f"：{ip['why_text']}"
        if not ip.get("tradeable"):
            seg += "。未过精准度门禁，仅观察不下单"
        parts.append(seg)
    for h in ("15", "30"):
        p = hz.get(h)
        if p and p.get("direction"):
            seg = f"{h}天看{p['direction']}"
            if p.get("prob") is not None:
                seg += f"（{round(p['prob'] * 100)}%）"
            if p.get("target"):
                seg += f"，目标 {p['target']}"
            if p.get("why_text"):
                seg += f"：{p['why_text']}"
            parts.append(seg)
    reasons = (dec or {}).get("reasons") or []
    if reasons:
        parts.append("长线因子面：" + "；".join(str(r) for r in reasons[:3]))
    if not parts:
        return "暂无足够数据生成分析（预测计算中或数据源不可达）。"
    return "。\n".join(parts) + "。\n（模拟盘研究，不构成投资建议）"


@app.get("/api/why")
def api_why(symbol: str = "BTCUSDT"):
    """多周期「为什么」聚合：4h/15天/30天归因 + 30天决策理由 → AI/本地总结。

    整体结果缓存 5 分钟（含 LLM 调用），防止前端轮询烧 token。
    """
    sym = symbol.upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    return JSONResponse(_cached(f"why:{sym}", 300, lambda: _why_compute(sym)))


def _why_compute(sym: str) -> dict:
    ip = None
    try:
        import jarvis_intraday_trader as jit
        for p in jit.stats().get("recent_predictions", []):
            if p.get("symbol") == sym:
                ip = p
                break
    except Exception:  # noqa: BLE001
        pass
    hz = {}
    try:
        cache = _horizon_disk_load()
        for h in ("15", "30"):
            hit = cache.get(f"{sym}:{h}")
            if hit:
                hz[h] = hit.get("data") or {}
    except Exception:  # noqa: BLE001
        pass
    dec = {}
    try:
        snap = _cached(f"snap:{sym}", 300, lambda: jb.build(sym))
        dec = (snap or {}).get("decision", {}) or {}
    except Exception:  # noqa: BLE001
        pass
    local = _why_local_summary(sym, ip, hz, dec)

    ans = None
    cfg = _llm_config()
    if cfg:
        context = {
            "symbol": sym,
            "pred_4h": {k: ip.get(k) for k in ("direction", "prob", "tradeable",
                                               "why_text", "reason", "entry",
                                               "stop", "take")} if ip else None,
            "pred_15d": hz.get("15"),
            "pred_30d": hz.get("30"),
            "decision_30d_reasons": (dec or {}).get("reasons", [])[:5],
        }
        system = (
            "你是『贾维斯』加密交易助手。基于给定的多周期量化预测（4小时/15天/30天，"
            "每条含方向、概率、目标点位、特征归因 why_text、精准度门禁状态），"
            "写一段 150 字以内的多维度行情解读：① 三个周期的结论是否一致；"
            "② 关键驱动因素（引用归因里的具体数字）；③ 门禁未过的周期明确提示仅供参考。"
            "不得编造数据，缺失就说缺失。简体中文，最后一句注明模拟盘研究不构成投资建议。"
        )
        payload = json.dumps({
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            "temperature": 0.4,
            "max_tokens": 400,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            cfg["base"] + "/chat/completions", data=payload, method="POST",
            headers={"Authorization": f"Bearer {cfg['key']}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            ans = (body.get("choices") or [{}])[0].get("message", {}) \
                .get("content", "").strip() or None
        except Exception:  # noqa: BLE001
            ans = None
    return {"symbol": sym, "summary": ans or local,
            "engine": "llm" if ans else "local",
            "pred_4h": ip, "horizons": hz}


@app.get("/api/watchlist")
def api_watchlist_get():
    import jarvis_config as jc_mod
    return JSONResponse({"symbols": jc_mod.get("watchlist")})


@app.post("/api/watchlist")
def api_watchlist_post(data: dict):
    """增删自选币：{"action":"add"|"remove","symbol":"PEPEUSDT"}。add 先校验交易对存在。"""
    import jarvis_config as jc_mod
    action = str((data or {}).get("action", "")).lower()
    sym = str((data or {}).get("symbol", "")).upper().replace("-", "").replace("/", "").strip()
    if action not in ("add", "remove") or not sym:
        return JSONResponse({"ok": False, "error": "参数错误：action 须为 add/remove，symbol 必填"},
                            status_code=400)
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    wl = list(jc_mod.get("watchlist") or [])
    if action == "add":
        if sym in wl:
            return JSONResponse({"ok": True, "symbols": wl, "note": "已在自选中"})
        if len(wl) >= 15:
            return JSONResponse({"ok": False, "error": "自选上限 15 个，请先删除再添加"},
                                status_code=400)
        info = jcd._get(jcd.SPOT_API + "/api/v3/exchangeInfo", {"symbol": sym})
        ok = isinstance(info, dict) and any(
            s.get("symbol") == sym and s.get("status") == "TRADING"
            for s in (info.get("symbols") or []))
        if not ok:
            return JSONResponse({"ok": False, "error": f"币安不存在可交易的 {sym}，请检查代码"},
                                status_code=400)
        wl.append(sym)
    else:
        if sym not in wl:
            return JSONResponse({"ok": False, "error": f"{sym} 不在自选中"}, status_code=400)
        if len(wl) <= 1:
            return JSONResponse({"ok": False, "error": "至少保留 1 个币种"}, status_code=400)
        wl.remove(sym)
    jc_mod.save({"watchlist": wl}, source="cockpit", note=f"{action} {sym}")
    return JSONResponse({"ok": True, "symbols": wl})


# ─────────────────── 十二套技术信号引擎 & 贾维斯自主推理 API ───────────────────

# 多周期共识聚合周期集（5m/30m 为短线增强档，权重见 jts.TF_WEIGHTS，低于长周期）
_TWELVE_TFS = ("5m", "15m", "30m", "1h", "4h")


def _twelve_basis(sym: str) -> dict | None:
    """套利系统双腿数据：spot-perp 基差序列统计（5min 缓存）。

    失败/样本不足返回 None → signal_arbitrage 按「数据不足」降级中性（不硬造）。
    按需拉取 + 进程内缓存，不新增定时任务（避免与 com.jarvis.daemon 重复轮询）。
    """
    def _calc():
        try:
            import jarvis_crypto_data as jcd
            return jcd.fetch_basis_series(sym) or None
        except Exception:  # noqa: BLE001 — 基差取数失败不拖垮信号主体
            return None
    return _cached(f"twelve:basis:{sym}", 300, _calc)


@app.get("/api/twelve/signals")
def api_twelve_signals(symbol: str = "BTCUSDT", tf: str = "4h"):
    """十二套技术单时间框架信号 + 分层共识（海龟/道氏/缠论…12 套逐一给方向与强度）。"""
    import jarvis_twelve_systems as jts
    sym = symbol.upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    iv = tf if tf in {"5m", "15m", "30m", "1h", "4h", "1d"} else "4h"

    def _calc():
        df = jts.fetch_klines_df(sym, iv, 300)
        if df is None or len(df) < 30:
            return {"ok": False, "error": "K线数据不足或拉取失败", "symbol": sym,
                    "tf": iv, "signals": [], "consensus": None}
        out = jts.analyze(df, basis_data=_twelve_basis(sym))
        return {"ok": True, "symbol": sym, "tf": iv,
                "price": round(float(df["close"].iloc[-1]), 6),
                "signals": out["signals"], "consensus": out["consensus"]}

    # 5m 一根 K 线 5 分钟，缓存同比缩短（15m+ 沿用 120s）
    return JSONResponse(_cached(f"twelve:sig:{sym}:{iv}", 60 if iv == "5m" else 120, _calc))


@app.get("/api/twelve/consensus")
def api_twelve_consensus(symbol: str = "BTCUSDT"):
    """十二套技术多时间框架（5m/15m/30m/1h/4h）加权共识：看涨/看跌总裁决。"""
    import jarvis_twelve_systems as jts
    sym = symbol.upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"

    def _calc():
        tf_cons: dict = {}
        price = None
        basis = _twelve_basis(sym)   # 基差与 TF 无关，整轮共用一份
        for tf in _TWELVE_TFS:
            df = jts.fetch_klines_df(sym, tf, 300)
            if df is None or len(df) < 30:
                continue
            out = jts.analyze(df, basis_data=basis)
            tf_cons[tf] = out["consensus"]
            if tf == "4h" or price is None:
                price = round(float(df["close"].iloc[-1]), 6)
        merged = jts.consensus_multi_tf(tf_cons)
        # 供需情绪层（多空比/资金费率/OI/恐贪）：同向增益置信、极端反向降级+警示。
        # 只附加 sentiment 键与 reasoning 尾注，不改共识原字段；失败不影响共识主体。
        try:
            import jarvis_sentiment as jsent
            senti = jsent.assess(sym)
            if senti.get("ok"):
                merged = jsent.apply_to_consensus(merged, senti)
        except Exception:  # noqa: BLE001
            pass
        # 「安全带」确认层（Delta 吸收背离 × 信号方向）：confirmed 小幅加成 /
        # conflict 降级警示；Delta 引擎（jarvis_delta_flow，MCP-5 并行开发）
        # 未就绪时 status=unavailable，不影响共识主体。强背离顺带落页内提醒。
        try:
            import jarvis_seatbelt as jsb
            delta_payload = _delta_payload(sym)
            merged = jsb.apply_to_consensus(merged, delta_payload)
            jsb.maybe_alert_strong_divergence(sym, delta_payload)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": bool(tf_cons), "symbol": sym, "price": price,
                "tf_available": sorted(tf_cons.keys()), "consensus": merged}

    return JSONResponse(_cached(f"twelve:cons:{sym}", 180, _calc))


def _delta_payload(sym: str, tf: str = "15m") -> dict | None:
    """取 Delta 引擎数据供安全带层消费（120s 缓存）。

    引擎模块（jarvis_delta_flow，MCP-5）未就绪 / 入口不匹配 / 取数失败一律
    返回 None → seatbelt 层显示 unavailable，绝不拖垮共识主体。入口按契约
    习惯依次探测 get_delta / assess / analyze。
    """
    def _calc():
        try:
            import jarvis_delta_flow as jdf
        except Exception:  # noqa: BLE001 — 引擎未就绪
            return None
        for entry in ("get_delta", "assess", "analyze"):
            fn = getattr(jdf, entry, None)
            if not callable(fn):
                continue
            try:
                out = fn(sym, tf, 200)
                if isinstance(out, dict):
                    return out
            except TypeError:
                try:
                    out = fn(symbol=sym, timeframe=tf)
                    if isinstance(out, dict):
                        return out
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001
                continue
        return None

    return _cached(f"delta:seatbelt:{sym}:{tf}", 120, _calc)


# ─────────────────── 走势预测引擎（规则概率 + AI 研判双轨）───────────────────

@app.get("/api/predict")
def api_predict(symbol: str = "BTCUSDT", timeframe: str = "15m",
                horizon: int = 16, mock: int = 0, llm: int = 1):
    """走势预测：方向概率 / 目标区间 / 期望路径 / AI 研判（jarvis_trend_predict）。

    mock=1 返回确定性联调假数据（mock:true，不联网）；
    llm=0 跳过 AI 轨（纯规则，无外部依赖）。未配 LLM Key 时自动降级规则轨。
    """
    import jarvis_trend_predict as jtp
    if int(mock):
        return JSONResponse(jtp.mock_predict(symbol, timeframe, horizon))
    use_llm = bool(int(llm))

    def _calc():
        try:
            return jtp.predict(symbol, timeframe, horizon, use_llm=use_llm)
        except Exception as exc:  # noqa: BLE001 — 预测失败不 500，前端按未就绪降级
            return {"ok": False, "error": repr(exc)[:200],
                    "symbol": symbol, "timeframe": timeframe,
                    "disclaimer": jtp.DISCLAIMER}

    key = f"predict:{jtp._norm_symbol(symbol)}:{jtp._norm_tf(timeframe)}:" \
          f"{jtp._norm_horizon(horizon)}:{int(use_llm)}"
    return JSONResponse(_cached(key, 120, _calc))


@app.get("/api/predict/backtest")
def api_predict_backtest(symbol: str = "BTCUSDT", timeframe: str = "15m",
                         horizon: int = 16, windows: int = 40):
    """预测准确率回测：逐历史窗口预测 vs 实际，输出方向命中率/区间覆盖率。

    只回测规则轨（可复现）；windows 夹在 5~120。结果缓存 30 分钟。
    """
    import jarvis_trend_predict as jtp

    def _calc():
        try:
            return jtp.backtest(symbol, timeframe, horizon, windows)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": repr(exc)[:200],
                    "symbol": symbol, "timeframe": timeframe}

    key = f"predict:bt:{jtp._norm_symbol(symbol)}:{jtp._norm_tf(timeframe)}:" \
          f"{jtp._norm_horizon(horizon)}:{max(5, min(120, int(windows)))}"
    return JSONResponse(_cached(key, 1800, _calc))


# ─────────────────── Delta/CVD 订单流（吸收背离安全带数据层）───────────────────

@app.get("/api/delta")
def api_delta(symbol: str = "BTCUSDT", timeframe: str = "15m",
              limit: int = 200, mock: int = 0):
    """Delta/CVD 序列 + 吸收背离检测（jarvis_delta_flow；MCP 安全带层消费）。

    mock=1 返回确定性联调假数据（含看涨吸收形态，mock:true，不联网）。
    """
    import jarvis_delta_flow as jdf
    if int(mock):
        return JSONResponse(jdf.mock_analyze(symbol, timeframe, limit))

    def _calc():
        try:
            return jdf.analyze(symbol, timeframe, limit)
        except Exception as exc:  # noqa: BLE001 — 检测失败不 500，前端按未就绪降级
            return {"ok": False, "error": repr(exc)[:200],
                    "symbol": symbol, "timeframe": timeframe,
                    "disclaimer": jdf.DISCLAIMER}

    key = f"delta:{jdf._norm_symbol(symbol)}:{jdf._norm_tf(timeframe)}:{jdf._norm_limit(limit)}"
    return JSONResponse(_cached(key, 60, _calc))


@app.get("/api/delta/backtest")
def api_delta_backtest(symbol: str = "BTCUSDT", timeframe: str = "15m",
                       max_bars: int = 1000):
    """吸收背离历史胜率回测（16/32 根双口径 + 随机基线；缓存 30 分钟）。"""
    import jarvis_delta_flow as jdf

    def _calc():
        try:
            return jdf.backtest(symbol, timeframe, max_bars=max_bars)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": repr(exc)[:200],
                    "symbol": symbol, "timeframe": timeframe}

    key = f"delta:bt:{jdf._norm_symbol(symbol)}:{jdf._norm_tf(timeframe)}:{max(200, min(int(max_bars), 1000))}"
    return JSONResponse(_cached(key, 1800, _calc))


# ─────────────────── 资金费率套利模拟盘（阿尔法策略）───────────────────

@app.get("/api/funding-arb/opportunities")
def api_funding_arb_opportunities(force: int = 0):
    """套利机会列表：watchlist 币种按当期费率年化降序（模块内 TTL 缓存 120s）。"""
    import jarvis_funding_arb as jfa
    try:
        return JSONResponse(jfa.fetch_opportunities(force=bool(int(force))))
    except Exception as exc:  # noqa: BLE001 — 机会列表失败不 500，前端按未就绪降级
        return JSONResponse({"ok": False, "error": repr(exc)[:200],
                             "opportunities": [], "disclaimer": jfa.DISCLAIMER})


@app.get("/api/funding-arb/positions")
def api_funding_arb_positions(status: str = "all"):
    """套利持仓列表（两腿详情 + 累计费率收益 + 年化；查询前自动补结欠账费率期）。"""
    import jarvis_funding_arb as jfa
    try:
        return JSONResponse(jfa.list_positions(status))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:200], "positions": []})


@app.post("/api/funding-arb/positions/open")
def api_funding_arb_open(symbol: str, capital: float, note: str = ""):
    """模拟建仓：现货多 + 永续 1x 空 两腿等量（delta 中性）。"""
    import jarvis_funding_arb as jfa
    try:
        return JSONResponse(jfa.open_position(symbol, capital, note=note))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:200]})


@app.post("/api/funding-arb/positions/close")
def api_funding_arb_close(position_id: int):
    """平仓：补结费率期后按当前两腿价格结算基差损益与总收益。"""
    import jarvis_funding_arb as jfa
    try:
        return JSONResponse(jfa.close_position(position_id))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:200]})


@app.get("/api/funding-arb/pnl")
def api_funding_arb_pnl():
    """套利收益总览：open/closed 分组汇总 + 全局累计费率收益与年化。"""
    import jarvis_funding_arb as jfa
    try:
        return JSONResponse(jfa.get_pnl())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:200]})


# ─────────────────── 市场情报页（免 Key 真实数据源）───────────────────

@app.get("/api/market-intel")
def api_market_intel():
    """情报页聚合：资金费率 / OI / 多空比 / 恐慌贪婪（真实数据，TTL 缓存 + 降级）。

    模块内自带各源独立 TTL 缓存与后台刷新，接口本身恒快速返回；
    未接入源（爆仓/链上，需第三方 key）在 unavailable 中说明。
    """
    import jarvis_market_intel as jmi
    try:
        return JSONResponse(jmi.get_intel())
    except Exception as exc:  # noqa: BLE001 — 情报页失败不应 500，前端按未接入处理
        return JSONResponse({"ok": False, "error": repr(exc)[:200]})


@app.get("/api/sentiment")
def api_sentiment(symbol: str = "BTCUSDT"):
    """供需情绪综合研判：多空比/资金费率/OI/恐贪四因子 + 综合分（-100~+100）。

    数据复用 market-intel 各源 TTL 缓存；爆仓/链上为预留因子位（key 配置后
    自动参与计分）。模块内已捕获异常（ok=False），不会 500。
    """
    import jarvis_sentiment as jsent
    return JSONResponse(_cached(f"sentiment:{symbol.upper()}", 60,
                                lambda: jsent.assess(symbol)))


@app.get("/api/regime")
def api_regime(symbol: str = "BTCUSDT"):
    """牛熊市体制识别：200D MA / 周线结构 / 长周期动量 / 情绪面多因子融合。

    输出 regime: bull|bear|range + score(-100~100) + confidence + 逐因子解释。
    大周期数据变化慢，缓存 15 分钟；模块内已捕获异常（ok=False），不会 500。
    """
    import jarvis_bull_bear as jbb
    return JSONResponse(_cached(f"regime:{symbol.upper()}", 900,
                                lambda: jbb.assess(symbol)))


@app.get("/api/volume-profile")
def api_volume_profile(symbol: str = "BTCUSDT", timeframe: str = "15m",
                       limit: int = 300, mock: int = 0):
    """Volume Profile 分布结构（反转四条件之 2+3）：分段 VP + 正态形态识别 +
    三连分布确认 + 回补目标位。?mock=1 返回确定性假数据；缓存 60s。
    """
    import jarvis_volume_profile as jvp
    if mock:
        return JSONResponse(jvp.mock_response(symbol, timeframe))
    key = f"vp:{symbol.upper()}:{timeframe}:{int(limit)}"
    return JSONResponse(_cached(key, 60,
                                lambda: jvp.assess(symbol, timeframe, limit)))


@app.get("/api/stop-hunt")
def api_stop_hunt(symbol: str = "BTCUSDT", timeframe: str = "15m",
                  limit: int = 120, mock: int = 0):
    """止损扫单检测（反转四条件之 4）：长影线刺破前低/前高快速收回 + 量能尖峰。

    ?mock=1 返回确定性假数据（看涨扫单形态）；缓存 60s。
    """
    import jarvis_stop_hunt as jsh
    if int(mock):
        return JSONResponse({"ok": True, "symbol": symbol.upper(),
                             "timeframe": timeframe, "mock": True,
                             **jsh.mock_result("bullish")})

    def _calc():
        try:
            return {"ok": True, "symbol": symbol.upper(), "timeframe": timeframe,
                    "mock": False, **jsh.detect(symbol, timeframe, limit)}
        except Exception as exc:  # noqa: BLE001 — 检测失败不 500，前端按未就绪降级
            return {"ok": False, "error": repr(exc)[:200],
                    "symbol": symbol.upper(), "timeframe": timeframe}

    return JSONResponse(_cached(f"stophunt:{symbol.upper()}:{timeframe}:{int(limit)}", 60, _calc))


@app.get("/api/reversal-score")
def api_reversal_score(symbol: str = "BTCUSDT", timeframe: str = "15m", mock: int = 0):
    """高胜率反转四条件叠加评分（MCP-6 反转面板消费）：

    聚合 Delta 背离（/api/delta）+ 多分布/三连确认（/api/volume-profile）+
    末端止损扫单（jarvis_stop_hunt），按方向投票叠加计分，4/4 = 高概率入场点。
    任一上游未就绪 → 对应条件标 unavailable 不计入 met，降级不报错；
    ?mock=1 全链 mock（三源均用确定性假数据）。缓存 60s。
    """
    import jarvis_stop_hunt as jsh

    sym = symbol.upper()
    if int(mock):
        import jarvis_delta_flow as jdf
        import jarvis_volume_profile as jvp
        out = jsh.aggregate_reversal_score(
            jdf.mock_analyze(sym, timeframe),
            jvp.mock_response(sym, timeframe),
            jsh.mock_result("bullish"),
        )
        return JSONResponse({**out, "symbol": sym, "timeframe": timeframe, "mock": True})

    def _calc():
        # 三个上游各自独立降级：失败置 None → 聚合层标 unavailable
        delta_resp = None
        try:
            import jarvis_delta_flow as jdf
            delta_resp = jdf.analyze(sym, timeframe)
        except Exception:  # noqa: BLE001
            delta_resp = None
        vp_resp = None
        try:
            import jarvis_volume_profile as jvp
            vp_resp = jvp.assess(sym, timeframe)
        except Exception:  # noqa: BLE001
            vp_resp = None
        hunt_resp = None
        try:
            hunt_resp = jsh.detect(sym, timeframe)
        except Exception:  # noqa: BLE001
            hunt_resp = None
        out = jsh.aggregate_reversal_score(delta_resp, vp_resp, hunt_resp)
        return {**out, "symbol": sym, "timeframe": timeframe, "mock": False}

    return JSONResponse(_cached(f"reversal:{sym}:{timeframe}", 60, _calc))


# ─────────────────── 合约仓位与风控计算器（Task #3）───────────────────

_POSCALC_KEYS = ("poscalc_capital_usdt", "poscalc_leverage", "poscalc_risk_pct",
                 "poscalc_margin_pct")


@app.get("/api/position-calc/config")
def api_poscalc_config_get():
    """仓位计算器旋钮：本金 / 杠杆 / 风险% / 保证金%（jarvis_config 持久化）。"""
    import jarvis_config as jc_mod
    return JSONResponse({k: jc_mod.get(k) for k in _POSCALC_KEYS})


@app.put("/api/position-calc/config")
def api_poscalc_config_put(data: dict):
    import jarvis_config as jc_mod
    patch = {k: v for k, v in (data or {}).items() if k in _POSCALC_KEYS}
    if not patch:
        return JSONResponse({"ok": False, "reason": "无有效字段"}, status_code=400)
    jc_mod.save(patch, source="desktop-poscalc", note="position-calc UI")
    return JSONResponse({"ok": True, "config": {k: jc_mod.get(k) for k in _POSCALC_KEYS}})


@app.get("/api/position-calc")
def api_position_calc(symbol: str = "BTCUSDT", tf: str = "auto",
                      capital: float | None = None, leverage: float | None = None,
                      risk_pct: float | None = None,
                      margin_pct: float | None = None,
                      entry: float | None = None):
    """基于当前信号共识 trade_plan 的完整下单建议（仓位/止损安全边距/分档止盈/爆仓价）。

    tf=auto 用多时间框架综合共识；否则用指定单周期共识。
    capital/leverage/risk_pct/margin_pct 可临时覆盖配置值（预览用，不落盘）。
    margin_pct 生效时用保证金法口径（名义=本金×保证金%×杠杆），忽略 risk_pct。
    entry 为用户手动入场价：止损/止盈/爆仓价随之平移重算。
    """
    import jarvis_config as jc_mod
    import jarvis_position_calc as jpc
    import jarvis_twelve_systems as jts

    sym = symbol.upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    iv = tf if tf in {"auto", "5m", "15m", "30m", "1h", "4h", "1d"} else "auto"
    cap = capital if capital is not None else jc_mod.get("poscalc_capital_usdt")
    lev = leverage if leverage is not None else jc_mod.get("poscalc_leverage")
    rp = risk_pct if risk_pct is not None else jc_mod.get("poscalc_risk_pct")
    mp = margin_pct if margin_pct is not None else jc_mod.get("poscalc_margin_pct")

    def _plan_and_price() -> tuple[dict | None, float | None, str]:
        """取共识 trade_plan（复用 twelve 端点的缓存键，避免重复算信号）。"""
        if iv == "auto":
            def _calc_cons():
                tf_cons: dict = {}
                price = None
                basis = _twelve_basis(sym)
                for t in _TWELVE_TFS:
                    df = jts.fetch_klines_df(sym, t, 300)
                    if df is None or len(df) < 30:
                        continue
                    out = jts.analyze(df, basis_data=basis)
                    tf_cons[t] = out["consensus"]
                    if t == "4h" or price is None:
                        price = round(float(df["close"].iloc[-1]), 6)
                merged = jts.consensus_multi_tf(tf_cons)
                return {"ok": bool(tf_cons), "symbol": sym, "price": price,
                        "tf_available": sorted(tf_cons.keys()), "consensus": merged}
            data = _cached(f"twelve:cons:{sym}", 180, _calc_cons)
            cons = (data or {}).get("consensus") or {}
            return cons.get("trade_plan"), (data or {}).get("price"), \
                str(cons.get("direction", "neutral"))

        def _calc_sig():
            df = jts.fetch_klines_df(sym, iv, 300)
            if df is None or len(df) < 30:
                return {"ok": False, "error": "K线数据不足或拉取失败", "symbol": sym,
                        "tf": iv, "signals": [], "consensus": None}
            out = jts.analyze(df, basis_data=_twelve_basis(sym))
            return {"ok": True, "symbol": sym, "tf": iv,
                    "price": round(float(df["close"].iloc[-1]), 6),
                    "signals": out["signals"], "consensus": out["consensus"]}
        data = _cached(f"twelve:sig:{sym}:{iv}", 120, _calc_sig)
        cons = (data or {}).get("consensus") or {}
        plan = cons.get("trade_plan")
        if plan and iv:
            plan = {**plan, "source_tf": plan.get("source_tf") or iv}
        return plan, (data or {}).get("price"), str(cons.get("direction", "neutral"))

    try:
        plan, price, direction = _plan_and_price()
        advice = jpc.advice_from_plan(plan, capital_usdt=cap, leverage=lev,
                                      risk_pct=rp, symbol=sym,
                                      margin_pct=mp, entry_override=entry)
        return JSONResponse({
            "ok": True, "symbol": sym, "tf": iv, "price": price,
            "direction": direction,
            "config": {"poscalc_capital_usdt": cap, "poscalc_leverage": lev,
                       "poscalc_risk_pct": rp, "poscalc_margin_pct": mp},
            "advice": advice,
        })
    except Exception as exc:  # noqa: BLE001 — 计算器故障不应 500 砸前端轮询
        return JSONResponse({"ok": False, "symbol": sym, "tf": iv,
                             "error": repr(exc)[:300]})


# 推理端点并发锁（按 symbol 粒度）：缓存未命中时防多请求并发双烧 LLM token
_REASON_LOCKS: "dict[str, _threading.Lock]" = {}
_REASON_LOCKS_GUARD = _threading.Lock()
_REASON_TTL = 300


@app.post("/api/jarvis/reason")
def api_jarvis_reason(data: dict):
    """贾维斯自主推理：十二套信号+共识+市场快照 → DeepSeek 链式推理（无 key 自动降级）。

    body: {"symbol": "BTCUSDT"}；命中缓存的响应带 cached:true。
    """
    import jarvis_reasoning as jre
    import jarvis_twelve_systems as jts
    sym = str((data or {}).get("symbol", "BTCUSDT")).upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    key = f"twelve:reason:{sym}"

    def _hit():
        h = _CACHE.get(key)
        if h and time.time() - h[0] < _REASON_TTL:
            return h[1]
        return None

    def _calc():
        df = jts.fetch_klines_df(sym, "4h", 300)
        if df is None or len(df) < 30:
            return {"ok": False, "error": "K线数据不足或拉取失败", "symbol": sym}
        out = jts.analyze(df, basis_data=_twelve_basis(sym))
        market = {"symbol": sym, "tf": "4h",
                  "price": round(float(df["close"].iloc[-1]), 6),
                  "atr": round(float(jts._atr(df).iloc[-1]), 6)}
        res = jre.reason(market, out["signals"], out["consensus"])
        return {"ok": True, "symbol": sym, "market": market,
                "consensus": out["consensus"], "reasoning": res}

    cached_val = _hit()
    if cached_val is not None:
        return JSONResponse({**cached_val, "cached": True})
    with _REASON_LOCKS_GUARD:
        lock = _REASON_LOCKS.setdefault(sym, _threading.Lock())
    with lock:
        cached_val = _hit()   # 双检：等锁期间可能已被并发请求算完
        if cached_val is not None:
            return JSONResponse({**cached_val, "cached": True})
        val = _calc()
        _CACHE[key] = (time.time(), val)
    return JSONResponse(val)


@app.get("/api/jarvis/insights")
def api_jarvis_insights(limit: int = 20, symbol: str | None = None):
    """贾维斯自主意识产出的最近洞察列表 [{ts,symbol,kind,title,detail,severity}]。"""
    import jarvis_reasoning as jre
    items = jre.list_insights(limit=limit, symbol=symbol)
    return JSONResponse({"ok": True, "insights": items, "total": len(items)})


@app.post("/api/twelve/cycle")
def api_twelve_cycle(symbols: str = "", dry_run: bool = False):
    """跑一轮 12 系统信号矩阵模拟跟盘：盯平仓 → 共识达标自动开仓（含信号归因）。

    symbols 缺省取 watchlist。
    """
    import jarvis_config as jc_mod
    syms = [s.strip() for s in symbols.split(",") if s.strip()] or \
        list(jc_mod.get("watchlist") or ["BTCUSDT"])
    try:
        out = jpt.run_twelve_cycle(syms, _trader_cfg(), dry_run=dry_run)
        return JSONResponse({"ok": True, "data": out})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:300]}, status_code=500)


@app.get("/api/twelve/signal-stats")
def api_twelve_signal_stats(symbol: str | None = None, direction: str | None = None,
                            tf: str | None = None, resonance: str | None = None,
                            regime: str | None = None, source: str = "realtime"):
    """12 系统信号胜率归因统计（基于已平仓的共识模拟单）。

    多维筛选（全部可选，无参调用兼容旧行为）：
      direction=long|short  tf=15m|1h|4h  resonance=1|2-3|4+
      regime=trending|ranging|breakout|unknown
      source=realtime|replay|all（默认 realtime=实时模拟单；replay=历史回放样本）
    非法取值按未传/默认处理，不报错。
    """
    def _norm(v, allowed):
        return v if v in allowed else None
    try:
        return JSONResponse({"ok": True, **jpt.signal_stats(
            symbol,
            direction=_norm(direction, ("long", "short")),
            tf=_norm(tf, ("5m", "15m", "30m", "1h", "4h")),
            resonance=_norm(resonance, ("1", "2-3", "4+")),
            regime=_norm(regime, ("trending", "ranging", "breakout", "unknown")),
            source=_norm(source, ("realtime", "replay", "all")) or "realtime",
        )})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:300]}, status_code=500)


@app.post("/api/twelve/replay")
def api_twelve_replay(data: dict | None = None):
    """启动 12 系统历史回放预积累（异步后台线程，进度查 /api/twelve/replay/status）。

    body（全部可选）：{"symbols": "BTC,ETH"|["BTC"], "tfs": "15m,1h"|["15m"],
    "days": 30, "stride": 1}；symbols 缺省取 watchlist，tfs 缺省 15m/1h/4h。
    已在回放中时返回 ok:false。
    """
    import jarvis_config as jc_mod
    import jarvis_signal_replay as jsr
    d = data or {}

    def _as_list(v, fallback):
        if isinstance(v, str):
            items = [x.strip() for x in v.split(",") if x.strip()]
            return items or fallback
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
            return items or fallback
        return fallback

    symbols = _as_list(d.get("symbols"), list(jc_mod.get("watchlist") or ["BTCUSDT"]))
    tfs = _as_list(d.get("tfs"), ["15m", "1h", "4h"])
    try:
        days = max(1, min(180, int(d.get("days") or 30)))
        stride = max(1, min(16, int(d.get("stride") or 1)))
    except (TypeError, ValueError):
        days, stride = 30, 1
    out = jsr.start_replay_async(symbols, tfs, days=days, stride=stride)
    return JSONResponse(out, status_code=200 if out.get("ok") else 409)


@app.get("/api/twelve/replay/status")
def api_twelve_replay_status():
    """回放进度：{running, progress(0-100), detail, result, error}。"""
    import jarvis_signal_replay as jsr
    return JSONResponse({"ok": True, **jsr.get_status()})


@app.get("/api/twelve/signal-winrate")
def api_twelve_signal_winrate(symbol: str = "BTCUSDT", tf: str = "4h"):
    """单信号级历史胜率统计（随信号矩阵展示：「该信号近 N 次胜率 63%」）。

    读 jarvis_signal_winrate 的缓存结果（无缓存返回 ok:true + stats:null，
    前端提示先跑回测）；重算走 POST /api/twelve/signal-winrate/run。
    响应：{ok, symbol, tf, stats: {horizon_bars, samples, systems:
    {turtle: {name_cn, long: {...}, short: {...}}, ...}, directions, computed_at}}
    逐笔明细（trades）体积大，不随聚合下发，走 /signal-winrate/trades。
    """
    import jarvis_signal_winrate as jsw
    sym = symbol.upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    iv = tf if tf in {"5m", "15m", "30m", "1h", "4h", "1d"} else "4h"
    try:
        stats = jsw.get_cached(sym, iv)
        if isinstance(stats, dict) and "trades" in stats:
            stats = {k: v for k, v in stats.items() if k != "trades"}
        return JSONResponse({"ok": True, "symbol": sym, "tf": iv, "stats": stats})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:300]}, status_code=500)


@app.get("/api/twelve/signal-winrate/trades")
def api_twelve_signal_winrate_trades(symbol: str = "BTCUSDT", tf: str = "4h",
                                     system: str = "", side: str = ""):
    """单信号胜率回测的逐笔样本明细（K 线图标记历史盈损点用）。

    query：symbol、tf、system（必填，如 turtle / rsi_smooth）、side（可选
    long/short 过滤）。复用 jarvis_signal_winrate 缓存的 trades 明细（与聚合
    胜率同一次回测、同一口径），不另跑回测。
    响应：{ok, symbol, tf, system, name_cn, horizon_bars, days, computed_at,
    trades: [{t, exit_t(ms), side, entry, sl, tp, exit_price, win, pnl_pct,
    bars_held, mode}]}；缓存缺失或旧版缓存无明细时 ok:false + need_run:true
    （前端引导先点「胜率回测」重算）。
    """
    import jarvis_signal_winrate as jsw
    sym = symbol.upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    iv = tf if tf in {"5m", "15m", "30m", "1h", "4h", "1d"} else "4h"
    if not system:
        return JSONResponse({"ok": False, "error": "缺少 system 参数"}, status_code=400)
    try:
        stats = jsw.get_cached(sym, iv)
        if not isinstance(stats, dict):
            return JSONResponse({"ok": False, "need_run": True, "symbol": sym, "tf": iv,
                                 "error": f"{sym} {iv} 尚未做过胜率回测"})
        all_trades = stats.get("trades")
        if not isinstance(all_trades, list):
            # 旧版缓存（无逐笔明细）：引导重跑一次回测刷新缓存结构
            return JSONResponse({"ok": False, "need_run": True, "symbol": sym, "tf": iv,
                                 "error": "现有回测缓存无逐笔明细，请重跑一次「胜率回测」"})
        picked = [t for t in all_trades if t.get("system") == system]
        if side in ("long", "short"):
            picked = [t for t in picked if t.get("side") == side]
        name_cn = (stats.get("systems", {}).get(system) or {}).get("name_cn", system)
        return JSONResponse({
            "ok": True, "symbol": sym, "tf": iv, "system": system,
            "side": side or None, "name_cn": name_cn,
            "horizon_bars": stats.get("horizon_bars"),
            "days": stats.get("days"),
            "computed_at": stats.get("computed_at"),
            "trades": picked,
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)[:300]}, status_code=500)


@app.post("/api/twelve/signal-winrate/run")
def api_twelve_signal_winrate_run(data: dict | None = None):
    """启动单信号级胜率回测（异步后台线程，进度查 /signal-winrate/status）。

    body（全部可选）：{"symbols": "BTC,ETH"|["BTC"], "tfs": "15m,1h"|["15m"],
    "days": 30, "stride": 1}；symbols 缺省取 watchlist，tfs 缺省 15m/1h/4h。
    已在跑时返回 ok:false（HTTP 409）。
    """
    import jarvis_config as jc_mod
    import jarvis_signal_winrate as jsw
    d = data or {}

    def _as_list(v, fallback):
        if isinstance(v, str):
            items = [x.strip() for x in v.split(",") if x.strip()]
            return items or fallback
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
            return items or fallback
        return fallback

    symbols = _as_list(d.get("symbols"), list(jc_mod.get("watchlist") or ["BTCUSDT"]))
    tfs = _as_list(d.get("tfs"), ["15m", "1h", "4h"])
    try:
        days = max(1, min(180, int(d.get("days") or 30)))
        stride = max(1, min(16, int(d.get("stride") or 1)))
    except (TypeError, ValueError):
        days, stride = 30, 1
    out = jsw.start_backtest_async(symbols, tfs, days=days, stride=stride)
    return JSONResponse(out, status_code=200 if out.get("ok") else 409)


@app.get("/api/twelve/signal-winrate/status")
def api_twelve_signal_winrate_status():
    """单信号胜率回测进度：{running, progress(0-100), detail, result, error}。"""
    import jarvis_signal_winrate as jsw
    return JSONResponse({"ok": True, **jsw.get_status()})


# ─────────────────── 信号大白话解读（一键解读，SSE 流式）───────────────────

_EXPLAIN_SIGNAL_SYS = (
    "你是一位耐心的加密货币交易助教，对象是完全没有交易基础的新手。"
    "用大白话解释，不堆术语；必须用到术语时立刻用一句话解释它。"
    "根据传入的信号数据输出 Markdown 短段落，总长 250~400 字，按以下结构：\n"
    "1. **这是什么信号**：它靠什么判断涨跌（用传入的 explain.type/trigger 转译成人话）\n"
    "2. **它现在在说什么**：方向、强度、触发依据（对应 direction/strength/reasoning）\n"
    "3. **历史胜率怎么读**：解释 win_rate/盈亏比/期望的含义；样本量小（low_sample）必须提醒别迷信\n"
    "4. **新手要注意什么**：1~2 条风险提示（含适用周期 best_tfs 与当前周期是否匹配）\n"
    "不要编造传入数据里没有的数字；结尾加一句「以上是教学解释，不构成投资建议」。"
)

_EXPLAIN_CONSENSUS_SYS = (
    "你是一位耐心的加密货币交易助教，对象是完全没有交易基础的新手。"
    "传入数据是 12 套技术分析系统对同一币种的投票（bullish=看涨/bearish=看跌/neutral=中性）。"
    "用大白话解释为什么会出现分歧（不同系统看的东西不一样：趋势类、震荡类、量价类各有视角，"
    "分歧是正常现象），当前投票分布说明市场处于什么状态，新手面对分歧该怎么办"
    "（等待更明确共识/轻仓/看更大周期等）。Markdown 短段落，总长 250~400 字，"
    "不要编造传入数据里没有的数字；结尾加一句「以上是教学解释，不构成投资建议」。"
)


@app.post("/api/twelve/signal-explain/stream")
def api_twelve_signal_explain_stream(data: dict | None = None):
    """信号矩阵「一键解读」：把单信号/共识分歧解释成小白话（SSE 流式）。

    body：{"mode": "signal"|"consensus", "symbol", "tf", "payload": {...}}
      - mode=signal：payload = 前端信号卡现成数据（name_cn/direction/strength/
        reasoning/explain/trade_plan/grade/横截面元信息），不重复取数
      - mode=consensus：payload = {votes, direction, confidence, per_system[]}
    未配置 LLM 时返回 JSON {ok:false, code:"not_configured"}（HTTP 200，前端
    引导去设置页，不报错）；配置了则 SSE 流式输出（复用 /api/ask/stream 帧格式），
    记账 module=signal_explain。
    """
    import jarvis_llm_config as jlc

    d = data or {}
    mode = d.get("mode") if d.get("mode") in ("signal", "consensus") else "signal"
    payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    symbol = str(d.get("symbol") or "BTCUSDT")
    tf = str(d.get("tf") or "4h")

    cfg = jlc.get_llm_config()
    if not cfg:
        return JSONResponse({"ok": False, "code": "not_configured",
                             "message": "未配置 AI（LLM API Key），请到「设置」页配置后再用一键解读"})

    sys_prompt = _EXPLAIN_CONSENSUS_SYS if mode == "consensus" else _EXPLAIN_SIGNAL_SYS
    digest = {"symbol": symbol, "tf": tf, **payload}
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": json.dumps(digest, ensure_ascii=False, default=str)},
    ]

    def _sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def gen():
        try:
            stream = jlc.chat_stream(messages, timeout=90, module="signal_explain")
            yield _sse({"type": "meta", "engine": "llm", "model": cfg.get("model")})
            got_any = False
            for delta in stream:
                got_any = True
                yield _sse({"type": "delta", "content": delta})
            if not got_any:
                yield _sse({"type": "delta", "content": "模型没有返回内容，请稍后重试。"})
            yield _sse({"type": "done"})
        except (jlc.LLMNotConfigured, jlc.LLMCallError) as exc:
            _log_emit(f"signal-explain LLM 失败: {exc}", "warn", "ask")
            yield _sse({"type": "error", "message": f"AI 调用失败：{str(exc)[:160]}，稍后重试"})
        except Exception as exc:  # noqa: BLE001 — 流中断兜底，已推送内容仍有效
            yield _sse({"type": "error", "message": f"解读中断：{repr(exc)[:120]}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


@app.get("/lite", response_class=HTMLResponse)
def lite():
    """小白模式单页：只看结论 + 操作 + K线信号 + 模拟战绩，自动刷新。"""
    return LITE_HTML

# 驾驶舱
@app.get("/cockpit", response_class=HTMLResponse)
def cockpit():
    """小白驾驶舱：左 K 线(画线提示点位) + 右 AI 面板/事件流/问答，自动跟盘。"""
    return COCKPIT_HTML


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>贾维斯 JARVIS · 加密决策仪表盘</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script src="https://s3.tradingview.com/tv.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#07090d;--card:#12161d;--card2:#0d1117;
    --bd:rgba(148,163,184,0.09);--bd2:rgba(148,163,184,0.22);
    --fg:#f1f5fb;--fg2:#c6d0de;--mut:#8b95a7;
    --up:#10d68c;--down:#ff5570;--accent:#4f8cff;--accent2:#7aa8ff;--warn:#f5c00e;
    --r:14px;--r2:9px;--r3:999px;
    --sh:0 1px 0 rgba(255,255,255,0.03),0 6px 18px -8px rgba(0,0,0,0.5);
    --blur:blur(14px) saturate(1.3);
    --font:"Inter",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
    --mono:"JetBrains Mono","Inter",ui-monospace,SFMono-Regular,Menlo,monospace;
    --ease:cubic-bezier(.22,.61,.36,1);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  ::selection{background:rgba(79,140,255,0.35)}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:rgba(148,163,184,0.18);border-radius:99px}
  ::-webkit-scrollbar-thumb:hover{background:rgba(148,163,184,0.32)}
  ::-webkit-scrollbar-track{background:transparent}
  body{
    background:
      radial-gradient(1100px 520px at 82% -10%, rgba(79,140,255,0.09), transparent 60%),
      radial-gradient(900px 480px at -8% 108%, rgba(16,214,140,0.05), transparent 55%),
      var(--bg);
    color:var(--fg);font-family:var(--font);padding:20px;
    -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  }
  @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  h1{font-size:20px;font-weight:800;letter-spacing:-.3px;display:flex;align-items:center;gap:10px;
    background:linear-gradient(100deg,#fff 20%,#9fc0ff 80%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .sub{color:var(--mut);font-size:12px;margin-top:4px}
  .bar{position:sticky;top:0;z-index:20;display:flex;gap:10px;align-items:center;margin:16px -20px;padding:10px 20px;flex-wrap:wrap;
    background:rgba(13,17,23,0.72);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-top:1px solid var(--bd);border-bottom:1px solid var(--bd)}
  select,button{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:var(--r2);padding:8px 12px;font-size:14px;cursor:pointer;font-family:var(--font);font-weight:600;transition:all .2s var(--ease)}
  select:hover,button:hover{border-color:var(--bd2)}
  .inp{background:var(--card2);color:var(--fg);border:1px solid var(--bd);border-radius:var(--r2);padding:8px;font-size:14px;width:110px;font-family:var(--font);transition:border-color .22s var(--ease)}
  .inp:focus{outline:none;border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button:hover{filter:brightness(1.12);transform:translateY(-1px)}
  .grid{display:grid;gap:14px}
  .cards{grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
  .card{background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent 42%),var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:14px 16px;box-shadow:var(--sh);animation:fadeUp .4s var(--ease) both;transition:border-color .25s var(--ease)}
  .card:hover{border-color:var(--bd2)}
  .card .k{color:var(--mut);font-size:12px}
  .card .v{font-size:22px;font-weight:700;margin-top:6px;font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.3px}
  .card .v small{font-size:12px;color:var(--mut);font-weight:400;font-family:var(--font)}
  .decision{display:grid;grid-template-columns:200px 1fr;gap:18px;margin-bottom:14px}
  .gauge{background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent 42%),var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:6px;box-shadow:var(--sh)}
  .plan{position:relative;overflow:hidden;background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent 42%),var(--card);border:1px solid var(--bd);border-top:2px solid rgba(79,140,255,0.55);border-radius:var(--r);padding:16px;box-shadow:var(--sh)}
  .plan::before{content:"";position:absolute;inset:0;background:radial-gradient(420px 130px at 18% 0%,rgba(79,140,255,0.10),transparent 70%);pointer-events:none}
  .plan h3{font-size:15px;margin-bottom:10px}
  .pill{display:inline-block;padding:3px 10px;border-radius:var(--r3);font-size:13px;font-weight:700}
  .reasons{margin-top:10px;color:var(--mut);font-size:13px;line-height:1.9}
  .charts{grid-template-columns:1fr 1fr}
  .chart{background:var(--card2);border:1px solid var(--bd);border-radius:var(--r);padding:12px;height:300px;box-shadow:var(--sh);transition:border-color .25s var(--ease)}
  .chart:hover{border-color:var(--bd2)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--bd)}
  td{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  th:first-child,td:first-child{text-align:left;font-family:var(--font)}
  th{color:var(--mut);font-weight:600}
  .pos{color:var(--up)} .neg{color:var(--down)} .warnc{color:var(--warn)}
  .sec{margin:18px 0 8px;font-size:13px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.6px}
  .loading{color:var(--mut)}
  .foot{color:var(--mut);font-size:11px;margin-top:18px;line-height:1.7}
  .foot a{color:var(--accent)}
  @media(max-width:820px){.charts{grid-template-columns:1fr}.decision{grid-template-columns:1fr}}
</style>
</head>
<body>
  <h1>🤖 贾维斯 JARVIS · 加密决策仪表盘</h1>
  <div class="sub">真实数据（Binance/CoinGecko/alternative.me/链上） · 因子经 P3 回测 + P4 样本外验证 · 仅研究不构成交易建议</div>

  <div class="bar">
    <select id="symbol">
      <option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>BNBUSDT</option>
    </select>
    <button class="primary" onclick="loadAll()">刷新</button>
    <span id="status" class="loading"></span>
  </div>

  <div class="decision">
    <div class="gauge" id="gauge"></div>
    <div class="plan" id="plan"><div class="loading">加载中…</div></div>
  </div>

  <div class="sec">因子归因（本次信心分由哪些因子贡献 · 绿加分 / 红减分）</div>
  <div class="card" style="height:240px"><div id="cAttr" style="height:100%"></div></div>

  <div class="sec">实时行情（TradingView · 免登录 · 多周期 K 线）</div>
  <div class="card" style="padding:0;overflow:hidden">
    <div id="tvchart" style="height:480px;width:100%"></div>
  </div>

  <div class="sec" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    贾维斯 K 线（含决策信号叠加：入场区间 / 硬止损 / 参考止盈）
    <span id="klineIv" style="display:flex;gap:6px">
      <button data-iv="15m" onclick="setIv('15m')">15m</button>
      <button data-iv="1h" class="primary" onclick="setIv('1h')">1h</button>
      <button data-iv="4h" onclick="setIv('4h')">4h</button>
      <button data-iv="1d" onclick="setIv('1d')">1d</button>
    </span>
    <span id="klineStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="card" style="height:420px"><div id="cKline" style="height:100%"></div></div>

  <div class="sec">真实数据快照</div>
  <div class="grid cards" id="cards"></div>

  <div class="sec">历史图表</div>
  <div class="grid charts">
    <div class="chart" id="chartPrice"></div>
    <div class="chart" id="chartFng"></div>
  </div>

  <div class="sec">已验证因子（事件研究：买入极度恐惧后前瞻收益 vs 基线）</div>
  <div class="card"><div id="factorTable"><div class="loading">加载中…</div></div></div>

  <div class="sec" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    置信度校准（贾维斯有多大把握 vs 实际兑现 · Brier 自我评分）
    <span id="calibH" style="display:flex;gap:6px">
      <button data-h="7" onclick="setCalibH(7)">7天</button>
      <button data-h="30" class="primary" onclick="setCalibH(30)">30天</button>
    </span>
    <span id="calibStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="grid charts">
    <div class="chart" id="cCalib"></div>
    <div class="card" style="height:300px;overflow:auto"><div id="calibStat"><div class="loading">加载中…</div></div></div>
  </div>

  <div class="sec" style="display:flex;align-items:center;gap:12px">
    历史战绩（贾维斯真实前向准确率追踪）
    <button onclick="recordToday()" style="font-size:12px;padding:5px 10px">记录今日决策</button>
    <span id="trackStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="grid charts">
    <div class="chart" id="chartTrack"></div>
    <div class="card" style="height:300px;overflow:auto"><div id="trackStat"><div class="loading">加载中…</div></div></div>
  </div>
  <div class="card" style="margin-top:14px"><div id="trackTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">💰 模拟交易台账（钱包余额 · 限价挂单 · 买卖成交记录）</div>
  <div class="grid cards" id="walletCards"><div class="loading">加载中…</div></div>

  <div class="card" style="margin-top:12px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <strong style="font-size:13px">挂限价单（自己指定点位）：</strong>
      <select id="loSym"><option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>BNBUSDT</option></select>
      <select id="loSide"><option value="buy">买入</option><option value="sell">卖出</option></select>
      <input id="loPrice" placeholder="限价" class="inp"/>
      <input id="loQty" placeholder="数量" class="inp"/>
      <input id="loSL" placeholder="止损(可选)" class="inp"/>
      <input id="loTP" placeholder="止盈(可选)" class="inp"/>
      <button class="primary" onclick="placeOrder()">挂单</button>
      <button onclick="matchOrders()">立即撮合</button>
      <span style="border-left:1px solid var(--bd);height:24px"></span>
      <input id="depAmt" placeholder="入金额" class="inp"/>
      <button onclick="deposit()">入金</button>
      <span style="border-left:1px solid var(--bd);height:24px"></span>
      <input id="cycSyms" placeholder="跟盘币种 BTC,ETH" class="inp" style="width:150px" value="BTC,ETH,SOL"/>
      <button class="primary" onclick="runCycle()">跑一轮自动跟盘</button>
      <button onclick="loadTrader()">刷新台账</button>
      <span id="loStatus" class="loading" style="font-size:12px"></span>
    </div>
    <div class="k" style="margin-top:8px">买单到价（现价≤限价）才成交并冻结资金；点「立即撮合」用当前现价撮合。「跑一轮自动跟盘」按贾维斯决策自动撮合+盯平仓+找开仓（需出网取价，约 10-30 秒）。</div>
  </div>

  <div class="sec">挂单簿（pending）</div>
  <div class="card"><div id="ordersTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">当前持仓</div>
  <div class="card"><div id="positionsTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">已平仓历史（战绩复盘）</div>
  <div class="card"><div id="closedStat" class="k" style="margin-bottom:8px"></div><div id="closedTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">买卖成交记录（钱包资金流水）</div>
  <div class="card"><div id="ledgerTable"><div class="loading">加载中…</div></div></div>

  <div class="foot" id="foot"></div>

<script>
const $ = id => document.getElementById(id);
let gauge, cPrice, cFng, cTrack, cKline, cCalib, cAttr;
let tvWidget = null, tvSymbol = null;
let klineIv = '1h';
let lastDecision = null;  // 缓存最近一次决策，供 K 线叠加信号
let lastTraderStat = null;  // 缓存最近一次跟盘账户总览，供持仓表复用现价/浮盈

function pct(x){return (x>0?'+':'')+x+'%';}
function cls(x){return x>0?'pos':(x<0?'neg':'');}

function renderTV(sym){
  // 免登录嵌入 TradingView 高级图表（实时 K 线、多周期），数据走 Binance 同源
  if(typeof TradingView==='undefined'){ return; }
  if(tvSymbol===sym && tvWidget) return;  // 同币种不重复创建
  tvSymbol = sym;
  $('tvchart').innerHTML = '';
  tvWidget = new TradingView.widget({
    container_id: 'tvchart',
    symbol: 'BINANCE:'+sym,
    interval: '60',
    timezone: 'Asia/Shanghai',
    theme: 'dark',
    style: '1',
    locale: 'zh_CN',
    autosize: true,
    enable_publishing: false,
    hide_side_toolbar: false,
    allow_symbol_change: true,
    studies: ['Volume@tv-basicstudies'],
    backgroundColor: '#0d1117',
    gridColor: '#1a212c'
  });
}

function setIv(iv){
  klineIv = iv;
  document.querySelectorAll('#klineIv button').forEach(b=>{
    b.className = (b.getAttribute('data-iv')===iv) ? 'primary' : '';
  });
  loadKline($('symbol').value);
}

async function loadKline(sym){
  $('klineStatus').textContent = '拉取 K 线…';
  try{
    const d = await (await fetch('/api/kline?symbol='+sym+'&interval='+klineIv+'&limit=200')).json();
    if(d.error){ $('klineStatus').textContent='K线失败: '+d.error; return; }
    renderKline(d);
    $('klineStatus').textContent = d.interval+' · '+d.rows.length+' 根';
  }catch(e){ $('klineStatus').textContent='K线失败: '+e; }
}

function renderKline(d){
  if(!cKline) cKline=echarts.init($('cKline'));
  const rows=d.rows||[];
  const dates=rows.map(r=>r.t);
  const ohlc=rows.map(r=>[r.o,r.c,r.l,r.h]);  // echarts: [open,close,low,high]
  const vol=rows.map(r=>r.v);
  const ax={axisLine:{lineStyle:{color:'#8b95a7'}},splitLine:{lineStyle:{color:'#1a212c'}}};
  // 叠加决策信号线
  const ml=[];
  const dec=lastDecision||{};
  if(dec.suggested_position_pct>0){
    if(dec.stop_loss) ml.push({yAxis:dec.stop_loss,name:'硬止损',lineStyle:{color:'#ff5570'},label:{formatter:'止损 '+dec.stop_loss,color:'#ff5570',position:'insideEndTop'}});
    if(dec.take_profit_ref) ml.push({yAxis:dec.take_profit_ref,name:'参考止盈',lineStyle:{color:'#10d68c'},label:{formatter:'止盈 '+dec.take_profit_ref,color:'#10d68c',position:'insideEndTop'}});
    if(dec.entry_zone){const parts=(''+dec.entry_zone).split('~').map(s=>parseFloat(s.trim())).filter(x=>!isNaN(x));
      parts.forEach((p,i)=>ml.push({yAxis:p,name:'入场',lineStyle:{color:'#4f8cff',type:'dashed'},label:{formatter:'入场 '+p,color:'#4f8cff',position:'insideEndTop'}}));}
  }
  cKline.setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
    legend:{data:['K线','成交量'],textStyle:{color:'#8b95a7'},top:0},
    grid:[{left:55,right:20,top:30,height:'62%'},{left:55,right:20,top:'74%',height:'16%'}],
    xAxis:[{type:'category',data:dates,...ax,axisLabel:{color:'#8b95a7'},boundaryGap:true},
           {type:'category',gridIndex:1,data:dates,axisLabel:{show:false},axisLine:{show:false}}],
    yAxis:[{scale:true,...ax,axisLabel:{color:'#8b95a7'}},
           {gridIndex:1,...ax,axisLabel:{show:false},splitLine:{show:false}}],
    dataZoom:[{type:'inside',xAxisIndex:[0,1],start:55,end:100},{type:'slider',xAxisIndex:[0,1],bottom:0,height:14,start:55,end:100,textStyle:{color:'#8b95a7'}}],
    series:[
      {name:'K线',type:'candlestick',data:ohlc,itemStyle:{color:'#10d68c',color0:'#ff5570',borderColor:'#10d68c',borderColor0:'#ff5570'},
       markLine:{symbol:'none',data:ml,silent:true}},
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:vol,itemStyle:{color:'#4f8cff80'}}
    ]
  });
}

async function loadAll(){
  const sym = $('symbol').value;
  renderTV(sym);
  loadKline(sym);
  $('status').textContent = '拉取真实数据中（约 10-20 秒）…';
  try{
    const snap = await (await fetch('/api/snapshot?symbol='+sym)).json();
    renderDecision(snap); renderCards(snap);
    $('status').textContent='更新于 '+snap.generated_at_utc+' UTC';
  }catch(e){ $('status').textContent='快照失败: '+e; }

  fetch('/api/series?symbol='+sym+'&days=365').then(r=>r.json()).then(renderCharts).catch(()=>{});
  fetch('/api/factor?symbol='+sym).then(r=>r.json()).then(renderFactor).catch(()=>{});
  fetch('/api/track?symbol='+sym).then(r=>r.json()).then(renderTrack).catch(()=>{});
  loadCalib(sym);
  loadTrader();
}

// ───────── 模拟交易台账 ─────────
async function loadTrader(){
  try{ lastTraderStat = await (await fetch('/api/trader/status')).json(); renderWallet(lastTraderStat); }catch(e){ lastTraderStat=null; }
  fetch('/api/orders').then(r=>r.json()).then(renderOrders).catch(()=>{});
  fetch('/api/positions?status=open').then(r=>r.json()).then(renderPositions).catch(()=>{});
  fetch('/api/positions?status=closed').then(r=>r.json()).then(renderClosed).catch(()=>{});
  fetch('/api/ledger?limit=40').then(r=>r.json()).then(renderLedger).catch(()=>{});
}

function renderWallet(st){
  const eqc=st.equity_change_pct;
  const cards=[
    ['总权益', (st.equity_usdt??'—')+' U', '较起始 '+(eqc==null?'—':pct(eqc))],
    ['可用现金', (st.cash_usdt??'—')+' U', '起始入金 '+(st.start_equity_usdt??'—')+'U'],
    ['挂单冻结', (st.frozen_usdt??'—')+' U', ''],
    ['持仓市值', (st.holdings_value_usdt??'—')+' U', (st.open_positions||0)+' 个持仓'],
    ['已实现盈亏', (st.realized_pnl_usdt??'—')+' U', '浮动 '+(st.unrealized_pnl_usdt??'—')+'U'],
    ['盈亏比', st.profit_factor??'—', '胜率 '+(st.win_rate_pct==null?'—':st.win_rate_pct+'%')],
  ];
  $('walletCards').innerHTML=cards.map(c=>`<div class="card"><div class="k">${c[0]}</div><div class="v">${c[1]}</div><div class="k" style="margin-top:4px">${c[2]}</div></div>`).join('');
}

function renderOrders(rows){
  if(!rows||!rows.length){ $('ordersTable').innerHTML='<div class="loading">暂无挂单</div>'; return; }
  const tr=rows.map(o=>`<tr><td>#${o.id}</td><td>${o.symbol}</td><td class="${o.side==='buy'?'pos':'neg'}">${o.side==='buy'?'买':'卖'}</td><td>${o.limit_price}</td><td>${o.qty}</td><td>${o.notional_usdt??'—'}</td><td>${o.created_date||''}</td><td><button onclick="cancelOrder(${o.id})" style="padding:3px 8px;font-size:12px">撤单</button></td></tr>`).join('');
  $('ordersTable').innerHTML=`<table><thead><tr><th>单号</th><th>币种</th><th>方向</th><th>限价</th><th>数量</th><th>冻结U</th><th>挂单日</th><th></th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderPositions(rows){
  if(!rows||!rows.length){ $('positionsTable').innerHTML='<div class="loading">暂无持仓</div>'; return; }
  const det={}; ((lastTraderStat&&lastTraderStat.open_detail)||[]).forEach(d=>det[d.id]=d);
  const tr=rows.map(p=>{
    const d=det[p.id]||{};
    const cur=(d.cur_price!=null)?d.cur_price:'—';
    const up=d.unrealized_usdt;
    const upPct=(d.cur_price!=null&&p.entry_price)?+(((d.cur_price/p.entry_price)-1)*100).toFixed(2):null;
    const upTxt=(up==null)?'—':((up>0?'+':'')+up+'U'+(upPct==null?'':` (${upPct>0?'+':''}${upPct}%)`));
    let held=null; if(p.entry_date){const t=new Date(p.entry_date+'T00:00:00'); if(!isNaN(t)) held=Math.floor((Date.now()-t.getTime())/86400000);}
    const ts=p.time_stop_days;
    const heldTxt=(held==null)?'—':`${held}/${ts??'—'} 天`;
    const heldCls=(held!=null&&ts&&held>=ts*0.8)?'neg':'';
    return `<tr><td>#${p.id}</td><td>${p.symbol}</td><td>${p.entry_price}</td><td>${cur}</td><td>${p.qty}</td><td class="${cls(up||0)}">${upTxt}</td><td class="neg">${p.stop_loss??'—'}</td><td class="pos">${p.take_profit??'—'}</td><td>${p.entry_date||''}</td><td class="${heldCls}">${heldTxt}</td><td><button onclick="closePos('${p.symbol}')" style="padding:3px 8px;font-size:12px">平仓</button></td></tr>`;
  }).join('');
  $('positionsTable').innerHTML=`<table><thead><tr><th>持仓</th><th>币种</th><th>入场价</th><th>现价</th><th>数量</th><th>浮动盈亏</th><th>止损</th><th>止盈</th><th>开仓日</th><th>持仓(天)</th><th></th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderClosed(rows){
  const st=lastTraderStat||{};
  $('closedStat').innerHTML=`已平仓 <b>${st.closed_trades??0}</b> 笔 · 胜率 <b>${st.win_rate_pct==null?'—':st.win_rate_pct+'%'}</b>（${st.wins??0} 胜 / ${st.losses??0} 负）· 盈亏比 <b>${st.profit_factor??'—'}</b> · 平均盈 <span class="pos">${st.avg_win_usdt??'—'}U</span> / 平均亏 <span class="neg">${st.avg_loss_usdt??'—'}U</span> · 已实现 <b class="${cls(st.realized_pnl_usdt||0)}">${st.realized_pnl_usdt??'—'}U</b>`;
  if(!rows||!rows.length){ $('closedTable').innerHTML='<div class="loading">暂无已平仓记录</div>'; return; }
  const rmap={stop:'止损',take:'止盈',time:'到期',signal:'反转',manual:'手动'};
  const tr=rows.map(p=>`<tr><td>#${p.id}</td><td>${p.symbol}</td><td>${p.entry_price}</td><td>${p.exit_price??'—'}</td><td>${p.qty}</td><td>${rmap[p.exit_reason]||p.exit_reason||'—'}</td><td class="${cls(p.realized_pnl_usdt||0)}">${p.realized_pnl_usdt==null?'—':((p.realized_pnl_usdt>0?'+':'')+p.realized_pnl_usdt+'U')}</td><td class="${cls(p.realized_pnl_pct||0)}">${p.realized_pnl_pct==null?'—':pct(p.realized_pnl_pct)}</td><td>${p.exit_date||''}</td></tr>`).join('');
  $('closedTable').innerHTML=`<table><thead><tr><th>持仓</th><th>币种</th><th>入场价</th><th>出场价</th><th>数量</th><th>平仓原因</th><th>盈亏U</th><th>盈亏%</th><th>平仓日</th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderLedger(rows){
  if(!rows||!rows.length){ $('ledgerTable').innerHTML='<div class="loading">暂无成交记录</div>'; return; }
  const map={deposit:'入金',buy:'买入',sell:'卖出',freeze:'冻结',unfreeze:'解冻'};
  const tr=rows.map(r=>`<tr><td>${r.dt||''}</td><td>${map[r.type]||r.type}</td><td>${r.symbol||'-'}</td><td class="${cls(r.amount_usdt)}">${(r.amount_usdt>0?'+':'')+r.amount_usdt}</td><td>${r.cash_after??'—'}</td><td>${r.note||''}</td></tr>`).join('');
  $('ledgerTable').innerHTML=`<table><thead><tr><th>时间</th><th>类型</th><th>币种</th><th>金额U</th><th>现金余额U</th><th>备注</th></tr></thead><tbody>${tr}</tbody></table>`;
}

async function placeOrder(){
  const sym=$('loSym').value, side=$('loSide').value, price=$('loPrice').value, qty=$('loQty').value, sl=$('loSL').value, tp=$('loTP').value;
  if(!price||!qty){ $('loStatus').textContent='请填写限价和数量'; return; }
  let url=`/api/orders/place?symbol=${sym}&side=${side}&price=${price}&qty=${qty}`;
  if(sl) url+=`&stop_loss=${sl}`;
  if(tp) url+=`&take_profit=${tp}`;
  $('loStatus').textContent='提交中…';
  try{
    const r=await (await fetch(url,{method:'POST'})).json();
    $('loStatus').textContent=r.ok?`✅ 挂单 #${r.order_id} 成功`:`❌ ${r.reason}`;
    if(r.ok){ $('loPrice').value=''; $('loQty').value=''; $('loSL').value=''; $('loTP').value=''; }
  }catch(e){ $('loStatus').textContent='挂单失败: '+e; }
  loadTrader();
}

async function cancelOrder(id){ await fetch('/api/orders/cancel?order_id='+id,{method:'POST'}); loadTrader(); }

async function matchOrders(){
  $('loStatus').textContent='撮合中（取现价）…';
  try{ const r=await (await fetch('/api/orders/match',{method:'POST'})).json();
    $('loStatus').textContent=`撮合完成：成交 ${(r.matched||[]).length} 笔`;
  }catch(e){ $('loStatus').textContent='撮合失败: '+e; }
  loadTrader();
}

async function runCycle(){
  const syms=($('cycSyms').value||'BTC,ETH,SOL').trim();
  $('loStatus').textContent='自动跟盘中（拉决策+取价，约 10-30 秒）…';
  try{
    const r=await (await fetch('/api/trader/cycle?symbols='+encodeURIComponent(syms),{method:'POST'})).json();
    const m=(r.matched||[]).length, c=(r.closed||[]).length, o=(r.opened||[]).length;
    $('loStatus').textContent=`跟盘完成：撮合 ${m} / 平仓 ${c} / 开仓 ${o} / 当前持仓 ${r.open_after??'—'}`;
  }catch(e){ $('loStatus').textContent='跟盘失败: '+e; }
  loadTrader();
}

async function deposit(){
  const amt=$('depAmt').value;
  if(!amt){ $('loStatus').textContent='请填写入金额'; return; }
  await fetch('/api/wallet/deposit?amount='+amt,{method:'POST'});
  $('depAmt').value=''; loadTrader();
}

async function closePos(sym){
  if(!confirm(sym+' 确认按现价平仓？')) return;
  await fetch('/api/positions/close?symbol='+sym,{method:'POST'});
  loadTrader();
}

let calibH = 30;
function setCalibH(h){
  calibH = h;
  document.querySelectorAll('#calibH button').forEach(b=>{
    b.className = (parseInt(b.getAttribute('data-h'))===h) ? 'primary' : '';
  });
  loadCalib($('symbol').value);
}

async function loadCalib(sym){
  $('calibStatus').textContent = '计算校准…';
  try{
    const c = await (await fetch('/api/calibration?symbol='+sym+'&horizon='+calibH)).json();
    renderCalib(c);
    $('calibStatus').textContent = c.n? ('样本 '+c.n+' 条') : '';
  }catch(e){ $('calibStatus').textContent='校准失败: '+e; }
}

function renderCalib(c){
  if(!cCalib) cCalib=echarts.init($('cCalib'));
  const bk=c.buckets||[];
  // 可靠性曲线：x=预测兑现率, y=实际兑现率；对角线=完美校准
  const pts=bk.map(b=>[b.pred_pct,b.actual_pct,b.n]);
  const ax={axisLine:{lineStyle:{color:'#8b95a7'}},splitLine:{lineStyle:{color:'#1a212c'}}};
  cCalib.setOption({
    title:{text:'可靠性曲线（贴对角线=越准）',textStyle:{color:'#f1f5fb',fontSize:13}},
    tooltip:{formatter:p=>p.seriesName==='理想'?'完美校准线':`预测兑现 ${p.data[0]}%<br>实际兑现 ${p.data[1]}%<br>样本 ${p.data[2]} 条`},
    grid:{left:50,right:25,top:50,bottom:40},
    xAxis:{type:'value',name:'贾维斯预测兑现率%',min:40,max:100,...ax,axisLabel:{color:'#8b95a7'}},
    yAxis:{type:'value',name:'实际兑现率%',min:0,max:100,...ax,axisLabel:{color:'#8b95a7'}},
    series:[
      {name:'理想',type:'line',data:[[50,50],[100,100]],showSymbol:false,lineStyle:{color:'#8b95a7',type:'dashed'},silent:true},
      {name:'贾维斯',type:'scatter',data:pts,symbolSize:p=>Math.min(40,12+p[2]),
       itemStyle:{color:'#4f8cff'},label:{show:true,position:'top',color:'#8b95a7',formatter:p=>'n='+p.data[2]}}
    ]
  });
  // 统计卡
  if(!c.n){ $('calibStat').innerHTML='<div class="loading">暂无可评估的方向性决策。先 backfill/record + evaluate 积累样本。</div>'; return; }
  const bssTxt = c.bss==null?'—':(c.bss>0?`<span class="pos">+${c.bss}</span>（比瞎猜强）`:`<span class="neg">${c.bss}</span>（不如瞎猜）`);
  let st=`<div style="font-size:13px;line-height:2">`+
    `${c.horizon}天前瞻 · 方向性样本 <b>${c.n}</b> 条 · 总体兑现率 <b>${c.overall_hit_pct}%</b><br>`+
    `Brier 分 <b>${c.brier}</b> <small>(越低越好，基线 ${c.brier_baseline})</small><br>`+
    `Brier 技巧分 BSS <b>${bssTxt}</b><br><br>`+
    `<b>分桶（预测 vs 实际）：</b>`;
  st+=`<table style="margin-top:6px"><thead><tr><th>把握度</th><th>样本</th><th>预测</th><th>实际</th></tr></thead><tbody>`;
  for(const b of bk){
    const gap=b.actual_pct-b.pred_pct;
    st+=`<tr><td>${b.label}</td><td>${b.n}</td><td>${b.pred_pct}%</td><td class="${cls(gap)}">${b.actual_pct}%</td></tr>`;
  }
  st+=`</tbody></table>`;
  st+=`<div class="foot" style="margin-top:10px">注：把握度由信心分映射（|score| 越大越自信）。实际低于预测=过度自信，高于预测=偏保守。这是贾维斯的「自我意识」体检。</div></div>`;
  $('calibStat').innerHTML=st;
}

async function recordToday(){
  const sym = $('symbol').value;
  $('trackStatus').textContent = '记录中…';
  try{
    const r = await (await fetch('/api/track/record?symbol='+sym,{method:'POST'})).json();
    const rec = r.record||{};
    $('trackStatus').textContent = rec.ok ? ('已记录 '+rec.as_of_date+'，并回填 '+(r.evaluate?.outcomes_filled??0)+' 条结果') : ('失败: '+(rec.error||''));
    fetch('/api/track?symbol='+sym).then(x=>x.json()).then(renderTrack);
  }catch(e){ $('trackStatus').textContent='失败: '+e; }
}

function renderTrack(t){
  const rep = t.report||{}, recent = t.recent||[];
  const bh = rep.by_horizon||{};
  // 柱状图：各前瞻 偏多 vs 中性 平均收益
  if(!cTrack) cTrack=echarts.init($('chartTrack'));
  const horizons = Object.keys(bh);
  const dirs = ['偏多（战术）','中性观望'];
  const ax={axisLine:{lineStyle:{color:'#8b95a7'}},splitLine:{lineStyle:{color:'#1a212c'}}};
  const series = dirs.map(d=>({name:d,type:'bar',
    data:horizons.map(h=>{const a=(bh[h].by_direction||{})[d]; return a&&a.n?a.avg_ret_pct:0;}),
    label:{show:true,position:'top',color:'#8b95a7',formatter:'{c}%'}}));
  cTrack.setOption({title:{text:'平均前向收益：偏多信号 vs 中性基线',textStyle:{color:'#f1f5fb',fontSize:13}},
    tooltip:{trigger:'axis'},legend:{textStyle:{color:'#8b95a7'},top:22},grid:{left:45,right:20,top:55,bottom:30},
    xAxis:{type:'category',data:horizons,...ax,axisLabel:{color:'#8b95a7'}},
    yAxis:{type:'value',name:'收益%',...ax,axisLabel:{color:'#8b95a7'}},
    color:['#10d68c','#8b95a7'], series});
  // 统计卡
  let st=`<div style="font-size:13px;line-height:2">累计快照 <b>${rep.total_snapshots??0}</b> 条 · 已评估 <b>${rep.evaluated_outcomes??0}</b> 条<br>`;
  for(const h in bh){const ov=bh[h].overall; if(!ov.n)continue;
    const bull=(bh[h].by_direction||{})['偏多（战术）'];
    st+=`<div style="margin-top:8px"><b>${h}前瞻</b> 全体均值 <span class="${cls(ov.avg_ret_pct)}">${pct(ov.avg_ret_pct)}</span>`;
    if(bull&&bull.n) st+=` · 偏多 <span class="${cls(bull.avg_ret_pct)}">${pct(bull.avg_ret_pct)}</span>（n=${bull.n}，命中 ${bull.hit_rate_pct??'-'}%）`;
    st+=`</div>`;}
  st+=`</div>`;
  $('trackStat').innerHTML = rep.total_snapshots? st : '<div class="loading">还没有快照。点上方「记录今日决策」开始积累，或命令行跑 backfill 立刻出历史战绩。</div>';
  // 最近表
  if(!recent.length){ $('trackTable').innerHTML='<div class="loading">暂无快照</div>'; return; }
  const fmt=(r,ok)=> r==null?'<span class="loading">待评估</span>':`<span class="${cls(r)}">${pct(r)}</span> ${ok===1?'✅':(ok===0?'❌':'·')}`;
  let rows='';
  for(const r of recent){
    rows+=`<tr><td>${r.as_of_date}</td><td>${r.price}</td><td>${r.conviction_score}</td><td>${r.direction}</td><td>${r.position_pct}%</td><td>${fmt(r.r7,r.c7)}</td><td>${fmt(r.r30,r.c30)}</td></tr>`;
  }
  $('trackTable').innerHTML=`<table><thead><tr><th>日期</th><th>价格</th><th>信心</th><th>方向</th><th>仓位</th><th>7天</th><th>30天</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderAttr(d){
  if(!cAttr) cAttr=echarts.init($('cAttr'));
  const attr=(d.attribution||[]).slice().sort((a,b)=>a.contribution-b.contribution);
  const ax={axisLine:{lineStyle:{color:'#8b95a7'}},splitLine:{lineStyle:{color:'#1a212c'}}};
  if(!attr.length){
    cAttr.setOption({title:{text:'本次无显式因子贡献（中性）',textStyle:{color:'#8b95a7',fontSize:13},left:'center',top:'center'}},true);
    return;
  }
  cAttr.setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:p=>{const it=p[0];const a=attr[it.dataIndex];return `<b>${a.factor}</b>: ${a.contribution>0?'+':''}${a.contribution}<br><span style="color:#8b95a7">${a.note}</span>`;}},
    grid:{left:120,right:40,top:20,bottom:25},
    xAxis:{type:'value',...ax,axisLabel:{color:'#8b95a7'},name:'对信心分的贡献'},
    yAxis:{type:'category',data:attr.map(a=>a.factor),...ax,axisLabel:{color:'#f1f5fb'}},
    series:[{type:'bar',data:attr.map(a=>({value:a.contribution,itemStyle:{color:a.contribution>=0?'#10d68c':'#ff5570'}})),
      label:{show:true,position:'right',color:'#8b95a7',formatter:p=>(p.value>0?'+':'')+p.value}}]
  },true);
}

function renderDecision(s){
  const d = s.decision||{}; const fac=s.factor_state||{};
  lastDecision = d;
  renderAttr(d);
  if(cKline) loadKline($('symbol').value);  // 决策更新后重绘 K 线以叠加信号
  const score = d.conviction_score ?? 0;
  if(!gauge) gauge = echarts.init($('gauge'));
  gauge.setOption({
    series:[{type:'gauge',min:-2,max:2,splitNumber:4,radius:'92%',
      axisLine:{lineStyle:{width:14,color:[[0.4,'#ff5570'],[0.6,'#8b95a7'],[1,'#10d68c']]}},
      pointer:{width:5}, progress:{show:false},
      axisLabel:{distance:-6,fontSize:9,color:'#8b95a7'}, axisTick:{show:false}, splitLine:{length:10},
      detail:{valueAnimation:true,fontSize:26,offsetCenter:[0,'58%'],formatter:'{value}',color:'#f1f5fb'},
      title:{offsetCenter:[0,'82%'],fontSize:12,color:'#8b95a7'},
      data:[{value:score,name:'信心分'}]}]
  });
  const dir = d.direction||'-';
  const color = score>=0.8?'#10d68c':(score<=-0.8?'#ff5570':'#8b95a7');
  let html = `<h3>决策：<span class="pill" style="background:${color}22;color:${color}">${dir}</span> &nbsp; 建议仓位 <b>${d.suggested_position_pct??0}%</b></h3>`;
  if(d.suggested_position_pct>0){
    html += `<table style="margin-top:6px">
      <tr><td>入场区间</td><td>${d.entry_zone||'-'}</td></tr>
      <tr><td>硬止损 (-10%)</td><td class="neg">${d.stop_loss||'-'}</td></tr>
      <tr><td>参考止盈 (+8%)</td><td class="pos">${d.take_profit_ref||'-'}</td></tr>
      <tr><td>时间止损</td><td>${d.time_stop_days||'-'} 天</td></tr>
      <tr><td>组合最大风险</td><td class="warnc">≈${d.max_risk_pct||'-'}%</td></tr></table>`;
  }
  html += `<div class="reasons"><b>依据：</b><br>` + (d.reasons||[]).map(r=>'· '+r).join('<br>') + `</div>`;
  $('plan').innerHTML = html;
}

function card(k,v,sub){return `<div class="card"><div class="k">${k}</div><div class="v">${v} ${sub?('<small>'+sub+'</small>'):''}</div></div>`;}

function renderCards(s){
  const f=s.real_data.funding||{}, oi=s.real_data.open_interest||{}, ls=s.real_data.long_short||{},
        fng=s.real_data.fear_greed||{}, ms=s.real_data.market_structure||{}, oc=s.real_data.onchain||{},
        fac=s.factor_state||{};
  let h='';
  h+=card('价格', fac.price??'-', 'USDT');
  h+=card('距高点回撤', (fac.drawdown_from_ath_pct??'-')+'%', fac.above_ma200?'200MA之上':'200MA之下');
  h+=card('资金费率(8h)', (f.last_funding_rate_8h_pct??'-')+'%', f.funding_regime||'');
  h+=card('恐慌贪婪', fng.fng_value??'-', fng.fng_class||'');
  h+=card('全网多空比', ls.global_long_short_ratio??'-', '大户 '+(ls.top_trader_long_short_ratio??'-'));
  h+=card('未平仓OI', oi.open_interest_contracts??'-', '7日 '+(oi.oi_7d_trend_pct??'-')+'%');
  h+=card('BTC市占率', (ms.btc_dominance_pct??'-')+'%', '总市值 '+(ms.total_market_cap_usd_b??'-')+'B');
  if(oc.hashrate_eh_s) h+=card('全网算力', oc.hashrate_eh_s+' EH/s', '内存池 '+(oc.mempool_tx_count??'-'));
  $('cards').innerHTML=h;
  $('foot').innerHTML='数据来源：Binance Futures/Spot、CoinGecko、alternative.me、blockchain.info/mempool.space（真实拉取，非估算）。'+
    '<br>因子 edge 经样本外验证偏弱，本仪表盘刻意以小仓位+硬止损+时间止损控制风险。不构成交易建议。'+
    '<br><a href="/cockpit">驾驶舱</a> · <a href="/lite">小白单页</a>';
}

function renderCharts(d){
  if(!cPrice) cPrice=echarts.init($('chartPrice'));
  if(!cFng) cFng=echarts.init($('chartFng'));
  const ax={axisLine:{lineStyle:{color:'#8b95a7'}},splitLine:{lineStyle:{color:'#1a212c'}}};
  cPrice.setOption({title:{text:'价格 & 距高点回撤',textStyle:{color:'#f1f5fb',fontSize:13}},
    tooltip:{trigger:'axis'},legend:{textStyle:{color:'#8b95a7'},top:22},grid:{left:55,right:55,top:55,bottom:30},
    xAxis:{type:'category',data:d.dates,...ax,axisLabel:{color:'#8b95a7'}},
    yAxis:[{type:'value',name:'价',...ax,axisLabel:{color:'#8b95a7'},scale:true},
           {type:'value',name:'回撤%',...ax,axisLabel:{color:'#8b95a7'},max:0}],
    series:[{name:'收盘价',type:'line',data:d.close,showSymbol:false,lineStyle:{color:'#4f8cff'}},
            {name:'回撤%',type:'line',yAxisIndex:1,data:d.drawdown_pct,showSymbol:false,areaStyle:{color:'#ff557022'},lineStyle:{color:'#ff5570'}}]});
  cFng.setOption({title:{text:'恐慌贪婪指数',textStyle:{color:'#f1f5fb',fontSize:13}},
    tooltip:{trigger:'axis'},grid:{left:45,right:20,top:45,bottom:30},
    xAxis:{type:'category',data:d.dates,...ax,axisLabel:{color:'#8b95a7'}},
    yAxis:{type:'value',min:0,max:100,...ax,axisLabel:{color:'#8b95a7'}},
    visualMap:{show:false,pieces:[{lte:25,color:'#ff5570'},{gt:25,lte:45,color:'#f5c00e'},{gt:45,lte:55,color:'#8b95a7'},{gt:55,lte:75,color:'#7ac74f'},{gt:75,color:'#10d68c'}]},
    series:[{name:'F&G',type:'line',data:d.fng,showSymbol:false}]});
}

function renderFactor(f){
  let rows='';
  for(const k in f){
    const lab=k.replace('fng_below_','F&G<');
    for(const h in f[k]){const m=f[k][h];
      rows+=`<tr><td>${lab}</td><td>${h.slice(1)}天</td><td>${m.n_events}</td><td>${m.cond_mean_ret_pct}%</td><td>${m.baseline_mean_ret_pct}%</td><td class="${cls(m.edge_pct)}">${pct(m.edge_pct)}</td><td>${m.cond_win_rate_pct}%</td></tr>`;
    }
  }
  $('factorTable').innerHTML=`<table><thead><tr><th>条件</th><th>持有</th><th>事件数</th><th>条件后收益</th><th>基线</th><th>超额edge</th><th>胜率</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="foot">注：F&G&lt;20 持有 30 天 edge 最佳（胜率显著高于基线）；持有 90 天 edge 转负——极度恐惧是<b>短期反弹</b>信号而非长期持仓。回撤≤-50% 因子经 P4 已判定过拟合，未纳入决策。</div>`;
}

window.addEventListener('resize',()=>{[gauge,cPrice,cFng,cTrack,cKline,cCalib,cAttr].forEach(c=>c&&c.resize());});
loadAll();
</script>
</body>
</html>
"""


LITE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>贾维斯 · 小白模式</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#07090d;--card:#12161d;--card2:#0d1117;
    --bd:rgba(148,163,184,0.09);--bd2:rgba(148,163,184,0.22);
    --fg:#f1f5fb;--fg2:#c6d0de;--mut:#8b95a7;
    --up:#10d68c;--down:#ff5570;--accent:#4f8cff;--accent2:#7aa8ff;--warn:#f5c00e;
    --r:14px;--r2:9px;--r3:999px;
    --sh:0 1px 0 rgba(255,255,255,0.03),0 6px 18px -8px rgba(0,0,0,0.5);
    --blur:blur(14px) saturate(1.3);
    --font:"Inter",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
    --mono:"JetBrains Mono","Inter",ui-monospace,SFMono-Regular,Menlo,monospace;
    --ease:cubic-bezier(.22,.61,.36,1);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  ::selection{background:rgba(79,140,255,0.35)}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:rgba(148,163,184,0.18);border-radius:99px}
  ::-webkit-scrollbar-track{background:transparent}
  body{
    background:
      radial-gradient(900px 420px at 82% -10%, rgba(79,140,255,0.09), transparent 60%),
      radial-gradient(760px 400px at -8% 108%, rgba(16,214,140,0.05), transparent 55%),
      var(--bg);
    color:var(--fg);font-family:var(--font);padding:16px;max-width:760px;margin:0 auto;
    -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  }
  @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
  h1{font-size:19px;font-weight:800;letter-spacing:-.3px;
    background:linear-gradient(100deg,#fff 20%,#9fc0ff 80%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  select,button{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:var(--r2);padding:7px 12px;font-size:14px;cursor:pointer;font-family:var(--font);font-weight:600;transition:all .2s var(--ease)}
  select:hover,button:hover{border-color:var(--bd2)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button:hover{filter:brightness(1.12);transform:translateY(-1px)}
  .refresh{margin-left:auto;color:var(--mut);font-size:12px;text-align:right;line-height:1.5}
  .verdict{position:relative;overflow:hidden;background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent 42%),var(--card);border:1px solid var(--bd);border-top:2px solid rgba(79,140,255,0.55);border-radius:var(--r);padding:20px;margin:12px 0;text-align:center;box-shadow:var(--sh);animation:fadeUp .4s var(--ease) both}
  .verdict::before{content:"";position:absolute;inset:0;background:radial-gradient(420px 130px at 50% 0%,rgba(79,140,255,0.10),transparent 70%);pointer-events:none}
  .verdict .big{font-size:34px;font-weight:800;letter-spacing:-.4px}
  .verdict .sub{color:var(--mut);font-size:14px;margin-top:8px}
  .conf{display:inline-block;padding:2px 12px;border-radius:var(--r3);font-size:13px;font-weight:700;margin-left:6px}
  .ops{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:12px 0}
  .op{background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent 42%),var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:12px 14px;box-shadow:var(--sh);animation:fadeUp .4s var(--ease) both;transition:border-color .2s var(--ease),transform .2s var(--ease)}
  .op:hover{border-color:var(--bd2);transform:translateY(-1px)}
  .op .k{color:var(--mut);font-size:12px}
  .op .v{font-size:19px;font-weight:700;margin-top:5px;font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  .why{background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent 42%),var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:14px 16px;margin:12px 0;font-size:14px;line-height:1.95;box-shadow:var(--sh);animation:fadeUp .4s var(--ease) both}
  .why b{color:var(--fg)} .why .li{color:var(--mut)}
  .sec{margin:16px 0 8px;font-size:12px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.6px}
  .chart{background:var(--card2);border:1px solid var(--bd);border-radius:var(--r);padding:10px;height:340px;box-shadow:var(--sh);transition:border-color .25s var(--ease)}
  .chart:hover{border-color:var(--bd2)}
  .ivbar{display:flex;gap:6px;margin-bottom:8px}
  .ivbar button{padding:5px 12px;font-size:13px}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}
  .stat{background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent 42%),var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:12px;text-align:center;box-shadow:var(--sh);animation:fadeUp .4s var(--ease) both;transition:border-color .2s var(--ease)}
  .stat:hover{border-color:var(--bd2)}
  .stat .k{color:var(--mut);font-size:12px}
  .stat .v{font-size:20px;font-weight:700;margin-top:5px;font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  .pos{color:var(--up)} .neg{color:var(--down)}
  .foot{color:var(--mut);font-size:11px;margin-top:18px;line-height:1.7;text-align:center}
  .foot a{color:var(--accent)}
  .pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--up);margin-right:5px;animation:p 1.6s infinite}
  @keyframes p{0%{opacity:.3}50%{opacity:1}100%{opacity:.3}}
</style>
</head>
<body>
  <div class="top">
    <h1>🤖 贾维斯 · 小白模式</h1>
    <select id="symbol" onchange="loadAll()">
      <option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>BNBUSDT</option>
    </select>
    <button class="primary" onclick="loadAll()">立即刷新</button>
    <span class="refresh"><span class="pulse"></span><span id="upAt">加载中…</span><br><span id="nextIn"></span></span>
  </div>

  <div class="verdict" id="verdict"><div class="big">加载中…</div></div>

  <div class="sec">该怎么操作（模拟，不构成投资建议）</div>
  <div class="ops" id="ops"></div>

  <div class="why" id="why"></div>

  <div class="sec" style="display:flex;align-items:center;gap:10px">
    K 线 + 贾维斯的建议线（蓝=入场 红=止损 绿=止盈）
    <span class="ivbar" id="ivbar">
      <button data-iv="1h" class="primary" onclick="setIv('1h')">1h</button>
      <button data-iv="4h" onclick="setIv('4h')">4h</button>
      <button data-iv="1d" onclick="setIv('1d')">1d</button>
    </span>
  </div>
  <div class="chart"><div id="kline" style="height:100%"></div></div>

  <div class="sec">模拟盘战绩（贾维斯自己跟单的真实表现）</div>
  <div class="stats" id="stats"></div>

  <div class="foot" id="foot">
    数据来源：Binance / CoinGecko / alternative.me / 链上（真实拉取）。本页每 60 秒自动刷新一次。<br>
    <a href="/cockpit">驾驶舱</a> · 想看完整专业版（因子归因 / 置信度校准 / 挂单台账）？打开 <a href="/">完整仪表盘</a>。
  </div>

<script>
const $ = id => document.getElementById(id);
const REFRESH = 60;                 // 自动刷新间隔（秒）
let kline, lastDec = null, klineIv = '1h', countdown = REFRESH, tick = null;

function pct(x){return (x==null?'—':((x>0?'+':'')+x+'%'));}
function cls(x){return x>0?'pos':(x<0?'neg':'');}

function verdictOf(d){
  const s = d.conviction_score ?? 0, abs = Math.abs(s);
  const conf = abs>=1.2?'高':(abs>=0.6?'中':'低');
  if(s>=0.6)  return {txt:'可小仓试多 📈', color:'#10d68c', conf, score:s};
  if(s<=-0.6) return {txt:'偏空 · 别追多 📉', color:'#ff5570', conf, score:s};
  return {txt:'先观望 ⏸', color:'#8b95a7', conf:'低', score:s};
}

function renderVerdict(s){
  const d = s.decision || {}; lastDec = d;
  const v = verdictOf(d);
  const price = (s.factor_state||{}).price;
  $('verdict').innerHTML =
    `<div class="big" style="color:${v.color}">${v.txt}</div>`+
    `<div class="sub">${($('symbol').value)} 现价约 <b style="color:var(--fg)">${price??'—'}</b> · `+
    `贾维斯把握度 <span class="conf" style="background:${v.color}22;color:${v.color}">${v.conf}</span> `+
    `<span style="font-size:12px">(信心分 ${v.score})</span></div>`;
  // 操作卡
  const pos = d.suggested_position_pct ?? 0;
  let ops = `<div class="op"><div class="k">建议仓位</div><div class="v">${pos}%</div></div>`;
  if(pos>0){
    ops += `<div class="op"><div class="k">入场区间</div><div class="v" style="font-size:15px">${d.entry_zone||'—'}</div></div>`;
    ops += `<div class="op"><div class="k">止损（守不住就卖）</div><div class="v neg">${d.stop_loss||'—'}</div></div>`;
    ops += `<div class="op"><div class="k">止盈（到了可落袋）</div><div class="v pos">${d.take_profit_ref||'—'}</div></div>`;
    ops += `<div class="op"><div class="k">最多持有</div><div class="v">${d.time_stop_days||'—'} 天</div></div>`;
  } else {
    ops += `<div class="op" style="grid-column:1/-1"><div class="k">为什么不给点位</div><div class="v" style="font-size:14px;color:var(--mut)">当前信号不够强，贾维斯建议空仓等更好的机会，不硬找入场点。</div></div>`;
  }
  $('ops').innerHTML = ops;
  // 通俗依据（取前 3 条）
  const rs = (d.reasons||[]).slice(0,3);
  $('why').innerHTML = `<b>贾维斯为什么这么说：</b><br>` +
    (rs.length ? rs.map(r=>'<span class="li">· '+r+'</span>').join('<br>') : '<span class="li">暂无明确依据，信号偏中性。</span>');
  if(kline) renderKlineOverlay();
}

function setIv(iv){
  klineIv = iv;
  document.querySelectorAll('#ivbar button').forEach(b=>{
    b.className = (b.getAttribute('data-iv')===iv) ? 'primary' : '';
  });
  loadKline();
}

async function loadKline(){
  try{
    const sym = $('symbol').value;
    const d = await (await fetch('/api/kline?symbol='+sym+'&interval='+klineIv+'&limit=160')).json();
    if(d.error) return;
    renderKline(d);
  }catch(e){}
}

let lastKline = null;
function renderKline(d){
  lastKline = d;
  if(!kline) kline = echarts.init($('kline'));
  renderKlineOverlay();
}

function renderKlineOverlay(){
  if(!kline || !lastKline) return;
  const rows = lastKline.rows || [];
  const dates = rows.map(r=>r.t);
  const ohlc = rows.map(r=>[r.o,r.c,r.l,r.h]);
  const ax = {axisLine:{lineStyle:{color:'#8b95a7'}},splitLine:{lineStyle:{color:'#1a212c'}}};
  const ml = []; const dec = lastDec || {};
  if(dec.suggested_position_pct>0){
    if(dec.stop_loss) ml.push({yAxis:dec.stop_loss,lineStyle:{color:'#ff5570'},label:{formatter:'止损 '+dec.stop_loss,color:'#ff5570',position:'insideEndTop'}});
    if(dec.take_profit_ref) ml.push({yAxis:dec.take_profit_ref,lineStyle:{color:'#10d68c'},label:{formatter:'止盈 '+dec.take_profit_ref,color:'#10d68c',position:'insideEndTop'}});
    if(dec.entry_zone){(''+dec.entry_zone).split('~').map(s=>parseFloat(s.trim())).filter(x=>!isNaN(x))
      .forEach(p=>ml.push({yAxis:p,lineStyle:{color:'#4f8cff',type:'dashed'},label:{formatter:'入场 '+p,color:'#4f8cff',position:'insideEndTop'}}));}
  }
  kline.setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
    grid:{left:55,right:18,top:14,bottom:48},
    xAxis:{type:'category',data:dates,...ax,axisLabel:{color:'#8b95a7'},boundaryGap:true},
    yAxis:{scale:true,...ax,axisLabel:{color:'#8b95a7'}},
    dataZoom:[{type:'inside',start:50,end:100},{type:'slider',bottom:8,height:14,start:50,end:100,textStyle:{color:'#8b95a7'}}],
    series:[{type:'candlestick',data:ohlc,itemStyle:{color:'#10d68c',color0:'#ff5570',borderColor:'#10d68c',borderColor0:'#ff5570'},
      markLine:{symbol:'none',data:ml,silent:true}}]
  });
}

async function loadStats(){
  try{
    const st = await (await fetch('/api/trader/status')).json();
    const cards = [
      ['总权益', (st.equity_usdt??'—')+' U', cls(st.equity_change_pct)],
      ['总收益', pct(st.equity_change_pct), cls(st.equity_change_pct)],
      ['胜率', (st.win_rate_pct==null?'—':st.win_rate_pct+'%'), ''],
      ['盈亏比', (st.profit_factor??'—'), ''],
      ['已平仓', (st.closed_trades??0)+' 笔', ''],
    ];
    $('stats').innerHTML = cards.map(c=>
      `<div class="stat"><div class="k">${c[0]}</div><div class="v ${c[2]}">${c[1]}</div></div>`).join('');
  }catch(e){ $('stats').innerHTML='<div class="stat"><div class="k">模拟盘</div><div class="v" style="font-size:13px">还没跑跟盘</div></div>'; }
}

async function loadAll(){
  countdown = REFRESH;
  $('upAt').textContent = '更新中…';
  try{
    const sym = $('symbol').value;
    const snap = await (await fetch('/api/snapshot?symbol='+sym)).json();
    renderVerdict(snap);
    const t = new Date();
    $('upAt').textContent = '更新于 ' + t.toLocaleTimeString('zh-CN',{hour12:false});
  }catch(e){ $('upAt').textContent = '刷新失败，下次重试'; }
  loadKline();
  loadStats();
}

function startTimer(){
  if(tick) clearInterval(tick);
  tick = setInterval(()=>{
    countdown--;
    $('nextIn').textContent = countdown>0 ? (countdown+' 秒后自动刷新') : '刷新中…';
    if(countdown<=0) loadAll();
  }, 1000);
}

window.addEventListener('resize',()=>{ if(kline) kline.resize(); });
loadAll();
startTimer();
</script>
</body>
</html>
"""


COCKPIT_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>贾维斯 · 驾驶舱</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.0.8/dist/lightweight-charts.standalone.production.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#07090d;--card:#12161d;--card2:#0d1117;--glass:#12161d;
    --bd:rgba(148,163,184,0.09);--bd2:rgba(148,163,184,0.22);
    --fg:#f1f5fb;--fg2:#c6d0de;--mut:#8b95a7;
    --up:#10d68c;--down:#ff5570;--accent:#4f8cff;--accent2:#7aa8ff;--warn:#f5c00e;
    --r:14px;--r2:9px;--r3:999px;
    --sp1:6px;--sp2:10px;--sp3:14px;--sp4:18px;
    --sh:0 1px 0 rgba(255,255,255,0.03),0 6px 18px -8px rgba(0,0,0,0.5);
    --sh2:0 2px 10px rgba(0,0,0,0.25);
    --blur:blur(14px) saturate(1.3);
    --font:"Inter",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
    --mono:"JetBrains Mono","Inter",ui-monospace,SFMono-Regular,Menlo,monospace;
    --ease:cubic-bezier(.22,.61,.36,1);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  ::selection{background:rgba(79,140,255,0.35)}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:rgba(148,163,184,0.18);border-radius:99px}
  ::-webkit-scrollbar-thumb:hover{background:rgba(148,163,184,0.32)}
  ::-webkit-scrollbar-track{background:transparent}
  .num{font-family:var(--mono);font-feature-settings:"tnum" 1;font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  body{
    background:
      radial-gradient(1100px 520px at 82% -10%, rgba(79,140,255,0.09), transparent 60%),
      radial-gradient(900px 480px at -8% 108%, rgba(16,214,140,0.05), transparent 55%),
      var(--bg);
    color:var(--fg);font-family:var(--font);height:100vh;
    display:flex;flex-direction:column;overflow:hidden;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  }
  @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  .hdr{display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid var(--bd);flex-wrap:wrap;background:rgba(13,17,23,0.72);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}
  .logo{font-size:16px;font-weight:800;letter-spacing:-.3px;display:flex;align-items:center;gap:7px;background:linear-gradient(100deg,#fff 20%,#9fc0ff 80%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .chips{display:flex;gap:6px}
  .chip{background:var(--card);border:1px solid var(--bd);border-radius:var(--r3);padding:5px 14px;font-size:13px;font-weight:600;cursor:pointer;color:var(--mut);transition:all .22s var(--ease);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}
  .chip:hover{color:var(--fg);border-color:var(--bd2);transform:translateY(-1px)}
  .chip.on{background:rgba(245,192,14,0.12);border-color:rgba(245,192,14,0.55);color:var(--warn);font-weight:700}
  .symin{background:var(--card2);color:var(--fg);border:1px solid var(--bd);border-radius:var(--r2);padding:6px 11px;font-size:13px;width:124px;transition:border-color .22s var(--ease)}
  .symin:focus{outline:none;border-color:var(--accent)}
  .price{font-size:18px;font-weight:800;margin-left:4px}
  .price #px{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.4px;transition:color .25s var(--ease)}
  .price #px.tick-up{color:var(--up)} .price #px.tick-down{color:var(--down)}
  .price small{font-size:12px;color:var(--mut);font-weight:500;font-family:var(--font)}
  .spacer{flex:1}
  .live{color:var(--mut);font-size:12px;text-align:right;line-height:1.4}
  .pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--up);margin-right:5px;animation:p 1.6s infinite}
  @keyframes p{0%{opacity:.3}50%{opacity:1}100%{opacity:.3}}
  .cockpit{flex:1;display:grid;grid-template-columns:1fr 430px;gap:var(--sp3);padding:var(--sp3);min-height:0}
  .chartcol{display:flex;flex-direction:column;gap:var(--sp2);min-height:0}
  .ivbar{display:flex;gap:6px;align-items:center}
  .ivbar button{background:var(--card);color:var(--mut);border:1px solid var(--bd);border-radius:8px;padding:5px 13px;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s var(--ease)}
  .ivbar button:hover{color:var(--fg);border-color:var(--bd2)}
  .ivbar button.on{background:rgba(79,140,255,0.14);border-color:rgba(79,140,255,0.55);color:var(--accent2);font-weight:700}
  .ivbar .hint{color:var(--mut);font-size:12px;margin-left:auto}
  #kline{flex:1;background:var(--card2);border:1px solid var(--bd);border-radius:var(--r);min-height:0;box-shadow:var(--sh);overflow:hidden}
  .sidecol{display:flex;flex-direction:column;gap:12px;min-height:0;overflow-y:auto;overflow-x:hidden;padding-right:4px}
  .sidecol::-webkit-scrollbar{width:6px}
  .sidecol::-webkit-scrollbar-thumb{background:var(--bd2);border-radius:99px}
  .sidecol::-webkit-scrollbar-track{background:transparent}
  .card{background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent 42%),var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:15px;box-shadow:var(--sh);animation:fadeUp .4s var(--ease) both;transition:border-color .25s var(--ease)}
  .card:hover{border-color:var(--bd2)}
  .copilot{position:relative;overflow:hidden;flex-shrink:0;border-top:2px solid rgba(79,140,255,0.55)}
  .copilot::before{content:"";position:absolute;inset:0;background:radial-gradient(420px 130px at 18% 0%,rgba(79,140,255,0.10),transparent 70%);pointer-events:none}
  .copilot .vd{font-size:24px;font-weight:800;letter-spacing:-.4px}
  .copilot .vsub{color:var(--mut);font-size:12px;margin-top:5px}
  .conf{display:inline-block;padding:1px 9px;border-radius:var(--r3);font-size:12px;font-weight:700;margin-left:4px}
  .opgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:13px;position:relative}
  .op{background:var(--card2);border:1px solid var(--bd);border-radius:var(--r2);padding:9px 11px;transition:border-color .2s var(--ease),transform .2s var(--ease)}
  .op:hover{border-color:var(--bd2);transform:translateY(-1px)}
  .op .k{color:var(--mut);font-size:11px;font-weight:500}
  .op .v{font-size:15px;font-weight:700;margin-top:4px;font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  .why{margin-top:11px;font-size:13px;line-height:1.7;color:var(--mut);border-top:1px solid var(--bd);padding-top:10px}
  .why b{color:var(--fg)}
  .panelhd{font-size:12px;font-weight:700;color:var(--mut);margin-bottom:11px;display:flex;align-items:center;gap:7px;text-transform:uppercase;letter-spacing:.6px}
  .events{flex:1;display:flex;flex-direction:column;min-height:120px}
  .evlist{flex:1;overflow:auto;display:flex;flex-direction:column;gap:8px}
  .ev{display:flex;gap:9px;font-size:12px;border-left:2px solid var(--bd);padding:3px 0 3px 9px}
  .ev.buy{border-color:var(--accent)} .ev.win{border-color:var(--up)} .ev.loss{border-color:var(--down)} .ev.flat{border-color:var(--mut)} .ev.info{border-color:var(--accent2)}
  .ev .t{color:var(--mut);white-space:nowrap;font-size:11px}
  .ev .tt{font-weight:600} .ev .dd{color:var(--mut);margin-top:2px}
  .empty{color:var(--mut);font-size:12px;text-align:center;padding:18px 0}
  .ask{display:flex;flex-direction:column;height:248px;flex-shrink:0}
  .chat{flex:1;overflow:auto;display:flex;flex-direction:column;gap:9px;padding-right:2px}
  .msg{font-size:13px;line-height:1.6;max-width:92%;padding:8px 11px;border-radius:11px}
  .msg.u{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:3px}
  .msg.a{align-self:flex-start;background:var(--card2);border:1px solid var(--bd);border-bottom-left-radius:3px}
  .askbar{display:flex;gap:7px;margin-top:9px}
  .askbar input{flex:1;background:var(--card2);color:var(--fg);border:1px solid var(--bd);border-radius:var(--r2);padding:9px 11px;font-size:13px;transition:border-color .22s var(--ease)}
  .askbar input:focus{outline:none;border-color:var(--accent)}
  .askbar button{background:var(--accent);border:none;border-radius:var(--r2);color:#fff;padding:0 16px;font-weight:700;cursor:pointer;transition:filter .2s var(--ease),transform .2s var(--ease)}
  .askbar button:hover{filter:brightness(1.1);transform:translateY(-1px)}
  .qsugg{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .qsugg span{background:var(--card2);border:1px solid var(--bd);border-radius:var(--r3);padding:4px 11px;font-size:11px;color:var(--mut);cursor:pointer;transition:all .2s var(--ease)}
  .qsugg span:hover{color:var(--fg);border-color:var(--bd2)}
  .strip{display:flex;align-items:stretch;gap:10px;padding:11px 14px;border-top:1px solid var(--bd);overflow-x:auto;background:var(--card2)}
  .stat{background:var(--card);border:1px solid var(--bd);border-radius:var(--r2);padding:8px 14px;min-width:98px;text-align:center;flex-shrink:0;transition:border-color .2s var(--ease)}
  .stat:hover{border-color:var(--bd2)}
  .stat .k{color:var(--mut);font-size:11px} .stat .v{font-size:16px;font-weight:700;margin-top:4px;font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  .pos{color:var(--up)} .neg{color:var(--down)} .mut{color:var(--mut)}
  /* 多周期预测卡（4h / 15天 / 30天 + 归因 + AI 解读） */
  .mhz{flex-shrink:0}
  .mhz .hzitem{background:var(--card2);border:1px solid var(--bd);border-radius:var(--r2);padding:9px 11px;margin-bottom:8px;transition:border-color .2s var(--ease),opacity .2s var(--ease);cursor:pointer;user-select:none}
  .mhz .hzitem:hover{border-color:var(--bd2)}
  .mhz .hzitem:last-of-type{margin-bottom:0}
  .mhz .hzitem.off{opacity:.42}
  .mhz .hzeye{margin-left:6px;font-size:11px;color:var(--mut);flex-shrink:0}
  .mhz .bandline{font-size:10.5px;color:var(--mut);margin-top:6px;font-family:var(--mono)}
  .mhz .bandline b{color:var(--fg2);font-weight:600}
  .mhz .row{display:flex;align-items:center;gap:8px;font-size:12px}
  .mhz .hz{min-width:44px;color:var(--fg2);font-weight:700;font-family:var(--mono);flex-shrink:0;font-size:12px}
  .mhz .hz small{display:block;font-size:9px;color:var(--mut);font-weight:500;letter-spacing:.4px}
  .mhz .tgt{margin-left:auto;font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--fg);font-weight:700;white-space:nowrap;font-size:12.5px}
  .mhz .tgt small{color:var(--mut);font-weight:500;font-size:10px;margin-right:3px}
  .dirchip{padding:2.5px 10px;border-radius:var(--r3);font-weight:700;font-size:12px;flex-shrink:0}
  .dir-up{background:rgba(16,214,140,0.14);color:var(--up);box-shadow:inset 0 0 0 1px rgba(16,214,140,0.28)}
  .dir-down{background:rgba(255,85,112,0.13);color:var(--down);box-shadow:inset 0 0 0 1px rgba(255,85,112,0.28)}
  .dir-flat{background:rgba(139,149,167,0.13);color:var(--mut);box-shadow:inset 0 0 0 1px rgba(139,149,167,0.25)}
  .gate{font-size:10px;padding:1.5px 7px;border-radius:var(--r3);border:1px solid var(--bd2);color:var(--mut);flex-shrink:0;cursor:help;background:rgba(139,149,167,0.06)}
  .gate.ok{color:var(--up);border-color:rgba(16,214,140,0.45);background:rgba(16,214,140,0.07)}
  .mhz .probwrap{display:flex;align-items:center;gap:6px;flex-shrink:0}
  .mhz .probbar{width:46px;height:4px;border-radius:99px;background:rgba(139,149,167,0.18);overflow:hidden}
  .mhz .probbar i{display:block;height:100%;border-radius:99px;transition:width .5s var(--ease)}
  .mhz .probtxt{font-size:11px;color:var(--fg2);font-weight:600}
  .mhz .whyline{font-size:11px;color:var(--mut);line-height:1.65;margin-top:7px;padding-top:7px;border-top:1px dashed var(--bd)}
  .mhz .calcing{display:flex;align-items:center;gap:8px;color:var(--mut);font-size:12px}
  .mhz .spin{width:12px;height:12px;border-radius:50%;border:2px solid rgba(139,149,167,0.25);border-top-color:var(--accent2);animation:spin 0.9s linear infinite;flex-shrink:0}
  @keyframes spin{to{transform:rotate(360deg)}}
  .mhz .histbtn{margin-left:4px;font-size:10px;padding:1.5px 7px;border-radius:var(--r3);border:1px solid var(--bd2);color:var(--mut);background:rgba(139,149,167,0.06);cursor:pointer;flex-shrink:0;transition:all .2s var(--ease)}
  .mhz .histbtn:hover{color:var(--fg)}
  .mhz .histbtn.on{color:var(--accent2);border-color:rgba(79,140,255,0.5);background:rgba(79,140,255,0.08)}
  .mhz .hzhist{margin-top:8px;padding-top:8px;border-top:1px dashed var(--bd);cursor:default}
  .mhz .hzhist canvas{width:100%;height:56px;display:block}
  .mhz .hzhist .hsum{font-size:10.5px;color:var(--mut);margin-top:5px;font-family:var(--mono)}
  .mhz .hzhist .hsum b{color:var(--fg2)}
  .aiwhy{margin-top:10px;font-size:12px;line-height:1.75;color:var(--fg2);background:var(--card2);border:1px solid var(--bd);border-radius:var(--r2);padding:10px 12px;white-space:pre-wrap;max-height:150px;overflow:auto}
  .aiwhy .tag{display:inline-block;font-size:10px;color:var(--warn);border:1px solid rgba(245,192,14,0.4);border-radius:4px;padding:0 5px;margin-right:6px}
  .actbtn{margin-left:auto;display:flex;gap:8px;align-items:center;flex-shrink:0}
  .actbtn button{background:var(--accent);border:none;border-radius:var(--r2);color:#fff;padding:9px 16px;font-weight:700;cursor:pointer;font-size:13px;transition:filter .2s var(--ease),transform .2s var(--ease)}
  .actbtn button.ghost{background:var(--card);border:1px solid var(--bd);color:var(--fg)}
  .actbtn button:hover{filter:brightness(1.12);transform:translateY(-1px)}
  .holds{font-size:11px;color:var(--mut);align-self:center;flex-shrink:0;max-width:300px}
  .foot{color:var(--mut);font-size:10px;padding:0 14px 8px;line-height:1.5}
  .foot a{color:var(--accent)}
  /* ───── 主动提醒中心：铃铛 + 通知面板 + 页内 toast ───── */
  .bellbtn{position:relative;background:var(--card);border:1px solid var(--bd);border-radius:var(--r2);color:var(--fg);font-size:15px;padding:5px 11px;cursor:pointer;transition:all .2s var(--ease)}
  .bellbtn:hover{border-color:var(--bd2);transform:translateY(-1px)}
  .bellbadge{position:absolute;top:-6px;right:-7px;background:var(--down);color:#fff;font-size:10px;font-weight:700;border-radius:var(--r3);padding:1px 5px;min-width:16px;text-align:center;box-shadow:0 0 0 2px var(--bg)}
  .notifwrap{position:fixed;top:56px;right:14px;width:400px;max-width:calc(100vw - 24px);max-height:calc(100vh - 90px);z-index:60;background:var(--card);border:1px solid var(--bd2);border-radius:var(--r);box-shadow:0 18px 44px -12px rgba(0,0,0,0.65);display:flex;flex-direction:column;overflow:hidden;animation:fadeUp .22s var(--ease) both}
  .ntop{display:flex;align-items:center;gap:6px;padding:10px 12px;border-bottom:1px solid var(--bd);background:var(--card2)}
  .ntab{font-size:12.5px;font-weight:600;color:var(--mut);padding:4px 11px;border-radius:var(--r3);cursor:pointer;border:1px solid transparent;transition:all .2s var(--ease)}
  .ntab:hover{color:var(--fg)}
  .ntab.on{background:rgba(79,140,255,0.14);border-color:rgba(79,140,255,0.5);color:var(--accent2)}
  .nact{font-size:11.5px;color:var(--mut);cursor:pointer;border:1px solid var(--bd);border-radius:var(--r3);padding:3px 10px;white-space:nowrap;transition:all .2s var(--ease)}
  .nact:hover{color:var(--fg);border-color:var(--bd2)}
  .nact.on{color:var(--up);border-color:rgba(16,214,140,0.4)}
  .nbody{flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:8px;min-height:140px}
  .nfoot{display:flex;gap:8px;padding:9px 12px;border-top:1px solid var(--bd);background:var(--card2)}
  .nfoot:empty{display:none}
  .nev{border-left:3px solid var(--bd2);background:var(--card2);border-radius:var(--r2);padding:8px 10px}
  .nev.warning{border-left-color:var(--warn)} .nev.critical{border-left-color:var(--down)} .nev.info{border-left-color:var(--accent)}
  .nev.unread{background:rgba(79,140,255,0.07)}
  .nevhd{display:flex;gap:8px;align-items:baseline}
  .nevtt{font-size:12.5px;font-weight:700;flex:1}
  .nevt{font-size:10.5px;color:var(--mut);white-space:nowrap}
  .nevdd{font-size:11.5px;color:var(--mut);line-height:1.6;margin-top:4px;word-break:break-word}
  .nrule{display:flex;align-items:center;gap:9px;background:var(--card2);border:1px solid var(--bd);border-radius:var(--r2);padding:8px 10px}
  .nkind{font-size:10.5px;font-weight:700;color:var(--accent2);border:1px solid rgba(79,140,255,0.4);border-radius:4px;padding:1px 6px;white-space:nowrap;flex-shrink:0}
  .nrdesc{flex:1;font-size:12px;line-height:1.5}
  .nrmeta{font-size:10.5px;color:var(--mut);margin-top:2px}
  .ndel{color:var(--mut);cursor:pointer;font-size:13px;padding:2px 5px;flex-shrink:0}
  .ndel:hover{color:var(--down)}
  .nsw{position:relative;display:inline-block;width:32px;height:18px;flex-shrink:0}
  .nsw input{display:none}
  .nsw i{position:absolute;inset:0;background:rgba(139,149,167,0.3);border-radius:99px;cursor:pointer;transition:background .2s var(--ease)}
  .nsw i::after{content:"";position:absolute;left:2px;top:2px;width:14px;height:14px;border-radius:50%;background:#fff;transition:transform .2s var(--ease)}
  .nsw input:checked+i{background:var(--up)}
  .nsw input:checked+i::after{transform:translateX(14px)}
  .nform{display:flex;flex-direction:column;gap:9px}
  .nkinds{display:flex;gap:6px;flex-wrap:wrap}
  .nkbtn{font-size:12px;font-weight:600;color:var(--mut);border:1px solid var(--bd);border-radius:var(--r3);padding:5px 13px;cursor:pointer;transition:all .2s var(--ease)}
  .nkbtn:hover{color:var(--fg);border-color:var(--bd2)}
  .nkbtn.on{background:rgba(245,192,14,0.12);border-color:rgba(245,192,14,0.55);color:var(--warn)}
  .nfrow{display:flex;align-items:center;gap:9px}
  .nfrow label{font-size:12px;color:var(--mut);width:76px;flex-shrink:0}
  .nfrow input,.nfrow select{flex:1;background:var(--card2);color:var(--fg);border:1px solid var(--bd);border-radius:var(--r2);padding:7px 10px;font-size:12.5px;min-width:0}
  .nfrow input:focus,.nfrow select:focus{outline:none;border-color:var(--accent)}
  .nhint{font-size:11px;color:var(--mut);line-height:1.65;background:rgba(79,140,255,0.06);border:1px dashed rgba(79,140,255,0.25);border-radius:var(--r2);padding:8px 10px}
  .nkl{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:11px}
  .nklbtn{font-size:11px;font-family:var(--mono);color:var(--fg2);border:1px solid var(--bd);border-radius:var(--r3);padding:3px 9px;cursor:pointer;transition:all .2s var(--ease)}
  .nklbtn:hover{border-color:var(--accent);color:var(--accent2)}
  .nsubmit{background:var(--accent);border:none;border-radius:var(--r2);color:#fff;padding:10px 0;font-weight:700;font-size:13px;cursor:pointer;transition:filter .2s var(--ease)}
  .nsubmit:hover{filter:brightness(1.1)}
  .toasts{position:fixed;right:14px;bottom:64px;z-index:70;display:flex;flex-direction:column;gap:8px;width:330px;max-width:calc(100vw - 24px);pointer-events:none}
  .toast{pointer-events:auto;background:var(--card);border:1px solid var(--bd2);border-left:3px solid var(--accent);border-radius:var(--r2);padding:10px 12px;box-shadow:0 12px 30px -10px rgba(0,0,0,0.6);cursor:pointer;animation:fadeUp .25s var(--ease) both;transition:opacity .4s var(--ease),transform .4s var(--ease)}
  .toast.warning{border-left-color:var(--warn)} .toast.critical{border-left-color:var(--down)}
  .toast.out{opacity:0;transform:translateY(6px)}
  .toast .tt{font-size:12.5px;font-weight:700}
  .toast .dd{font-size:11.5px;color:var(--mut);margin-top:3px;line-height:1.5}
  @media(max-width:980px){.cockpit{grid-template-columns:1fr;overflow:auto}.sidecol{overflow:visible}#kline{height:340px;flex:none}.ask{height:300px}}
</style>
</head>
<body>
  <div class="hdr">
    <div class="logo">🤖 贾维斯驾驶舱</div>
    <div class="chips" id="chips"></div>
    <input class="symin" id="symin" placeholder="＋添加币种 如 DOGE" onkeydown="if(event.key==='Enter')pickInput()"/>
    <div class="price"><span id="px">—</span> <small id="pxsub"></small></div>
    <div class="spacer"></div>
    <button class="bellbtn" id="bellBtn" onclick="toggleNotif()" title="通知中心：信号反转 / 价格关键位 / 割肉后回升提醒">🔔<span class="bellbadge" id="bellBadge" style="display:none">0</span></button>
    <div class="live"><span class="pulse"></span><span id="upAt">加载中…</span><br><span id="nextIn"></span></div>
  </div>

  <div class="notifwrap" id="notifPanel" style="display:none">
    <div class="ntop">
      <span class="ntab on" data-ntab="events" onclick="notifTab('events')">通知</span>
      <span class="ntab" data-ntab="rules" onclick="notifTab('rules')">规则</span>
      <span class="ntab" data-ntab="new" onclick="notifTab('new')">＋新建提醒</span>
      <span style="flex:1"></span>
      <span class="nact" id="nbrowserBtn" onclick="enableBrowserNotif()" title="开启后触发提醒时弹浏览器系统通知，页面不在前台也能收到">🖥 系统通知</span>
    </div>
    <div class="nbody" id="notifBody"><div class="empty">加载中…</div></div>
    <div class="nfoot" id="notifFoot"></div>
  </div>
  <div class="toasts" id="toasts"></div>

  <div class="cockpit">
    <div class="chartcol">
      <div class="ivbar" id="ivbar">
        <button data-iv="15m" onclick="setIv('15m')">15分</button>
        <button data-iv="1h" onclick="setIv('1h')">1时</button>
        <button data-iv="4h" class="on" onclick="setIv('4h')">4时</button>
        <button data-iv="1d" onclick="setIv('1d')">日线</button>
        <span style="width:1px;height:16px;background:var(--bd2);margin:0 2px"></span>
        <button data-vis="core" onclick="toggleVis('core')">核心线</button>
        <button data-vis="sr" onclick="toggleVis('sr')">支撑阻力</button>
        <button data-vis="trend" onclick="toggleVis('trend')">趋势通道</button>
        <button data-vis="marks" onclick="toggleVis('marks')">买卖点</button>
        <span class="hint" title="蓝虚线=入场 红=止损 绿=止盈 ▲▼=买卖点 · 黄=MA7 蓝=MA25 · 底部柱=成交量 · 徽标依次为 4h/15天/30天 三周期预测；「15天目标/30天目标」虚线=分位回归 p50 目标价；灰虚线/👁=观察模式参考点位（未过精准度门禁，引擎不开仓）">?</span>
        <span class="hint" id="predBadge"></span>
      </div>
      <div id="kline"></div>
    </div>

    <div class="sidecol">
      <div class="card copilot" id="copilot"><div class="vd">加载中…</div></div>

      <div class="card mhz">
        <div class="panelhd">🎯 多周期预测 <span class="mut" style="font-weight:400;text-transform:none;letter-spacing:0">方向 · 点位 · 为什么</span></div>
        <div id="mhzRows"><div class="empty">加载中…</div></div>
        <div class="aiwhy" id="aiwhy" style="display:none"></div>
      </div>

      <div class="card events">
        <div class="panelhd">📡 实时事件流 <span class="mut" style="font-weight:400">（真实行情 + 模拟盘）</span></div>
        <div class="evlist" id="evlist"><div class="empty">加载中…</div></div>
      </div>

      <div class="card ask">
        <div class="panelhd">💬 问贾维斯</div>
        <div class="chat" id="chat"></div>
        <div class="qsugg" id="qsugg">
          <span onclick="quick('现在该买吗')">现在该买吗</span>
          <span onclick="quick('止盈止损在哪')">止盈止损在哪</span>
          <span onclick="quick('为什么这么判断')">为什么</span>
          <span onclick="quick('最近战绩怎么样')">最近战绩</span>
        </div>
        <div class="askbar">
          <input id="qin" placeholder="问问该买什么、卖多少、为什么…" onkeydown="if(event.key==='Enter')sendAsk()"/>
          <button onclick="sendAsk()">问</button>
        </div>
      </div>
    </div>
  </div>

  <div class="strip" id="strip"></div>
  <div class="foot">全程模拟盘(paper)不碰真钱 · 数据来自 Binance/CoinGecko/alternative.me · 仅研究不构成投资建议 · <a href="/lite">小白单页</a> · <a href="/">专业版</a></div>

<script>
const $ = id => document.getElementById(id);
const REFRESH = 60;
let SYMS = ['BTCUSDT','ETHUSDT','SOLUSDT'];   // 启动后由 /api/watchlist 覆盖
let sym = 'BTCUSDT', iv = '4h';
let chart, candleSeries=null, volSeries=null, maSeries={}, markersPrim=null, priceLines=[], chartKey='';
let lastDec=null, lastFac=null, lastKline=null, lastPositions=[], lastIntraday=null, lastHorizons=null;
let countdown = REFRESH, tick=null;
const TZ = new Date().getTimezoneOffset()*60;   // 秒：让 X 轴显示本地时间
const barTime = r => Math.floor(r.ts/1000) - TZ;

// 画线分组开关（localStorage 持久化）：core=入场/止损/止盈+4h预测 sr=支撑阻力 trend=趋势/斐波/通道/形态 marks=买卖点
let VIS = {core:true, sr:true, trend:false, marks:true};
try{ const v=JSON.parse(localStorage.getItem('cockpitVis')||'null'); if(v&&typeof v==='object') VIS={...VIS,...v}; }catch(e){}
// 多周期卡开关（点卡片开/关该周期在 K 线上的预测线；localStorage 持久化）
let HZVIS = {'4h':true,'15':true,'30':true};
try{ const v=JSON.parse(localStorage.getItem('cockpitHzVis')||'null'); if(v&&typeof v==='object') HZVIS={...HZVIS,...v}; }catch(e){}
function toggleHz(k){
  HZVIS[k]=!HZVIS[k];
  try{ localStorage.setItem('cockpitHzVis',JSON.stringify(HZVIS)); }catch(e){}
  renderMhz(); if(chart) drawOverlay();
}
function renderVisBtns(){
  document.querySelectorAll('#ivbar button[data-vis]').forEach(b=>{
    b.className = VIS[b.getAttribute('data-vis')] ? 'on' : '';
  });
}
function toggleVis(k){
  VIS[k]=!VIS[k];
  try{ localStorage.setItem('cockpitVis', JSON.stringify(VIS)); }catch(e){}
  renderVisBtns(); drawChart();
}

function fmt(n){ if(n==null||isNaN(n)) return '—'; n=+n; const d=Math.abs(n)>=100?2:(Math.abs(n)>=1?3:6); return n.toLocaleString('en-US',{maximumFractionDigits:d}); }
function pct(x){ return x==null?'—':((x>0?'+':'')+x+'%'); }

function renderChips(){
  $('chips').innerHTML = SYMS.map(s=>
    `<div class="chip ${s===sym?'on':''}" onclick="pick('${s}')">${s.replace('USDT','')}`+
    `<span onclick="event.stopPropagation();rmSym('${s}')" title="移出自选" style="margin-left:6px;opacity:.55;font-weight:400">×</span></div>`
  ).join('')
    + (SYMS.includes(sym)?'':`<div class="chip on">${sym.replace('USDT','')}</div>`);
}
async function loadWatchlist(){
  try{ const d=await (await fetch('/api/watchlist')).json();
    if(Array.isArray(d.symbols)&&d.symbols.length){ SYMS=d.symbols; if(!SYMS.includes(sym)) sym=SYMS[0]; }
  }catch(e){}
  renderChips();
}
async function addSym(v){
  try{
    const r=await fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'add',symbol:v})});
    const d=await r.json();
    if(!r.ok){ alert(d.error||'添加失败'); return false; }
    SYMS=d.symbols||SYMS; renderChips(); return true;
  }catch(e){ return false; }
}
async function rmSym(s){
  try{
    const r=await fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'remove',symbol:s})});
    const d=await r.json();
    if(!r.ok){ alert(d.error||'删除失败'); return; }
    SYMS=d.symbols||SYMS;
    if(sym===s){ sym=SYMS[0]; loadAll(); }
    renderChips();
  }catch(e){}
}
function pick(s){ sym=s; renderChips(); loadAll(); }
async function pickInput(){
  let v=$('symin').value.trim().toUpperCase().replace(/[-/]/g,''); if(!v) return;
  if(!v.endsWith('USDT')) v+='USDT'; $('symin').value='';
  const ok=await addSym(v);            // 输入即加入自选（校验币安存在），失败只看不加
  sym=v; renderChips(); loadAll();
}
function setIv(x){ iv=x; document.querySelectorAll('#ivbar button[data-iv]').forEach(b=>b.className=(b.getAttribute('data-iv')===x?'on':'')); loadKline(); }

function verdictOf(d){
  const s=d.conviction_score??0, a=Math.abs(s);
  const conf=a>=1.2?'高':(a>=0.6?'中':'低');
  if(s>=0.6)  return {txt:'可小仓试多 📈',color:'#10d68c',conf,s};
  if(s<=-0.6) return {txt:'偏空·别追多 📉',color:'#ff5570',conf,s};
  return {txt:'先观望 ⏸',color:'#8b95a7',conf:'低',s};
}
function rr(d){
  const sl=+d.stop_loss, tp=+d.take_profit_ref; let e=null;
  if(d.entry_zone){ const ps=(''+d.entry_zone).split('~').map(x=>parseFloat(x)).filter(x=>!isNaN(x)); if(ps.length) e=ps.reduce((a,b)=>a+b,0)/ps.length; }
  if(!e||isNaN(sl)||isNaN(tp)||e<=sl) return null;
  return ((tp-e)/(e-sl));
}
function entryMid(d){ if(!d.entry_zone) return null; const ps=(''+d.entry_zone).split('~').map(x=>parseFloat(x)).filter(x=>!isNaN(x)); return ps.length?ps.reduce((a,b)=>a+b,0)/ps.length:null; }

function renderCopilot(snap){
  const d=snap.decision||{}, fac=snap.factor_state||{}; lastDec=d; lastFac=fac;
  const price=fac.price; $('px').textContent=fmt(price); $('pxsub').textContent=sym.replace('USDT','/USDT');
  if(d._error||fac._error){ $('copilot').innerHTML=`<div class="vd" style="color:var(--mut)">取数暂不可用</div><div class="why">${d._error||fac._error||''}</div>`; return; }
  const v=verdictOf(d), pos=d.suggested_position_pct??0, r=rr(d);
  let ops='';
  ops+=`<div class="op"><div class="k">建议仓位</div><div class="v">${pos}%</div></div>`;
  if(pos>0){
    ops+=`<div class="op"><div class="k">入场区间</div><div class="v" style="font-size:13px">${d.entry_zone||'—'}</div></div>`;
    ops+=`<div class="op"><div class="k">盈亏比</div><div class="v pos">${r?r.toFixed(2)+':1':'—'}</div></div>`;
    ops+=`<div class="op"><div class="k">止损</div><div class="v neg">${fmt(d.stop_loss)}</div></div>`;
    ops+=`<div class="op"><div class="k">止盈</div><div class="v pos">${fmt(d.take_profit_ref)}</div></div>`;
    ops+=`<div class="op"><div class="k">最多持有</div><div class="v">${d.time_stop_days||'—'}天</div></div>`;
  }else{
    ops+=`<div class="op" style="grid-column:2/4"><div class="k">为什么不给点位</div><div class="v" style="font-size:12px;color:var(--mut)">信号不够强，空仓等更好机会</div></div>`;
  }
  const rs=(d.reasons||[]).slice(0,2);
  $('copilot').innerHTML=
    `<div class="vd" style="color:${v.color}">${v.txt}<span class="conf" style="background:${v.color}22;color:${v.color}">把握${v.conf}</span></div>`+
    `<div class="vsub">${sym.replace('USDT','')} 现价 ${fmt(price)} · 信心分 ${v.s}</div>`+
    `<div class="opgrid">${ops}</div>`+
    `<div class="why"><b>为什么：</b>${rs.length?rs.join('；'):'当前因子偏中性，无明确方向。'}</div>`;
  if(chart) drawOverlay();
}

async function loadKline(){
  const reqSym=sym, reqIv=iv;
  try{
    const d=await (await fetch('/api/kline?symbol='+reqSym+'&interval='+reqIv+'&limit=180')).json();
    if(reqSym!==sym||reqIv!==iv) return;            // 期间已切币，丢弃过期响应
    if(d.error||!(d.rows&&d.rows.length)){
      if(sym+'|'+iv!==chartKey&&candleSeries){ candleSeries.setData([]); chartKey=sym+'|'+iv; }
      return;
    }
    lastKline=d; drawChart();
  }catch(e){}
}

function drawChart(){
  if(!chart){
    chart = LightweightCharts.createChart($('kline'), {
      autoSize:true,
      layout:{ background:{type:'solid',color:'#0d1117'}, textColor:'#8b95a7', fontSize:11, fontFamily:'Inter,-apple-system,PingFang SC,Microsoft YaHei,sans-serif', attributionLogo:false },
      grid:{ vertLines:{color:'rgba(139,149,167,0.07)'}, horzLines:{color:'rgba(139,149,167,0.07)'} },
      rightPriceScale:{ borderColor:'rgba(139,149,167,0.2)', scaleMargins:{top:0.12,bottom:0.12} },
      timeScale:{ borderColor:'rgba(139,149,167,0.2)', timeVisible:true, secondsVisible:false, rightOffset:8, barSpacing:9, minBarSpacing:3 },
      crosshair:{ mode:1, vertLine:{color:'#4f8cff',width:1,style:LightweightCharts.LineStyle.Dotted,labelBackgroundColor:'#4f8cff'}, horzLine:{color:'#4f8cff',labelBackgroundColor:'#4f8cff'} },
      localization:{ locale:'zh-CN', priceFormatter:p=>fmt(p) }
    });
    candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor:'#10d68c', downColor:'#ff5570', borderVisible:true,
      borderUpColor:'#10d68c', borderDownColor:'#ff5570',
      wickUpColor:'#10d68c', wickDownColor:'#ff5570'
    });
    // 成交量副图（底部 ~18%，独立价格轴，对标 trendiq）
    volSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
      priceScaleId:'vol', priceFormat:{type:'volume'},
      lastValueVisible:false, priceLineVisible:false
    });
    chart.priceScale('vol').applyOptions({ scaleMargins:{top:0.82,bottom:0}, borderVisible:false });
    // MA 均线（细线，半透，不抢蜡烛戏）
    maSeries.ma7  = chart.addSeries(LightweightCharts.LineSeries,{color:'rgba(240,185,11,0.85)',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
    maSeries.ma25 = chart.addSeries(LightweightCharts.LineSeries,{color:'rgba(96,165,250,0.85)',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
    markersPrim = LightweightCharts.createSeriesMarkers(candleSeries, []);
  }
  const rows=(lastKline&&lastKline.rows)||[];
  const data=rows.map(r=>({time:barTime(r),open:r.o,high:r.h,low:r.l,close:r.c}));
  const key=sym+'|'+iv;
  if(key!==chartKey){
    candleSeries.setData(data);
    drawVolMA(rows);
    chart.timeScale().fitContent();
    chartKey=key;
    openWS();                 // 历史就位后，切到 Binance WebSocket 逐 tick 实时推送
  }
  // key 未变时不在这里更新蜡烛——由 WebSocket 实时 update()，避免 REST 旧值覆盖更新
  if(VIS.trend){ drawTrendlines(); drawFib(); drawChannel(); drawPattern(); }
  else { clearTrend(); clearFib(); clearChan(); if(vtopPrim) vtopPrim.setPts(null); }
  drawOverlay();
}

// ───────── 成交量副图 + MA 均线（对标 trendiq）─────────
function ma(rows,n){
  const out=[]; let sum=0;
  for(let i=0;i<rows.length;i++){
    sum+=rows[i].c;
    if(i>=n) sum-=rows[i-n].c;
    if(i>=n-1) out.push({time:barTime(rows[i]),value:+(sum/n).toFixed(2)});
  }
  return out;
}
function drawVolMA(rows){
  if(!chart||!volSeries||!rows||!rows.length) return;
  volSeries.setData(rows.map(r=>({
    time:barTime(r), value:+r.v||0,
    color:(r.c>=r.o)?'rgba(14,203,129,0.45)':'rgba(246,70,93,0.45)'
  })));
  if(maSeries.ma7)  maSeries.ma7.setData(ma(rows,7));
  if(maSeries.ma25) maSeries.ma25.setData(ma(rows,25));
}

// ───────── 斜趋势线（连真实摆动高/低点，形成上下轨通道，对标 trendiq）─────────
let trendSeries=[], fibSeries=[], chanSeries=[];
function clearTrend(){ trendSeries.forEach(s=>{ try{chart.removeSeries(s);}catch(e){} }); trendSeries=[]; }
function clearFib(){ fibSeries.forEach(s=>{ try{chart.removeSeries(s);}catch(e){} }); fibSeries=[]; }
function clearChan(){ chanSeries.forEach(s=>{ try{chart.removeSeries(s);}catch(e){} }); chanSeries=[]; }
function pivots(rows,k,type){
  const out=[];
  for(let i=k;i<rows.length-k;i++){
    let ok=true;
    for(let j=i-k;j<=i+k&&ok;j++){
      if(j===i) continue;
      if(type==='high'){ if(rows[j].h>rows[i].h) ok=false; }
      else{ if(rows[j].l<rows[i].l) ok=false; }
    }
    if(ok) out.push({t:barTime(rows[i]), v:(type==='high'?rows[i].h:rows[i].l)});
  }
  return out;
}
function addTrend(p1,p2,color){
  if(!lastKline||!lastKline.rows.length||p2.t<=p1.t) return;
  const rows=lastKline.rows, lastT=barTime(rows[rows.length-1]);
  const slope=(p2.v-p1.v)/(p2.t-p1.t);
  const endV=p2.v+slope*(lastT-p2.t);                 // 沿趋势延伸到最新一根
  const s=chart.addSeries(LightweightCharts.LineSeries,{color,lineWidth:1,
    lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false,
    pointMarkersVisible:false});
  s.setData([{time:p1.t,value:+p1.v},{time:lastT,value:+endV.toFixed(2)}]);
  trendSeries.push(s);
}
function drawTrendlines(){
  if(!chart||!lastKline) return;
  clearTrend();
  const rows=lastKline.rows||[]; if(rows.length<20) return;
  const win=rows.slice(-Math.min(rows.length,120));
  const k=Math.max(2,Math.round(win.length/24));      // 摆动点检测半窗
  const hs=pivots(win,k,'high'), ls=pivots(win,k,'low');
  if(hs.length>=2) addTrend(hs[hs.length-2],hs[hs.length-1],'rgba(214,184,92,0.78)');    // 上轨/阻力趋势线（雅致金，1px）
  if(ls.length>=2) addTrend(ls[ls.length-2],ls[ls.length-1],'rgba(64,196,160,0.78)');    // 下轨/支撑趋势线（雅致青，1px）
}

// ───────── 斐波那契回撤（近窗高低点定波段 · 黄金档 0.5/0.618 加粗，淡紫不抢戏）─────────
const FIBS=[0.236,0.382,0.5,0.618,0.786];
function drawFib(){
  if(!chart||!lastKline) return;
  clearFib();
  const rows=lastKline.rows||[]; if(rows.length<30) return;
  const win=rows.slice(-Math.min(rows.length,120));
  let hi=-Infinity,lo=Infinity,hiI=0,loI=0;
  for(let i=0;i<win.length;i++){ if(win[i].h>hi){hi=win[i].h;hiI=i;} if(win[i].l<lo){lo=win[i].l;loI=i;} }
  if(hi<=lo) return;
  const up=hiI>loI;                                   // 高点在低点之后=上涨波，回撤档从高往低
  const t0=barTime(win[Math.min(hiI,loI)]), t1=barTime(rows[rows.length-1]);
  const LS=LightweightCharts.LineStyle;
  FIBS.forEach(r=>{
    const lv = up ? hi-(hi-lo)*r : lo+(hi-lo)*r;
    const gold = (r===0.5||r===0.618);
    const s=chart.addSeries(LightweightCharts.LineSeries,{
      color: gold?'rgba(168,85,247,0.85)':'rgba(168,85,247,0.34)', lineWidth:1,
      lineStyle: gold?LS.Solid:LS.Dotted,
      lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false, pointMarkersVisible:false
    });
    s.setData([{time:t0,value:+lv.toFixed(2)},{time:t1,value:+lv.toFixed(2)}]);
    fibSeries.push(s);
  });
}

// ───────── 回归通道（近窗收盘价最小二乘中轴 + 最大残差上下轨 · 中性石板色）─────────
function drawChannel(){
  if(!chart||!lastKline) return;
  clearChan();
  const rows=lastKline.rows||[]; if(rows.length<30) return;
  const win=rows.slice(-Math.min(rows.length,100)); const n=win.length;
  let sx=0,sy=0,sxx=0,sxy=0;
  for(let i=0;i<n;i++){ const y=win[i].c; sx+=i; sy+=y; sxx+=i*i; sxy+=i*y; }
  const denom=n*sxx-sx*sx, slope=denom===0?0:(n*sxy-sx*sy)/denom, intc=(sy-slope*sx)/n;
  let mxP=-Infinity,mxN=Infinity;
  for(let i=0;i<n;i++){ const res=win[i].c-(slope*i+intc); if(res>mxP)mxP=res; if(res<mxN)mxN=res; }
  const t0=barTime(win[0]), t1=barTime(win[n-1]);
  const y0=intc, y1=slope*(n-1)+intc;
  const LS=LightweightCharts.LineStyle;
  const mk=(a,b,col,st)=>{
    const s=chart.addSeries(LightweightCharts.LineSeries,{color:col,lineWidth:1,lineStyle:st,
      lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false,pointMarkersVisible:false});
    s.setData([{time:t0,value:+a.toFixed(2)},{time:t1,value:+b.toFixed(2)}]); chanSeries.push(s);
  };
  mk(y0+mxP,y1+mxP,'rgba(148,163,184,0.42)',LS.Dashed);   // 上轨
  mk(y0,y1,'rgba(148,163,184,0.78)',LS.Solid);            // 回归中轴
  mk(y0+mxN,y1+mxN,'rgba(148,163,184,0.42)',LS.Dashed);   // 下轨
}

// ───────── V 形反转顶 形态（左低点→顶→右低点 阴影三角 · 自绘 canvas 图元）─────────
function detectVtop(rows){
  const W=Math.min(rows.length,90); if(W<25) return null;
  const seg=rows.slice(-W);
  let pi=-1,ph=-Infinity;
  for(let i=8;i<seg.length-4;i++){ if(seg[i].h>ph){ph=seg[i].h;pi=i;} }   // 留边距找最高点=反转顶
  if(pi<0) return null;
  const lstart=Math.max(0,pi-30); let li=lstart,lv=Infinity;
  for(let i=lstart;i<=pi;i++){ if(seg[i].l<lv){lv=seg[i].l;li=i;} }        // 顶左侧最低=左基
  const rend=Math.min(seg.length-1,pi+30); let ri=pi,rv=Infinity;
  for(let i=pi;i<=rend;i++){ if(seg[i].l<rv){rv=seg[i].l;ri=i;} }          // 顶右侧最低=右基
  if(li>=pi||ri<=pi) return null;
  const peak=seg[pi].h;
  const dropL=(peak-seg[li].l)/peak, dropR=(peak-seg[ri].l)/peak;
  if(dropL<0.004||dropR<0.004) return null;                               // 两侧涨/跌幅太小不算反转
  return { left:{time:barTime(seg[li]),value:seg[li].l},
           peak:{time:barTime(seg[pi]),value:peak},
           right:{time:barTime(seg[ri]),value:seg[ri].l} };
}

function makeVtopPrimitive(){
  return {
    pts:null, chart:null, series:null, req:null,
    attached(p){ this.chart=p.chart; this.series=p.series; this.req=p.requestUpdate; },
    detached(){ this.chart=this.series=this.req=null; },
    setPts(pts){ this.pts=pts; if(this.req) this.req(); },
    paneViews(){
      const self=this;
      return [{ renderer(){ return { draw(target){
        const pts=self.pts; if(!pts) return;
        target.useBitmapCoordinateSpace(scope=>{
          const ctx=scope.context, hr=scope.horizontalPixelRatio, vr=scope.verticalPixelRatio, ts=self.chart.timeScale();
          const P=[pts.left,pts.peak,pts.right].map(p=>{
            const x=ts.timeToCoordinate(p.time), y=self.series.priceToCoordinate(p.value);
            return (x==null||y==null)?null:{x:x*hr,y:y*vr};
          });
          if(P.some(p=>p==null)) return;
          ctx.beginPath(); ctx.moveTo(P[0].x,P[0].y); ctx.lineTo(P[1].x,P[1].y); ctx.lineTo(P[2].x,P[2].y); ctx.closePath();
          ctx.fillStyle='rgba(124,92,214,0.16)'; ctx.fill();
          ctx.lineWidth=1*hr; ctx.strokeStyle='rgba(214,150,60,0.85)'; ctx.stroke();
          // 标签「V 形反转顶」+ 向下箭头
          const peak=P[1], fs=11*vr;
          ctx.font='600 '+fs+'px -apple-system,PingFang SC,sans-serif';
          ctx.textAlign='center'; ctx.fillStyle='rgba(232,200,120,0.95)';
          ctx.fillText('V 形反转顶', peak.x, peak.y-22*vr);
          ctx.beginPath(); ctx.moveTo(peak.x,peak.y-16*vr); ctx.lineTo(peak.x-3*hr,peak.y-21*vr); ctx.lineTo(peak.x+3*hr,peak.y-21*vr); ctx.closePath();
          ctx.fillStyle='rgba(232,200,120,0.95)'; ctx.fill();
        });
      } }; } }];
    }
  };
}

// ───────── 预测区间色带（target_lo~target_hi 画成 K 线右侧半透明色带 · 自绘图元）─────────
function makeBandPrimitive(){
  return {
    bands:null, chart:null, series:null, req:null,
    attached(p){ this.chart=p.chart; this.series=p.series; this.req=p.requestUpdate; },
    detached(){ this.chart=this.series=this.req=null; },
    setBands(bands){ this.bands=bands&&bands.length?bands:null; if(this.req) this.req(); },
    paneViews(){
      const self=this;
      return [{ renderer(){ return { draw(target){
        const bands=self.bands; if(!bands) return;
        target.useBitmapCoordinateSpace(scope=>{
          const ctx=scope.context, hr=scope.horizontalPixelRatio, vr=scope.verticalPixelRatio;
          const W=scope.bitmapSize.width;
          for(const b of bands){
            const yLo=self.series.priceToCoordinate(b.lo), yHi=self.series.priceToCoordinate(b.hi);
            if(yLo==null||yHi==null) continue;
            let x0=null;
            if(b.fromTime!=null){ const c=self.chart.timeScale().timeToCoordinate(b.fromTime); if(c!=null) x0=c*hr; }
            if(x0==null) x0=W*0.62;              // 起点兜底：画在图表右侧 38% 区域
            const top=Math.min(yHi,yLo)*vr, hgt=Math.abs(yLo-yHi)*vr;
            ctx.fillStyle=b.fill; ctx.fillRect(x0, top, W-x0, hgt);
            ctx.strokeStyle=b.edge; ctx.lineWidth=1*hr; ctx.setLineDash([4*hr,4*hr]);
            ctx.strokeRect(x0, top, W-x0, hgt); ctx.setLineDash([]);
            if(b.label){
              ctx.font='600 '+(10*vr)+'px Inter,-apple-system,PingFang SC,sans-serif';
              ctx.textAlign='left'; ctx.fillStyle=b.edge;
              ctx.fillText(b.label, x0+4*hr, top+11*vr);
            }
          }
        });
      } }; } }];
    }
  };
}
let bandPrim=null;
function drawBands(){
  if(!chart||!candleSeries) return;
  if(!bandPrim){ bandPrim=makeBandPrimitive(); try{ candleSeries.attachPrimitive(bandPrim); }catch(e){ bandPrim=null; return; } }
  const bands=[];
  const rows=(lastKline&&lastKline.rows)||[];
  // 起点=倒数第 12 根 bar，让色带盖住最近走势的右侧延伸区
  const fromTime=rows.length>12?barTime(rows[rows.length-12]):null;
  if(VIS.core){
    const hz=(lastHorizons&&lastHorizons.symbol===sym&&lastHorizons.horizons)||{};
    for(const h of ['15','30']){
      const p=hz[h];
      if(!p||!HZVIS[h]||p.target_lo==null||p.target_hi==null) continue;
      const up=p.direction==='涨', down=p.direction==='跌';
      const rgb=p.tradeable?(up?'16,214,140':(down?'255,85,112':'139,149,167')):'139,149,167';
      bands.push({lo:+p.target_lo, hi:+p.target_hi, fromTime,
        fill:`rgba(${rgb},0.07)`, edge:`rgba(${rgb},0.4)`,
        label:h+'天 p10~p90'+(p.tradeable?'':'·参考')});
    }
  }
  bandPrim.setBands(bands);
}

let vtopPrim=null;
function drawPattern(){
  if(!chart||!candleSeries||!lastKline) return;
  if(!vtopPrim){ vtopPrim=makeVtopPrimitive(); try{ candleSeries.attachPrimitive(vtopPrim); }catch(e){ vtopPrim=null; return; } }
  const v=detectVtop(lastKline.rows||[]);
  vtopPrim.setPts(v);   // 无形态时传 null，渲染器自动跳过
}

// ───────── Binance WebSocket 逐 tick 实时 K 线 ─────────
let ws=null, wsRetry=0, wsWanted='', wsStatus='连接实时行情…';
function wsLive(){ return ws && ws.readyState===1; }
function renderLive(){
  const el=$('nextIn'); if(!el) return;
  el.textContent = wsStatus + ' · 决策 ' + (countdown>0?countdown+'s 后刷新':'刷新中…');
}
function setWsStatus(t){ wsStatus=t; renderLive(); }
function closeWS(){ if(ws){ try{ ws.onclose=null; ws.onerror=null; ws.close(); }catch(e){} ws=null; } }
function openWS(){
  const want=sym+'|'+iv; wsWanted=want; closeWS();
  let sock;
  try{ sock=new WebSocket('wss://stream.binance.com:9443/ws/'+sym.toLowerCase()+'@kline_'+iv); }
  catch(e){ setWsStatus('🟡 实时不可用·轮询兜底'); return; }
  ws=sock;
  sock.onopen=()=>{ wsRetry=0; setWsStatus('🟢 实时 · Binance WS'); };
  sock.onmessage=(ev)=>{
    if(wsWanted!==want) return;                 // 期间已切币/切周期，忽略旧流
    let m; try{ m=JSON.parse(ev.data); }catch(e){ return; }
    const k=m.k; if(!k||!candleSeries) return;
    const t=Math.floor(k.t/1000)-TZ;
    const bar={time:t, open:+k.o, high:+k.h, low:+k.l, close:+k.c};
    try{ candleSeries.update(bar); }catch(e){}  // 逐 tick 更新当前蜡烛，丝滑不重画
    try{ if(volSeries) volSeries.update({time:t, value:+k.v||0, color:(+k.c>=+k.o)?'rgba(14,203,129,0.45)':'rgba(246,70,93,0.45)'}); }catch(e){}
    const pxEl=$('px'), prev=parseFloat((pxEl.textContent||'').replace(/,/g,''));
    pxEl.textContent=fmt(+k.c);
    if(!isNaN(prev)&&+k.c!==prev){ pxEl.classList.toggle('tick-up',+k.c>prev); pxEl.classList.toggle('tick-down',+k.c<prev); }
    $('upAt').textContent='实时 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  };
  sock.onerror=()=>{ try{ sock.close(); }catch(e){} };
  sock.onclose=()=>{ if(wsWanted===want){ wsRetry=Math.min(wsRetry+1,6); setWsStatus('🟡 实时重连中…'); setTimeout(()=>{ if(wsWanted===want) openWS(); }, 1000*wsRetry); } };
}

function nearestRow(tsMs){
  const rows=(lastKline&&lastKline.rows)||[]; if(!rows.length||!rows[0].ts) return null;
  let best=null,bd=Infinity;
  for(const r of rows){ const dd=Math.abs(r.ts-tsMs); if(dd<bd){bd=dd;best=r;} }
  return best;
}

function drawOverlay(){
  if(!chart||!candleSeries||!lastKline) return;
  const rows=lastKline.rows||[];
  const LS=LightweightCharts.LineStyle;
  priceLines.forEach(pl=>{ try{candleSeries.removePriceLine(pl);}catch(e){} }); priceLines=[];
  function addPL(price,color,title,style,w){
    if(price==null||isNaN(+price)) return;
    priceLines.push(candleSeries.createPriceLine({price:+price,color,lineWidth:(w||(style===LS.Dotted?1:2)),lineStyle:style,axisLabelVisible:true,title}));
  }
  const d=lastDec||{}, fac=lastFac||{}, e=entryMid(d), pos=(d.suggested_position_pct||0)>0;
  // 核心 3 线：只画当前有效决策的 入场/止损/止盈（历史线不画，保持清爽）
  if(VIS.core){
    if(pos){
      if(e) addPL(+e.toFixed(2),'#4f8cff','入 '+fmt(e),LS.Dashed);
      addPL(d.stop_loss,'#ff5570','损 '+fmt(d.stop_loss),LS.Solid);
      addPL(d.take_profit_ref,'#10d68c','盈 '+fmt(d.take_profit_ref),LS.Solid);
    }
    // 4h 盘中引擎最新预测：达标画彩色实点位；未达标画灰色参考线（观察模式，不开仓）
    const ip=intradayPredOf(sym);
    if(ip&&HZVIS['4h']){
      if(ip.tradeable&&ip.stop&&ip.take){
        addPL(ip.stop,'rgba(246,70,93,0.75)','4h损 '+fmt(ip.stop),LS.Dotted,1);
        addPL(ip.take,'rgba(14,203,129,0.75)','4h盈 '+fmt(ip.take),LS.Dotted,1);
      }else if(ip.stop&&ip.take){
        addPL(ip.stop,'rgba(135,148,166,0.6)','4h损·参考 '+fmt(ip.stop),LS.Dotted,1);
        addPL(ip.take,'rgba(135,148,166,0.6)','4h盈·参考 '+fmt(ip.take),LS.Dotted,1);
      }else if(ip.direction==='震荡'&&ip.entry&&ip.atr_pct){
        // 震荡无方向点位：画 ±0.5×ATR 预计波动区间（与标签阈值同口径），灰色仅参考
        const half=ip.entry*(ip.atr_pct/100)*0.5;
        addPL(ip.entry+half,'rgba(135,148,166,0.55)','4h区间上·参考 '+fmt(ip.entry+half),LS.Dotted,1);
        addPL(ip.entry-half,'rgba(135,148,166,0.55)','4h区间下·参考 '+fmt(ip.entry-half),LS.Dotted,1);
      }
    }
    // 15/30 天中线目标：达标按方向着色，未达标灰色参考（分位回归 p50 + p10~p90 区间）
    const hz=(lastHorizons&&lastHorizons.symbol===sym&&lastHorizons.horizons)||{};
    for(const h of ['15','30']){
      const p=hz[h];
      if(!p||p.target==null||!HZVIS[h]) continue;
      const col=p.tradeable
        ?(p.direction==='涨'?'rgba(14,203,129,0.8)':(p.direction==='跌'?'rgba(246,70,93,0.8)':'rgba(132,142,156,0.7)'))
        :'rgba(132,142,156,0.5)';
      addPL(p.target,col,h+'天目标'+(p.tradeable?'':'·参考')+' '+fmt(p.target),LS.Dashed,1);
      // p10~p90 分位区间边界：更淡的虚点线，展示预测的不确定幅度
      const bandCol=p.tradeable?col.replace(/0\.[78]\)$/,'0.35)'):'rgba(132,142,156,0.28)';
      if(p.target_hi!=null) addPL(p.target_hi,bandCol,h+'天区间上 '+fmt(p.target_hi),LS.Dotted,1);
      if(p.target_lo!=null) addPL(p.target_lo,bandCol,h+'天区间下 '+fmt(p.target_lo),LS.Dotted,1);
    }
  }
  if(fac.price) addPL(+fac.price,'#8b95a7','现价',LS.Dotted,1);
  // 自动支撑/阻力（摆动点聚类 · 只留触碰最多的 3 档，避免糊成一团）
  if(VIS.sr){
  const win=rows.slice(-Math.min(rows.length,120));
  if(win.length>10){
    const k=Math.max(2,Math.round(win.length/24));
    const piv=[...pivots(win,k,'high'),...pivots(win,k,'low')].map(p=>p.v).sort((a,b)=>a-b);
    const span=(Math.max(...win.map(r=>r.h))-Math.min(...win.map(r=>r.l)))||1;
    const tol=span*0.012;
    const cl=[];
    for(const p of piv){ const tail=cl[cl.length-1];
      if(tail&&Math.abs(p-tail.level)<=tol){ tail.sum+=p; tail.cnt++; tail.level=tail.sum/tail.cnt; }
      else cl.push({sum:p,cnt:1,level:p}); }
    const cur=(fac.price!=null?+fac.price:rows[rows.length-1].c);
    cl.filter(c=>c.cnt>=2).sort((a,b)=>b.cnt-a.cnt).slice(0,3).forEach(c=>{
      const isRes=c.level>=cur;
      const strong=Math.min(c.cnt,5)/5;                                   // 触碰强度 0..1
      const base=isRes?'246,70,93':'14,203,129';
      addPL(+c.level.toFixed(2), 'rgba('+base+','+(0.3+0.5*strong).toFixed(2)+')',
            (isRes?'阻力':'支撑')+'·'+c.cnt+'触', LS.Dashed, 1+Math.round(2*strong));
    });
  }
  }
  // 模拟买卖点 ▲▼（只留最近 20 个）
  let mks=[];
  if(VIS.marks){
  (lastPositions||[]).forEach(p=>{
    if(p.opened_ts&&p.entry_price){ const r=nearestRow(p.opened_ts*1000); if(r) mks.push({time:barTime(r),position:'belowBar',color:'#10d68c',shape:'arrowUp',text:'买'}); }
    if(p.status==='closed'&&p.closed_ts&&p.exit_price){ const r=nearestRow(p.closed_ts*1000); if(r){ const isWin=(p.realized_pnl_usdt||0)>=0; mks.push({time:barTime(r),position:'aboveBar',color:isWin?'#10d68c':'#ff5570',shape:'arrowDown',text:'卖'}); } }
  });
  mks.sort((a,b)=>a.time-b.time);
  mks=mks.slice(-20);
  }
  if(markersPrim) markersPrim.setMarkers(mks);
  drawBands();
}

// ───────── 4h 盘中引擎：最新预测徽标 + K 线叠加 ─────────
function intradayPredOf(s){
  const ps=(lastIntraday&&lastIntraday.recent_predictions)||[];
  return ps.find(p=>p.symbol===s)||null;
}
function renderPredBadge(){
  const el=$('predBadge'); if(!el) return;
  const dirColor=d=>d==='涨'?'#10d68c':(d==='跌'?'#ff5570':'#8b95a7');
  const seg=[];
  const ip=intradayPredOf(sym);
  if(ip){
    seg.push(`4h: <b style="color:${dirColor(ip.direction)}">${ip.direction}</b>`+
      (ip.prob!=null?` ${Math.round(ip.prob*100)}%`:'')+
      (ip.tradeable?'':` <span title="${(ip.reason||'')} — 模型未过精准度门禁，灰线仅参考、引擎不开仓">👁</span>`));
  }
  const hz=(lastHorizons&&lastHorizons.symbol===sym&&lastHorizons.horizons)||{};
  for(const h of ['15','30']){
    const p=hz[h];
    if(p&&p.direction){
      seg.push(`${h}天: <b style="color:${dirColor(p.direction)}">${p.direction}</b>`+
        (p.prob!=null?` ${Math.round(p.prob*100)}%`:'')+
        (p.target!=null?` ➜${fmt(p.target)}`:'')+
        (p.tradeable?'':` <span title="${(p.reason||'')} — 未过门禁，目标线仅灰色参考">👁</span>`));
    }else if(lastHorizons&&(lastHorizons.computing||[]).includes(h)){
      seg.push(`${h}天: <span style="color:#8b95a7">计算中…</span>`);
    }
  }
  el.innerHTML=seg.join(' <span style="color:var(--bd2)">|</span> ');
}
async function loadIntraday(){
  try{
    lastIntraday=await (await fetch('/api/intraday/stats')).json();
    renderPredBadge(); renderMhz();
    if(chart) drawOverlay();
  }catch(e){}
}
async function loadHorizons(){
  try{
    lastHorizons=await (await fetch('/api/horizons?symbol='+sym)).json();
    renderPredBadge(); renderMhz();
    if(chart) drawOverlay();
    // 计算中则 30s 后再拉一次（后台线程算完即有缓存）
    if((lastHorizons.computing||[]).length){ setTimeout(loadHorizons, 30000); }
  }catch(e){}
}
// ── 多周期预测卡：三行（4h/15天/30天）+ 每行归因 + AI 解读；点卡片开关该周期 K 线预测线 ──
// 「命中」按钮展开该周期的历史命中面板：4h=逐 bar 命中曲线（真实回填），15/30=OOS 验证摘要。
let HZHIST={};                       // key -> true 展开
let hitHistCache={sym:null,data:null,at:0};
function toggleHzHist(key){
  HZHIST[key]=!HZHIST[key];
  renderMhz();
  if(HZHIST[key] && key==='4h') loadHitHist();
}
async function loadHitHist(){
  const want=sym;
  if(hitHistCache.sym===want && Date.now()-hitHistCache.at<60000){ drawHitCanvas(); return; }
  try{
    const d=await (await fetch('/api/intraday/hits?symbol='+want+'&limit=120')).json();
    if(want!==sym || d.error) return;
    hitHistCache={sym:want,data:d,at:Date.now()};
    renderMhz();
  }catch(e){}
}
function drawHitCanvas(){
  const cv=$('hitcv'); if(!cv) return;
  const d=(hitHistCache.sym===sym&&hitHistCache.data)||null; if(!d) return;
  const dpr=window.devicePixelRatio||1, W=cv.clientWidth||300, H=cv.clientHeight||56;
  cv.width=W*dpr; cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr); ctx.clearRect(0,0,W,H);
  const roll=d.rolling||[], preds=(d.predictions||[]).filter(p=>p.hit!=null);
  if(!roll.length){ ctx.fillStyle='#8b95a7'; ctx.font='11px Inter,sans-serif'; ctx.fillText('暂无已回填的命中样本',8,H/2+4); return; }
  const padL=4,padR=4,padT=6,padB=12, iw=W-padL-padR, ih=H-padT-padB;
  const x=i=>padL+iw*(roll.length<=1?0.5:i/(roll.length-1));
  const y=r=>padT+ih*(1-r);
  // 50% 基准线
  ctx.strokeStyle='rgba(139,149,167,0.25)'; ctx.setLineDash([3,3]); ctx.beginPath();
  ctx.moveTo(padL,y(0.5)); ctx.lineTo(W-padR,y(0.5)); ctx.stroke(); ctx.setLineDash([]);
  // 滚动命中率曲线（近 20 次窗口）
  ctx.strokeStyle='#4f8cff'; ctx.lineWidth=1.5; ctx.beginPath();
  roll.forEach((r,i)=>{ i?ctx.lineTo(x(i),y(r.rate)):ctx.moveTo(x(i),y(r.rate)); });
  ctx.stroke();
  // 逐 bar 命中点：底部一排 绿=中 红=错
  const n=preds.length;
  preds.forEach((p,i)=>{
    const px=padL+iw*(n<=1?0.5:i/(n-1));
    ctx.fillStyle=p.hit===1?'#10d68c':'#ff5570';
    ctx.beginPath(); ctx.arc(px,H-5,2,0,Math.PI*2); ctx.fill();
  });
}
function hzHistPanel(key,p){
  if(!HZHIST[key]) return '';
  const stop='onclick="event.stopPropagation()"';
  if(key==='4h'){
    const d=(hitHistCache.sym===sym&&hitHistCache.data)||null;
    const sum=d&&d.n_evaluated
      ?`已回填 <b>${d.n_evaluated}</b> 次 · 总命中 <b>${d.hit_rate==null?'—':Math.round(d.hit_rate*100)+'%'}</b> · 蓝线=近20次滚动命中率 · 底部点=逐bar对错`
      :'加载中…';
    return `<div class="hzhist" ${stop}><canvas id="hitcv"></canvas><div class="hsum">${sum}</div></div>`;
  }
  // 15/30 天：无逐 bar 历史，展示 walk-forward 样本外验证摘要
  const hitTxt=(p&&p.oos_hit_rate!=null)?Math.round(p.oos_hit_rate*100)+'%':'—';
  const pTxt=(p&&p.p_value!=null)?p.p_value:'—';
  return `<div class="hzhist" ${stop}><div class="hsum" style="margin-top:0;line-height:1.8">`+
    `walk-forward 样本外命中率 <b>${hitTxt}</b> · 置换检验 p 值 <b>${pTxt}</b><br>`+
    `${(p&&p.tradeable)?'双指标达标，预测线按方向着色':'未达门禁（需命中率&gt;基线且 p&lt;0.05），K 线上只画灰色参考线'}</div></div>`;
}
function mhzRow(key, label, sub, p, tgt, calcHint){
  const on=HZVIS[key]!==false;
  const wrap=(inner)=>`<div class="hzitem${on?'':' off'}" onclick="toggleHz('${key}')" `+
    `title="点击${on?'隐藏':'显示'}该周期在 K 线上的预测线">${inner}</div>`;
  const hz=`<span class="hz">${label}<small>${sub}</small></span>`;
  const eye=`<span class="hzeye">${on?'👁':'—'}</span>`;
  if(!p||!p.direction){
    const hint=(p&&p.reason)?p.reason:(calcHint||'计算中，首次约需 1-2 分钟…');
    return wrap(`<div class="row">${hz}<span class="calcing"><span class="spin"></span>${hint}</span></div>`);
  }
  const cls=p.direction==='涨'?'dir-up':(p.direction==='跌'?'dir-down':'dir-flat');
  const clr=p.direction==='涨'?'var(--up)':(p.direction==='跌'?'var(--down)':'var(--mut)');
  const gate=p.tradeable
    ?'<span class="gate ok" title="通过精准度门禁：样本外命中率与置换检验双达标，引擎可自动模拟下单">已达标</span>'
    :`<span class="gate" title="${(p.reason||'')} — 未过精准度门禁，点位仅供参考，引擎不开仓">观察</span>`;
  const hist=`<span class="histbtn${HZHIST[key]?' on':''}" onclick="event.stopPropagation();toggleHzHist('${key}')" `+
    `title="展开/收起该周期的历史命中记录">命中</span>`;
  const prob=p.prob!=null
    ?`<span class="probwrap"><span class="probbar"><i style="width:${Math.round(p.prob*100)}%;background:${clr}"></i></span>`+
     `<span class="probtxt num">${Math.round(p.prob*100)}%</span></span>`:'';
  const band=(p.target_lo!=null&&p.target_hi!=null)
    ?`<div class="bandline">区间 <b class="num">${fmt(p.target_lo)}</b> ~ <b class="num">${fmt(p.target_hi)}</b>（p10~p90）</div>`:'';
  const why=p.why_text?`<div class="whyline">${p.why_text}</div>`:'';
  return wrap(`<div class="row">${hz}`+
    `<span class="dirchip ${cls}">${p.direction}</span>${prob}${gate}${hist}`+
    (tgt!=null?`<span class="tgt"><small>目标</small>${fmt(tgt)}</span>`:'')+eye+`</div>${band}${why}${hzHistPanel(key,p)}`);
}
function renderMhz(){
  const el=$('mhzRows'); if(!el) return;
  const ip=intradayPredOf(sym);
  const hz=(lastHorizons&&lastHorizons.symbol===sym&&lastHorizons.horizons)||{};
  const computing=(lastHorizons&&lastHorizons.computing)||[];
  el.innerHTML =
    mhzRow('4h','4h','盘中', ip, ip&&(ip.take||null), '等待 4h 引擎出预测（每根 4h K 线收盘后更新）') +
    mhzRow('15','15天','中线', hz['15'], hz['15']&&hz['15'].target, computing.includes('15')?undefined:'待计算') +
    mhzRow('30','30天','长线', hz['30'], hz['30']&&hz['30'].target, computing.includes('30')?undefined:'待计算');
  if(HZHIST['4h']) requestAnimationFrame(drawHitCanvas);
}
async function loadWhy(){
  try{
    const want=sym;
    const d=await (await fetch('/api/why?symbol='+sym)).json();
    if(want!==sym) return;
    const el=$('aiwhy'); if(!el) return;
    el.style.display='block';
    el.innerHTML=`<span class="tag">${d.engine==='llm'?'AI 解读':'本地解读'}</span>${(d.summary||'').replace(/</g,'&lt;')}`;
  }catch(e){}
}

async function loadEvents(){
  try{
    const d=await (await fetch('/api/events?symbol='+sym+'&limit=30')).json();
    const evs=d.events||[];
    $('evlist').innerHTML = evs.length ? evs.map(e=>
      `<div class="ev ${e.tone}"><div><div class="tt">${e.title}</div><div class="dd">${e.detail||''}</div></div><div class="t" style="margin-left:auto">${e.time}</div></div>`
    ).join('') : '<div class="empty">暂无模拟盘动作。点右下「自动跟盘」让贾维斯开始跟单。</div>';
  }catch(e){ $('evlist').innerHTML='<div class="empty">事件加载失败</div>'; }
}

async function loadStats(){
  try{
    const st=await (await fetch('/api/trader/status?symbol=')).json();
    lastPositions=await (await fetch('/api/positions?status=all')).json();
    const tp=st.total_pnl_usdt, eq=st.equity_change_pct;
    const it=lastIntraday||{};
    const hr=it.hit_rate_7d==null?'—':Math.round(it.hit_rate_7d*100)+'%';
    const halted=!!it.halted;
    $('strip').innerHTML=
      `<div class="stat"><div class="k">账户权益</div><div class="v">${fmt(st.equity_usdt)}U</div></div>`+
      `<div class="stat"><div class="k">总盈亏</div><div class="v ${tp>0?'pos':(tp<0?'neg':'')}">${tp>0?'+':''}${fmt(tp)}U</div></div>`+
      `<div class="stat"><div class="k">胜率</div><div class="v">${st.win_rate_pct==null?'—':st.win_rate_pct+'%'}</div></div>`+
      `<div class="stat"><div class="k">4h命中·7天</div><div class="v">${hr}</div></div>`+
      `<div class="stat"><div class="k">持仓/已平</div><div class="v">${st.open_positions}/${st.closed_trades}</div></div>`+
      `<div class="stat"><div class="k">4h引擎</div><div class="v ${halted?'neg':'pos'}">${halted?'🛑熔断':'🛡️正常'}</div></div>`+
      `<div class="holds">${(st.open_detail||[]).length?('持仓：'+st.open_detail.map(o=>o.symbol.replace('USDT','')+' @'+fmt(o.entry)+(o.unrealized_usdt!=null?(' '+(o.unrealized_usdt>=0?'+':'')+fmt(o.unrealized_usdt)+'U'):'')).join('，')):'当前无持仓'}</div>`+
      `<div class="actbtn"><button class="ghost" onclick="loadAll()">刷新</button><button onclick="runCycle()" id="cycBtn">▶ 自动跟盘一轮</button></div>`;
    if(chart) drawOverlay();
  }catch(e){ $('strip').innerHTML='<div class="empty">战绩加载失败</div>'; }
}

async function runCycle(){
  const b=$('cycBtn'); if(b){ b.textContent='跟盘中…(约10-30秒)'; b.disabled=true; }
  try{
    const syms=[...new Set([...SYMS, sym])].map(s=>s.replace('USDT','')).join(',');
    await fetch('/api/trader/cycle?symbols='+encodeURIComponent(syms),{method:'POST'});
  }catch(e){}
  if(b){ b.textContent='▶ 自动跟盘一轮'; b.disabled=false; }
  loadEvents(); loadStats();
}

function addMsg(t,who){ const m=document.createElement('div'); m.className='msg '+who; m.textContent=t; $('chat').appendChild(m); $('chat').scrollTop=$('chat').scrollHeight; return m; }
function quick(q){ $('qin').value=q; sendAsk(); }
async function sendAsk(){
  const q=$('qin').value.trim(); if(!q) return; $('qin').value='';
  addMsg(q,'u'); const a=addMsg('贾维斯思考中…','a');
  try{
    const d=await (await fetch('/api/ask?symbol='+sym+'&q='+encodeURIComponent(q),{method:'POST'})).json();
    a.textContent=(d.answer||'我暂时答不上来，换个问法试试。')+(d.engine==='llm'?' 🧠':'');
  }catch(e){ a.textContent='网络抖动，稍后再问我一次。'; }
}

async function loadSnapshot(){
  try{
    const snap=await (await fetch('/api/snapshot?symbol='+sym)).json();
    renderCopilot(snap);
    if(!wsLive()) $('upAt').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  }catch(e){ if(!wsLive()) $('upAt').textContent='刷新失败，下次重试'; }
}

// 侧栏（决策/事件/战绩）走 60s 轮询；K 线蜡烛由 WebSocket 实时推送，二者解耦。
// loadKline 在此仅用于刷新趋势线/档位（key 未变不会重画蜡烛、不扰动 WS）。
function refreshSide(){ loadSnapshot(); loadEvents(); loadIntraday(); loadHorizons(); loadStats(); loadKline(); loadWhy(); }
function loadAll(){ countdown=REFRESH; loadKline(); refreshSide(); if(HZHIST['4h']) loadHitHist(); }   // 切币/切周期/首次：重拉历史 K 线并重连 WS
window.addEventListener('resize',()=>{ if(HZHIST['4h']) requestAnimationFrame(drawHitCanvas); });
function startTimer(){ if(tick)clearInterval(tick); tick=setInterval(()=>{ countdown--; renderLive(); if(countdown<=0){ countdown=REFRESH; refreshSide(); } },1000); }

// ───────── 主动提醒中心：通知中心面板 + SSE 实时推送 + 浏览器系统通知 ─────────
const KIND_CN={signal_flip:'信号反转', price_level:'价格关键位', reentry:'割肉回升'};
let notifOpen=false, notifTabCur='events', notifUnread=0, notifES=null, notifESRetry=0;
let newRuleKind='signal_flip', _klLevels=[], _nrKlLabel='';

function esc(s){ return (''+(s??'')).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function renderBadge(){
  const b=$('bellBadge'); if(!b) return;
  b.style.display = notifUnread>0?'':'none';
  b.textContent = notifUnread>99?'99+':notifUnread;
}
async function refreshUnread(){
  try{ const d=await (await fetch('/api/alert-center/unread-count')).json();
    if(d.ok){ notifUnread=d.unread||0; renderBadge(); } }catch(e){}
}
function toggleNotif(){
  notifOpen=!notifOpen; $('notifPanel').style.display=notifOpen?'':'none';
  if(notifOpen) notifTab(notifTabCur);
}
document.addEventListener('click',(e)=>{
  if(!notifOpen) return;
  const p=$('notifPanel'), b=$('bellBtn');
  if(p&&!p.contains(e.target)&&b&&!b.contains(e.target)){ notifOpen=false; p.style.display='none'; }
});
function notifTab(t){
  notifTabCur=t;
  document.querySelectorAll('#notifPanel .ntab').forEach(el=>el.classList.toggle('on', el.getAttribute('data-ntab')===t));
  if(t==='events') renderNotifEvents();
  else if(t==='rules') renderNotifRules();
  else renderNotifNew();
}
async function renderNotifEvents(){
  const body=$('notifBody'); body.innerHTML='<div class="empty">加载中…</div>';
  try{
    const d=await (await fetch('/api/alert-center/events?limit=80')).json();
    if(!d.ok) throw 0;
    notifUnread=d.unread||0; renderBadge();
    const evs=d.events||[];
    body.innerHTML = evs.length ? evs.map(e=>
      `<div class="nev ${e.severity}${e.read?'':' unread'}">`+
      `<div class="nevhd"><span class="nevtt">${esc(e.title)}</span><span class="nevt">${e.time}</span></div>`+
      (e.detail?`<div class="nevdd">${esc(e.detail).replace(/\n/g,'<br>')}</div>`:'')+
      `</div>`).join('')
      : '<div class="empty">还没有提醒。点「＋新建提醒」创建你的第一条监控规则：<br>信号反转 / 价格关键位 / 割肉后回升。</div>';
    $('notifFoot').innerHTML =
      `<span class="nact" onclick="markAllRead()">全部标为已读</span>`+
      `<span class="nact" onclick="runAcCheck(this)">立即巡检一轮</span>`;
  }catch(e){ body.innerHTML='<div class="empty">加载失败，稍后重试</div>'; }
}
async function renderNotifRules(){
  const body=$('notifBody'); body.innerHTML='<div class="empty">加载中…</div>';
  try{
    const d=await (await fetch('/api/alert-center/rules')).json();
    if(!d.ok) throw 0;
    const rs=d.rules||[];
    body.innerHTML = rs.length ? rs.map(r=>
      `<div class="nrule">`+
      `<span class="nkind">${KIND_CN[r.kind]||r.kind}</span>`+
      `<div class="nrdesc">${esc(r.desc)}`+
      ((r.triggered_count||!r.enabled)?`<div class="nrmeta">${r.triggered_count?('已触发 '+r.triggered_count+' 次'):''}${!r.enabled?(r.triggered_count?' · ':'')+'已停用':''}</div>`:'')+
      `</div>`+
      `<label class="nsw" title="启用/停用"><input type="checkbox" ${r.enabled?'checked':''} onchange="toggleRule('${r.id}',this.checked)"><i></i></label>`+
      `<span class="ndel" onclick="delRule('${r.id}')" title="删除规则">✕</span>`+
      `</div>`).join('')
      : '<div class="empty">还没有监控规则，点「＋新建提醒」创建。</div>';
    $('notifFoot').innerHTML='<span style="font-size:10.5px;color:var(--mut);line-height:1.6">一次性规则触发后自动停用（可重新打开开关续用）；信号反转规则持续监控。</span>';
  }catch(e){ body.innerHTML='<div class="empty">加载失败，稍后重试</div>'; }
}
function renderNotifNew(){
  const body=$('notifBody');
  const k=newRuleKind;
  const kindBtns=Object.entries(KIND_CN).map(([id,cn])=>
    `<span class="nkbtn${k===id?' on':''}" onclick="newRuleKind='${id}';renderNotifNew()">${cn}</span>`).join('');
  let fields='';
  if(k==='signal_flip'){
    fields=
      `<div class="nfrow"><label>周期</label><select id="nrTf"><option value="4h">4小时（推荐）</option><option value="1h">1小时</option><option value="15m">15分钟</option><option value="1d">日线</option></select></div>`+
      `<div class="nfrow"><label>置信度门槛</label><select id="nrConf"><option value="0">全部方向变化都提醒</option><option value="0.5">≥50% 才提醒</option><option value="0.75">≥75%（只要强信号）</option></select></div>`+
      `<div class="nhint">盯住该币的十二套技术共识方向：看涨↔看跌互翻、信号建立、信号转中性都会立即通知。持续监控，每次变化都提醒——系统翻空时你不会再蒙在鼓里。</div>`;
  }else if(k==='price_level'){
    fields=
      `<div class="nfrow"><label>方向</label><select id="nrDir"><option value="above">涨破（向上穿越）</option><option value="below">跌破（向下穿越）</option></select></div>`+
      `<div class="nfrow"><label>目标价</label><input id="nrPrice" placeholder="如 100000" inputmode="decimal"></div>`+
      `<div class="nfrow"><label>重复提醒</label><select id="nrRepeat"><option value="0">触发一次后停用</option><option value="1">每次穿越都提醒</option></select></div>`+
      `<div class="nkl" id="nrKl"><span class="mut">系统关键位加载中…</span></div>`+
      `<div class="nhint">价格从另一侧「穿越」目标价才触发，创建时不会误报。⛰=系统阻力位（建议涨破提醒），🛟=系统支撑位（建议跌破提醒），点击一键填入。</div>`;
  }else{
    fields=
      `<div class="nfrow"><label>我的平仓价</label><input id="nrExit" placeholder="你割肉时的价格，如 92000" inputmode="decimal"></div>`+
      `<div class="nfrow"><label>确认幅度</label><select id="nrConfirm"><option value="0">站回即提醒</option><option value="0.5" selected>+0.5%（防插针）</option><option value="1">+1%</option><option value="2">+2%</option></select></div>`+
      `<div class="nfrow"><label>持仓方向</label><select id="nrSide"><option value="long">我平的是多单（等价格涨回来）</option><option value="short">我平的是空单（等价格跌回去）</option></select></div>`+
      `<div class="nhint">割肉后价格又涨回去却不知情？标记你的平仓价，价格重新站回该位置时立即提醒你评估再入场，不再错过反弹行情。</div>`;
  }
  body.innerHTML=
    `<div class="nform">`+
    `<div class="nkinds">${kindBtns}</div>`+
    `<div class="nfrow"><label>币种</label><input id="nrSym" value="${esc(sym.replace('USDT',''))}" placeholder="如 BTC"></div>`+
    fields+
    `<div class="nfrow"><label>备注</label><input id="nrNote" placeholder="可选，触发时随提醒展示"></div>`+
    `<button class="nsubmit" onclick="submitRule(this)">创建提醒规则</button>`+
    `</div>`;
  $('notifFoot').innerHTML='';
  if(k==='price_level') loadKlSuggest();
}
async function loadKlSuggest(){
  const el=$('nrKl'); if(!el) return;
  try{
    const s=($('nrSym')&&$('nrSym').value)||sym;
    const d=await (await fetch('/api/alert-center/key-levels?symbol='+encodeURIComponent(s))).json();
    if(!d.ok||!(d.levels||[]).length){ el.innerHTML='<span class="mut">暂无系统关键位建议（K线计算中或数据不足）</span>'; return; }
    _klLevels=d.levels.slice(0,6);
    el.innerHTML='<span class="mut">系统关键位（点击填入）：</span>'+_klLevels.map((l,i)=>
      `<span class="nklbtn" onclick="useKl(${i})" title="${esc(l.label)} · 来源 ${esc(l.source)}">${l.suggest_direction==='above'?'⛰':'🛟'} ${fmt(l.price)}</span>`).join('');
  }catch(e){ el.innerHTML='<span class="mut">关键位加载失败</span>'; }
}
function useKl(i){
  const l=_klLevels[i]; if(!l) return;
  const pe=$('nrPrice'); if(pe) pe.value=l.price;
  const de=$('nrDir'); if(de) de.value=l.suggest_direction;
  _nrKlLabel=(l.label||'')+(l.source?('·'+l.source):'');
}
async function submitRule(btn){
  const k=newRuleKind;
  const payload={kind:k, symbol:(($('nrSym')&&$('nrSym').value)||sym), note:($('nrNote')?$('nrNote').value:'')};
  if(k==='signal_flip'){
    payload.tf=$('nrTf').value; payload.min_confidence=parseFloat($('nrConf').value)||0;
  }else if(k==='price_level'){
    payload.target_price=parseFloat($('nrPrice').value);
    payload.direction=$('nrDir').value;
    payload.repeat=$('nrRepeat').value==='1';
    if(_nrKlLabel) payload.label=_nrKlLabel;
    if(!(payload.target_price>0)){ alert('请填写目标价'); return; }
  }else{
    payload.exit_price=parseFloat($('nrExit').value);
    payload.confirm_pct=parseFloat($('nrConfirm').value)||0;
    payload.side=$('nrSide').value;
    if(!(payload.exit_price>0)){ alert('请填写你的平仓价'); return; }
  }
  btn.disabled=true; btn.textContent='创建中…';
  try{
    const r=await fetch('/api/alert-center/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(!d.ok){ alert(d.reason||'创建失败'); }
    else{ _nrKlLabel=''; toast({title:'✅ 提醒规则已创建',detail:d.rule.desc,severity:'info'},false); notifTab('rules'); }
  }catch(e){ alert('网络异常，稍后再试'); }
  btn.disabled=false; btn.textContent='创建提醒规则';
}
async function toggleRule(id,on){
  try{ await fetch('/api/alert-center/rules/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:on})}); }catch(e){}
}
async function delRule(id){
  if(!confirm('删除这条提醒规则？')) return;
  try{ await fetch('/api/alert-center/rules/'+id,{method:'DELETE'}); renderNotifRules(); }catch(e){}
}
async function markAllRead(){
  try{
    await fetch('/api/alert-center/events/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})});
    notifUnread=0; renderBadge();
    if(notifTabCur==='events') renderNotifEvents();
  }catch(e){}
}
async function runAcCheck(el){
  if(el) el.textContent='巡检中…';
  try{
    const d=await (await fetch('/api/alert-center/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})).json();
    toast({title:`🔍 巡检完成：检查 ${d.checked} 条规则，触发 ${d.triggered} 条`,severity:'info'},false);
    if(notifTabCur==='events') renderNotifEvents();
  }catch(e){}
  if(el) el.textContent='立即巡检一轮';
}
// 浏览器系统通知（Notification API）
function browserNotifState(){ return ('Notification' in window)?Notification.permission:'unsupported'; }
function renderBrowserBtn(){
  const el=$('nbrowserBtn'); if(!el) return;
  const st=browserNotifState();
  el.textContent = st==='granted'?'🖥 系统通知已开':(st==='denied'?'🖥 系统通知被拒':'🖥 开启系统通知');
  el.classList.toggle('on', st==='granted');
}
async function enableBrowserNotif(){
  if(!('Notification' in window)){ alert('当前浏览器不支持系统通知'); return; }
  if(Notification.permission==='default'){ try{ await Notification.requestPermission(); }catch(e){} }
  else if(Notification.permission==='denied'){ alert('通知权限已被浏览器拒绝，请在地址栏左侧的站点设置里手动允许通知'); }
  renderBrowserBtn();
  if(Notification.permission==='granted'){
    try{ new Notification('贾维斯提醒中心',{body:'浏览器系统通知已开启，触发提醒时即使页面不在前台也会弹出。',tag:'jarvis-ac-test'}); }catch(e){}
  }
}
function sysNotify(ev){
  try{
    if(browserNotifState()!=='granted') return;
    const n=new Notification(ev.title,{body:(ev.detail||'').split('\n')[0].slice(0,120),tag:'jarvis-ac-'+(ev.id||Date.now())});
    n.onclick=()=>{ try{ window.focus(); if(!notifOpen) toggleNotif(); n.close(); }catch(e){} };
  }catch(e){}
}
// 页内 toast（点击展开通知中心）
function toast(ev,sys=true){
  const box=$('toasts'); if(!box) return;
  const el=document.createElement('div');
  el.className='toast '+(ev.severity||'info');
  el.innerHTML=`<div class="tt">${esc(ev.title)}</div>`+(ev.detail?`<div class="dd">${esc((ev.detail||'').split('\n')[0]).slice(0,160)}</div>`:'');
  el.onclick=()=>{ el.remove(); if(!notifOpen) toggleNotif(); };
  box.appendChild(el);
  while(box.children.length>4) box.removeChild(box.firstChild);
  setTimeout(()=>{ el.classList.add('out'); setTimeout(()=>{ try{el.remove();}catch(e){} },420); },8000);
  if(sys) sysNotify(ev);
}
// SSE：后端触发提醒实时推到页面（断线自动重连，兜底靠未读数轮询）
function openNotifStream(){
  if(notifES){ try{ notifES.close(); }catch(e){} notifES=null; }
  let es;
  try{ es=new EventSource('/api/alert-center/stream'); }catch(e){ return; }
  notifES=es;
  es.onmessage=(m)=>{
    let ev; try{ ev=JSON.parse(m.data); }catch(e){ return; }
    if(ev.type==='hello'){ notifESRetry=0; notifUnread=ev.unread||0; renderBadge(); return; }
    notifUnread++; renderBadge();
    toast(ev,true);
    if(notifOpen&&notifTabCur==='events') renderNotifEvents();
  };
  es.onerror=()=>{
    try{ es.close(); }catch(e){}
    if(notifES===es){ notifES=null; notifESRetry=Math.min(notifESRetry+1,6);
      setTimeout(openNotifStream, 3000*Math.max(notifESRetry,1)); }
  };
}
function initAlertCenter(){
  renderBadge(); refreshUnread(); renderBrowserBtn(); openNotifStream();
  setInterval(refreshUnread, 90000);   // SSE 断连期间的未读数兜底
}

renderChips();
renderVisBtns();
addMsg('你好！我是贾维斯。问我「现在该买什么」「卖多少」「为什么这么判断」，或直接点下面的快捷问题。','a');
loadWatchlist().then(()=>{ loadAll(); });
startTimer();
initAlertCenter();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯可视化仪表盘")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7899)
    args = ap.parse_args()
    # access_log=False：逐请求耗时由 _access_timing 中间件统一记录，避免与
    # uvicorn 自带访问日志重复刷屏。
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
