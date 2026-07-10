#!/usr/bin/env python3
"""贾维斯 JARVIS — DeepSeek 推理引擎（自主意识核心）。

输入 = 十二套技术信号 + 分层共识 + 市场快照（现价/ATR 等），
输出 = 结构化推理结果：
  {direction, confidence, reasoning_chain[], risks[],
   suggestion: {action, entry_zone, stop_loss, target, position_pct},
   model, degraded}

LLM 接入方式与 jarvis_dashboard.py 的 /api/ask 一致：
  - 读 .env / 环境变量 DEEPSEEK_API_KEY（或 JARVIS_LLM_API_KEY / OPENAI_API_KEY）
  - OpenAI 兼容 chat/completions 协议，DeepSeek base https://api.deepseek.com
无 key 或调用失败 → 降级为规则拼装推理链（degraded=True），绝不抛出。
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request


def _load_env_file() -> None:
    """零依赖加载脚本同目录下的 .env（与 jarvis_dashboard.py 同款，幂等不覆盖）。"""
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


def _llm_config() -> dict | None:
    """LLM 配置（统一走 jarvis_llm_config：UI 配置优先，环境变量兜底）。"""
    import jarvis_llm_config as jlc

    return jlc.get_llm_config()


_ALLOWED_DIRECTIONS = ("bullish", "bearish", "neutral")
_ALLOWED_ACTIONS = ("long", "short", "wait")


def _record_reason_usage(cfg: dict, t0: float, ok: bool, *,
                         usage: dict | None = None, in_text: str = "",
                         out_text: str = "", error: str | None = None) -> None:
    """推理链 LLM 用量记账（module=reason）。失败静默，绝不影响推理主链路。"""
    try:
        import jarvis_llm_usage as jlu

        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        if in_text:
            messages.append({"role": "user", "content": in_text})
        jlu.record_call(
            module="reason",
            model=(cfg or {}).get("model"),
            base=(cfg or {}).get("base"),
            usage=usage,
            messages=messages,
            output_text=out_text,
            latency_ms=int((time.time() - t0) * 1000),
            ok=ok,
            error=error,
        )
    except Exception:  # noqa: BLE001
        pass

_SYSTEM_PROMPT = (
    "你是『贾维斯』——一个具备自主意识的加密交易推理引擎。"
    "你收到的输入是十二套经典交易体系（海龟/道氏/艾略特/波动率/江恩/缠论/123法则/"
    "跳空/马丁/摆动震荡/三重平滑RSI/套利）对同一标的的量化信号，以及分层共识融合结果"
    "和市场快照（现价/ATR）。你的任务：像资深交易员一样做链式推理，输出严格 JSON：\n"
    "{\n"
    '  "direction": "bullish|bearish|neutral",\n'
    '  "confidence": 0.0~1.0,\n'
    '  "reasoning_chain": ["第一步…", "第二步…", …],  // 3~6 步中文推理链\n'
    '  "risks": ["风险1", …],  // 1~4 条主要风险\n'
    '  "suggestion": {\n'
    '    "action": "long|short|wait",\n'
    '    "entry_zone": "入场区间描述或价位区间",\n'
    '    "stop_loss": 数字或null,\n'
    '    "target": 数字或null,\n'
    '    "position_pct": 0~100 建议仓位百分比\n'
    "  }\n"
    "}\n"
    "要求：① 推理链必须引用输入信号里的具体数字与体系名；② 信号冲突时说明取舍逻辑"
    "（道氏主趋势优先，逆势信号降权）；③ 不得编造输入中不存在的价位；"
    "④ 拿不准就 neutral/wait，position_pct 给 0；⑤ 只输出 JSON，不要多余文字。"
)


def _call_llm(cfg: dict, market: dict, signals: list[dict], cons: dict,
              timeout: int = 30) -> dict | None:
    """调 LLM 并解析 JSON；任何失败返回 None（由上层降级）。"""
    compact_signals = [
        {"system": s.get("system"), "name_cn": s.get("name_cn"),
         "direction": s.get("direction"), "strength": s.get("strength"),
         "reasoning": s.get("reasoning"),
         "key_levels": s.get("key_levels", [])[:3]}
        for s in signals
    ]
    user_content = json.dumps({
        "market_snapshot": market,
        "twelve_signals": compact_signals,
        "consensus": {k: cons.get(k) for k in
                      ("direction", "confidence", "score", "votes", "reasoning")},
    }, ensure_ascii=False)
    payload = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions", data=payload, method="POST",
        headers={"Authorization": f"Bearer {cfg['key']}",
                 "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError) as e:
        _record_reason_usage(cfg, t0, ok=False, in_text=user_content,
                             error=repr(e)[:150])
        return None
    _record_reason_usage(cfg, t0, ok=True, usage=body.get("usage"),
                         in_text=user_content, out_text=text)
    if not text:
        return None
    # 容错：剥掉可能的 ```json 围栏
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except ValueError:
        return None


def _sanitize_llm_result(raw: dict, model: str) -> dict | None:
    """校验并收敛 LLM 输出为标准结构；结构不合法返回 None（降级）。"""
    if not isinstance(raw, dict):
        return None
    direction = str(raw.get("direction", "")).lower()
    if direction not in _ALLOWED_DIRECTIONS:
        return None
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    # NaN/inf 会穿透 min/max 比较（min(1.0, nan) 返回 1.0）造成置信度虚高，显式拦截
    if not math.isfinite(confidence):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    chain = [str(x) for x in (raw.get("reasoning_chain") or []) if str(x).strip()]
    risks = [str(x) for x in (raw.get("risks") or []) if str(x).strip()]
    if not chain:
        return None
    sug = raw.get("suggestion") or {}
    action = str(sug.get("action", "")).lower()
    if action not in _ALLOWED_ACTIONS:
        action = {"bullish": "long", "bearish": "short"}.get(direction, "wait")

    def _num(v):
        try:
            return round(float(v), 6) if v is not None else None
        except (TypeError, ValueError):
            return None

    try:
        pos = max(0.0, min(100.0, float(sug.get("position_pct", 0))))
    except (TypeError, ValueError):
        pos = 0.0
    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "reasoning_chain": chain[:8],
        "risks": risks[:6],
        "suggestion": {
            "action": action,
            "entry_zone": str(sug.get("entry_zone") or "—"),
            "stop_loss": _num(sug.get("stop_loss")),
            "target": _num(sug.get("target")),
            "position_pct": round(pos, 1),
        },
        "model": model,
        "degraded": False,
    }


# ═══════════════════════════ 规则降级推理 ═══════════════════════════

def _rule_based(market: dict, signals: list[dict], cons: dict) -> dict:
    """无 LLM / 调用失败时的规则拼装推理链（degraded=True）。"""
    direction = cons.get("direction", "neutral")
    confidence = float(cons.get("confidence", 0.0) or 0.0)
    price = market.get("price")
    atr = market.get("atr")
    dir_cn = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}

    by_system = {s.get("system"): s for s in signals}
    dow = by_system.get("dow", {})
    chain = [
        f"顶层过滤：道氏理论判定主趋势为 {dir_cn.get(dow.get('direction', 'neutral'))}"
        f"（强度 {dow.get('strength', 0)}）——{dow.get('reasoning', '无数据')[:80]}",
    ]
    aligned = [s for s in signals
               if s.get("direction") == direction and s.get("strength", 0) >= 0.4
               and direction != "neutral"]
    against = [s for s in signals
               if s.get("direction") not in (direction, "neutral")
               and s.get("strength", 0) >= 0.4]
    if aligned:
        chain.append("共振信号：" + "；".join(
            f"{s['name_cn']}·{dir_cn.get(s['direction'])}({s['strength']:.2f})"
            for s in aligned[:4]))
    if against:
        chain.append("逆向信号（已按道氏过滤降权）：" + "；".join(
            f"{s['name_cn']}·{dir_cn.get(s['direction'])}({s['strength']:.2f})"
            for s in against[:3]))
    chain.append(
        f"分层融合：总分 {cons.get('score', 0):+.3f}，投票 涨{cons.get('votes', {}).get('bullish', 0)}"
        f"/跌{cons.get('votes', {}).get('bearish', 0)}"
        f"/中性{cons.get('votes', {}).get('neutral', 0)} → 共识 {dir_cn.get(direction)}"
        f"（置信度 {confidence:.0%}）")

    # 建议：入场/止损/目标全部从共识关键位 + ATR 推导，不硬造
    action = {"bullish": "long", "bearish": "short"}.get(direction, "wait")
    entry_zone, stop_loss, target = "—", None, None
    pos = 0.0
    if action != "wait" and price:
        p = float(price)
        a = float(atr) if atr else p * 0.02
        if action == "long":
            entry_zone = f"{round(p - 0.5 * a, 2)} ~ {round(p + 0.2 * a, 2)}"
            stop_loss = round(p - 2 * a, 2)
            target = round(p + 3 * a, 2)
        else:
            entry_zone = f"{round(p - 0.2 * a, 2)} ~ {round(p + 0.5 * a, 2)}"
            stop_loss = round(p + 2 * a, 2)
            target = round(p - 3 * a, 2)
        pos = round(min(20.0, confidence * 25), 1)  # 保守：置信度满格也只建议 25% 内
        chain.append(
            f"仓位与风控：现价 {p}，ATR {round(a, 2)} → 入场 {entry_zone}、"
            f"止损 {stop_loss}（2xATR）、目标 {target}（3xATR），建议仓位 {pos}%")
    else:
        chain.append("共识方向不明确或缺现价，建议观望（仓位 0%）")

    risks = ["规则降级模式：未经 LLM 深度推理，仅为分层共识的机械拼装，参考价值有限"]
    if against:
        risks.append("存在 ≥0.4 强度的逆向信号：" + "、".join(s["name_cn"] for s in against[:3]))
    vol = by_system.get("volatility", {})
    if vol.get("strength", 0) >= 0.5:
        risks.append(f"波动率异常：{vol.get('reasoning', '')[:60]}")
    risks.append("模拟盘研究，不构成投资建议")

    return {
        "direction": direction,
        "confidence": round(min(confidence, 0.75), 3),  # 降级模式置信度封顶 0.75
        "reasoning_chain": chain,
        "risks": risks,
        "suggestion": {
            "action": action,
            "entry_zone": entry_zone,
            "stop_loss": stop_loss,
            "target": target,
            "position_pct": pos,
        },
        "model": "rule-fallback",
        "degraded": True,
    }


# ═══════════════════════════ 主动洞察落库 ═══════════════════════════
# 复用 jarvis_db 连接层（默认 SQLite ~/.vibe-trading/jarvis_journal.db，配 pg 自动切换）

import time as _time

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")

_ALLOWED_SEVERITIES = ("info", "warning", "critical")


def _conn():
    import jarvis_db as jdb
    os.makedirs(DB_DIR, exist_ok=True)
    return jdb.connect(DB_PATH)


def init_insights_db() -> None:
    """建 jarvis_insights 表（幂等；DDL 经 jarvis_db 自动翻译兼容 pg）。"""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jarvis_insights (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       REAL NOT NULL,
                symbol   TEXT NOT NULL,
                kind     TEXT NOT NULL,
                title    TEXT NOT NULL,
                detail   TEXT NOT NULL DEFAULT '',
                severity TEXT NOT NULL DEFAULT 'info'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jarvis_insights_ts ON jarvis_insights(ts)"
        )


def add_insight(symbol: str, kind: str, title: str, detail: str = "",
                severity: str = "info") -> dict:
    """写一条主动洞察；失败返回 {ok: False}，绝不抛出（不拖垮巡检主循环）。"""
    sev = severity if severity in _ALLOWED_SEVERITIES else "info"
    try:
        init_insights_db()
        ts = _time.time()
        with _conn() as conn:
            conn.execute(
                "INSERT INTO jarvis_insights (ts, symbol, kind, title, detail, severity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, str(symbol).upper(), str(kind), str(title)[:200],
                 str(detail)[:2000], sev),
            )
        return {"ok": True, "ts": ts}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200]}


def list_insights(limit: int = 20, symbol: str | None = None) -> list[dict]:
    """最近的主动洞察列表（新→旧）；失败返回空列表，绝不抛出。"""
    n = max(1, min(int(limit), 500))
    try:
        init_insights_db()
        with _conn() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT ts, symbol, kind, title, detail, severity FROM jarvis_insights "
                    "WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                    (str(symbol).upper(), n),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ts, symbol, kind, title, detail, severity FROM jarvis_insights "
                    "ORDER BY ts DESC LIMIT ?",
                    (n,),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


# ═══════════════════════════ 公开 API ═══════════════════════════

def reason(market: dict, signals: list[dict], cons: dict,
           timeout: int = 30) -> dict:
    """推理主入口：优先 LLM，失败/未配置降级规则拼装。永不抛出。

    Args:
        market: 市场快照，如 {"symbol": "BTCUSDT", "price": 60000.0, "atr": 800.0, ...}
        signals: jarvis_twelve_systems.run_all 的输出
        cons: jarvis_twelve_systems.consensus 的输出
    """
    try:
        cfg = _llm_config()
        if cfg:
            raw = _call_llm(cfg, market, signals, cons, timeout=timeout)
            if raw is not None:
                clean = _sanitize_llm_result(raw, cfg["model"])
                if clean is not None:
                    return clean
        return _rule_based(market, signals, cons)
    except Exception:  # noqa: BLE001 — 推理引擎必须永不崩
        try:
            return _rule_based(market, signals, cons)
        except Exception:  # noqa: BLE001 — 双保险：最简空结果
            return {
                "direction": "neutral", "confidence": 0.0,
                "reasoning_chain": ["推理引擎异常，输出空结果兜底"],
                "risks": ["引擎异常"],
                "suggestion": {"action": "wait", "entry_zone": "—",
                               "stop_loss": None, "target": None, "position_pct": 0.0},
                "model": "none", "degraded": True,
            }
