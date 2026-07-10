#!/usr/bin/env python3
"""贾维斯 JARVIS — LLM 用量/成本记账层。

每次 LLM 调用（ask/reason/review/strategy_gen/strategy_evolve/scalper_evolve/test）
记录一行：时间、功能模块、模型、tokens、估算成本、耗时、成败。

存储：
  - 主存：jarvis_db 兼容层（配置 JARVIS_DB_URL 时写 PostgreSQL，否则本地 SQLite，
    与 jarvis_journal 同库同口径）
  - 降级：DB 写失败时追加 ~/.vibe-trading/llm_usage.jsonl，绝不阻断 LLM 主链路

成本口径：
  - 非流式调用取响应 body.usage 的真实 tokens；流式/缺 usage 时按字符启发式估算
    并标记 estimated=1
  - 单价表内置常见模型（USD / 1M tokens，按官网牌价整理，仅供估算），可用
    ~/.vibe-trading/llm_pricing.json 覆盖：{"模型前缀": {"input": x, "output": y}}

内容日志：
  - prompt_text 存 messages 的 JSON 数组（保留 role 结构，单条消息超限截断并加
    「…[截断]」，整体保持 JSON 可解析）；response_text 存拼接后的完整回复文本（超限截断）
  - prompt_chars / response_chars 记原始总长度，便于识别截断
  - 保留期：内容字段默认保留 30 天（~/.vibe-trading/llm_usage_config.json 的
    content_retention_days 可配），过期只清 prompt_text/response_text，记账行与
    成本统计不丢；清理在写入时机会式触发（每进程至多 6 小时一次）

对外 API（均绝不抛出）：
  record_call(...)   写一条记账（含内容日志）
  query_usage(...)   聚合查询（今日/本月/按日/按模块/按模型 + 最近明细，可按模块筛选/分页）
  get_detail(id)     取单条完整内容（prompt_text/response_text）
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import jarvis_db as jdb

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")
FALLBACK_JSONL = os.path.join(DB_DIR, "llm_usage.jsonl")
PRICING_PATH = os.path.join(DB_DIR, "llm_pricing.json")
USAGE_CONFIG_PATH = os.path.join(DB_DIR, "llm_usage_config.json")

# 内容日志截断上限（字符）：单条消息 / 整个 messages JSON / 响应文本
_MSG_CHAR_LIMIT = 6000
_PROMPT_CHAR_LIMIT = 16000
_RESPONSE_CHAR_LIMIT = 16000
_TRUNC_MARK = "…[截断]"

_DEFAULT_RETENTION_DAYS = 30
_CLEANUP_INTERVAL_S = 6 * 3600
_last_cleanup_ts = 0.0

# USD / 1M tokens（input, output）。前缀匹配（最长优先），大小写不敏感。
# 牌价随时会变，这里只做估算基准；llm_pricing.json 可整体覆盖/追加。
_PRICING_DEFAULT: dict[str, dict[str, float]] = {
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "qwen-turbo": {"input": 0.05, "output": 0.10},
    "qwen-plus": {"input": 0.11, "output": 0.28},
    "qwen-max": {"input": 0.34, "output": 1.37},
    "moonshot-v1-8k": {"input": 1.70, "output": 1.70},
    "moonshot-v1-32k": {"input": 3.40, "output": 3.40},
    "moonshot-v1-128k": {"input": 8.50, "output": 8.50},
}
# 未命中任何前缀时的兜底单价（避免成本恒为 0 造成"看起来免费"的误导）
_PRICING_FALLBACK = {"input": 0.50, "output": 1.50}

_TABLE_READY = False


def _conn():
    os.makedirs(DB_DIR, exist_ok=True)
    return jdb.connect(DB_PATH)


def init_db() -> None:
    """建表 + 存量表加内容字段（幂等）。经 jarvis_db 兼容层，SQLite/PG 双后端可用。"""
    global _TABLE_READY
    if _TABLE_READY:
        return
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_usage (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                REAL    NOT NULL,
                day               TEXT    NOT NULL,
                module            TEXT    NOT NULL,
                model             TEXT,
                base              TEXT,
                prompt_tokens     INTEGER,
                completion_tokens INTEGER,
                total_tokens      INTEGER,
                cost_usd          REAL,
                latency_ms        INTEGER,
                ok                INTEGER NOT NULL,
                error             TEXT,
                estimated         INTEGER NOT NULL,
                prompt_text       TEXT,
                response_text     TEXT,
                prompt_chars      INTEGER,
                response_chars    INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_usage_day ON llm_usage(day)"
        )
    # 存量表迁移：逐列 ALTER（PG 经兼容层翻成 IF NOT EXISTS 无报错；
    # SQLite 列已存在会抛错，逐列独立连接执行避免污染事务）
    for col, ddl in (("prompt_text", "TEXT"), ("response_text", "TEXT"),
                     ("prompt_chars", "INTEGER"), ("response_chars", "INTEGER")):
        try:
            with _conn() as conn:
                conn.execute(f"ALTER TABLE llm_usage ADD COLUMN {col} {ddl}")
        except Exception:  # noqa: BLE001 — 列已存在
            pass
    _TABLE_READY = True


# ─────────────────────────── tokens / 成本估算 ───────────────────────────

def estimate_tokens(text: str) -> int:
    """无 usage 时的启发式估算：CJK ≈ 0.6 token/字，其余 ≈ 1 token/4 字符。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, int(cjk * 0.6 + other / 4))


