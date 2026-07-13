#!/usr/bin/env python3
"""[M2 s4] Binance WS 实时数据地基离线 smoketest：不联网。

覆盖：流名构建（配置开关组合）/ 组合流消息解析分发（四流 mock）/
环形缓冲容量截断 / 回调注册与异常隔离 / 指数退避序列 /
forceOrder 落库与查询（临时 DB）/ health 结构 / 配置键落位。
"""

from __future__ import annotations

import json
import os
import tempfile

import jarvis_ws_stream as jws

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name} {extra}")


CFG_ALL = {"ws_stream_kline": True, "ws_stream_aggtrade": True,
           "ws_stream_forceorder": True, "ws_stream_depth": True,
           "ws_kline_interval": "1m", "ws_depth_speed": "250ms",
           "ws_buffer_size": 5, "ws_force_order_persist": False}

# ── 1. 流名构建 ──
names = jws.build_stream_names(["BTCUSDT", "ETHUSDT"], CFG_ALL)
check("四流×2币=8流", len(names) == 8, str(names))
check("流名全小写符号", "btcusdt@kline_1m" in names and "ethusdt@depth@250ms" in names,
      str(names))
cfg_part = dict(CFG_ALL, ws_stream_depth=False, ws_stream_forceorder=False)
names2 = jws.build_stream_names(["BTCUSDT"], cfg_part)
check("开关关闭的流不生成", names2 == ["btcusdt@kline_1m", "btcusdt@aggTrade"], str(names2))
cfg_iv = dict(CFG_ALL, ws_kline_interval="15m", ws_depth_speed="100ms")
names3 = jws.build_stream_names(["BTCUSDT"], cfg_iv)
check("周期/速率参数生效", "btcusdt@kline_15m" in names3 and "btcusdt@depth@100ms" in names3,
      str(names3))
# 现货回退模式：forceOrder 无对应流自动跳过；depth 250ms 归一 100ms
names_spot = jws.build_stream_names(["BTCUSDT"], CFG_ALL, market="spot")
check("现货模式 forceOrder 跳过", not any("forceOrder" in n for n in names_spot),
      str(names_spot))
check("现货 depth 归一 100ms", "btcusdt@depth@100ms" in names_spot, str(names_spot))
check("端点策略表 4 档", len(jws._ENDPOINT_PLANS) == 4
      and jws._ENDPOINT_PLANS[0][0] == "futures+proxy"
      and jws._ENDPOINT_PLANS[2][0] == "spot+proxy")

# ── 2. 消息分类 ──
check("kline 分类", jws._classify("btcusdt@kline_1m") == ("kline", "BTCUSDT"))
check("aggTrade 分类", jws._classify("ethusdt@aggTrade") == ("aggTrade", "ETHUSDT"))
check("forceOrder 分类", jws._classify("solusdt@forceOrder") == ("forceOrder", "SOLUSDT"))
check("depth 分类", jws._classify("btcusdt@depth@250ms") == ("depth", "BTCUSDT"))
check("未知流分类 None", jws._classify("btcusdt@bookTicker") == (None, None))
check("坏流名 None", jws._classify("garbage") == (None, None))

# ── 3. 分发：mock 四流消息 → 缓冲 + 统计 + 回调 ──
got_cb: list = []
jws.register_callback("aggTrade", lambda sym, d: got_cb.append((sym, d.get("p"))))


def mk(stream, data):
    return json.dumps({"stream": stream, "data": data})


st, sym = jws.dispatch(mk("btcusdt@kline_1m", {"e": "kline", "k": {"c": "60000"}}), CFG_ALL)
check("dispatch kline", (st, sym) == ("kline", "BTCUSDT"))
st, sym = jws.dispatch(mk("btcusdt@aggTrade", {"e": "aggTrade", "p": "60001", "q": "0.5"}),
                       CFG_ALL)
check("dispatch aggTrade", (st, sym) == ("aggTrade", "BTCUSDT"))
check("aggTrade 回调触发", got_cb == [("BTCUSDT", "60001")], str(got_cb))
st, sym = jws.dispatch(mk("btcusdt@depth@250ms", {"e": "depthUpdate", "b": [], "a": []}),
                       CFG_ALL)
check("dispatch depth", (st, sym) == ("depth", "BTCUSDT"))
check("缓冲可读", len(jws.latest("kline", "BTCUSDT")) == 1
      and jws.latest("aggTrade", "btcusdt", 1)[0]["p"] == "60001")

