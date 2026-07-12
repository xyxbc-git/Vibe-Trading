"""Volume Profile 分布结构引擎冒烟测试。合成 K 线，不联网不碰真实库。"""
from __future__ import annotations

import math

import jarvis_volume_profile as jvp

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


BAR_MS = 900_000  # 15m


def mk_bar(i: int, center: float, spread: float, volume: float) -> dict:
    return {"time": i * BAR_MS, "open": center, "high": center + spread,
            "low": center - spread, "close": center, "volume": volume}


def mk_normal_seg(i0: int, n: int, center: float) -> list[dict]:
    """正态段：价格围绕 center 窄幅震荡，量在中心最大、两侧衰减（钟形）。"""
    rows = []
    for k in range(n):
        # 价格在 center±1.2% 内做往返；量按高斯钟形
        off = math.sin(k / n * math.pi * 4) * center * 0.012
        dist = abs(off) / (center * 0.012)  # 0=中心 1=边缘
        v = 100.0 * math.exp(-3.0 * dist * dist) + 8.0
        rows.append(mk_bar(i0 + k, center + off, center * 0.004, v))
    return rows


def mk_oneway_seg(i0: int, n: int, start: float, end: float) -> list[dict]:
    """直拉段：价格单调直线上行，每根量均匀（无密集换手区）。"""
    step = (end - start) / n
    return [mk_bar(i0 + k, start + step * (k + 0.5), abs(step) * 0.6, 50.0)
            for k in range(n)]


# ── 1. compute_profile 基础 ──
seg = mk_normal_seg(0, 40, 60000.0)
p = jvp.compute_profile(seg)
check("profile 非空", p is not None)
check("POC 落在震荡中心附近", abs(p["poc"] - 60000.0) / 60000.0 < 0.01,
      f"poc={p['poc']:.0f}")
check("VAH>POC>VAL", p["vah"] > p["poc"] > p["val"])
va_cov = sum(p["volumes"][p["va_l"]:p["va_r"] + 1]) / p["total_volume"]
check("价值区覆盖 ≥70%", va_cov >= 0.70, f"cov={va_cov:.2f}")
check("数据不足返回 None", jvp.compute_profile(seg[:3]) is None)

# ── 2. 形态识别 ──
check("正态段判 normal", jvp.classify_shape(p) == "normal",
      jvp.classify_shape(p))

one = jvp.compute_profile(mk_oneway_seg(0, 40, 60000.0, 66000.0))
check("直拉段判 flat", jvp.classify_shape(one) == "flat", jvp.classify_shape(one))

# 偏斜段：量峰贴在区间底部（长上影结构）
skew_rows = []
for k in range(40):
    v = 120.0 if k < 6 else 12.0  # 量集中在最初的低价区
    price = 60000.0 + (k * 40.0)
    skew_rows.append(mk_bar(k, price, 30.0, v))
sk = jvp.compute_profile(skew_rows)
check("量贴边段判 skewed", jvp.classify_shape(sk) == "skewed", jvp.classify_shape(sk))

# ── 3. 台阶式三连正态 → multi-distribution + tripleConfirmed ──
# 6 段 × 40 根 = 240 根，自适应窗口 = 240/6 = 40 与台阶对齐（理想台阶结构）
rows = []
i = 0
for center in (59000.0, 60000.0, 61500.0, 63000.0):  # 四个台阶正态
    rows += mk_normal_seg(i, 40, center)
    i += 40
rows += mk_oneway_seg(i, 80, 63500.0, 65500.0)  # 末两段直拉突破
i += 80
out = jvp.analyze(rows, "BTC", "15m")
st = out["structure"]
check("识别出 ≥5 段分布", len(out["distributions"]) >= 5,
      f"segs={len(out['distributions'])}")
