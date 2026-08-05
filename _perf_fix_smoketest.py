#!/usr/bin/env python3
"""[任务H] 性能优化落地离线 smoketest：不联网（上游全部 mock/仿真）。

覆盖：
  方案1  _get 超时元组 + fast 短预算（尝试次数/退避序列/末次不空睡）+ 磁盘缓存兜底
        + _okx_swap_price fast 透传
  方案2  _cached 单飞 + 旧值兜底（新鲜命中 / 过期旧值秒返+后台刷新 / 冷启动并发单飞
        / 刷新失败保留旧值）
  方案3  /api/alerts/price 进程内缓存（TTL 内不重复回源）
  方案4  WS 常量收紧 + 成功策略磁盘持久化往返 + 连续失败跳转决策矩阵
  移交项 market_intel._fetch_price_24h 全量解析（USDT 过滤/坏行跳过/顶层兼容口径）
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name} {extra}")


# ══════════════════ 方案1：_get 预算 ══════════════════
import jarvis_crypto_data as jcd

check("TIMEOUT 已拆 (connect,read)=(3,5)", jcd.TIMEOUT == (3, 5), str(jcd.TIMEOUT))

_orig_get = jcd.requests.get
_orig_sleep = jcd.time.sleep


def _budget_probe(**kwargs):
    """打桩 requests.get 永远失败 + 记录 sleep，测 _get 的尝试次数与退避序列。"""
    calls = {"n": 0, "timeouts": []}
    sleeps: list[float] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        calls["timeouts"].append(timeout)
        raise RuntimeError("simulated network down")

    jcd.requests.get = fake_get
    jcd.time.sleep = lambda s: sleeps.append(s)
    try:
        out = jcd._get("http://smoketest.invalid/x", {"a": 1}, **kwargs)
    finally:
        jcd.requests.get = _orig_get
        jcd.time.sleep = _orig_sleep
    return calls, sleeps, out


calls, sleeps, out = _budget_probe(fast=True)
check("fast 预算：2 次尝试", calls["n"] == 2, str(calls["n"]))
check("fast 预算：退避仅 0.5s（末次不空睡）", sleeps == [0.5], str(sleeps))
check("fast 预算：请求带 (3,5) 超时", all(t == (3, 5) for t in calls["timeouts"]),
      str(calls["timeouts"]))
check("彻底失败返回 _error 封套", isinstance(out, dict) and "_error" in out, str(out))

calls, sleeps, out = _budget_probe()
check("默认预算：4 次尝试（旧行为保留）", calls["n"] == 4, str(calls["n"]))
check("默认预算：退避 1.5/3/6（末次不空睡）", sleeps == [1.5, 3.0, 6.0], str(sleeps))

calls, sleeps, _ = _budget_probe(retries=3)
check("显式 retries 优先于 profile", calls["n"] == 3, str(calls["n"]))

# 磁盘缓存兜底：预写缓存 → 全部重试失败 → 返回缓存数据
_url = "http://smoketest.invalid/cached"
jcd._cache_write(jcd._cache_key(_url, None), [1, 2, 3])


def _probe_with_cache():
    def fake_get(url, params=None, headers=None, timeout=None):
        raise RuntimeError("down")
    jcd.requests.get = fake_get
    jcd.time.sleep = lambda s: None
    try:
        return jcd._get(_url, None, fast=True)
    finally:
        jcd.requests.get = _orig_get
        jcd.time.sleep = _orig_sleep


check("重试烧完回退磁盘缓存", _probe_with_cache() == [1, 2, 3])


def _probe_okx_fast():
    sleeps: list[float] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        raise RuntimeError("down")
    jcd.requests.get = fake_get
    jcd.time.sleep = lambda s: sleeps.append(s)
    try:
        p = jcd._okx_swap_price("BTCUSDT", fast=True)
    finally:
        jcd.requests.get = _orig_get
        jcd.time.sleep = _orig_sleep
    return p, sleeps


_p, _s = _probe_okx_fast()
check("_okx_swap_price fast 透传（退避 0.5s×1）", _p is None and _s == [0.5], str(_s))

# ══════════════════ 方案2：_cached 单飞 + 旧值兜底 ══════════════════
import jarvis_dashboard as jd

cnt = {"n": 0}


def _fn_v():
    cnt["n"] += 1
    return {"v": cnt["n"]}


v1 = jd._cached("smk:swr", 60, _fn_v)
v2 = jd._cached("smk:swr", 60, _fn_v)
check("新鲜命中不重算", v1 == v2 == {"v": 1} and cnt["n"] == 1, f"{v1}/{v2}/{cnt}")

# 过期：旧值秒返 + 后台刷新
slow = {"n": 0}


def _fn_slow():
    slow["n"] += 1
    time.sleep(0.3)
    return {"v": 100 + slow["n"]}


jd._CACHE["smk:stale"] = (time.time() - 999, {"v": "old"})
t0 = time.time()
got = jd._cached("smk:stale", 1, _fn_slow)
elapsed = time.time() - t0
check("过期旧值立即返回（不阻塞）", got == {"v": "old"} and elapsed < 0.2,
      f"{got} elapsed={elapsed:.3f}s")
# 并发第二请求：刷新单飞不叠加
jd._cached("smk:stale", 1, _fn_slow)
deadline = time.time() + 3
while time.time() < deadline and jd._CACHE["smk:stale"][1] == {"v": "old"}:
    time.sleep(0.05)
check("后台刷新落新值", jd._CACHE["smk:stale"][1] == {"v": 101},
      str(jd._CACHE["smk:stale"][1]))
time.sleep(0.4)  # 若误起第二个刷新线程，这里会等到 n=2
check("刷新单飞（同 key 只刷一次）", slow["n"] == 1, str(slow["n"]))

# 冷启动并发单飞：8 线程同击全新 key，只算 1 次
cold = {"n": 0}


def _fn_cold():
    cold["n"] += 1
    time.sleep(0.3)
    return {"v": "cold"}


results: list = []
ths = [threading.Thread(target=lambda: results.append(jd._cached("smk:cold", 60, _fn_cold)))
       for _ in range(8)]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=5)
check("冷启动 8 并发单飞只算 1 次", cold["n"] == 1 and len(results) == 8
      and all(r == {"v": "cold"} for r in results), f"n={cold['n']} res={len(results)}")

# 刷新失败保留旧值
jd._CACHE["smk:fail"] = (time.time() - 999, {"v": "keep"})


def _fn_boom():
    raise RuntimeError("refresh boom")


got = jd._cached("smk:fail", 1, _fn_boom)
time.sleep(0.3)
check("刷新失败旧值保留不抛出", got == {"v": "keep"}
      and jd._CACHE["smk:fail"][1] == {"v": "keep"})

# ══════════════════ 方案3：/api/alerts/price 缓存 ══════════════════
pc = {"n": 0}
_orig_cp = jd.jpa.current_price
jd.jpa.current_price = lambda sym: (pc.__setitem__("n", pc["n"] + 1) or 12345.6)
try:
    r1 = json.loads(bytes(jd.api_alerts_price("BTCUSDT").body))
    r2 = json.loads(bytes(jd.api_alerts_price("btc").body))  # 归一到同 key
finally:
    jd.jpa.current_price = _orig_cp
check("alerts/price 响应契约不变", r1 == {"symbol": "BTCUSDT", "price": 12345.6}, str(r1))
check("TTL 内不重复回源（含符号归一）", pc["n"] == 1 and r2 == r1, f"calls={pc['n']}")

# ══════════════════ 方案4：WS 策略 ══════════════════
import jarvis_ws_stream as jws

check("首帧确认窗 15→8s", jws.FIRST_FRAME_TIMEOUT_S == 8.0,
      str(jws.FIRST_FRAME_TIMEOUT_S))
check("连接超时 12→6s", jws.SOCK_CONNECT_TIMEOUT_S == 6.0,
      str(jws.SOCK_CONNECT_TIMEOUT_S))

_tmp = os.path.join(tempfile.mkdtemp(prefix="jws_plan_"), "plan.json")
_orig_path = jws.LAST_GOOD_PLAN_PATH
jws.LAST_GOOD_PLAN_PATH = _tmp
try:
    jws._save_last_good_plan(2)
    with open(_tmp, encoding="utf-8") as f:
        raw = json.load(f)
    check("成功策略按名称落盘", raw.get("name") == "spot+proxy", str(raw))
    check("落盘策略读回为索引", jws._load_last_good_plan() == 2)
    with open(_tmp, "w", encoding="utf-8") as f:
        f.write('{"name": "ghost+plan"}')
    check("未知名称读回 None（不错位）", jws._load_last_good_plan() is None)
    with open(_tmp, "w", encoding="utf-8") as f:
        f.write("{broken")
    check("损坏文件读回 None 不抛", jws._load_last_good_plan() is None)
finally:
    jws.LAST_GOOD_PLAN_PATH = _orig_path

_orig_good = jws._LAST_GOOD_PLAN["idx"]
try:
    jws._LAST_GOOD_PLAN["idx"] = 2
    check("失败 1 次不跳转", jws._apply_fail_jump(1, 1) == 1)
    check("连续失败 2 次跳历史策略", jws._apply_fail_jump(2, 1) == 2)
    check("已在历史策略上不跳", jws._apply_fail_jump(2, 2) == 2)
    check("失败 3 次继续正常轮换", jws._apply_fail_jump(3, 0) == 0)
    jws._LAST_GOOD_PLAN["idx"] = None
    check("无历史记录不跳", jws._apply_fail_jump(2, 1) == 1)
finally:
    jws._LAST_GOOD_PLAN["idx"] = _orig_good

# ══════════════════ 移交项：market_intel 全量 24h ══════════════════
import jarvis_market_intel as jmi

_FAKE_ROWS = [
    {"symbol": "BTCUSDT", "lastPrice": "65000.1", "priceChangePercent": "1.23"},
    {"symbol": "ETHUSDT", "lastPrice": "3200.5", "priceChangePercent": "-2.5"},
    {"symbol": "ETHBTC", "lastPrice": "0.05", "priceChangePercent": "0.1"},   # 非 USDT 滤掉
    {"symbol": "DOGEUSDT", "lastPrice": None, "priceChangePercent": "x"},     # 坏行跳过
]
_orig_gj = jmi._get_json
jmi._get_json = lambda url, params=None: _FAKE_ROWS
try:
    out = jmi._fetch_price_24h()
finally:
    jmi._get_json = _orig_gj
check("全量 all 映射（USDT 过滤+坏行跳过）",
      set(out["all"]) == {"BTCUSDT", "ETHUSDT"}, str(set(out.get("all", {}))))
check("顶层保留 BTC 兼容口径", out["symbol"] == "BTCUSDT"
      and out["last_price"] == 65000.1 and out["change_pct"] == 1.23, str(out))
check("逐币字段完整", out["all"]["ETHUSDT"] == {"last_price": 3200.5,
                                            "change_pct": -2.5},
      str(out["all"].get("ETHUSDT")))

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
