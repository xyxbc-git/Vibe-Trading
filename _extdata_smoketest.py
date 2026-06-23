"""离线 smoketest：T-13 扩展数据源（Coinglass 清算 / Glassnode 净流）。不联网。

验证：
  1) 无 Key → 优雅降级 _skipped（不报错、不联网）
  2) _coin 提取币种基名
  3) _data_key：env 优先，其次 data_keys.json
  4) 有 Key 但请求异常 → _error 降级不抛出（打桩 requests.get 抛错）
  5) 有 Key 正常 → 解析清算/净流字段（打桩 requests.get 返回假数据）
  6) markdown 在无 Key 时显示「—（未配置 Key）」
"""
import os
import json
import tempfile
import types

import jarvis_crypto_data as jcd

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# 确保无 env key
for e in ("JARVIS_COINGLASS_KEY", "JARVIS_GLASSNODE_KEY"):
    os.environ.pop(e, None)

# ── 2. _coin ──
check("_coin BTCUSDT=BTC", jcd._coin("BTCUSDT") == "BTC")
check("_coin ETH-USDT=ETH", jcd._coin("ETH-USDT") == "ETH")
check("_coin SOLUSDC=SOL", jcd._coin("SOLUSDC") == "SOL")

# ── 1. 无 Key 优雅降级（临时把 data_keys 指向不存在文件）──
orig_keys_path = jcd.DATA_KEYS_PATH
jcd.DATA_KEYS_PATH = "/nonexistent/data_keys.json"
liq = jcd.fetch_liquidations("BTCUSDT")
fl = jcd.fetch_exchange_flows("BTCUSDT")
check("无 Key 清算 _skipped", liq.get("_skipped") is True, str(liq))
check("无 Key 净流 _skipped", fl.get("_skipped") is True, str(fl))

# ── 3. _data_key env 优先 + 文件回退 ──
os.environ["JARVIS_COINGLASS_KEY"] = "env-key"
check("env 优先", jcd._data_key("JARVIS_COINGLASS_KEY", "coinglass") == "env-key")
os.environ.pop("JARVIS_COINGLASS_KEY", None)
with tempfile.TemporaryDirectory() as td:
    kp = os.path.join(td, "data_keys.json")
    with open(kp, "w", encoding="utf-8") as f:
        json.dump({"coinglass": "file-key"}, f)
    jcd.DATA_KEYS_PATH = kp
    check("文件回退", jcd._data_key("JARVIS_COINGLASS_KEY", "coinglass") == "file-key")
jcd.DATA_KEYS_PATH = "/nonexistent/data_keys.json"

# ── 4&5. 有 Key 时打桩 requests.get ──
os.environ["JARVIS_COINGLASS_KEY"] = "k1"
os.environ["JARVIS_GLASSNODE_KEY"] = "k2"


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


_real_get = jcd.requests.get


def fake_get_ok(url, params=None, headers=None, timeout=None):
    if "coinglass" in url:
        return FakeResp({"data": [{"longLiquidationUsd": 300.0, "shortLiquidationUsd": 100.0}]})
    if "glassnode" in url:
        return FakeResp([{"t": 1, "v": -1234.5}])
    return FakeResp({})


def fake_get_boom(url, params=None, headers=None, timeout=None):
    raise RuntimeError("network down")


jcd.requests.get = fake_get_ok
liq2 = jcd.fetch_liquidations("BTCUSDT")
fl2 = jcd.fetch_exchange_flows("BTCUSDT")
check("有 Key 清算解析多空", liq2.get("long_liquidation_usd_24h") == 300.0 and liq2.get("short_liquidation_usd_24h") == 100.0, str(liq2))
check("清算多头占比 75%", liq2.get("long_liq_share_pct") == 75.0, str(liq2.get("long_liq_share_pct")))
check("净流流出偏多", fl2.get("exchange_netflow_native_24h") == -1234.5 and "outflow" in fl2.get("netflow_state", ""), str(fl2))

jcd.requests.get = fake_get_boom
liq3 = jcd.fetch_liquidations("BTCUSDT")
check("有 Key 异常 → _error 不抛出", liq3.get("_error") is not None and "_skipped" not in liq3, str(liq3))

jcd.requests.get = _real_get
os.environ.pop("JARVIS_COINGLASS_KEY", None)
os.environ.pop("JARVIS_GLASSNODE_KEY", None)
jcd.DATA_KEYS_PATH = orig_keys_path

# ── 6. markdown 无 Key 降级行 ──
d = {
    "symbol": "BTCUSDT", "fetched_at_utc": "x", "funding": {}, "basis": {}, "open_interest": {},
    "long_short": {}, "taker": {}, "fear_greed": {}, "market_structure": {},
    "liquidations": {"_skipped": True}, "exchange_flows": {"_skipped": True}, "_sources": {},
}
md = jcd.to_markdown(d)
check("markdown 含扩展源标题", "扩展数据源（T-13" in md)
check("markdown 清算降级显示", "未配置 JARVIS_COINGLASS_KEY" in md)
check("markdown 净流降级显示", "未配置 JARVIS_GLASSNODE_KEY" in md)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