check("正态段数 ≥3", st["normalCount"] >= 3, f"normalCount={st['normalCount']}")
check("三连分布确认成立", st["tripleConfirmed"] is True)
check("结构 = multi-distribution", st["type"] == "multi-distribution", st["type"])
check("note 是中文解释", "正态" in st["note"] and "三连" in st["note"])
check("symbol 补全 USDT", out["symbol"] == "BTCUSDT")
check("volumeShare 归一", abs(sum(d["volumeShare"] for d in out["distributions"]) - 1.0) < 0.01)
check("时间为 ISO 格式", out["distributions"][0]["startT"].endswith("Z"))

cur = out["currentPosition"]
check("现价在末段（直拉高位）→ above", cur["vsLastPoc"] == "above", cur["vsLastPoc"])
check("回补目标 = 下方最近正态 POC ≈63000",
      cur["pullbackTarget"] is not None and abs(cur["pullbackTarget"] - 63000.0) < 400,
      f"target={cur['pullbackTarget']}")

# ── 4. 纯单边直拉 → one-way ──
rows2 = mk_oneway_seg(0, 240, 60000.0, 90000.0)
out2 = jvp.analyze(rows2, "ETHUSDT", "1h")
check("单边直拉 → one-way", out2["structure"]["type"] == "one-way",
      out2["structure"]["type"])
check("直拉不构成三连确认", out2["structure"]["tripleConfirmed"] is False)

# ── 5. 空数据降级 ──
out3 = jvp.analyze([], "BTC", "15m")
check("空数据 → mixed + note", out3["structure"]["type"] == "mixed"
      and out3["currentPosition"]["pullbackTarget"] is None)

# ── 6. mock 契约完整性与确定性 ──
m1 = jvp.mock_response()
m2 = jvp.mock_response()
check("mock 确定性（两次一致）", m1 == m2)
check("mock 契约字段齐全",
      all(k in m1 for k in ("ok", "symbol", "timeframe", "distributions",
                            "structure", "currentPosition", "updatedAt", "disclaimer")))
check("mock 三连确认样例", m1["structure"]["tripleConfirmed"] is True
      and m1["structure"]["normalCount"] == 3)
d0 = m1["distributions"][0]
check("mock 分布字段齐全",
      all(k in d0 for k in ("startT", "endT", "poc", "vah", "val", "shape", "volumeShare")))

# ═══════════════ t18b 增补：低量缺口 + 分布合并 + 不做反转铁律 ═══════════════

# ── 7. 缺口识别与回补判定 ──
# 两个台阶正态中心差 8%（60000 → 65000），价值区必分离 → 上行缺口
rows_gap = mk_normal_seg(0, 40, 60000.0) + mk_normal_seg(40, 40, 65000.0)
out_gap = jvp.analyze(rows_gap, "BTC", "15m")
gaps = out_gap["gaps"]
check("台阶跳空识别出缺口", len(gaps) >= 1, f"gaps={len(gaps)}")
if gaps:
    g0 = gaps[0]
    check("gap 字段齐全",
          all(k in g0 for k in ("priceHigh", "priceLow", "betweenT", "filled", "note")),
          str(g0.keys()))
    check("gap 区间有序", g0["priceHigh"] > g0["priceLow"],
          f"{g0['priceLow']}~{g0['priceHigh']}")
    check("betweenT 为两个 ISO", len(g0["betweenT"]) == 2
          and all(t.endswith("Z") for t in g0["betweenT"]))
    check("跳空未回踩 → filled=False", g0["filled"] is False, str(g0["filled"]))
    check("未回补 note 提示大概率回补", "大概率被回补" in g0["note"], g0["note"])

# 第三段价格跌回第一台阶 → 上行缺口被穿越 → 存在 filled=True 的缺口
rows_filled = rows_gap + mk_normal_seg(80, 40, 60000.0)
out_filled = jvp.analyze(rows_filled, "BTC", "15m")
check("价格回落穿越 → 出现已回补缺口",
      any(g["filled"] for g in out_filled["gaps"]),
      str([(g["priceLow"], g["priceHigh"], g["filled"]) for g in out_filled["gaps"]]))

