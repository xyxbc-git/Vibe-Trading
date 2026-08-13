#!/usr/bin/env python3
"""导师 AI 解释层（jarvis_mentor_explain + dashboard explain 端点）离线 smoketest。

不联网、不调 LLM、不碰真实 DB / 真实档案目录：
PROFILE_DIR 重定向临时目录；jarvis_trade_mentor 对接面用 sys.modules stub 验证
（避免触发 agent-8 的 ensure_schema 真库写入）；末尾验证 dashboard 可导入且
/api/mentor/explain/stream 路由已注册。
"""

from __future__ import annotations

import json
import sys
import tempfile
import types

import pandas as pd

import jarvis_mentor_explain as jme

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


def _sweep_df() -> pd.DataFrame:
    """60 根平盘 + 一根下插针 + 一根上插针 + 收尾：受控扫单样本（n=2）。"""
    rows = [{"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
             "volume": 1000.0} for _ in range(60)]
    rows.append({"open": 100.0, "high": 100.4, "low": 98.0, "close": 100.2,
                 "volume": 1500.0})   # 刺破前低 99.5 收回 → 扫多头止损
    rows.append({"open": 100.2, "high": 102.5, "low": 99.8, "close": 100.0,
                 "volume": 1500.0})   # 刺破前高 100.5 回落 → 扫空头止损
    rows += [{"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
              "volume": 1000.0} for _ in range(4)]
    return pd.DataFrame(rows)


REQUIRED_SECTIONS = ("## 波动特性", "## 典型扫单深度", "## 适合周期", "## 我的纪律")

