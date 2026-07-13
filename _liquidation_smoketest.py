"""离线冒烟：爆仓面板引擎（纯函数，不联网）——归一化 / 窗口统计 / 大额事件 / 簇检测。

mock forceOrder 序列覆盖：实时消息形态 + 历史库行形态、去重、方向语义
（SELL=多头被强平）、窗口过滤、大额阈值、同向簇合并与门槛。
"""
import jarvis_liquidation as jl

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


NOW = 1_800_000_000_000  # 固定 now（毫秒），全部用例确定性


def rt(sym, side, price, qty, t):
    """实时消息形态（币安组合流 data）"""
    return {"o": {"s": sym, "S": side, "ap": str(price), "q": str(qty), "T": t}}


def hist(sym, side, price, qty, t, notional=None):
    """历史库行形态（force_orders 表）"""
    return {"symbol": sym, "side": side, "price": price, "qty": qty,
            "avg_price": price, "trade_time": t,
            "notional": notional if notional is not None else price * qty}


# ─── 1. normalize_event：两种形态 + 方向语义 + 脏数据 ───
e = jl.normalize_event(rt("BTCUSDT", "SELL", 60000, 0.5, NOW))
check("实时形态归一：SELL=多头被强平", e is not None and e["side_liquidated"] == "long"
      and e["notional"] == 30000.0, str(e))
e = jl.normalize_event(hist("ETHUSDT", "BUY", 3000, 10, NOW))
check("历史形态归一：BUY=空头被强平", e is not None and e["side_liquidated"] == "short"
      and e["notional"] == 30000.0, str(e))
check("脏数据（缺 side）→ None", jl.normalize_event({"o": {"s": "X", "T": NOW}}) is None)
check("非 dict → None", jl.normalize_event("garbage") is None)
check("notional=0 → None",
      jl.normalize_event(rt("BTCUSDT", "SELL", 0, 0, NOW)) is None)

# ─── 2. dedupe：实时缓冲与历史库重叠去重 ───
dup_rt = jl.normalize_event(rt("BTCUSDT", "SELL", 60000, 0.5, NOW))
dup_hist = jl.normalize_event(hist("BTCUSDT", "SELL", 60000, 0.5, NOW))
uniq = jl.dedupe_events([dup_rt, dup_hist])
check("同笔爆仓双源去重 → 1 条", len(uniq) == 1, f"n={len(uniq)}")

# ─── 3. window_stats：窗口过滤 + 多空分桶 + dominance ───
events = [jl.normalize_event(x) for x in [
    rt("BTCUSDT", "SELL", 60000, 1.0, NOW - 10 * 60_000),    # 多爆 60k（窗口内）
    rt("BTCUSDT", "SELL", 60000, 0.5, NOW - 20 * 60_000),    # 多爆 30k（窗口内）
    rt("ETHUSDT", "BUY", 3000, 10, NOW - 30 * 60_000),       # 空爆 30k（窗口内）
    rt("BTCUSDT", "SELL", 60000, 2.0, NOW - 90 * 60_000),    # 多爆 120k（60min 窗口外）
]]
st = jl.window_stats(events, 60, NOW)
check("窗口过滤：60min 外不计", st["long_usd"] == 90000.0 and st["short_usd"] == 30000.0,
      f"long={st['long_usd']} short={st['short_usd']}")
check("笔数统计", st["long_count"] == 2 and st["short_count"] == 1)
check("dominance=+0.5（多头爆仓主导）", st["dominance"] == 0.5, f"d={st['dominance']}")
check("时间序列分桶 3 桶", len(st["series"]) == 3, f"n={len(st['series'])}")
st_all = jl.window_stats(events, 120, NOW)
check("扩到 120min 窗口外单进来", st_all["long_usd"] == 210000.0,
      f"long={st_all['long_usd']}")
check("空事件 → 全零不除零", jl.window_stats([], 60, NOW)["dominance"] == 0.0)

