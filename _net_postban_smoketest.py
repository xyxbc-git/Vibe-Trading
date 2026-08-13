#!/usr/bin/env python3
"""封禁到期软着陆（post-ban 单探针 + 阶梯冷却）冒烟自测（出网预算审计 2026-08-13）。

只依赖 jarvis_net（纯标准库）。覆盖：无封禁史旧行为不变、硬封禁期内不变、
到期后单探针放行且其余短路、探针存活满确认窗恢复常态、再封 streak 递增且
阶梯附加冷却生效、连续违规窗外 streak 重置、探针文件损坏/IO 异常兜底放行。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jarvis_net as jn  # noqa: E402

_tmp = tempfile.mkdtemp()
jn._BAN_PATH = os.path.join(_tmp, "net_ban.json")
jn._PROBE_PATH = os.path.join(_tmp, "net_probe.json")
jn._BAN_RELOAD_S = 0.0          # 测试内即时重读
jn._PROBE_CONFIRM_S = 0.4       # 缩短确认窗
jn._REBAN_WINDOW_S = 30.0
jn._REBAN_PAD_BASE_S = 0.5      # 缩短阶梯基数
jn._REBAN_PAD_CAP_S = 2.0

HOST = "fapi.binance.com"
_results = []


def check(name, cond):
    _results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


def _reset():
    jn._ban_cache.clear()
    jn._ban_read_at = 0.0
    for p in (jn._BAN_PATH, jn._PROBE_PATH):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


# 1. 无封禁史：旧行为不变
_reset()
check("无封禁史 banned_until=0", jn.banned_until(HOST) == 0.0)

# 2. 硬封禁期内：返回登记截止（旧行为不变）
_reset()
_until = time.time() + 0.35
jn.report_ban(HOST, _until)
check("硬封禁期内返回截止", abs(jn.banned_until(HOST) - _until) < 0.01)

# 3. 到期后：第一个调用方=探针放行，第二个调用方拿合成截止短路
time.sleep(0.4)
jn._ban_read_at = 0.0
first = jn.banned_until(HOST)
second = jn.banned_until(HOST)
check("到期后探针放行(首个=0)", first == 0.0)
check("其余调用方短路(>now)", second > time.time())

# 4. 探针存活满确认窗（无新封禁登记）→ 恢复常态并清理状态
time.sleep(0.45)
jn._ban_read_at = 0.0
ok_after = jn.banned_until(HOST)
state = json.load(open(jn._PROBE_PATH)) if os.path.exists(jn._PROBE_PATH) else {}
check("确认窗后恢复常态=0", ok_after == 0.0)
check("恢复后探针状态已清理", HOST not in state)

# 5. 再封（连续违规窗内）：streak 递增，试路时点带阶梯附加冷却
_reset()
t1 = time.time() + 0.2
jn.report_ban(HOST, t1)
t2 = time.time() + 0.4
jn.report_ban(HOST, t2)          # 第二次登记（更晚 until）→ streak=2
rec = json.load(open(jn._PROBE_PATH)).get(HOST) or {}
check("再封 streak=2", int(rec.get("streak") or 0) == 2)
check("阶梯附加冷却生效(probe_at=until+0.5s)",
      abs(float(rec.get("probe_at") or 0) - (t2 + 0.5)) < 0.05)
time.sleep(0.45)                 # 硬封禁已过、附加冷却未满
jn._ban_read_at = 0.0
check("附加冷却期内仍短路", jn.banned_until(HOST) > time.time())
time.sleep(0.5)                  # 越过 probe_at（t2+0.5，自起点 ≈0.9s 处）
jn._ban_read_at = 0.0
check("附加冷却期满探针放行", jn.banned_until(HOST) == 0.0)

# 6. 连续违规窗外再封：streak 重置为 1（无附加冷却）
_reset()
jn._REBAN_WINDOW_S = 0.1
t3 = time.time() + 0.15
jn.report_ban(HOST, t3)
time.sleep(0.3)                  # 超出违规窗
t4 = time.time() + 0.2
jn.report_ban(HOST, t4)
rec = json.load(open(jn._PROBE_PATH)).get(HOST) or {}
check("违规窗外 streak 重置=1", int(rec.get("streak") or 0) == 1)
check("streak=1 无附加冷却", abs(float(rec.get("probe_at") or 0) - t4) < 0.05)
jn._REBAN_WINDOW_S = 30.0

# 7. 探针文件损坏：视为空状态，放行不抛
_reset()
with open(jn._PROBE_PATH, "w", encoding="utf-8") as f:
    f.write("{corrupt json")
check("探针文件损坏兜底放行", jn.banned_until(HOST) == 0.0)

# 8. 探针层 IO 异常（路径不可创建）：回退旧行为放行
_reset()
jn._PROBE_PATH = "/dev/null/sub/net_probe.json"
jn.report_ban(HOST, time.time() + 0.1)   # 登记主链路不受探针层故障影响
time.sleep(0.15)
jn._ban_read_at = 0.0
check("探针层IO异常回退旧行为", jn.banned_until(HOST) == 0.0)

print("---")
_passed = sum(_results)
print("ALL PASS" if _passed == len(_results)
      else "SOME FAIL: %d/%d" % (_passed, len(_results)))
sys.exit(0 if _passed == len(_results) else 1)