def _load_pricing() -> dict[str, dict[str, float]]:
    table = dict(_PRICING_DEFAULT)
    try:
        if os.path.exists(PRICING_PATH):
            with open(PRICING_PATH, encoding="utf-8") as f:
                user = json.load(f) or {}
            for k, v in user.items():
                if isinstance(v, dict) and "input" in v and "output" in v:
                    table[str(k).lower()] = {
                        "input": float(v["input"]), "output": float(v["output"]),
                    }
    except Exception:  # noqa: BLE001 — 单价表损坏不影响记账
        pass
    return table


def estimate_cost_usd(model: str | None, prompt_tokens: int, completion_tokens: int) -> float:
    """按单价表估算成本（USD）。模型名前缀匹配，最长优先。"""
    table = _load_pricing()
    m = (model or "").lower()
    price = _PRICING_FALLBACK
    best_len = 0
    for prefix, p in table.items():
        if m.startswith(prefix) and len(prefix) > best_len:
            price, best_len = p, len(prefix)
    return round(
        prompt_tokens / 1e6 * price["input"] + completion_tokens / 1e6 * price["output"],
        6,
    )


# ─────────────────────────── 内容日志序列化 / 保留期 ───────────────────────────

def _serialize_messages(messages: list[dict] | None) -> tuple[str | None, int]:
    """messages → (JSON 字符串, 原始总字符数)。

    单条消息内容超 _MSG_CHAR_LIMIT 截断加标记；整体仍超 _PROMPT_CHAR_LIMIT 时
    从头部丢弃较早消息（保留 system + 最新消息优先），JSON 始终可解析。
    """
    if not messages:
        return None, 0
    total_chars = 0
    slim: list[dict] = []
    for m in messages:
        role = str((m or {}).get("role", "user") or "user")
        content = str((m or {}).get("content", "") or "")
        total_chars += len(content)
        if len(content) > _MSG_CHAR_LIMIT:
            content = content[:_MSG_CHAR_LIMIT] + _TRUNC_MARK
        slim.append({"role": role, "content": content})
    dropped = 0

    def _dump() -> str:
        head = ([{"role": "note", "content": f"（更早的 {dropped} 条消息已省略）"}]
                if dropped else [])
        return json.dumps(head + slim, ensure_ascii=False)

    text = _dump()
    # 超限时从最早的非 system 消息开始丢，保住 system 与最新轮次
    while len(text) > _PROMPT_CHAR_LIMIT and len(slim) > 1:
        drop_idx = next((i for i, m in enumerate(slim) if m["role"] != "system"), 0)
        slim.pop(drop_idx)
        dropped += 1
        text = _dump()
    return text, total_chars


def _truncate_response(text: str | None) -> tuple[str | None, int]:
    if not text:
        return None, 0
    n = len(text)
    if n > _RESPONSE_CHAR_LIMIT:
        return text[:_RESPONSE_CHAR_LIMIT] + _TRUNC_MARK, n
    return text, n


def content_retention_days() -> int:
    """内容日志保留天数（默认 30；llm_usage_config.json 的 content_retention_days）。"""
    try:
        if os.path.exists(USAGE_CONFIG_PATH):
            with open(USAGE_CONFIG_PATH, encoding="utf-8") as f:
                v = int((json.load(f) or {}).get("content_retention_days", _DEFAULT_RETENTION_DAYS))
            return max(1, min(v, 3650))
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_RETENTION_DAYS