with tempfile.TemporaryDirectory() as tmpd:
    _orig_dir = jme.PROFILE_DIR
    jme.PROFILE_DIR = tmpd
    try:
        # ── 1. 首次生成模板（无 K 线 → 占位符） ──────────────────────────
        r1 = jme.ensure_profile("BTCUSDT")
        check("1a 首次生成 created=True 且落盘", r1["created"] is True
              and r1["path"] and r1["path"].startswith(tmpd), str(r1["path"]))
        check("1b 模板四区段齐全",
              all(s in r1["content"] for s in REQUIRED_SECTIONS),
              str([s for s in REQUIRED_SECTIONS if s not in r1["content"]]))
        check("1c 无数据时留待补充占位", "⏳待补充" in r1["content"])
        check("1d 「我的纪律」含默认纪律条目", "单笔风险" in r1["content"])

        # ── 2. 用户编辑保护：再次 ensure 不覆盖 ─────────────────────────
        with open(r1["path"], "a", encoding="utf-8") as f:
            f.write("- 我自己加的规则：绝不满仓\n")
        r2 = jme.ensure_profile("BTCUSDT")
        check("2a 已存在 created=False", r2["created"] is False)
        check("2b 用户编辑内容原样保留", "绝不满仓" in r2["content"])
        check("2c load_profile 读到同一内容",
              jme.load_profile("BTCUSDT") == r2["content"])
        check("2d load_profile 无档案返回 None", jme.load_profile("NOPE") is None)

        # ── 3. 扫单深度实算（有 K 线 → 数值填入） ────────────────────────
        st = jme.sweep_depth_stats(_sweep_df())
        check("3a 扫单统计 n=2 且分位齐全", st is not None and st["n"] == 2
              and st["median_atr"] is not None and st["p90_atr"] is not None
              and st["p90_atr"] >= st["median_atr"], str(st))
        check("3b 数据不足返回 None",
              jme.sweep_depth_stats(_sweep_df().head(10)) is None
              and jme.sweep_depth_stats(None) is None)
        r3 = jme.ensure_profile("ETHUSDT", df=_sweep_df(), tf="15m")
        check("3c 有数据档案实算填入（×ATR 数字，扫单区无占位）",
              "×ATR" in r3["content"]
              and "插针深度中位数" in r3["content"], r3["content"][:200])
        check("3d 波动特性 ATR 均幅已实算", "ATR14 均幅：约" in r3["content"])

        # ── 4. 路径安全：符号净化防穿越 ─────────────────────────────────
        check("4a 符号净化 btc/usdt→BTCUSDT",
              jme._safe_symbol("btc/usdt") == "BTCUSDT")
        check("4b 路径穿越输入被净化且落在档案目录内",
              jme.profile_path("../../evil").startswith(tmpd)
              and jme.profile_path("../../evil").endswith("EVIL.md"),
              jme.profile_path("../../evil"))
        check("4c 空符号兜底 UNKNOWN", jme._safe_symbol(None) == "UNKNOWN")

        # ── 5. 对接面容错（sys.modules stub，避免真库写入） ──────────────
        _orig_mod = sys.modules.get("jarvis_trade_mentor")
        try:
            stub = types.ModuleType("jarvis_trade_mentor")
            stub.get_plan = lambda pid: {"id": pid, "symbol": "BTCUSDT",
                                         "verdict": {"light": "red"}}
            sys.modules["jarvis_trade_mentor"] = stub
            b = jme.fetch_plan_bundle("7")
            check("5a getter 命中且 plan_id 温和转 int",
                  b is not None and b["id"] == 7, str(b))

            stub2 = types.ModuleType("jarvis_trade_mentor")
            stub2.get_plan = lambda pid: (_ for _ in ()).throw(RuntimeError("db down"))
            sys.modules["jarvis_trade_mentor"] = stub2
            check("5b getter 抛异常 → None 不外抛", jme.fetch_plan_bundle(1) is None)

            stub3 = types.ModuleType("jarvis_trade_mentor")
            sys.modules["jarvis_trade_mentor"] = stub3
            check("5c 无任何 getter → None", jme.fetch_plan_bundle(1) is None)

            stub4 = types.ModuleType("jarvis_trade_mentor")
            stub4.get_plan = lambda pid: None      # 计划不存在
            sys.modules["jarvis_trade_mentor"] = stub4
            check("5d 计划不存在 → None", jme.fetch_plan_bundle(999) is None)
        finally:
            if _orig_mod is not None:
                sys.modules["jarvis_trade_mentor"] = _orig_mod
            else:
                sys.modules.pop("jarvis_trade_mentor", None)

        # ── 6. 样例证据包与消息组装 ─────────────────────────────────────
        sb = jme.sample_bundle()
        check("6a 样例对齐 get_plan 契约（打平行+verdict）",
              {"symbol", "direction", "entry", "stop_loss", "take_profit",
               "emotion_score", "light", "verdict"}.issubset(sb.keys())
              and {"light", "score", "items", "summary"}.issubset(sb["verdict"].keys()),
              str(set(sb.keys())))
        check("6b 样例 items 含 fail 与 unavailable 级别（解释层必须覆盖）",
              {"fail", "unavailable"}.issubset(
                  {it["level"] for it in sb["verdict"]["items"]}))

        msgs = jme.build_messages(sb)
        check("6c messages=system+user 两条", len(msgs) == 2
              and msgs[0]["role"] == "system" and msgs[1]["role"] == "user")
        check("6d 免责与不改判纪律写入 system",
              "不构成投资建议" in msgs[0]["content"]
              and "不许自行改判" in msgs[0]["content"])
        check("6e 情绪口径对齐 1-5 自评（≥4 上头）",
              "≥4" in msgs[0]["content"] and jme.EMOTION_HOT_SCORE == 4)
        digest = json.loads(msgs[1]["content"])
        check("6f user 载荷含档案+裁决且符号已净化",
              digest["symbol"] == "BTCUSDT" and "profile_md" in digest
              and digest["verdict"]["light"] == "red", str(list(digest.keys())))
        check("6g 档案缺失时自动建档（内容进载荷）",
              "## 我的纪律" in digest["profile_md"])
        # 显式传 profile_md 时不落盘、原样进载荷
        msgs2 = jme.build_messages(sb, profile_md="# 自定义档案\n- 只做 4h")
        check("6h 显式档案原样进载荷",
              json.loads(msgs2[1]["content"])["profile_md"].startswith("# 自定义档案"))
    finally:
        jme.PROFILE_DIR = _orig_dir

# ── 8. [R3] 返空重试 + 本地解读三级降级（注入假 chat_stream，全离线） ────
from jarvis_llm_config import LLMCallError  # noqa: E402


