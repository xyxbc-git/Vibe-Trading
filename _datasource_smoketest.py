#!/usr/bin/env python3
"""数据源切换器冒烟自测（任务 J2）。不联网：探测走 monkeypatch 构造响应。

覆盖：模式读写/非法值、策略屏蔽矩阵（binance 锁源屏 OKX / okx 屏可替代端点
且 K线与探测端点豁免）、banned_until 策略合成、switch 先探测后生效（探测失败
不切换 / 成功切换）、封禁期探测短路不真发、status 结构与如实标注、异常兜底。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jarvis_net as jn  # noqa: E402
import jarvis_datasource as jds  # noqa: E402

_tmp = tempfile.mkdtemp()
jn._BAN_PATH = os.path.join(_tmp, "net_ban.json")
jn._PROBE_PATH = os.path.join(_tmp, "net_probe.json")
jn._SOURCE_MODE_PATH = os.path.join(_tmp, "datasource_mode.json")
jn._BAN_RELOAD_S = 0.0
jn._MODE_RELOAD_S = 0.0
jds._PROBE_STATE_PATH = os.path.join(_tmp, "datasource_probe.json")

FAPI_PREM = "https://fapi.binance.com/fapi/v1/premiumIndex"
FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
FAPI_TIME = "https://fapi.binance.com/fapi/v1/time"
OKX_TICKER = "https://www.okx.com/api/v5/market/ticker"
OKX_TIME = "https://www.okx.com/api/v5/public/time"

_results = []


def check(name, cond):
    _results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


def _reset():
    jn._ban_cache.clear()
    jn._ban_read_at = 0.0
    jn._mode_cache["mode"] = None
    jn._mode_cache["read_at"] = 0.0
    for p in (jn._BAN_PATH, jn._PROBE_PATH, jn._SOURCE_MODE_PATH,
              jds._PROBE_STATE_PATH):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


# ── 1. 模式读写 ──
_reset()
check("默认模式 auto", jn.get_source_mode() == "auto")
jn.set_source_mode("okx", by="smoketest")
check("set/get 模式=okx", jn.get_source_mode() == "okx")
try:
    jn.set_source_mode("bitmex")
    check("非法模式抛 ValueError", False)
except ValueError:
    check("非法模式抛 ValueError", True)

# ── 2. 策略屏蔽矩阵 ──
_reset()
jn.set_source_mode("auto")
check("auto 不屏蔽币安可替代端点", jn.banned_until(FAPI_PREM) == 0.0)
check("auto 不屏蔽 OKX", jn.banned_until(OKX_TICKER) == 0.0)

jn.set_source_mode("binance")
check("binance 锁源屏蔽 OKX 端点", jn.banned_until(OKX_TICKER) > time.time())
check("binance 模式不屏蔽币安自身", jn.banned_until(FAPI_PREM) == 0.0)
check("binance 模式豁免 OKX 探测端点", jn.banned_until(OKX_TIME) == 0.0)

jn.set_source_mode("okx")
check("okx 屏蔽币安 premiumIndex", jn.banned_until(FAPI_PREM) > time.time())
check("okx 不屏蔽币安 klines（无等价数据仍用主源）",
      jn.banned_until(FAPI_KLINES) == 0.0)
check("okx 豁免币安探测端点", jn.banned_until(FAPI_TIME) == 0.0)
check("okx 模式裸 host 不屏蔽（check_symbol 等 host 级查询不受扰）",
      jn.banned_until("fapi.binance.com") == 0.0)

# ── 3. 策略与真实封禁合成（取更晚者）──
_reset()
jn.set_source_mode("okx")
_real_ban = time.time() + 300
jn.report_ban(FAPI_PREM, _real_ban)
check("真实封禁晚于策略窗时返回真实截止",
      abs(jn.banned_until(FAPI_PREM) - _real_ban) < 1.0)

# ── 4. switch 先探测后生效（monkeypatch jcd._get 构造响应）──
_reset()
import jarvis_crypto_data as jcd  # noqa: E402

_orig_get = jcd._get


def _get_ok(url, params=None, retries=None, *, fast=False, ttl=None):
    if "binance" in url:
        return {"serverTime": 1786600000000}
    return {"code": "0", "data": [{"ts": "1786600000000"}]}


def _get_fail(url, params=None, retries=None, *, fast=False, ttl=None):
    return {"_error": "ReadTimeout(构造失败)"}


jcd._get = _get_ok
res = jds.switch("okx", by="smoketest")
check("探测通过才切换 ok=True", res.get("ok") is True and jn.get_source_mode() == "okx")
check("切换响应含探测延迟", isinstance(res.get("probe", {}).get("latency_ms"), float))

jcd._get = _get_fail
res = jds.switch("binance", by="smoketest")
check("探测失败不切换 ok=False", res.get("ok") is False)
check("失败后模式保持 okx", jn.get_source_mode() == "okx")
check("失败响应带人话原因", "探测失败" in str(res.get("reason", "")))

res = jds.switch("auto", by="smoketest")
check("auto 直接生效不探测", res.get("ok") is True and jn.get_source_mode() == "auto")
check("auto 未知模式拒绝", jds.switch("huobi").get("ok") is False)

# ── 5. 封禁期探测短路不真发 ──
_reset()
jn.set_source_mode("auto")
jn.report_ban(FAPI_TIME, time.time() + 600)
_called = {"n": 0}


def _get_counting(url, params=None, retries=None, *, fast=False, ttl=None):
    _called["n"] += 1
    return {"serverTime": 1}


jcd._get = _get_counting
p = jds.probe("binance")
check("封禁期探测短路 ok=False", p.get("ok") is False and p.get("short_circuit") is True)
check("封禁期探测零真实请求", _called["n"] == 0)
check("短路原因含截止时间", "短路" in str(p.get("reason", "")))

# ── 6. status 结构与如实标注 ──
jcd._get = _orig_get
_reset()
jn.set_source_mode("okx")
st = jds.status()
check("status.mode=okx", st.get("mode") == "okx")
check("status 能力矩阵含 klines 仅币安",
      st["capability"]["klines_multi_tf"] == {"binance": True, "okx": False})
check("okx 模式下 klines 如实标注仍用主源",
      "仍用主源" in st["effective_source"]["klines_multi_tf"])
check("okx 模式下 funding 标注走 okx",
      st["effective_source"]["funding_rate"].startswith("okx"))
check("status 含降级语义说明", "缓存" in st.get("degrade_semantics", ""))
check("status 含三源健康结构",
      all(k in st.get("sources", {}) for k in ("binance_futures", "binance_spot", "okx")))

# ── 7. 模式文件损坏兜底 auto ──
_reset()
with open(jn._SOURCE_MODE_PATH, "w", encoding="utf-8") as f:
    f.write("{corrupt")
check("模式文件损坏回退默认 auto", jn.get_source_mode() == "auto")

print("---")
_passed = sum(_results)
print("ALL PASS" if _passed == len(_results)
      else "SOME FAIL: %d/%d" % (_passed, len(_results)))
sys.exit(0 if _passed == len(_results) else 1)
