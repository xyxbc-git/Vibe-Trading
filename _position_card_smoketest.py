"""一次性验证：/api/positions 给自创单持仓附加 plan_leverage/plan_margin_usdt/plan_notional_usdt。

用临时 DB（不碰真实库）：造一笔带 trade-plan note 的 filled 限价单 + 关联持仓，
直接调 jarvis_dashboard.api_positions 断言 plan_* 列注入与无快照行的缺省行为。
"""
import json
import os
import tempfile

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_paper_trader as jpt
import jarvis_wallet as jw

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


jw.reset(10_000.0)

# 1) 自创单：note 带 trade-plan 快照（100x 杠杆、130U 保证金、13000U 名义）
note = json.dumps({
    "kind": "trade-plan", "tf": "auto", "leverage": 100,
    "margin_usdt": 130.0, "notional_usdt": 13_000.0, "entry": 100_000.0,
})
r = jw.place_limit_order("BTCUSDT", "buy", 100_000.0, 0.0013,
                         stop_loss=98_000.0, take_profit=103_000.0,
                         note=note, source="user-created")
check("自创单挂单成功", r.get("ok"), str(r))
pid = jpt._insert_position("BTCUSDT", 0.0013, "2026-07-12", 100_000.0, None,
                           98_000.0, 103_000.0, 30, None, signal_source="limit")
jw.mark_filled(r["order_id"], 100_000.0, pid)

# 2) 无快照的系统持仓（现货全额）
pid2 = jpt._insert_position("ETHUSDT", 0.5, "2026-07-12", 3_000.0, None,
                            2_900.0, 3_200.0, 30, None, signal_source="brief")

# 3) 调 dashboard API（import 放这里：模块级代码较重，先把库准备好）
import jarvis_dashboard as jd

rows = json.loads(bytes(jd.api_positions(status="open").body))
by_id = {row["id"]: row for row in rows}

p1 = by_id.get(pid)
check("自创单持仓在列", p1 is not None)
if p1:
    check("plan_leverage=100", p1.get("plan_leverage") == 100, str(p1.get("plan_leverage")))
    check("plan_margin_usdt=130", p1.get("plan_margin_usdt") == 130.0, str(p1.get("plan_margin_usdt")))
    check("plan_notional_usdt=13000", p1.get("plan_notional_usdt") == 13_000.0,
          str(p1.get("plan_notional_usdt")))

p2 = by_id.get(pid2)
check("系统持仓在列", p2 is not None)
if p2:
    check("系统持仓无 plan_* 列", "plan_leverage" not in p2, str(p2.keys()))

print("---")
print("ALL PASS" if not fails else f"FAILED: {fails}")
raise SystemExit(0 if not fails else 1)
