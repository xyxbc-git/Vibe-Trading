#!/usr/bin/env python3
"""贾维斯 JARVIS — 执行手（M1）：把决策简报下成 QuantDinger 的 paper 单。

闭环最后一跳：jarvis_brief 出交易计划 → 机器化风控护栏 → 经 QuantDinger
Agent Gateway(/api/agent/v1) 下**模拟盘**单 → 落审计 → 监控面板可见。

默认 **paper-only**，绝不碰真钱：服务端 token `paper_only=true` + 部署级
`AGENT_LIVE_TRADING_ENABLED` 双开关，本脚本只走模拟单路径。

护栏（来自整合计划 §5 安全红线 + M1）：
  1. 只在「偏多（战术）」且信心分达阈值、建议仓位>0 时才动手；其余一律观望不下单。
  2. 必须带硬止损，否则拒绝下单。
  3. 单笔仓位上限封顶（max_position_pct）。
  4. 组合最大风险（仓位×止损幅度）≤ max_portfolio_risk_pct，超了自动缩仓。
  5. kill-switch 一键撤掉本账户所有未成交模拟单。
  6. 幂等键 = symbol + 决策日期，防止同一计划重复下单。

配置优先级：CLI 参数 > 环境变量 > ~/.vibe-trading/executor_config.json > 内置默认。
敏感的 agent token **不硬编码**，从 env `QUANTDINGER_AGENT_TOKEN` 或配置文件读。

用法：
  export QUANTDINGER_AGENT_TOKEN=qd_agent_xxx
  python jarvis_executor.py BTCUSDT                 # 出计划 + 下 paper 单
  python jarvis_executor.py BTCUSDT --dry-run       # 只演练护栏，不真下单
  python jarvis_executor.py BTCUSDT --equity 5000   # 覆盖账户权益（用于 sizing）
  python jarvis_executor.py --kill-switch           # 急停：撤所有未成交模拟单
  python jarvis_executor.py BTCUSDT --json          # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid

import requests

import jarvis_brief as jb

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
CONFIG_PATH = os.path.join(CONFIG_DIR, "executor_config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_executor.log")
STATUS_PATH = os.path.join(CONFIG_DIR, "jarvis_executor_status.json")

# 内置默认（保守）。可被配置文件 / 环境变量 / CLI 覆盖。
DEFAULTS = {
    "gateway_base": "http://localhost:8888",
    "agent_token": "",            # 切勿在此硬编码；走 env / 配置文件
    "market": "Crypto",           # QuantDinger 市场名（首字母大写）
    "account_equity_usdt": 1000.0,
    "max_position_pct": 40.0,      # 单笔仓位上限（与 brief 弱因子上限一致）
    "max_portfolio_risk_pct": 1.5, # 组合最大风险红线
    "min_conviction": 0.8,         # 信心分门槛（与 brief 偏多触发一致）
    "stop_loss_drop_pct": 10.0,    # 用于 sizing 的止损幅度（brief 硬止损 -10%）
    "sizing_method": "fixed",      # [T-11] fixed=固定比例（默认）| kelly=分数凯利
    "kelly_fraction": 0.5,         # [T-11] 分数凯利系数（越小越保守）
    "request_timeout_s": 30,
}

LONG_PREFIX = "偏多"  # brief 方向："偏多（战术）"


# ─────────────────────────── 配置 / 日志 ───────────────────────────

def _log(msg: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 日志失败不应中断主流程
        pass


def load_config(cli: dict | None = None) -> dict:
    """合并 内置默认 < 配置中心(T-15) < executor 配置文件 < 环境变量 < CLI。"""
    cfg = dict(DEFAULTS)
    # [T-15] 统一配置中心的共享风控旋钮作为基线（默认=内置默认，零回归）。
    try:
        import jarvis_config as jcfg
        C = jcfg.load()
        for k in ("max_position_pct", "max_portfolio_risk_pct", "min_conviction",
                  "stop_loss_drop_pct", "account_equity_usdt",
                  "sizing_method", "kelly_fraction"):
            if k in C:
                cfg[k] = C[k]
    except Exception as exc:  # noqa: BLE001 — 配置中心异常不拖垮执行手
        _log(f"⚠️ 读取配置中心失败（用内置默认继续）: {exc}")
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if v is not None})
    except Exception as exc:  # noqa: BLE001
        _log(f"⚠️ 读取配置文件失败（用默认继续）: {exc}")

    env_map = {
        "gateway_base": "QUANTDINGER_GATEWAY_BASE",
        "agent_token": "QUANTDINGER_AGENT_TOKEN",
        "market": "QUANTDINGER_MARKET",
        "account_equity_usdt": "JARVIS_ACCOUNT_EQUITY",
    }
    for key, env in env_map.items():
        val = os.getenv(env)
        if val:
            cfg[key] = float(val) if key == "account_equity_usdt" else val

    if cli:
        cfg.update({k: v for k, v in cli.items() if v is not None})
    return cfg


# ─────────────────────────── 护栏 ───────────────────────────

def evaluate_guardrails(decision: dict, cfg: dict) -> dict:
    """对 brief 决策套用机器化风控，返回是否放行 + 最终下单参数。

    返回 {action, reason, side, position_pct, qty, entry_price, stop_loss,
          take_profit, projected_risk_pct, clamped}。
    action ∈ {"place", "skip"}。
    """
    direction = (decision.get("direction") or "").strip()
    score = float(decision.get("conviction_score") or 0.0)
    pos_pct = float(decision.get("suggested_position_pct") or 0.0)
    stop_loss = decision.get("stop_loss")
    take_profit = decision.get("take_profit_ref")

    # 1. 方向门禁：只做「偏多（战术）」。
    if not direction.startswith(LONG_PREFIX):
        return {"action": "skip", "reason": f"方向为「{direction or '未知'}」，非偏多 → 观望不下单"}

    # 1b. 信心分门槛。
    if score < float(cfg["min_conviction"]):
        return {"action": "skip", "reason": f"信心分 {score} < 阈值 {cfg['min_conviction']} → 观望"}

    # 1c. 建议仓位为 0。
    if pos_pct <= 0:
        return {"action": "skip", "reason": "建议仓位为 0% → 观望不下单"}

    # 2. 必须有硬止损。
    if not stop_loss:
        return {"action": "skip", "reason": "决策缺少硬止损 → 拒绝下单（护栏 2）"}

    entry_price = _entry_price(decision)
    if not entry_price or entry_price <= 0:
        return {"action": "skip", "reason": "无有效入场价（行情取数失败？）→ 暂不下单"}

    # [T-11] 动态仓位：sizing_method=kelly 时按胜率+盈亏比用分数凯利重算建议仓位。
    # 凯利只会更保守或相近，绝不突破下方的仓位上限/组合风险红线（红线永远最后封顶）。
    sizing_info: dict = {"method": str(cfg.get("sizing_method", "fixed")), "applied": False}
    if str(cfg.get("sizing_method", "fixed")) == "kelly":
        ev = decision.get("expected_value") or {}
        try:
            import jarvis_sizing as js
            sug = js.suggest_position_pct(
                ev.get("win_prob"), ev.get("take_profit_pct"), ev.get("stop_loss_pct"),
                method="kelly", kelly_fraction=float(cfg.get("kelly_fraction", 0.5)),
                cap_pct=float(cfg["max_position_pct"]), fixed_pct=pos_pct,
            )
            if sug.get("position_pct") is not None and "fixed" not in str(sug.get("method", "")):
                sizing_info = {
                    "method": "kelly", "applied": True, "fixed_pct": pos_pct,
                    "kelly_pct": sug["position_pct"], "kelly_star": sug.get("kelly_star"),
                    "reason": sug.get("reason"),
                }
                pos_pct = sug["position_pct"]
        except Exception:  # noqa: BLE001 — sizing 异常回退固定比例
            sizing_info = {"method": "fixed(fallback)", "applied": False, "reason": "凯利计算异常"}

    if pos_pct <= 0:
        return {"action": "skip", "reason": "动态仓位建议≈0（负 edge / 凯利归零）→ 观望不下单"}

    # 3. 单笔仓位上限封顶。
    clamped = False
    if pos_pct > float(cfg["max_position_pct"]):
        pos_pct = float(cfg["max_position_pct"])
        clamped = True

    # 4. 组合风险红线：仓位% × 止损幅度% / 100 ≤ max_portfolio_risk_pct。
    stop_drop = abs(entry_price - float(stop_loss)) / entry_price * 100.0
    projected_risk = pos_pct * stop_drop / 100.0
    max_risk = float(cfg["max_portfolio_risk_pct"])
    if projected_risk > max_risk and stop_drop > 0:
        pos_pct = round(max_risk / stop_drop * 100.0, 2)
        projected_risk = max_risk
        clamped = True

    if pos_pct <= 0:
        return {"action": "skip", "reason": "缩仓后仓位≈0 → 不下单"}

    # 5. sizing：名义价值 = 权益 × 仓位% → qty = 名义 / 入场价。
    notional = float(cfg["account_equity_usdt"]) * pos_pct / 100.0
    qty = round(notional / entry_price, 8)
    if qty <= 0:
        return {"action": "skip", "reason": "下单数量≈0（权益过小或币价过高）→ 不下单"}

    # [T-07] 下单前滑点预估：按名义额 + 币种流动性给出预估冲击成本与成交价。
    slippage = None
    try:
        import jarvis_slippage as js
        sym = decision.get("symbol") or cfg.get("_symbol") or ""
        slippage = js.estimate_slippage_pct(sym, notional, side="buy")
        slippage["est_fill_price"] = js.apply_fill_price(entry_price, "buy", slippage.get("one_way_bps", 0))
    except Exception:  # noqa: BLE001 — 滑点预估失败不影响下单
        slippage = None

    return {
        "action": "place",
        "reason": "护栏通过",
        "side": "buy",
        "position_pct": pos_pct,
        "qty": qty,
        "notional_usdt": round(notional, 2),
        "entry_price": entry_price,
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit) if take_profit else None,
        "projected_risk_pct": round(projected_risk, 3),
        "clamped": clamped,
        "sizing": sizing_info,
        "slippage": slippage,
    }


def _entry_price(decision: dict) -> float | None:
    """从决策取参考入场价：优先 entry_zone 中点，回退 stop_loss/0.9 推回。"""
    zone = decision.get("entry_zone")
    if isinstance(zone, str) and "~" in zone:
        try:
            lo, hi = (float(x.strip()) for x in zone.split("~"))
            return round((lo + hi) / 2.0, 2)
        except Exception:  # noqa: BLE001
            pass
    sl = decision.get("stop_loss")
    if sl:
        return round(float(sl) / 0.90, 2)  # brief 硬止损 = price*0.9
    return None


# ─────────────────────────── Agent Gateway 调用 ───────────────────────────

def _headers(cfg: dict, idem: str | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {cfg['agent_token']}",
        "Content-Type": "application/json",
    }
    if idem:
        h["Idempotency-Key"] = idem
    return h


def place_paper_order(cfg: dict, symbol: str, order: dict, idem_key: str) -> dict:
    url = f"{cfg['gateway_base'].rstrip('/')}/api/agent/v1/quick-trade/orders"
    payload = {
        "market": cfg["market"],
        "symbol": symbol,
        "side": order["side"],
        "qty": order["qty"],
        "order_type": "market",
    }
    resp = requests.post(
        url, headers=_headers(cfg, idem_key), json=payload,
        timeout=int(cfg["request_timeout_s"]),
    )
    return _parse(resp)


def kill_switch(cfg: dict) -> dict:
    url = f"{cfg['gateway_base'].rstrip('/')}/api/agent/v1/quick-trade/kill-switch"
    resp = requests.post(url, headers=_headers(cfg), json={}, timeout=int(cfg["request_timeout_s"]))
    return _parse(resp)


def _parse(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"raw": resp.text[:500]}
    return {"http": resp.status_code, "body": body}


# ─────────────────────────── 主流程 ───────────────────────────

def execute(symbol: str, cfg: dict, dry_run: bool = False) -> dict:
    """出计划 → 护栏 → 下单（或演练）。永不抛出，异常落到 result.error。"""
    result: dict = {"symbol": symbol, "dry_run": dry_run, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not cfg.get("agent_token") and not dry_run:
        result["error"] = "未配置 agent_token（设 env QUANTDINGER_AGENT_TOKEN 或写入配置文件）"
        _log(f"❌ {symbol} {result['error']}")
        return result

    # T-09 组合级熔断门禁：触发则全局停单（paper-only，异常放行）
    if not dry_run:
        try:
            import jarvis_circuit_breaker as _cb
            _g = _cb.guard_new_order(cfg)
            if not _g.get("allow"):
                result["error"] = "熔断生效，停止下单：" + str(_g.get("reason"))
                result["circuit_breaker"] = _g
                _log("熔断阻断下单 " + str(symbol) + "：" + str(_g.get("reason")))
                return result
        except Exception as _exc:  # noqa: BLE001
            _log("熔断检查异常（放行 paper-only）：" + repr(_exc)[:160])

    try:
        brief = jb.build(symbol)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"构建决策简报失败: {exc!r}"[:300]
        _log(f"❌ {symbol} {result['error']}")
        return result

    decision = brief.get("decision", {})
    if "_error" in decision:
        result["error"] = f"决策不可用: {decision['_error']}"
        _log(f"⚠️ {symbol} {result['error']}")
        return result

    result["decision"] = {
        "direction": decision.get("direction"),
        "conviction_score": decision.get("conviction_score"),
        "suggested_position_pct": decision.get("suggested_position_pct"),
        "entry_zone": decision.get("entry_zone"),
        "stop_loss": decision.get("stop_loss"),
        "take_profit_ref": decision.get("take_profit_ref"),
    }

    cfg["_symbol"] = symbol  # [T-07] 供滑点预估识别币种流动性分层
    guard = evaluate_guardrails(decision, cfg)
    result["guardrails"] = guard

    if guard["action"] == "skip":
        _log(f"⏭️ {symbol} 不下单：{guard['reason']}")
        result["placed"] = False
        _write_status(result)
        return result

    _log(
        f"✅ {symbol} 护栏通过 → 拟下 {guard['side']} {guard['qty']} "
        f"(仓位 {guard['position_pct']}% / 名义 {guard['notional_usdt']}U / "
        f"风险 {guard['projected_risk_pct']}%{' [已缩仓]' if guard['clamped'] else ''})"
    )

    if dry_run:
        result["placed"] = False
        result["note"] = "dry-run：护栏通过但未真实下单"
        _log(f"🧪 {symbol} dry-run，跳过真实下单")
        _write_status(result)
        return result

    # 幂等键 = symbol + 决策日期，同一计划重复跑不重复下单。
    as_of = brief.get("factor_state", {}).get("as_of") or time.strftime("%Y-%m-%d")
    idem_key = f"jarvis-{symbol}-{as_of}"
    try:
        order_symbol = symbol if symbol.endswith("USDT") else symbol + "USDT"
        resp = place_paper_order(cfg, order_symbol, guard, idem_key)
        result["gateway_response"] = resp
        ok = resp.get("http") == 200 and (resp.get("body", {}).get("code") == 0)
        data = (resp.get("body") or {}).get("data") or {}
        result["placed"] = bool(ok)
        result["order_uid"] = data.get("order_uid")
        result["fill_price"] = data.get("fill_price")
        result["order_status"] = data.get("status")
        if ok:
            _log(
                f"📨 {symbol} 已下 paper 单 uid={data.get('order_uid')} "
                f"status={data.get('status')} fill={data.get('fill_price')}"
            )
            # M2 闭环对账：登记订单号关联（决策参考价 ↔ 真实成交），失败不影响下单。
            try:
                import jarvis_reconcile as jr
                lk = jr.link_order(
                    order_uid=data.get("order_uid"), symbol=order_symbol,
                    as_of_date=as_of, side=guard["side"], qty=guard["qty"],
                    decision_price=guard.get("entry_price"), status=data.get("status"),
                )
                result["reconcile_link"] = lk
            except Exception as exc:  # noqa: BLE001
                _log(f"⚠️ {symbol} 对账登记失败（不影响下单）: {exc!r}"[:200])
        else:
            _log(f"⚠️ {symbol} 下单返回非成功: http={resp.get('http')} body={resp.get('body')}")
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"下单请求异常: {exc!r}"[:300]
        result["placed"] = False
        _log(f"❌ {symbol} {result['error']}")

    _write_status(result)
    return result


def _write_status(result: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="贾维斯执行手：决策简报 → QuantDinger paper 单")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT", help="币种，如 BTCUSDT")
    ap.add_argument("--dry-run", action="store_true", help="只跑护栏不真实下单")
    ap.add_argument("--kill-switch", action="store_true", help="急停：撤所有未成交模拟单")
    ap.add_argument("--equity", type=float, default=None, help="覆盖账户权益(USDT)用于 sizing")
    ap.add_argument("--gateway", default=None, help="覆盖 Agent Gateway base URL")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = ap.parse_args()

    cli = {}
    if args.equity is not None:
        cli["account_equity_usdt"] = args.equity
    if args.gateway:
        cli["gateway_base"] = args.gateway
    cfg = load_config(cli)

    if args.kill_switch:
        if not cfg.get("agent_token"):
            print("❌ 未配置 agent_token，无法急停")
            return
        res = kill_switch(cfg)
        _log(f"🛑 kill-switch: {res}")
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else f"kill-switch → {res}")
        return

    res = execute(args.symbol, cfg, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        g = res.get("guardrails", {})
        print(f"\n=== 执行结果 {res['symbol']} ===")
        print(f"决策: {res.get('decision')}")
        print(f"护栏: {g.get('action')} — {g.get('reason')}")
        if res.get("placed"):
            print(f"已下单: uid={res.get('order_uid')} status={res.get('order_status')} fill={res.get('fill_price')}")
        elif res.get("error"):
            print(f"错误: {res['error']}")


if __name__ == "__main__":
    main()
