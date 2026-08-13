#!/usr/bin/env python3
"""降级缓存诚实性冒烟自测（任务 J3）。完全离线：缓存/封禁登记全走临时目录。

覆盖：封禁降级 dict 附 _stale/_stale_age_s 且磁盘缓存零污染、list 响应走
last_get_meta 旁路、TTL 内新鲜路径字节级零回归、market_intel stale 时 ts 不
刷新+get_intel 暴露 stale_parts、快照同步源 stale 跳过不制造假新鲜行、
非 stale 走原路径。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jarvis_net as jn  # noqa: E402
import jarvis_crypto_data as jcd  # noqa: E402

_tmp = tempfile.mkdtemp()
jn._BAN_PATH = os.path.join(_tmp, "net_ban.json")
jn._PROBE_PATH = os.path.join(_tmp, "net_probe.json")
jn._SOURCE_MODE_PATH = os.path.join(_tmp, "datasource_mode.json")
jn._BAN_RELOAD_S = 0.0
jcd.CACHE_DIR = os.path.join(_tmp, "cache")
jcd.DEGRADE_LOG = os.path.join(_tmp, "degrade.log")

FAPI_PREM = "https://fapi.binance.com/fapi/v1/premiumIndex"
FAPI_T24 = "https://fapi.binance.com/fapi/v1/ticker/24hr"

_results = []


def check(name, cond):
    _results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


def _seed(url, params, data, age_s):
    key = jcd._cache_key(url, params)
    os.makedirs(jcd.CACHE_DIR, exist_ok=True)
    path = os.path.join(jcd.CACHE_DIR, key + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time() - age_s, "data": data}, f, ensure_ascii=False)
    return path


# ── 1. 封禁降级：dict 响应附 _stale，磁盘缓存零污染 ──
prem_params = {"symbol": "ETHUSDT"}
prem_path = _seed(FAPI_PREM, prem_params,
                  {"markPrice": "1877.99", "lastFundingRate": "0.0001"}, age_s=100)
jn.report_ban("fapi.binance.com", time.time() + 600)
out = jcd._get(FAPI_PREM, prem_params)
check("封禁降级 dict 附 _stale=True", isinstance(out, dict) and out.get("_stale") is True)
check("_stale_age_s≈100", 95 <= int(out.get("_stale_age_s") or 0) <= 110)
check("原字段保留", out.get("markPrice") == "1877.99")
check("旁路 meta.stale=True", jcd.last_get_meta().get("stale") is True)
_raw = json.load(open(prem_path, encoding="utf-8"))
check("磁盘缓存零 _stale 污染", "_stale" not in (_raw.get("data") or {}))

# ── 2. list 响应：本体不打标，旁路可感知 ──
_seed(FAPI_T24, None, [
    {"symbol": "BTCUSDT", "lastPrice": "63429.69", "priceChangePercent": "-1.2"},
    {"symbol": "ETHUSDT", "lastPrice": "1877.99", "priceChangePercent": "0.8"},
], age_s=200)
out2 = jcd._get(FAPI_T24, None)
check("list 响应原样返回", isinstance(out2, list) and len(out2) == 2)
check("list 旁路 meta.stale=True", jcd.last_get_meta().get("stale") is True)
meta_age = jcd.last_get_meta().get("age_s")
check("list 旁路 age≈200", meta_age is not None and 195 <= int(meta_age) <= 210)

# ── 3. TTL 内新鲜路径零回归（premiumIndex TTL=30s，龄 5s）──
fresh_data = {"markPrice": "1900.00", "lastFundingRate": "0.0002"}
_seed(FAPI_PREM, {"symbol": "BTCUSDT"}, fresh_data, age_s=5)
out3 = jcd._get(FAPI_PREM, {"symbol": "BTCUSDT"})
check("TTL 内返回与种子字节级一致（无 _stale）", out3 == fresh_data)
check("TTL 内旁路 meta.stale=False", jcd.last_get_meta().get("stale") is False)

# ── 4. market_intel：stale 时 ts 不刷新 + get_intel 暴露 ──
import jarvis_market_intel as jmi  # noqa: E402

_now = time.time()
with jmi._LOCK:
    jmi._STORE.clear()
    for name in ("funding", "oi", "long_short", "fng"):
        jmi._STORE[name] = {"ts": _now, "data": {"symbol": "BTCUSDT", "value": 1,
                                                 "rates": {}}, "error": None}
    jmi._STORE["price_24h"] = {"ts": 1000.0, "data": {"sentinel": 1}, "error": None}

jmi._refresh(["price_24h"])   # ticker/24hr 被封 → 降级 list 缓存 → stale
ent = jmi._STORE["price_24h"]
check("stale 时 ts 不刷新（保留 1000.0）", ent.get("ts") == 1000.0)
check("_STORE 打 stale 标", ent.get("stale") is True)
check("降级数据仍可用（含 all 映射）",
      isinstance(ent.get("data"), dict) and "ETHUSDT" in (ent["data"].get("all") or {}))

intel = jmi.get_intel()
check("intel.stale_parts 含 price_24h", "price_24h" in (intel.get("stale_parts") or []))
p24 = intel.get("price_24h") or {}
check("price_24h part 带 stale=True + 旧 ts", p24.get("stale") is True and p24.get("ts") == 1000)
check("非 stale part 不带 stale 键", "stale" not in (intel.get("oi") or {}))

# ── 5. 快照同步：源 stale 跳过；非 stale 走原路径 ──
import jarvis_sync_tasks_b as tb  # noqa: E402


class _Ctx:
    config = {"dashboard_base_url": "http://127.0.0.1:1", "http_timeout_s": 1}
    symbols = ["ETHUSDT"]
    mysql = type("M", (), {"get": staticmethod(lambda: None)})()
    cursors = None
    dry_run = False


_orig_http = tb._http_json
tb._http_json = lambda base, path, timeout: {"ok": True, "stale_parts": ["price_24h"]}
res = tb.sync_market_snapshot(_Ctx())
check("源 stale 跳过 rows=0", res.rows == 0 and res.error is None)
check("cursor 标 stale-skip", str(res.cursor_value or "").startswith("stale-skip@"))

tb._http_json = lambda base, path, timeout: {"ok": True}
res2 = tb.sync_market_snapshot(_Ctx())
tb._http_json = _orig_http
check("非 stale 进入正常路径（MySQL 不可达暂存分支）",
      res2.rows == 0 and "MySQL" in str(res2.error or ""))

print("---")
_passed = sum(_results)
print("ALL PASS" if _passed == len(_results)
      else "SOME FAIL: %d/%d" % (_passed, len(_results)))
sys.exit(0 if _passed == len(_results) else 1)