def _mk_fn(script: list, calls: list):
    """script[i] = 第 i 次调用行为：list[str]=逐段输出 / Exception=首包前抛错。"""
    def fn(messages, **kw):
        i = len(calls)
        calls.append({"messages": messages, "kw": kw})
        behavior = script[min(i, len(script) - 1)]
        if isinstance(behavior, Exception):
            raise behavior
        yield from behavior
    return fn


_SB = jme.sample_bundle()
_PROFILE = "# X\n\n## 我的纪律\n- 单笔风险 ≤ 1%\n- 不逆势加仓\n"

# 8a 正常：一次出内容，不重试不加注
c1: list = []
out1 = "".join(jme.stream_explanation(_SB, profile_md=_PROFILE,
                                      chat_stream_fn=_mk_fn([["你好", "答案"]], c1)))
check("R3-a 正常路径直通（1 次调用，无降级提示）",
      out1 == "你好答案" and len(c1) == 1 and "本地解读" not in out1, out1[:80])

# 8b 首次返空 → 精简 prompt + max_tokens=2000 重试成功
c2: list = []
out2 = "".join(jme.stream_explanation(_SB, profile_md=_PROFILE,
                                      chat_stream_fn=_mk_fn([[], ["精简答案OK"]], c2)))
check("R3-b 返空自动重试且带提示", "首次返回为空" in out2 and "精简答案OK" in out2
      and len(c2) == 2, out2[:120])
check("R3-b 重试用精简 prompt + 显式 2000 预算",
      c2[1]["kw"].get("max_tokens") == jme.RETRY_MAX_TOKENS
      and c2[1]["messages"][0]["content"] == jme.MENTOR_SYS_LITE
      and len(c2[1]["messages"][1]["content"]) < len(c2[0]["messages"][1]["content"]),
      str(c2[1]["kw"]))

# 8c 两次都空 → 本地解读兜底（标注 + 灯色 + 逐条证据 + 免责）
c3: list = []
out3 = "".join(jme.stream_explanation(_SB, profile_md=_PROFILE,
                                      chat_stream_fn=_mk_fn([[], []], c3)))
check("R3-c 仍空降级本地解读", "已降级本地解读" in out3 and "本地解读，AI 暂不可用" in out3
      and len(c3) == 2, out3[:150])
check("R3-c 本地解读含灯色/硬红线/纪律对照/免责",
      "红灯" in out3 and "一票否决" in out3 and "不逆势加仓" in out3
      and "不构成投资建议" in out3, out3[-120:])

# 8d 首包前调用失败 → 直接本地解读（错误文案带原因）
c4: list = []
out4 = "".join(jme.stream_explanation(_SB, profile_md=_PROFILE,
                                      chat_stream_fn=_mk_fn([LLMCallError("HTTP 500: boom")], c4)))
check("R3-d 调用失败降级本地并带原因", "AI 调用失败" in out4 and "HTTP 500" in out4
      and "本地解读，AI 暂不可用" in out4 and len(c4) == 1, out4[:150])

# 8e 精简消息：证据按 fail 优先排序、≤5 条、纪律入载荷
lite = jme.build_messages_lite(_SB, profile_md=_PROFILE)
_ld = json.loads(lite[1]["content"])
check("R3-e lite 载荷骨架（checks≤5 且 fail 优先）",
      len(_ld["verdict"]["checks"]) <= 5
      and _ld["verdict"]["checks"][0]["level"] == "fail"
      and _ld["verdict"]["light"] == "red"
      and "单笔风险" in _ld["discipline"], str(_ld["verdict"]["checks"][0]))

# 8f 本地解读：坏输入不抛出
check("R3-f 本地解读坏输入兜底", "本地解读" in jme.local_explanation({})
      and "不构成投资建议" in jme.local_explanation(None))

# ── 7. dashboard 可导入且解释路由已注册 ─────────────────────────────────
try:
    import jarvis_dashboard as jd
    check("7a dashboard import 无异常", True)
    paths = {getattr(r, "path", "") for r in jd.app.routes}
    check("7b /api/mentor/explain/stream 已注册",
          "/api/mentor/explain/stream" in paths)
except Exception as exc:  # noqa: BLE001
    check("7a dashboard import 无异常", False, repr(exc)[:200])

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
