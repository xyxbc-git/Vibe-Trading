"""模拟下单全生命周期追踪冒烟测试。临时 SQLite + mock 发件函数，不联网不碰真实库。

覆盖：周期解析 / 挂单打标 / 到点回录（挂单+持仓双路径、未到点不回录、去重）/
成交入场邮件（signal_tf 传播、去重）/ 盈亏阈值邮件（做多做空镜像、每仓每阈值一次、
重启不重发）/ 查询接口（列表+单条时间线）。
"""
from __future__ import annotations

import json
import os
import tempfile
import time

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_price_alert as jpa

jpa.CONFIG_DIR = _d
jpa.CONFIG_PATH = os.path.join(_d, "price_alert_config.json")
jpa._DB_INITIALIZED = False

import jarvis_order_lifecycle as jol
import jarvis_paper_trader as jpt
import jarvis_wallet as jw

# ── mock 发件：拦截 jol._deliver 的底层 send_email + 收件人配置 ──
SENT: list[dict] = []


def _fake_send_email(subject, body, to_list, cfg=None, dry_run=False):
    SENT.append({"subject": subject, "body": body, "to": list(to_list)})
    return {"ok": True, "to": list(to_list)}


jpa.send_email = _fake_send_email
_orig_load_config = jpa.load_config


def _fake_load_config():
    cfg = _orig_load_config()
    cfg["recipients"] = ["me@example.com"]
    return cfg


jpa.load_config = _fake_load_config

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


NOW = time.time()

# ── 1. 周期解析 ──
check("tf 30m=1800s", jol.tf_seconds("30m") == 1800)
check("tf 4h=14400s", jol.tf_seconds("4h") == 14400)
check("tf 1d=86400s", jol.tf_seconds("1d") == 86400)
check("tf multi 按 4h", jol.tf_seconds("multi") == 14400)
check("tf 大小写兼容", jol.tf_seconds("30M") == 1800)
check("tf 非法返回 None", jol.tf_seconds("abc") is None and jol.tf_seconds("") is None
      and jol.tf_seconds("0m") is None)

# ── 2. 建表幂等 + 挂单打标 ──
jol.ensure_schema()
jol.ensure_schema()
check("ensure_schema 幂等", True)

jw.ensure_account(10000.0)
lo = jw.place_limit_order("BTCUSDT", "buy", 50000.0, 0.01,
                          stop_loss=48000.0, take_profit=55000.0)
check("挂单登记", lo.get("ok"), json.dumps(lo, ensure_ascii=False)[:100])
OID = lo["order_id"]

r = jol.tag_order_tf(OID, "30m")
check("挂单打标 30m", r.get("ok"), str(r))
with jj._conn() as conn:
    row = dict(conn.execute("SELECT * FROM limit_orders WHERE id=?", (OID,)).fetchone())
check("limit_orders.signal_tf 落库", row.get("signal_tf") == "30m")
check("非法 tf 拒绝打标", not jol.tag_order_tf(OID, "xyz").get("ok"))
check("不存在的挂单拒绝", not jol.tag_order_tf(999999, "30m").get("ok"))

# 把下单时间拨回 31 分钟前，模拟周期已到
with jj._conn() as conn:
    conn.execute("UPDATE limit_orders SET created_ts=? WHERE id=?",
                 (NOW - 31 * 60, OID))

# ── 3. 到点回录（挂单路径：未成交挂单） ──
out = jol.run_sweep(now=NOW, price_of=lambda s: 51234.0)
check("挂单到点回录 1 条", len(out["snapshots"]) == 1, json.dumps(out["snapshots"]))
with jj._conn() as conn:
    snap = dict(conn.execute("SELECT * FROM order_tf_snapshots WHERE order_id=?",
                             (OID,)).fetchone())
check("快照记当前价", abs(snap["snap_price"] - 51234.0) < 1e-9)
check("快照参考价=挂单价", abs(snap["entry_ref_price"] - 50000.0) < 1e-9)
check("快照 tf=30m", snap["signal_tf"] == "30m")

out2 = jol.run_sweep(now=NOW, price_of=lambda s: 51500.0)
check("同一挂单不重复回录", len(out2["snapshots"]) == 0)

# ── 4. 未到点不回录 ──
lo2 = jw.place_limit_order("ETHUSDT", "buy", 3000.0, 0.1)
OID2 = lo2["order_id"]
jol.tag_order_tf(OID2, "4h")   # 刚下单，4h 未到
out3 = jol.run_sweep(now=NOW, price_of=lambda s: 3100.0)
check("未到点不回录", all(s.get("order_id") != OID2 for s in out3["snapshots"]))