# 价值区连续的平稳走势 → 无缺口
out_nogap = jvp.analyze(mk_normal_seg(0, 40, 60000.0) + mk_normal_seg(40, 40, 60400.0),
                        "BTC", "15m")
check("价值区衔接无缺口", len(out_nogap["gaps"]) == 0, str(out_nogap["gaps"]))

# ── 8. nearestUnfilledGap ──
cur_gap = out_gap["currentPosition"]
check("currentPosition 含 nearestUnfilledGap 字段", "nearestUnfilledGap" in cur_gap)
check("未回补缺口进入回补目标",
      cur_gap["nearestUnfilledGap"] is not None
      and cur_gap["nearestUnfilledGap"]["priceHigh"] > cur_gap["nearestUnfilledGap"]["priceLow"],
      str(cur_gap["nearestUnfilledGap"]))
check("note 提及未回补缺口", "未回补" in cur_gap["note"], cur_gap["note"])
cur_nogap = out_nogap["currentPosition"]
check("无缺口时 nearestUnfilledGap=None", cur_nogap["nearestUnfilledGap"] is None)

# ── 9. 分布合并（三个儿子合成一个爸爸）──
# 三段中心几乎重叠（60000/60200/60100）→ 价值区纠缠 → 合并 childCount≥3
rows_merge = (mk_normal_seg(0, 40, 60000.0) + mk_normal_seg(40, 40, 60200.0)
              + mk_normal_seg(80, 40, 60100.0))
out_merge = jvp.analyze(rows_merge, "BTC", "15m")
mp = out_merge["mergedProfile"]
check("重叠分布触发合并", mp is not None, str(mp))
if mp:
    check("merged 字段齐全",
          all(k in mp for k in ("poc", "vah", "val", "childCount", "note")), str(mp.keys()))
    check("childCount ≥ 3", mp["childCount"] >= 3, str(mp["childCount"]))
    check("合并价值区包住各子分布 POC",
          all(mp["val"] <= d["poc"] <= mp["vah"] for d in out_merge["distributions"][:3]),
          f"val={mp['val']} vah={mp['vah']}")
    check("合并 note 含爸爸语义", "合成一个爸爸" in mp["note"], mp["note"])

# 台阶分离场景：同台阶内相邻段可合并（同一分布被窗口切开），但合并簇
# 绝不能跨越缺口把两个台阶焊在一起 → 合并价值区必须完整落在单一台阶内
mp_gap = out_gap["mergedProfile"]
check("合并簇不跨越缺口台阶",
      mp_gap is None or mp_gap["vah"] < 63000.0 or mp_gap["val"] > 62000.0,
      str(mp_gap))

# ── 10. one-way 不做反转铁律 ──
check("one-way note 含不做反转铁律", "不做反转" in out2["structure"]["note"],
      out2["structure"]["note"])
check("one-way note 含单一分布难回归", "难以回归" in out2["structure"]["note"])

# ── 11. mock 增补字段样例 ──
check("mock 含 gaps 两例（一已回补一未回补）",
      len(m1["gaps"]) == 2 and m1["gaps"][0]["filled"] is True
      and m1["gaps"][1]["filled"] is False)
check("mock 含 mergedProfile 三合一",
      m1["mergedProfile"] is not None and m1["mergedProfile"]["childCount"] == 3)
check("mock currentPosition 含 nearestUnfilledGap",
      m1["currentPosition"]["nearestUnfilledGap"] == {"priceHigh": 62400.0,
                                                      "priceLow": 61950.0})

# ── 12. 空数据降级（增补字段）──
out_empty = jvp.analyze([], "BTC", "15m")
check("空数据 gaps=[] merged=None",
      out_empty["gaps"] == [] and out_empty["mergedProfile"] is None
      and out_empty["currentPosition"]["nearestUnfilledGap"] is None)

print()
if fails:
    print(f"FAILED: {len(fails)} 项 → {fails}")
    raise SystemExit(1)
print("Volume Profile 引擎冒烟测试全部通过 ✅")
