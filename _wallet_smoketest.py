"""离线全链路冒烟测试：入金→限价挂单→撮合成交→持仓→平仓→钱包对账。用临时 DB，不碰真实库。"""
import os
import tempfile

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_executor as jx
import jarvis_paper_trader as jpt
import jarvis_wallet as jw

cfg = jx.load_config()
cfg["account_equity_usdt"] = 1000.0
cfg["agent_token"] = ""  # 隔离真实 token：纯离线撮合，平仓用传入价而非真实下单
# [风控篇 P0-2] 本用例验证的是钱包记账数学，摩擦归零保持确定性断言
# （手续费/滑点数学在 _risk_fixes_smoketest.py 单独覆盖）
cfg["paper_fee_pct"] = 0.0
cfg["paper_slippage_pct"] = 0.0
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# 1. 入金
jw.reset(1000.0)
w = jw.get_wallet()
check("入金 1000", abs(w["cash_usdt"] - 1000) < 1e-6, f"cash={w['cash_usdt']}")

# 2. 限价买单冻结
r = jw.place_limit_order("BTCUSDT", "buy", 60000, 0.001)
w = jw.get_wallet()
check("挂买单成功", r.get("ok"), str(r))
check("冻结 60 现金 940", abs(w["cash_usdt"] - 940) < 1e-6 and abs(w["frozen_usdt"] - 60) < 1e-6,
      f"cash={w['cash_usdt']} frozen={w['frozen_usdt']}")

# 3. 余额不足拒单
r2 = jw.place_limit_order("BTCUSDT", "buy", 2000000, 1)
check("余额不足拒单", not r2.get("ok"), str(r2))

# 4. 撮合（现价 59000 ≤ 限价 60000 → 成交）
jpt.latest_price = lambda c, s: 59000.0
m = jpt.match_limit_orders(cfg)
w = jw.get_wallet()
poss = jpt.open_positions("BTCUSDT")
check("撮合成交 1 笔", len(m) == 1, str(m))
check("成交后冻结归零", abs(w["frozen_usdt"]) < 1e-6, f"frozen={w['frozen_usdt']}")
check("成交后建仓 1 个", len(poss) == 1, f"持仓={len(poss)}")
check("成交价=限价 60000", poss and abs(poss[0]["entry_price"] - 60000) < 1e-6,
      f"entry={poss[0]['entry_price'] if poss else None}")

# 5. 平仓回款（现价 66000）
res = jpt._close_position(poss[0], 66000.0, "manual", cfg)
w = jw.get_wallet()
check("平仓盈亏 +6U / +10%", abs(res["pnl_usdt"] - 6) < 1e-6 and abs(res["pnl_pct"] - 10) < 1e-6, str(res))
check("回款后现金 1006", abs(w["cash_usdt"] - 1006) < 1e-6, f"cash={w['cash_usdt']}")

# 6. 账户对账
st = jpt.stats(cfg)
check("权益 1006", abs(st["equity_usdt"] - 1006) < 1e-6, f"equity={st['equity_usdt']}")
check("已实现盈亏 6U", abs(st["realized_pnl_usdt"] - 6) < 1e-6, f"realized={st['realized_pnl_usdt']}")

# 7. 撤单解冻
r3 = jw.place_limit_order("ETHUSDT", "buy", 1000, 0.5)  # 冻结 500
w1 = jw.get_wallet()
c = jw.cancel_limit_order(r3["order_id"])
w2 = jw.get_wallet()
check("撤单解冻退回", c.get("ok") and abs(w2["cash_usdt"] - (w1["cash_usdt"] + 500)) < 1e-6,
      f"撤前{w1['cash_usdt']} 撤后{w2['cash_usdt']}")

# 8. 流水完整
led = jw.ledger(20)
types = [x["type"] for x in led]
check("流水含 买/卖/冻结/解冻", all(t in types for t in ["buy", "sell", "freeze", "unfreeze"]), str(types))

print("\n=== " + ("全部通过" if not fails else f"失败 {len(fails)}: {fails}") + " ===")