# ── 5. 成交入场邮件（撮合钩子 + signal_tf 传播 + 去重） ──
SENT.clear()
cfg = {"agent_token": ""}   # 纯离线：不打真实网关
jpt.latest_price = lambda c, s: 49000.0   # 现价 ≤ 买单限价 50000 → 成交
filled = jpt.match_limit_orders(cfg)
check("限价买单成交", len(filled) == 1 and filled[0]["order_id"] == OID, str(filled))
PID = filled[0]["position_id"]

check("入场邮件已发", len(SENT) == 1, str([s["subject"] for s in SENT]))
check("入场邮件主题含币种+多单+周期",
      "BTCUSDT" in SENT[0]["subject"] and "多单" in SENT[0]["subject"]
      and "30m" in SENT[0]["subject"], SENT[0]["subject"])
body = SENT[0]["body"]
check("入场邮件正文含入损盈三价",
      "50,000" in body and "48,000" in body and "55,000" in body)
check("入场邮件正文含下单/成交时间", "下单时间" in body and "成交时间" in body)

poss = jpt.open_positions("BTCUSDT")
check("signal_tf 传播到持仓行", poss and poss[0].get("signal_tf") == "30m",
      str(poss[0].get("signal_tf") if poss else None))
check("持仓来源=limit", poss[0].get("signal_source") == "limit")

SENT.clear()
r = jol.on_order_filled({**row, "position_id": PID}, PID, 50000.0)
check("入场邮件去重（重复钩子不再发）", len(SENT) == 0 and "已发过" in str(r.get("skipped")))

# ── 6. 盈亏阈值邮件：做多 +20% ──
SENT.clear()
entry = poss[0]["entry_price"]   # 50000
out4 = jol.run_sweep(now=NOW, price_of=lambda s: entry * 1.25)   # +25%
gain_hits = [a for a in out4["alerts"] if a["kind"] == jol.EVENT_GAIN_ALERT
             and a["position_id"] == PID]
check("做多浮盈 +25% 触发 gain_alert", len(gain_hits) == 1, json.dumps(out4["alerts"]))
check("浮盈邮件已发且主题含 +25", len(SENT) == 1 and "+25.00%" in SENT[0]["subject"],
      SENT[0]["subject"] if SENT else "无邮件")

SENT.clear()
out5 = jol.run_sweep(now=NOW, price_of=lambda s: entry * 1.30)   # 再涨到 +30%
check("同仓 gain 阈值只发一次", len(SENT) == 0 and not [
    a for a in out5["alerts"] if a["kind"] == jol.EVENT_GAIN_ALERT])

# 「重启」模拟：内存状态清零后再扫，仍不重发（去重标记在库里）
SENT.clear()
out6 = jol.run_sweep(now=NOW, price_of=lambda s: entry * 1.30)
check("重启后不重发（去重落库）", len(SENT) == 0)

# ── 7. 盈亏阈值邮件：做多 -50%（同仓两阈值独立） ──
SENT.clear()
out7 = jol.run_sweep(now=NOW, price_of=lambda s: entry * 0.45)   # -55%
loss_hits = [a for a in out7["alerts"] if a["kind"] == jol.EVENT_LOSS_ALERT
             and a["position_id"] == PID]
check("做多浮亏 -55% 触发 loss_alert", len(loss_hits) == 1)
check("浮亏邮件主题含 -55", len(SENT) == 1 and "-55.00%" in SENT[0]["subject"],
      SENT[0]["subject"] if SENT else "无邮件")

# ── 8. 做空镜像：价格跌 → 浮盈；价格涨 → 浮亏 ──
check("做空浮盈口径", abs(jol._direction_pnl_pct("sell", 100.0, 75.0) - 25.0) < 1e-9)
check("做空浮亏口径", abs(jol._direction_pnl_pct("sell", 100.0, 160.0) + 60.0) < 1e-9)

SPID = jpt._insert_position("SOLUSDT", 10.0, time.strftime("%Y-%m-%d"), 100.0,
                            None, 130.0, 70.0, 30, None, side="sell",
                            signal_source="manual", signal_tf="1h")
SENT.clear()
out8 = jol.run_sweep(now=NOW, price_of=lambda s: 75.0)   # 空单价格跌 25% → 浮盈+25%
check("空单价格跌触发 gain_alert", any(
    a["kind"] == jol.EVENT_GAIN_ALERT and a["position_id"] == SPID
    for a in out8["alerts"]), json.dumps(out8["alerts"]))
check("空单浮盈邮件含空单字样", any("空单" in s["subject"] for s in SENT))

SENT.clear()
out9 = jol.run_sweep(now=NOW, price_of=lambda s: 160.0)   # 空单价格涨 60% → 浮亏-60%
check("空单价格涨触发 loss_alert", any(
    a["kind"] == jol.EVENT_LOSS_ALERT and a["position_id"] == SPID
    for a in out9["alerts"]))

