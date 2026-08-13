#!/usr/bin/env python3
"""出网审计加固冒烟（任务 N1）。完全离线：requests.get 打桩计数。

覆盖：同参 single-flight 并发去重（5 并发 1 真发）、异参不误合并、领跑者
失败时跟随者自行走链路（不雪崩不死锁）、降级日志进程标记、jarvis_net
观测读取器（weight_records/budget_usage/ban_records/probe_state）。
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jarvis_net as jn  # noqa: E402
import jarvis_crypto_data as jcd  # noqa: E402

_tmp = tempfile.mkdtemp()
jn._BAN_PATH = os.path.join(_tmp, "net_ban.json")
jn._PROBE_PATH = os.path.join(_tmp, "net_probe.json")
jn._BUDGET_PATH = os.path.join(_tmp, "net_budget.json")
jn._WEIGHT_PATH = os.path.join(_tmp, "net_weight.json")
jn._SOURCE_MODE_PATH = os.path.join(_tmp, "datasource_mode.json")
jn._BAN_RELOAD_S = 0.0
jcd.CACHE_DIR = os.path.join(_tmp, "cache")
jcd.DEGRADE_LOG = os.path.join(_tmp, "degrade.log")

PREM = "https://fapi.binance.com/fapi/v1/premiumIndex"

_results = []


def check(name, cond):
    _results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


class _Resp:
    status_code = 200
    headers: dict = {}

    @staticmethod
    def json():
        return {"markPrice": "1877.99", "lastFundingRate": "0.0001"}


_calls = {"n": 0}
_orig_requests_get = jcd.requests.get


def _slow_get(url, params=None, headers=None, timeout=None):
    _calls["n"] += 1
    time.sleep(0.4)
    return _Resp()


# ── 1. 同参 single-flight：5 并发只放 1 支真实出网 ──
jcd.requests.get = _slow_get
res_box: list = [None] * 5


def _worker(i):
    res_box[i] = jcd._get(PREM, {"symbol": "ETHUSDT"})


threads = [threading.Thread(target=_worker, args=(i,)) for i in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=15)
check("5 并发同参只 1 次真实出网", _calls["n"] == 1)
check("全部线程拿到有效数据",
      all(isinstance(r, dict) and r.get("markPrice") == "1877.99" for r in res_box))

# ── 2. 异参不误合并 ──
_calls["n"] = 0
r1 = jcd._get(PREM, {"symbol": "BTCUSDT"})
r2 = jcd._get(PREM, {"symbol": "SOLUSDT"})
check("异参各自出网（2 次）", _calls["n"] == 2 and r1 and r2)

# ── 3. TTL 内直出不出网（single-flight 之前的第一道闸不回归）──
_calls["n"] = 0
r3 = jcd._get(PREM, {"symbol": "ETHUSDT"})
check("TTL 内直出零出网", _calls["n"] == 0 and r3.get("markPrice") == "1877.99")

# ── 4. 领跑者失败：跟随者自行走链路，不死锁不雪崩 ──
def _fail_get(url, params=None, headers=None, timeout=None):
    _calls["n"] += 1
    time.sleep(0.2)
    raise OSError("构造失败")


jcd.requests.get = _fail_get
_calls["n"] = 0
res_box2: list = [None] * 3


def _worker2(i):
    res_box2[i] = jcd._get(PREM, {"symbol": "XRPUSDT"}, retries=1, fast=True)


threads = [threading.Thread(target=_worker2, args=(i,)) for i in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=20)
check("领跑者失败跟随者不死锁（全部返回）",
      all(r is not None for r in res_box2))
check("失败路径均返回 _error（无缓存可回）",
      all(isinstance(r, dict) and "_error" in r for r in res_box2))
jcd.requests.get = _orig_requests_get

# ── 5. 降级日志进程标记 ──
jcd._degrade_log("N1 标记自检")
_log_txt = open(jcd.DEGRADE_LOG, encoding="utf-8").read()
check("降级日志带 进程名:pid 标记",
      f"[{jcd._PROC_TAG}]" in _log_txt and str(os.getpid()) in _log_txt)

# ── 6. jarvis_net 观测读取器 ──
with open(jn._WEIGHT_PATH, "w", encoding="utf-8") as f:
    json.dump({"fapi.binance.com": {"w": 2379.0, "ts": time.time()}}, f)
w = jn.weight_records()
check("weight_records 读出 IP 回报水位", w.get("fapi.binance.com", {}).get("w") == 2379.0)

jn.budget_take("unit.test.host", 100, cost=7.0)
jn.budget_take("unit.test.host", 100, cost=5.0)
usage = jn.budget_usage()
check("budget_usage 汇总自家 60s 用量", usage.get("unit.test.host") == 12.0)

jn.report_ban("fapi.binance.com", time.time() + 300)
br = jn.ban_records()
check("ban_records 读出有效封禁", "fapi.binance.com" in br)
check("probe_state 可读（软着陆状态）", isinstance(jn.probe_state(), dict))

print("---")
_passed = sum(_results)
print("ALL PASS" if _passed == len(_results)
      else "SOME FAIL: %d/%d" % (_passed, len(_results)))
sys.exit(0 if _passed == len(_results) else 1)