# ─── 4. find_large：大额阈值 + 倒序 + 截断（窗口过滤是 build_summary 的职责） ───
large = jl.find_large(events, 50000.0)
check("大额阈值 5 万：全量 2 笔（60k+120k）", len(large) == 2
      and {e["notional"] for e in large} == {60000.0, 120000.0}, f"n={len(large)}")
large2 = jl.find_large(events, 25000.0)
check("阈值降到 2.5 万：4 笔且时间倒序", len(large2) == 4
      and large2[0]["trade_time"] > large2[-1]["trade_time"], f"n={len(large2)}")

# ─── 5. detect_clusters：同向密集 + 链式合并 + 门槛 ───
base = NOW - 10 * 60_000
cluster_events = [jl.normalize_event(rt("BTCUSDT", "SELL", 60000, 0.1, base + i * 30_000))
                  for i in range(6)]                      # 多头 6 笔，间隔 30s < 180s → 1 簇
sparse = [jl.normalize_event(rt("ETHUSDT", "BUY", 3000, 1, base + i * 300_000))
          for i in range(4)]                              # 空头 4 笔，间隔 300s > 180s → 无簇
cl = jl.detect_clusters(cluster_events + sparse, 180, 5)
check("同向密集 6 笔成 1 簇", len(cl) == 1 and cl[0]["count"] == 6
      and cl[0]["side_liquidated"] == "long", f"clusters={len(cl)}")
check("簇文案含加速警示", "加速" in cl[0]["note"], cl[0]["note"][:40])
check("稀疏事件不成簇（间隔>滑窗）",
      all(c["side_liquidated"] != "short" for c in cl))
cl2 = jl.detect_clusters(cluster_events, 180, 7)
check("门槛升到 7 笔 → 6 笔不成簇", len(cl2) == 0, f"n={len(cl2)}")
# 两段密集中间隔断 → 2 簇
two_burst = ([jl.normalize_event(rt("BTCUSDT", "SELL", 60000, 0.1, base + i * 10_000))
              for i in range(5)]
             + [jl.normalize_event(rt("BTCUSDT", "SELL", 60000, 0.1,
                                      base + 20 * 60_000 + i * 10_000))
                for i in range(5)])
cl3 = jl.detect_clusters(two_burst, 180, 5)
check("两段密集隔断 → 2 簇", len(cl3) == 2, f"n={len(cl3)}")

# ─── 6. build_summary：整体封装（含窗口内外过滤联动） ───
s = jl.build_summary(events + cluster_events, window_min=60, large_usd=50000.0,
                     cluster_window_s=180, cluster_min_count=5, now_ms=NOW)
check("summary 结构齐全",
      all(k in s for k in ("window_min", "stats", "large", "clusters", "thresholds")))
check("summary 簇检测只吃窗口内事件", len(s["clusters"]) == 1,
      f"n={len(s['clusters'])}")
check("summary 大额只含窗口内（120k 窗口外被滤）",
      all(e["notional"] != 120000.0 for e in s["large"])
      and any(e["notional"] == 60000.0 for e in s["large"]),
      f"large={[e['notional'] for e in s['large']]}")
check("summary 阈值透传", s["thresholds"]["large_usd"] == 50000.0
      and s["thresholds"]["cluster_min_count"] == 5)

# ─── 7. 门面 summary()：ws 不可用时优雅降级（本测试环境无 WS 连接） ───
out = jl.summary("BTCUSDT")
check("门面永不抛出且带降级引导", out.get("ok") in (True, False)
      and ("guidance" in out), f"ok={out.get('ok')} degraded={out.get('degraded')}")
check("降级态 guidance 含代理放行提示",
      not out.get("degraded") or "fstream.binance.com" in (out.get("guidance") or ""))

print("\n=== " + ("全部通过" if not fails else f"失败 {len(fails)}: {fails}") + " ===")
raise SystemExit(1 if fails else 0)