# ── 9. 持仓路径到点回录（直接开仓带 tf；成交转持仓的不双记） ──
with jj._conn() as conn:
    conn.execute("UPDATE paper_positions SET opened_ts=? WHERE id=?",
                 (NOW - 3700, SPID))   # 1h 单开仓 61 分钟前 → 已到点
out10 = jol.run_sweep(now=NOW, price_of=lambda s: 80.0)
pos_snaps = [s for s in out10["snapshots"] if s.get("position_id") == SPID]
check("直接开仓持仓到点回录", len(pos_snaps) == 1, json.dumps(out10["snapshots"]))
with jj._conn() as conn:
    n_btc_pos_snap = conn.execute(
        "SELECT COUNT(*) AS n FROM order_tf_snapshots "
        "WHERE position_id=? AND order_id IS NULL", (PID,)).fetchone()["n"]
check("挂单成交转持仓不双记", int(n_btc_pos_snap) == 0)

out11 = jol.run_sweep(now=NOW, price_of=lambda s: 81.0)
check("持仓回录去重", not [s for s in out11["snapshots"] if s.get("position_id") == SPID])

# ── 10. 阈值可配（jarvis_config 注入） ──
import jarvis_config as jc_mod

_orig_get = jc_mod.get
jc_mod.get = lambda k, d=None, path=None: (
    5.0 if k == "lifecycle_gain_alert_pct"
    else 90.0 if k == "lifecycle_loss_alert_pct"
    else _orig_get(k, d, path))
try:
    g, l = jol._thresholds()
    check("阈值可配 5/90", g == 5.0 and l == 90.0, f"gain={g} loss={l}")
finally:
    jc_mod.get = _orig_get
g, l = jol._thresholds()
check("阈值缺省 20/50", g == 20.0 and l == 50.0)

# ── 11. 查询接口 ──
ls = jol.lifecycle_list()
check("列表 ok 且含挂单/持仓条目", ls.get("ok") and any(
    it["ref"] == f"order-{OID}" for it in ls["items"]) and any(
    it["ref"] == f"pos-{SPID}" for it in ls["items"]),
    json.dumps([it["ref"] for it in ls["items"]]))
o_item = next(it for it in ls["items"] if it["ref"] == f"order-{OID}")
check("列表条目含快照/事件计数", o_item["snapshots"] >= 1 and o_item["events"] >= 3,
      json.dumps(o_item))

lc = jol.lifecycle_of(f"order-{OID}")
check("按 order 查生命周期", lc.get("ok") and lc["order"]["id"] == OID
      and lc["position"]["id"] == PID)
stages = [t["stage"] for t in lc["timeline"]]
check("时间线含 下单→回录→成交→阈值",
      "placed" in stages and "tf_snapshot" in stages and "filled" in stages
      and jol.EVENT_GAIN_ALERT in stages and jol.EVENT_LOSS_ALERT in stages,
      json.dumps(stages))

lc2 = jol.lifecycle_of(str(PID))
check("按持仓数字 id 查（反查挂单）", lc2.get("ok") and lc2["order"]["id"] == OID)
lc3 = jol.lifecycle_of(f"pos-{SPID}")
check("按 pos-id 查直接开仓单", lc3.get("ok") and lc3["order"] is None
      and lc3["position"]["id"] == SPID)
check("不存在引用 404 语义", not jol.lifecycle_of("pos-424242").get("ok"))
check("非法引用拒绝", not jol.lifecycle_of("bad-ref").get("ok"))

# ── 12. 巡检对发信失败/取价失败的容错 ──
SENT.clear()
BAD_PID = jpt._insert_position("DOGEUSDT", 100.0, time.strftime("%Y-%m-%d"), 0.10,
                               None, 0.2, 0.05, 30, None, side="buy",
                               signal_source="manual")
out12 = jol.run_sweep(now=NOW, price_of=lambda s: None)   # 全部取价失败
check("取价失败整轮不抛错", isinstance(out12, dict) and out12["alerts"] == [])

_orig_deliver = jol._deliver
jol._deliver = lambda subject, body: {"ok": False, "reason": "SMTP down"}
try:
    out13 = jol.run_sweep(now=NOW, price_of=lambda s: 0.15)   # +50% 盈利
    hit = [a for a in out13["alerts"] if a["position_id"] == BAD_PID]
    check("发信失败仍落去重标记不抛错", len(hit) == 1)
finally:
    jol._deliver = _orig_deliver
SENT.clear()
out14 = jol.run_sweep(now=NOW, price_of=lambda s: 0.15)
check("发信失败后不重试轰炸（标记已落）", len(SENT) == 0 and not [
    a for a in out14["alerts"] if a["position_id"] == BAD_PID])

print()
if fails:
    print(f"FAILED: {len(fails)} 项 → {fails}")
    raise SystemExit(1)
print("模拟下单全生命周期冒烟测试全部通过 ✅")