def _maybe_cleanup_content() -> None:
    """机会式清理过期内容字段（保留记账行）。每进程至多 6 小时跑一次。"""
    global _last_cleanup_ts
    now = time.time()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL_S:
        return
    _last_cleanup_ts = now
    cutoff = time.strftime("%Y-%m-%d",
                           time.localtime(now - content_retention_days() * 86400))
    with _conn() as conn:
        conn.execute(
            "UPDATE llm_usage SET prompt_text=NULL, response_text=NULL "
            "WHERE day < ? AND (prompt_text IS NOT NULL OR response_text IS NOT NULL)",
            (cutoff,),
        )


# ─────────────────────────── 写入 ───────────────────────────

def _write_jsonl(row: dict) -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    with open(FALLBACK_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_call(
    *,
    module: str,
    model: str | None,
    base: str | None = None,
    usage: dict | None = None,
    messages: list[dict] | None = None,
    output_text: str | None = None,
    latency_ms: int | None = None,
    ok: bool = True,
    error: str | None = None,
) -> None:
    """记一条 LLM 调用账（含内容日志）。绝不抛出、绝不阻断主链路。

    usage 有值（响应 body.usage）→ 用真实 tokens；否则按 messages/output_text 估算。
    """
    try:
        u = usage or {}
        pt = u.get("prompt_tokens")
        ct = u.get("completion_tokens")
        estimated = 0
        if pt is None or ct is None:
            estimated = 1
            in_text = ""
            for m in messages or []:
                in_text += str((m or {}).get("content", "") or "")
            pt = estimate_tokens(in_text) if pt is None else pt
            ct = estimate_tokens(output_text or "") if ct is None else ct
        pt, ct = int(pt or 0), int(ct or 0)
        total = int(u.get("total_tokens") or (pt + ct))
        prompt_text, prompt_chars = _serialize_messages(messages)
        response_text, response_chars = _truncate_response(output_text)
        row = {
            "ts": time.time(),
            "day": time.strftime("%Y-%m-%d"),
            "module": str(module or "unknown")[:40],
            "model": str(model or "")[:80] or None,
            "base": str(base or "")[:120] or None,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": total,
            "cost_usd": estimate_cost_usd(model, pt, ct),
            "latency_ms": int(latency_ms) if latency_ms is not None else None,
            "ok": 1 if ok else 0,
            "error": str(error)[:200] if error else None,
            "estimated": estimated,
            "prompt_text": prompt_text,
            "response_text": response_text,
            "prompt_chars": prompt_chars,
            "response_chars": response_chars,
        }
        try:
            init_db()
            with _conn() as conn:
                conn.execute(
                    """
                    INSERT INTO llm_usage
                        (ts, day, module, model, base, prompt_tokens, completion_tokens,
                         total_tokens, cost_usd, latency_ms, ok, error, estimated,
                         prompt_text, response_text, prompt_chars, response_chars)
                    VALUES (:ts, :day, :module, :model, :base, :prompt_tokens,
                            :completion_tokens, :total_tokens, :cost_usd, :latency_ms,
                            :ok, :error, :estimated,
                            :prompt_text, :response_text, :prompt_chars, :response_chars)
                    """,
                    row,
                )
        except Exception:  # noqa: BLE001 — DB 不可用降级 jsonl
            _write_jsonl(row)
        try:
            _maybe_cleanup_content()
        except Exception:  # noqa: BLE001 — 清理失败不影响记账
            pass
    except Exception:  # noqa: BLE001 — 记账自身任何异常都吞掉
        pass


# ─────────────────────────── 查询 ───────────────────────────

def _rows_from_db(since_day: str) -> list[dict]:
    init_db()
    with _conn() as conn:
        cur = conn.execute(
            "SELECT id, ts, day, module, model, prompt_tokens, completion_tokens, "
            "total_tokens, cost_usd, latency_ms, ok, error, estimated, "
            "(prompt_text IS NOT NULL OR response_text IS NOT NULL) AS has_content "
            "FROM llm_usage WHERE day >= ? ORDER BY ts DESC",
            (since_day,),
        )
        return [dict(r) for r in cur.fetchall()]


def _rows_from_jsonl(since_day: str, max_lines: int = 5000) -> list[dict]:
    if not os.path.exists(FALLBACK_JSONL):
        return []
    try:
        with open(FALLBACK_JSONL, encoding="utf-8") as f:
            lines = f.readlines()[-max_lines:]
        out = []
        for line in lines:
            try:
                r = json.loads(line)
                if str(r.get("day", "")) >= since_day:
                    out.append(r)
            except ValueError:
                continue
        return out
    except OSError:
        return []


def _agg(rows: list[dict], key: str) -> list[dict]:
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = str(r.get(key) or "—")
        b = buckets.setdefault(k, {key: k, "calls": 0, "ok_calls": 0,
                                    "total_tokens": 0, "cost_usd": 0.0})
        b["calls"] += 1
        b["ok_calls"] += 1 if r.get("ok") else 0
        b["total_tokens"] += int(r.get("total_tokens") or 0)
        b["cost_usd"] += float(r.get("cost_usd") or 0.0)
    out = sorted(buckets.values(), key=lambda x: -x["cost_usd"])
    for b in out:
        b["cost_usd"] = round(b["cost_usd"], 4)
    return out


def _sum(rows: list[dict]) -> dict:
    return {
        "calls": len(rows),
        "ok_calls": sum(1 for r in rows if r.get("ok")),
        "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in rows),
        "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in rows),
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
        "cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in rows), 4),
        "estimated_calls": sum(1 for r in rows if r.get("estimated")),
    }


def query_usage(days: int = 30, recent: int = 20,
                module: str | None = None, offset: int = 0) -> dict:
    """聚合查询。DB 与 jsonl 降级记录合并统计（两边互斥不重复计数）。

    module / offset 只作用于 recent 明细列表（筛选 + 分页）；聚合始终为全量口径。
    明细不携带内容大文本，只回 has_content 标记，完整内容走 get_detail(id)。
    """
    days = max(1, min(int(days), 365))
    recent = max(1, min(int(recent), 100))
    offset = max(0, min(int(offset), 100000))
    since_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    try:
        rows = _rows_from_db(since_day)
    except Exception:  # noqa: BLE001 — DB 读失败只剩 jsonl
        rows = []
    rows += _rows_from_jsonl(since_day)
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)

    today = time.strftime("%Y-%m-%d")
    month_prefix = today[:7]
    by_day_map: dict[str, list[dict]] = {}
    for r in rows:
        by_day_map.setdefault(str(r.get("day") or "—"), []).append(r)
    by_day = [
        {"day": d, **{k: v for k, v in _sum(rs).items()
                      if k in ("calls", "total_tokens", "cost_usd")}}
        for d, rs in sorted(by_day_map.items(), reverse=True)
    ]

    detail_pool = ([r for r in rows if str(r.get("module") or "") == module]
                   if module else rows)
    recent_rows = [
        {
            "id": r.get("id"),
            "ts": r.get("ts"),
            "module": r.get("module"),
            "model": r.get("model"),
            "total_tokens": r.get("total_tokens"),
            "cost_usd": r.get("cost_usd"),
            "latency_ms": r.get("latency_ms"),
            "ok": bool(r.get("ok")),
            "error": r.get("error"),
            "estimated": bool(r.get("estimated")),
            # jsonl 降级行无 id，但自身携带内容字段，可直接内嵌返回
            "has_content": bool(r.get("has_content")
                                or r.get("prompt_text") or r.get("response_text")),
        }
        for r in detail_pool[offset:offset + recent]
    ]
    return {
        "days": days,
        "today": _sum([r for r in rows if r.get("day") == today]),
        "month": _sum([r for r in rows if str(r.get("day") or "").startswith(month_prefix)]),
        "window": _sum(rows),
        "by_day": by_day,
        "by_module": _agg(rows, "module"),
        "by_model": _agg(rows, "model"),
        "recent": recent_rows,
        "recent_total": len(detail_pool),
        "recent_offset": offset,
        "module_filter": module or None,
        "content_retention_days": content_retention_days(),
        "pricing_note": "成本为按内置/用户单价表的估算值，非账单口径；"
                        "可编辑 ~/.vibe-trading/llm_pricing.json 校准",
    }


def get_detail(record_id: int) -> dict | None:
    """按 id 取单条完整记录（含 prompt_text/response_text）。失败/不存在返回 None。"""
    try:
        init_db()
        with _conn() as conn:
            cur = conn.execute(
                "SELECT id, ts, day, module, model, base, prompt_tokens, "
                "completion_tokens, total_tokens, cost_usd, latency_ms, ok, error, "
                "estimated, prompt_text, response_text, prompt_chars, response_chars "
                "FROM llm_usage WHERE id = ?",
                (int(record_id),),
            )
            row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["ok"] = bool(d.get("ok"))
        d["estimated"] = bool(d.get("estimated"))
        return d
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    print(json.dumps(query_usage(days=30, recent=10), ensure_ascii=False, indent=2))
