#!/usr/bin/env python3
"""跨进程共享分钟预算冒烟自测（任务L 治本）。

只依赖 jarvis_net（纯标准库），不引入 requests 等重依赖，任何 python3 可跑。
覆盖：限额放行/超限拒绝、跨进程同文件共享计数、URL 归一化、host 隔离、
limit<=0 不限、窗口过期恢复、IO 异常兜底放行、block 超时返回。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jarvis_net as jn  # noqa: E402

_tmp = tempfile.mkdtemp()
jn._BUDGET_PATH = os.path.join(_tmp, "net_budget.json")

_results = []


def check(name, cond):
    _results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


r = [jn.budget_take("fapi.binance.com", 3) for _ in range(3)]
check("限额3内连续3次放行", r == [True, True, True])
check("超限第4次拒绝", jn.budget_take("fapi.binance.com", 3) is False)
check("同文件共享计数仍拒绝(模拟另一进程)", jn.budget_take("fapi.binance.com", 3) is False)
check("URL归一化同host仍拒绝",
      jn.budget_take("https://fapi.binance.com/fapi/v1/klines?x=1", 3) is False)
check("不同host独立放行", jn.budget_take("api.binance.com", 3) is True)
check("limit<=0不限放行", jn.budget_take("x.com", 0) is True)

# 窗口过期后恢复：缩短窗口 + sleep
jn._BUDGET_WINDOW_S = 0.5
time.sleep(0.6)
check("窗口过期后恢复放行", jn.budget_take("fapi.binance.com", 3) is True)

# IO 异常兜底：路径不可创建（/dev/null 非目录）→ 静默放行 True
jn._BUDGET_PATH = "/dev/null/sub/net_budget.json"
check("IO异常兜底放行", jn.budget_take("fapi.binance.com", 1) is True)

# block=True 超限应在 max_wait 内返回 False
jn._BUDGET_PATH = os.path.join(_tmp, "b3.json")
jn._BUDGET_WINDOW_S = 60.0
jn.budget_take("h.com", 1)  # 占满 limit=1
_t0 = time.time()
_blocked = jn.budget_take("h.com", 1, block=True, max_wait=0.3)
_dt = time.time() - _t0
check("block超限在max_wait内返回False", _blocked is False and 0.25 < _dt < 3.0)

print("---")
_passed = sum(_results)
print("ALL PASS" if _passed == len(_results)
      else "SOME FAIL: %d/%d" % (_passed, len(_results)))
sys.exit(0 if _passed == len(_results) else 1)