# 坏消息优雅降级
check("坏 JSON 不抛", jws.dispatch("{not json", CFG_ALL) == (None, None))
check("缺 data 不抛", jws.dispatch(json.dumps({"stream": "btcusdt@aggTrade"}), CFG_ALL)
      == (None, None))
check("非 dict 不抛", jws.dispatch(json.dumps([1, 2]), CFG_ALL) == (None, None))

# 回调抛异常不拖垮分发
jws.register_callback("kline", lambda sym, d: 1 / 0)
st, _ = jws.dispatch(mk("btcusdt@kline_1m", {"k": {"c": "60001"}}), CFG_ALL)
check("回调异常隔离", st == "kline")

# ── 4. 环形缓冲容量截断 ──
for i in range(12):
    jws.dispatch(mk("ethusdt@aggTrade", {"p": str(i)}), CFG_ALL)  # cap=5
buf = jws.latest("aggTrade", "ETHUSDT")
check("环形缓冲截断到 cap=5", len(buf) == 5 and buf[-1]["p"] == "11" and buf[0]["p"] == "7",
      str([b["p"] for b in buf]))

# ── 5. 指数退避 ──
seq = [jws.next_backoff(i, 1.0, 60.0) for i in range(8)]
check("退避序列 1,2,4,...封顶60", seq == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0],
      str(seq))
check("退避非法输入兜底", jws.next_backoff("x", "y", "z") == 5.0)

# ── 6. forceOrder 落库 + 查询（临时 DB）──
tmp_db = os.path.join(tempfile.mkdtemp(prefix="jws_"), "fo.db")
fo_msg = {"e": "forceOrder",
          "o": {"s": "BTCUSDT", "S": "SELL", "p": "59000", "q": "0.02",
                "ap": "59010", "X": "FILLED", "T": 1760000000000}}
check("forceOrder 落库", jws.persist_force_order(fo_msg, db_path=tmp_db))
rows = jws.force_orders_recent("BTCUSDT", db_path=tmp_db)
check("落库可查回", len(rows) == 1 and rows[0]["side"] == "SELL"
      and abs(rows[0]["notional"] - 59010 * 0.02) < 1e-6, str(rows))
# dispatch 带持久化开关
cfg_persist = dict(CFG_ALL, ws_force_order_persist=True)
jws.dispatch(mk("btcusdt@forceOrder", fo_msg), cfg_persist, fo_db_path=tmp_db)
rows2 = jws.force_orders_recent("BTCUSDT", db_path=tmp_db)
check("dispatch 落库开关生效", len(rows2) == 2, str(len(rows2)))
check("坏 forceOrder 不抛", not jws.persist_force_order({"o": {"q": "abc"}},
                                                      db_path=tmp_db) or True)

# ── 7. health 结构 ──
h = jws.health()
check("health 顶层键齐全",
      all(k in h for k in ("running", "connected", "reconnects", "streams", "buffered")),
      str(list(h.keys())))
check("health 每流键齐全",
      all(set(h["streams"][t]) >= {"last_msg_ts", "msg_count", "rate_per_min"}
          for t in jws.STREAM_TYPES))
check("health 计数已累计", h["streams"]["aggTrade"]["msg_count"] >= 13,
      str(h["streams"]["aggTrade"]))

# ── 8. 配置键落位（GROUPS+BOUNDS+DEFAULTS 三处）──
import jarvis_config as jc
ws_keys = [k for k in jc.DEFAULTS if k.startswith("ws_")]
check("ws 配置键 11 个齐全", len(ws_keys) == 11, str(ws_keys))
check("ws 键全部有分组", all(k in jc.GROUPS for k in ws_keys),
      str([k for k in ws_keys if k not in jc.GROUPS]))
check("ws_enabled 默认开", jc.DEFAULTS["ws_enabled"] is True)
check("数值键有 BOUNDS", all(k in jc.BOUNDS for k in
      ("ws_buffer_size", "ws_reconnect_base_s", "ws_reconnect_max_s")))
check("枚举键有 ENUMS", "ws_kline_interval" in jc.ENUMS and "ws_depth_speed" in jc.ENUMS)
check("buffer 夹护栏", jc.clamp("ws_buffer_size", 5) == 100
      and jc.clamp("ws_buffer_size", 10 ** 9) == 100_000)

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
