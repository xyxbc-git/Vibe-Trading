#!/usr/bin/env python3
"""贾维斯 JARVIS - 可视化仪表盘（独立 FastAPI 应用）。

把前面跑通的 4 个脚本（真实数据 / 因子回测 / 样本外验证 / 闭环简报）
封装成一个网页仪表盘，浏览器里一眼看清：
  - 实时决策简报（信心分 / 仓位 / 止损 / 时间止损）
  - 真实数据卡片（资金费率/OI/多空比/情绪/链上/市场结构）
  - 恐慌贪婪 & 回撤历史曲线（echarts）
  - 已验证因子事件研究表（含 P4 过拟合警示）

独立端口运行，不影响 Vibe-Trading 主服务。
用法：
  ./.venv/bin/python jarvis_dashboard.py            # 默认 127.0.0.1:7899
  ./.venv/bin/python jarvis_dashboard.py --port 7899
"""

from __future__ import annotations

import argparse
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import jarvis_brief as jb
import jarvis_crypto_data as jcd
import jarvis_executor as jx
import jarvis_journal as jj
import jarvis_paper_trader as jpt
import jarvis_wallet as jw
from jarvis_factor_backtest import _build_series, event_study, fetch_fng_all, fetch_price_daily

app = FastAPI(title="贾维斯仪表盘")

# 简单内存缓存：{key: (ts, value)}，TTL 控制刷新频率
_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: int, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


@app.get("/api/snapshot")
def snapshot(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    data = _cached(f"snap:{sym}", 300, lambda: jb.build(sym))
    return JSONResponse(data)


@app.get("/api/series")
def series(symbol: str = "BTCUSDT", days: int = 365):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        prices = fetch_price_daily(spot)
        fng = fetch_fng_all()
        dates = sorted(prices)
        closes, _ma, dd = _build_series(dates, prices)
        sl = slice(max(0, len(dates) - days), len(dates))
        ds = dates[sl]
        return {
            "dates": ds,
            "close": [round(c, 2) for c in closes[sl]],
            "drawdown_pct": [round(x * 100, 2) for x in dd[sl]],
            "fng": [fng.get(d) for d in ds],
        }

    return JSONResponse(_cached(f"series:{sym}:{days}", 600, _calc))


@app.get("/api/kline")
def kline(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 200):
    """Binance 公开 K线（免 Key），供 echarts 自绘蜡烛图 + 叠加决策信号。"""
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    allowed = {"1m", "5m", "15m", "1h", "4h", "1d"}
    iv = interval if interval in allowed else "1h"
    lim = max(20, min(int(limit), 500))

    def _calc():
        raw = jcd._get(jcd.SPOT_API + "/api/v3/klines", {"symbol": spot, "interval": iv, "limit": lim})
        if isinstance(raw, dict):
            return {"error": raw.get("_error", "kline fetch failed"), "rows": []}
        fmt = "%m-%d" if iv in ("1d",) else "%m-%d %H:%M"
        rows = []
        for k in raw:
            ts = time.localtime(k[0] / 1000)
            rows.append({
                "t": time.strftime(fmt, ts),
                "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                "c": float(k[4]), "v": round(float(k[5]), 2),
            })
        return {"symbol": spot, "interval": iv, "rows": rows}

    return JSONResponse(_cached(f"kline:{spot}:{iv}:{lim}", 60, _calc))


@app.get("/api/factor")
def factor(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        prices = fetch_price_daily(spot)
        fng = fetch_fng_all()
        dates = sorted(set(prices) & set(fng))
        return event_study(dates, prices, fng)

    return JSONResponse(_cached(f"factor:{sym}", 1800, _calc))


@app.get("/api/calibration")
def calibration(symbol: str = "BTCUSDT", horizon: int = 30):
    """置信度校准：贾维斯的信心 vs 实际兑现率 + Brier 分。

    口径（透明可复核）：
      - 把信心分 score 的绝对值映射为「方向判断把握度」implied_prob = 0.5 + min(|score|,2)/2 * 0.45（0.5~0.95）。
      - 只统计已评估且方向可判定（偏多/偏空）的快照；中性观望不计入。
      - 实际命中 y = correct(1/0)。Brier = mean((p-y)^2)，越低越好；
        基线 Brier = mean((0.5-y)^2)；Brier 技巧分 BSS = 1 - Brier/基线（>0 说明比瞎猜强）。
    """
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    h = 7 if int(horizon) == 7 else 30
    ckey, rkey = (f"c{h}", f"r{h}")

    def _calc():
        rows = jj.list_recent(spot, 100000)
        pts = []  # (implied_prob, y, score)
        for r in rows:
            score = r.get("conviction_score")
            y = r.get(ckey)
            if score is None or y is None:
                continue
            conf = min(2.0, abs(float(score))) / 2.0
            p = round(0.5 + conf * 0.45, 4)
            pts.append((p, int(y), float(score)))
        n = len(pts)
        if n == 0:
            return {"symbol": spot, "horizon": h, "n": 0, "buckets": [], "brier": None,
                    "brier_baseline": None, "bss": None}
        edges = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]
        labels = ["50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]
        buckets = []
        for i in range(len(labels)):
            lo, hi = edges[i], edges[i + 1]
            grp = [t for t in pts if lo <= t[0] < hi]
            if not grp:
                continue
            buckets.append({
                "label": labels[i],
                "n": len(grp),
                "pred_pct": round(100 * sum(t[0] for t in grp) / len(grp), 1),
                "actual_pct": round(100 * sum(t[1] for t in grp) / len(grp), 1),
            })
        brier = sum((p - y) ** 2 for p, y, _ in pts) / n
        base = sum((0.5 - y) ** 2 for _, y, _ in pts) / n
        bss = (1 - brier / base) if base > 0 else None
        return {
            "symbol": spot, "horizon": h, "n": n,
            "overall_hit_pct": round(100 * sum(y for _, y, _ in pts) / n, 1),
            "buckets": buckets,
            "brier": round(brier, 4),
            "brier_baseline": round(base, 4),
            "bss": round(bss, 3) if bss is not None else None,
        }

    return JSONResponse(_cached(f"calib:{spot}:{h}", 120, _calc))


@app.get("/api/track")
def track(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        return {
            "report": jj.report(spot),
            "recent": jj.list_recent(spot, 30),
        }

    return JSONResponse(_cached(f"track:{sym}", 300, _calc))


@app.post("/api/track/record")
def track_record(symbol: str = "BTCUSDT"):
    """落一条今日快照并回填到期收益，然后清缓存。"""
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    rec = jj.record(spot)
    ev = jj.evaluate(spot)
    _CACHE.pop(f"track:{sym}", None)
    return JSONResponse({"record": rec, "evaluate": ev})


# ─────────────────────────── 模拟交易台账 API ───────────────────────────

def _trader_cfg():
    return jx.load_config()


@app.get("/api/wallet")
def api_wallet():
    cfg = _trader_cfg()
    return JSONResponse(jw.ensure_account(cfg.get("account_equity_usdt", 1000.0)))


@app.get("/api/trader/status")
def api_trader_status(symbol: str | None = None):
    """跟盘账户总览：钱包总权益 + 盈亏比 + 持仓明细。"""
    return JSONResponse(jpt.stats(_trader_cfg(), symbol))


@app.get("/api/positions")
def api_positions(symbol: str | None = None, status: str = "all"):
    rows = jpt.all_positions(symbol)
    if status == "open":
        rows = [r for r in rows if r["status"] == "open"]
    elif status == "closed":
        rows = [r for r in rows if r["status"] == "closed"]
    return JSONResponse(rows)


@app.get("/api/orders")
def api_orders(symbol: str | None = None, all: bool = False):
    return JSONResponse(jw.all_limit_orders(symbol) if all else jw.pending_orders(symbol))


@app.get("/api/ledger")
def api_ledger(limit: int = 50, symbol: str | None = None):
    return JSONResponse(jw.ledger(limit, symbol))


@app.post("/api/wallet/deposit")
def api_deposit(amount: float):
    if amount <= 0:
        return JSONResponse({"ok": False, "reason": "金额必须 > 0"}, status_code=400)
    return JSONResponse(jw.deposit(amount))


@app.post("/api/orders/place")
def api_place_order(symbol: str, side: str, price: float, qty: float,
                    stop_loss: float | None = None, take_profit: float | None = None):
    cfg = _trader_cfg()
    jw.ensure_account(cfg.get("account_equity_usdt", 1000.0))
    return JSONResponse(jw.place_limit_order(symbol, side, price, qty,
                                             stop_loss=stop_loss, take_profit=take_profit))


@app.post("/api/orders/cancel")
def api_cancel_order(order_id: int):
    return JSONResponse(jw.cancel_limit_order(order_id))


@app.post("/api/orders/match")
def api_match_orders():
    return JSONResponse({"matched": jpt.match_limit_orders(_trader_cfg())})


@app.post("/api/positions/close")
def api_close_position(symbol: str):
    cfg = _trader_cfg()
    out = []
    for p in jpt.open_positions(symbol):
        price = jpt.latest_price(cfg, p["symbol"]) or p.get("entry_price")
        out.append(jpt._close_position(p, price, "manual", cfg))
    return JSONResponse({"closed": out})


@app.post("/api/trader/cycle")
def api_trader_cycle(symbols: str = "BTC,ETH,SOL", dry_run: bool = False):
    """跑一轮自动跟盘：撮合限价单 → 盯平仓 → 按决策找开仓。"""
    cfg = _trader_cfg()
    syms = [s.strip() for s in symbols.split(",") if s.strip()] or ["BTC"]
    return JSONResponse(jpt.run_cycle(syms, cfg, dry_run=dry_run))


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>贾维斯 JARVIS · 加密决策仪表盘</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script src="https://s3.tradingview.com/tv.js"></script>
<style>
  :root{--bg:#0b0e14;--card:#141a24;--bd:#222c3a;--fg:#e6edf3;--mut:#8b98a9;--up:#16c784;--down:#ea3943;--accent:#3b82f6;--warn:#f0b90b;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--fg);font-family:-apple-system,"PingFang SC","Microsoft YaHei",Inter,sans-serif;padding:20px;}
  h1{font-size:20px;font-weight:700;display:flex;align-items:center;gap:10px}
  .sub{color:var(--mut);font-size:12px;margin-top:4px}
  .bar{display:flex;gap:10px;align-items:center;margin:16px 0;flex-wrap:wrap}
  select,button{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:8px 12px;font-size:14px;cursor:pointer}
  .inp{background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:8px;font-size:14px;width:110px}
  button.primary{background:var(--accent);border-color:var(--accent)}
  button:hover{filter:brightness(1.15)}
  .grid{display:grid;gap:14px}
  .cards{grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px}
  .card .k{color:var(--mut);font-size:12px}
  .card .v{font-size:22px;font-weight:700;margin-top:6px}
  .card .v small{font-size:12px;color:var(--mut);font-weight:400}
  .decision{display:grid;grid-template-columns:200px 1fr;gap:18px;margin-bottom:14px}
  .gauge{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:6px}
  .plan{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px}
  .plan h3{font-size:15px;margin-bottom:10px}
  .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:13px;font-weight:600}
  .reasons{margin-top:10px;color:var(--mut);font-size:13px;line-height:1.9}
  .charts{grid-template-columns:1fr 1fr}
  .chart{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;height:300px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--bd)}
  th:first-child,td:first-child{text-align:left}
  .pos{color:var(--up)} .neg{color:var(--down)} .warnc{color:var(--warn)}
  .sec{margin:18px 0 8px;font-size:15px;font-weight:600}
  .loading{color:var(--mut)}
  .foot{color:var(--mut);font-size:11px;margin-top:18px;line-height:1.7}
  @media(max-width:820px){.charts{grid-template-columns:1fr}.decision{grid-template-columns:1fr}}
</style>
</head>
<body>
  <h1>🤖 贾维斯 JARVIS · 加密决策仪表盘</h1>
  <div class="sub">真实数据（Binance/CoinGecko/alternative.me/链上） · 因子经 P3 回测 + P4 样本外验证 · 仅研究不构成交易建议</div>

  <div class="bar">
    <select id="symbol">
      <option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>BNBUSDT</option>
    </select>
    <button class="primary" onclick="loadAll()">刷新</button>
    <span id="status" class="loading"></span>
  </div>

  <div class="decision">
    <div class="gauge" id="gauge"></div>
    <div class="plan" id="plan"><div class="loading">加载中…</div></div>
  </div>

  <div class="sec">因子归因（本次信心分由哪些因子贡献 · 绿加分 / 红减分）</div>
  <div class="card" style="height:240px"><div id="cAttr" style="height:100%"></div></div>

  <div class="sec">实时行情（TradingView · 免登录 · 多周期 K 线）</div>
  <div class="card" style="padding:0;overflow:hidden">
    <div id="tvchart" style="height:480px;width:100%"></div>
  </div>

  <div class="sec" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    贾维斯 K 线（含决策信号叠加：入场区间 / 硬止损 / 参考止盈）
    <span id="klineIv" style="display:flex;gap:6px">
      <button data-iv="15m" onclick="setIv('15m')">15m</button>
      <button data-iv="1h" class="primary" onclick="setIv('1h')">1h</button>
      <button data-iv="4h" onclick="setIv('4h')">4h</button>
      <button data-iv="1d" onclick="setIv('1d')">1d</button>
    </span>
    <span id="klineStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="card" style="height:420px"><div id="cKline" style="height:100%"></div></div>

  <div class="sec">真实数据快照</div>
  <div class="grid cards" id="cards"></div>

  <div class="sec">历史图表</div>
  <div class="grid charts">
    <div class="chart" id="chartPrice"></div>
    <div class="chart" id="chartFng"></div>
  </div>

  <div class="sec">已验证因子（事件研究：买入极度恐惧后前瞻收益 vs 基线）</div>
  <div class="card"><div id="factorTable"><div class="loading">加载中…</div></div></div>

  <div class="sec" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    置信度校准（贾维斯有多大把握 vs 实际兑现 · Brier 自我评分）
    <span id="calibH" style="display:flex;gap:6px">
      <button data-h="7" onclick="setCalibH(7)">7天</button>
      <button data-h="30" class="primary" onclick="setCalibH(30)">30天</button>
    </span>
    <span id="calibStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="grid charts">
    <div class="chart" id="cCalib"></div>
    <div class="card" style="height:300px;overflow:auto"><div id="calibStat"><div class="loading">加载中…</div></div></div>
  </div>

  <div class="sec" style="display:flex;align-items:center;gap:12px">
    历史战绩（贾维斯真实前向准确率追踪）
    <button onclick="recordToday()" style="font-size:12px;padding:5px 10px">记录今日决策</button>
    <span id="trackStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="grid charts">
    <div class="chart" id="chartTrack"></div>
    <div class="card" style="height:300px;overflow:auto"><div id="trackStat"><div class="loading">加载中…</div></div></div>
  </div>
  <div class="card" style="margin-top:14px"><div id="trackTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">💰 模拟交易台账（钱包余额 · 限价挂单 · 买卖成交记录）</div>
  <div class="grid cards" id="walletCards"><div class="loading">加载中…</div></div>

  <div class="card" style="margin-top:12px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <strong style="font-size:13px">挂限价单（自己指定点位）：</strong>
      <select id="loSym"><option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>BNBUSDT</option></select>
      <select id="loSide"><option value="buy">买入</option><option value="sell">卖出</option></select>
      <input id="loPrice" placeholder="限价" class="inp"/>
      <input id="loQty" placeholder="数量" class="inp"/>
      <input id="loSL" placeholder="止损(可选)" class="inp"/>
      <input id="loTP" placeholder="止盈(可选)" class="inp"/>
      <button class="primary" onclick="placeOrder()">挂单</button>
      <button onclick="matchOrders()">立即撮合</button>
      <span style="border-left:1px solid var(--bd);height:24px"></span>
      <input id="depAmt" placeholder="入金额" class="inp"/>
      <button onclick="deposit()">入金</button>
      <span style="border-left:1px solid var(--bd);height:24px"></span>
      <input id="cycSyms" placeholder="跟盘币种 BTC,ETH" class="inp" style="width:150px" value="BTC,ETH,SOL"/>
      <button class="primary" onclick="runCycle()">跑一轮自动跟盘</button>
      <button onclick="loadTrader()">刷新台账</button>
      <span id="loStatus" class="loading" style="font-size:12px"></span>
    </div>
    <div class="k" style="margin-top:8px">买单到价（现价≤限价）才成交并冻结资金；点「立即撮合」用当前现价撮合。「跑一轮自动跟盘」按贾维斯决策自动撮合+盯平仓+找开仓（需出网取价，约 10-30 秒）。</div>
  </div>

  <div class="sec">挂单簿（pending）</div>
  <div class="card"><div id="ordersTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">当前持仓</div>
  <div class="card"><div id="positionsTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">已平仓历史（战绩复盘）</div>
  <div class="card"><div id="closedStat" class="k" style="margin-bottom:8px"></div><div id="closedTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">买卖成交记录（钱包资金流水）</div>
  <div class="card"><div id="ledgerTable"><div class="loading">加载中…</div></div></div>

  <div class="foot" id="foot"></div>

<script>
const $ = id => document.getElementById(id);
let gauge, cPrice, cFng, cTrack, cKline, cCalib, cAttr;
let tvWidget = null, tvSymbol = null;
let klineIv = '1h';
let lastDecision = null;  // 缓存最近一次决策，供 K 线叠加信号
let lastTraderStat = null;  // 缓存最近一次跟盘账户总览，供持仓表复用现价/浮盈

function pct(x){return (x>0?'+':'')+x+'%';}
function cls(x){return x>0?'pos':(x<0?'neg':'');}

function renderTV(sym){
  // 免登录嵌入 TradingView 高级图表（实时 K 线、多周期），数据走 Binance 同源
  if(typeof TradingView==='undefined'){ return; }
  if(tvSymbol===sym && tvWidget) return;  // 同币种不重复创建
  tvSymbol = sym;
  $('tvchart').innerHTML = '';
  tvWidget = new TradingView.widget({
    container_id: 'tvchart',
    symbol: 'BINANCE:'+sym,
    interval: '60',
    timezone: 'Asia/Shanghai',
    theme: 'dark',
    style: '1',
    locale: 'zh_CN',
    autosize: true,
    enable_publishing: false,
    hide_side_toolbar: false,
    allow_symbol_change: true,
    studies: ['Volume@tv-basicstudies'],
    backgroundColor: '#141a24',
    gridColor: '#222c3a'
  });
}

function setIv(iv){
  klineIv = iv;
  document.querySelectorAll('#klineIv button').forEach(b=>{
    b.className = (b.getAttribute('data-iv')===iv) ? 'primary' : '';
  });
  loadKline($('symbol').value);
}

async function loadKline(sym){
  $('klineStatus').textContent = '拉取 K 线…';
  try{
    const d = await (await fetch('/api/kline?symbol='+sym+'&interval='+klineIv+'&limit=200')).json();
    if(d.error){ $('klineStatus').textContent='K线失败: '+d.error; return; }
    renderKline(d);
    $('klineStatus').textContent = d.interval+' · '+d.rows.length+' 根';
  }catch(e){ $('klineStatus').textContent='K线失败: '+e; }
}

function renderKline(d){
  if(!cKline) cKline=echarts.init($('cKline'));
  const rows=d.rows||[];
  const dates=rows.map(r=>r.t);
  const ohlc=rows.map(r=>[r.o,r.c,r.l,r.h]);  // echarts: [open,close,low,high]
  const vol=rows.map(r=>r.v);
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  // 叠加决策信号线
  const ml=[];
  const dec=lastDecision||{};
  if(dec.suggested_position_pct>0){
    if(dec.stop_loss) ml.push({yAxis:dec.stop_loss,name:'硬止损',lineStyle:{color:'#ea3943'},label:{formatter:'止损 '+dec.stop_loss,color:'#ea3943',position:'insideEndTop'}});
    if(dec.take_profit_ref) ml.push({yAxis:dec.take_profit_ref,name:'参考止盈',lineStyle:{color:'#16c784'},label:{formatter:'止盈 '+dec.take_profit_ref,color:'#16c784',position:'insideEndTop'}});
    if(dec.entry_zone){const parts=(''+dec.entry_zone).split('~').map(s=>parseFloat(s.trim())).filter(x=>!isNaN(x));
      parts.forEach((p,i)=>ml.push({yAxis:p,name:'入场',lineStyle:{color:'#3b82f6',type:'dashed'},label:{formatter:'入场 '+p,color:'#3b82f6',position:'insideEndTop'}}));}
  }
  cKline.setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
    legend:{data:['K线','成交量'],textStyle:{color:'#8b98a9'},top:0},
    grid:[{left:55,right:20,top:30,height:'62%'},{left:55,right:20,top:'74%',height:'16%'}],
    xAxis:[{type:'category',data:dates,...ax,axisLabel:{color:'#8b98a9'},boundaryGap:true},
           {type:'category',gridIndex:1,data:dates,axisLabel:{show:false},axisLine:{show:false}}],
    yAxis:[{scale:true,...ax,axisLabel:{color:'#8b98a9'}},
           {gridIndex:1,...ax,axisLabel:{show:false},splitLine:{show:false}}],
    dataZoom:[{type:'inside',xAxisIndex:[0,1],start:55,end:100},{type:'slider',xAxisIndex:[0,1],bottom:0,height:14,start:55,end:100,textStyle:{color:'#8b98a9'}}],
    series:[
      {name:'K线',type:'candlestick',data:ohlc,itemStyle:{color:'#16c784',color0:'#ea3943',borderColor:'#16c784',borderColor0:'#ea3943'},
       markLine:{symbol:'none',data:ml,silent:true}},
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:vol,itemStyle:{color:'#3b82f680'}}
    ]
  });
}

async function loadAll(){
  const sym = $('symbol').value;
  renderTV(sym);
  loadKline(sym);
  $('status').textContent = '拉取真实数据中（约 10-20 秒）…';
  try{
    const snap = await (await fetch('/api/snapshot?symbol='+sym)).json();
    renderDecision(snap); renderCards(snap);
    $('status').textContent='更新于 '+snap.generated_at_utc+' UTC';
  }catch(e){ $('status').textContent='快照失败: '+e; }

  fetch('/api/series?symbol='+sym+'&days=365').then(r=>r.json()).then(renderCharts).catch(()=>{});
  fetch('/api/factor?symbol='+sym).then(r=>r.json()).then(renderFactor).catch(()=>{});
  fetch('/api/track?symbol='+sym).then(r=>r.json()).then(renderTrack).catch(()=>{});
  loadCalib(sym);
  loadTrader();
}

// ───────── 模拟交易台账 ─────────
async function loadTrader(){
  try{ lastTraderStat = await (await fetch('/api/trader/status')).json(); renderWallet(lastTraderStat); }catch(e){ lastTraderStat=null; }
  fetch('/api/orders').then(r=>r.json()).then(renderOrders).catch(()=>{});
  fetch('/api/positions?status=open').then(r=>r.json()).then(renderPositions).catch(()=>{});
  fetch('/api/positions?status=closed').then(r=>r.json()).then(renderClosed).catch(()=>{});
  fetch('/api/ledger?limit=40').then(r=>r.json()).then(renderLedger).catch(()=>{});
}

function renderWallet(st){
  const eqc=st.equity_change_pct;
  const cards=[
    ['总权益', (st.equity_usdt??'—')+' U', '较起始 '+(eqc==null?'—':pct(eqc))],
    ['可用现金', (st.cash_usdt??'—')+' U', '起始入金 '+(st.start_equity_usdt??'—')+'U'],
    ['挂单冻结', (st.frozen_usdt??'—')+' U', ''],
    ['持仓市值', (st.holdings_value_usdt??'—')+' U', (st.open_positions||0)+' 个持仓'],
    ['已实现盈亏', (st.realized_pnl_usdt??'—')+' U', '浮动 '+(st.unrealized_pnl_usdt??'—')+'U'],
    ['盈亏比', st.profit_factor??'—', '胜率 '+(st.win_rate_pct==null?'—':st.win_rate_pct+'%')],
  ];
  $('walletCards').innerHTML=cards.map(c=>`<div class="card"><div class="k">${c[0]}</div><div class="v">${c[1]}</div><div class="k" style="margin-top:4px">${c[2]}</div></div>`).join('');
}

function renderOrders(rows){
  if(!rows||!rows.length){ $('ordersTable').innerHTML='<div class="loading">暂无挂单</div>'; return; }
  const tr=rows.map(o=>`<tr><td>#${o.id}</td><td>${o.symbol}</td><td class="${o.side==='buy'?'pos':'neg'}">${o.side==='buy'?'买':'卖'}</td><td>${o.limit_price}</td><td>${o.qty}</td><td>${o.notional_usdt??'—'}</td><td>${o.created_date||''}</td><td><button onclick="cancelOrder(${o.id})" style="padding:3px 8px;font-size:12px">撤单</button></td></tr>`).join('');
  $('ordersTable').innerHTML=`<table><thead><tr><th>单号</th><th>币种</th><th>方向</th><th>限价</th><th>数量</th><th>冻结U</th><th>挂单日</th><th></th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderPositions(rows){
  if(!rows||!rows.length){ $('positionsTable').innerHTML='<div class="loading">暂无持仓</div>'; return; }
  const det={}; ((lastTraderStat&&lastTraderStat.open_detail)||[]).forEach(d=>det[d.id]=d);
  const tr=rows.map(p=>{
    const d=det[p.id]||{};
    const cur=(d.cur_price!=null)?d.cur_price:'—';
    const up=d.unrealized_usdt;
    const upPct=(d.cur_price!=null&&p.entry_price)?+(((d.cur_price/p.entry_price)-1)*100).toFixed(2):null;
    const upTxt=(up==null)?'—':((up>0?'+':'')+up+'U'+(upPct==null?'':` (${upPct>0?'+':''}${upPct}%)`));
    let held=null; if(p.entry_date){const t=new Date(p.entry_date+'T00:00:00'); if(!isNaN(t)) held=Math.floor((Date.now()-t.getTime())/86400000);}
    const ts=p.time_stop_days;
    const heldTxt=(held==null)?'—':`${held}/${ts??'—'} 天`;
    const heldCls=(held!=null&&ts&&held>=ts*0.8)?'neg':'';
    return `<tr><td>#${p.id}</td><td>${p.symbol}</td><td>${p.entry_price}</td><td>${cur}</td><td>${p.qty}</td><td class="${cls(up||0)}">${upTxt}</td><td class="neg">${p.stop_loss??'—'}</td><td class="pos">${p.take_profit??'—'}</td><td>${p.entry_date||''}</td><td class="${heldCls}">${heldTxt}</td><td><button onclick="closePos('${p.symbol}')" style="padding:3px 8px;font-size:12px">平仓</button></td></tr>`;
  }).join('');
  $('positionsTable').innerHTML=`<table><thead><tr><th>持仓</th><th>币种</th><th>入场价</th><th>现价</th><th>数量</th><th>浮动盈亏</th><th>止损</th><th>止盈</th><th>开仓日</th><th>持仓(天)</th><th></th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderClosed(rows){
  const st=lastTraderStat||{};
  $('closedStat').innerHTML=`已平仓 <b>${st.closed_trades??0}</b> 笔 · 胜率 <b>${st.win_rate_pct==null?'—':st.win_rate_pct+'%'}</b>（${st.wins??0} 胜 / ${st.losses??0} 负）· 盈亏比 <b>${st.profit_factor??'—'}</b> · 平均盈 <span class="pos">${st.avg_win_usdt??'—'}U</span> / 平均亏 <span class="neg">${st.avg_loss_usdt??'—'}U</span> · 已实现 <b class="${cls(st.realized_pnl_usdt||0)}">${st.realized_pnl_usdt??'—'}U</b>`;
  if(!rows||!rows.length){ $('closedTable').innerHTML='<div class="loading">暂无已平仓记录</div>'; return; }
  const rmap={stop:'止损',take:'止盈',time:'到期',signal:'反转',manual:'手动'};
  const tr=rows.map(p=>`<tr><td>#${p.id}</td><td>${p.symbol}</td><td>${p.entry_price}</td><td>${p.exit_price??'—'}</td><td>${p.qty}</td><td>${rmap[p.exit_reason]||p.exit_reason||'—'}</td><td class="${cls(p.realized_pnl_usdt||0)}">${p.realized_pnl_usdt==null?'—':((p.realized_pnl_usdt>0?'+':'')+p.realized_pnl_usdt+'U')}</td><td class="${cls(p.realized_pnl_pct||0)}">${p.realized_pnl_pct==null?'—':pct(p.realized_pnl_pct)}</td><td>${p.exit_date||''}</td></tr>`).join('');
  $('closedTable').innerHTML=`<table><thead><tr><th>持仓</th><th>币种</th><th>入场价</th><th>出场价</th><th>数量</th><th>平仓原因</th><th>盈亏U</th><th>盈亏%</th><th>平仓日</th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderLedger(rows){
  if(!rows||!rows.length){ $('ledgerTable').innerHTML='<div class="loading">暂无成交记录</div>'; return; }
  const map={deposit:'入金',buy:'买入',sell:'卖出',freeze:'冻结',unfreeze:'解冻'};
  const tr=rows.map(r=>`<tr><td>${r.dt||''}</td><td>${map[r.type]||r.type}</td><td>${r.symbol||'-'}</td><td class="${cls(r.amount_usdt)}">${(r.amount_usdt>0?'+':'')+r.amount_usdt}</td><td>${r.cash_after??'—'}</td><td>${r.note||''}</td></tr>`).join('');
  $('ledgerTable').innerHTML=`<table><thead><tr><th>时间</th><th>类型</th><th>币种</th><th>金额U</th><th>现金余额U</th><th>备注</th></tr></thead><tbody>${tr}</tbody></table>`;
}

async function placeOrder(){
  const sym=$('loSym').value, side=$('loSide').value, price=$('loPrice').value, qty=$('loQty').value, sl=$('loSL').value, tp=$('loTP').value;
  if(!price||!qty){ $('loStatus').textContent='请填写限价和数量'; return; }
  let url=`/api/orders/place?symbol=${sym}&side=${side}&price=${price}&qty=${qty}`;
  if(sl) url+=`&stop_loss=${sl}`;
  if(tp) url+=`&take_profit=${tp}`;
  $('loStatus').textContent='提交中…';
  try{
    const r=await (await fetch(url,{method:'POST'})).json();
    $('loStatus').textContent=r.ok?`✅ 挂单 #${r.order_id} 成功`:`❌ ${r.reason}`;
    if(r.ok){ $('loPrice').value=''; $('loQty').value=''; $('loSL').value=''; $('loTP').value=''; }
  }catch(e){ $('loStatus').textContent='挂单失败: '+e; }
  loadTrader();
}

async function cancelOrder(id){ await fetch('/api/orders/cancel?order_id='+id,{method:'POST'}); loadTrader(); }

async function matchOrders(){
  $('loStatus').textContent='撮合中（取现价）…';
  try{ const r=await (await fetch('/api/orders/match',{method:'POST'})).json();
    $('loStatus').textContent=`撮合完成：成交 ${(r.matched||[]).length} 笔`;
  }catch(e){ $('loStatus').textContent='撮合失败: '+e; }
  loadTrader();
}

async function runCycle(){
  const syms=($('cycSyms').value||'BTC,ETH,SOL').trim();
  $('loStatus').textContent='自动跟盘中（拉决策+取价，约 10-30 秒）…';
  try{
    const r=await (await fetch('/api/trader/cycle?symbols='+encodeURIComponent(syms),{method:'POST'})).json();
    const m=(r.matched||[]).length, c=(r.closed||[]).length, o=(r.opened||[]).length;
    $('loStatus').textContent=`跟盘完成：撮合 ${m} / 平仓 ${c} / 开仓 ${o} / 当前持仓 ${r.open_after??'—'}`;
  }catch(e){ $('loStatus').textContent='跟盘失败: '+e; }
  loadTrader();
}

async function deposit(){
  const amt=$('depAmt').value;
  if(!amt){ $('loStatus').textContent='请填写入金额'; return; }
  await fetch('/api/wallet/deposit?amount='+amt,{method:'POST'});
  $('depAmt').value=''; loadTrader();
}

async function closePos(sym){
  if(!confirm(sym+' 确认按现价平仓？')) return;
  await fetch('/api/positions/close?symbol='+sym,{method:'POST'});
  loadTrader();
}

let calibH = 30;
function setCalibH(h){
  calibH = h;
  document.querySelectorAll('#calibH button').forEach(b=>{
    b.className = (parseInt(b.getAttribute('data-h'))===h) ? 'primary' : '';
  });
  loadCalib($('symbol').value);
}

async function loadCalib(sym){
  $('calibStatus').textContent = '计算校准…';
  try{
    const c = await (await fetch('/api/calibration?symbol='+sym+'&horizon='+calibH)).json();
    renderCalib(c);
    $('calibStatus').textContent = c.n? ('样本 '+c.n+' 条') : '';
  }catch(e){ $('calibStatus').textContent='校准失败: '+e; }
}

function renderCalib(c){
  if(!cCalib) cCalib=echarts.init($('cCalib'));
  const bk=c.buckets||[];
  // 可靠性曲线：x=预测兑现率, y=实际兑现率；对角线=完美校准
  const pts=bk.map(b=>[b.pred_pct,b.actual_pct,b.n]);
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  cCalib.setOption({
    title:{text:'可靠性曲线（贴对角线=越准）',textStyle:{color:'#e6edf3',fontSize:13}},
    tooltip:{formatter:p=>p.seriesName==='理想'?'完美校准线':`预测兑现 ${p.data[0]}%<br>实际兑现 ${p.data[1]}%<br>样本 ${p.data[2]} 条`},
    grid:{left:50,right:25,top:50,bottom:40},
    xAxis:{type:'value',name:'贾维斯预测兑现率%',min:40,max:100,...ax,axisLabel:{color:'#8b98a9'}},
    yAxis:{type:'value',name:'实际兑现率%',min:0,max:100,...ax,axisLabel:{color:'#8b98a9'}},
    series:[
      {name:'理想',type:'line',data:[[50,50],[100,100]],showSymbol:false,lineStyle:{color:'#8b98a9',type:'dashed'},silent:true},
      {name:'贾维斯',type:'scatter',data:pts,symbolSize:p=>Math.min(40,12+p[2]),
       itemStyle:{color:'#3b82f6'},label:{show:true,position:'top',color:'#8b98a9',formatter:p=>'n='+p.data[2]}}
    ]
  });
  // 统计卡
  if(!c.n){ $('calibStat').innerHTML='<div class="loading">暂无可评估的方向性决策。先 backfill/record + evaluate 积累样本。</div>'; return; }
  const bssTxt = c.bss==null?'—':(c.bss>0?`<span class="pos">+${c.bss}</span>（比瞎猜强）`:`<span class="neg">${c.bss}</span>（不如瞎猜）`);
  let st=`<div style="font-size:13px;line-height:2">`+
    `${c.horizon}天前瞻 · 方向性样本 <b>${c.n}</b> 条 · 总体兑现率 <b>${c.overall_hit_pct}%</b><br>`+
    `Brier 分 <b>${c.brier}</b> <small>(越低越好，基线 ${c.brier_baseline})</small><br>`+
    `Brier 技巧分 BSS <b>${bssTxt}</b><br><br>`+
    `<b>分桶（预测 vs 实际）：</b>`;
  st+=`<table style="margin-top:6px"><thead><tr><th>把握度</th><th>样本</th><th>预测</th><th>实际</th></tr></thead><tbody>`;
  for(const b of bk){
    const gap=b.actual_pct-b.pred_pct;
    st+=`<tr><td>${b.label}</td><td>${b.n}</td><td>${b.pred_pct}%</td><td class="${cls(gap)}">${b.actual_pct}%</td></tr>`;
  }
  st+=`</tbody></table>`;
  st+=`<div class="foot" style="margin-top:10px">注：把握度由信心分映射（|score| 越大越自信）。实际低于预测=过度自信，高于预测=偏保守。这是贾维斯的「自我意识」体检。</div></div>`;
  $('calibStat').innerHTML=st;
}

async function recordToday(){
  const sym = $('symbol').value;
  $('trackStatus').textContent = '记录中…';
  try{
    const r = await (await fetch('/api/track/record?symbol='+sym,{method:'POST'})).json();
    const rec = r.record||{};
    $('trackStatus').textContent = rec.ok ? ('已记录 '+rec.as_of_date+'，并回填 '+(r.evaluate?.outcomes_filled??0)+' 条结果') : ('失败: '+(rec.error||''));
    fetch('/api/track?symbol='+sym).then(x=>x.json()).then(renderTrack);
  }catch(e){ $('trackStatus').textContent='失败: '+e; }
}

function renderTrack(t){
  const rep = t.report||{}, recent = t.recent||[];
  const bh = rep.by_horizon||{};
  // 柱状图：各前瞻 偏多 vs 中性 平均收益
  if(!cTrack) cTrack=echarts.init($('chartTrack'));
  const horizons = Object.keys(bh);
  const dirs = ['偏多（战术）','中性观望'];
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  const series = dirs.map(d=>({name:d,type:'bar',
    data:horizons.map(h=>{const a=(bh[h].by_direction||{})[d]; return a&&a.n?a.avg_ret_pct:0;}),
    label:{show:true,position:'top',color:'#8b98a9',formatter:'{c}%'}}));
  cTrack.setOption({title:{text:'平均前向收益：偏多信号 vs 中性基线',textStyle:{color:'#e6edf3',fontSize:13}},
    tooltip:{trigger:'axis'},legend:{textStyle:{color:'#8b98a9'},top:22},grid:{left:45,right:20,top:55,bottom:30},
    xAxis:{type:'category',data:horizons,...ax,axisLabel:{color:'#8b98a9'}},
    yAxis:{type:'value',name:'收益%',...ax,axisLabel:{color:'#8b98a9'}},
    color:['#16c784','#8b98a9'], series});
  // 统计卡
  let st=`<div style="font-size:13px;line-height:2">累计快照 <b>${rep.total_snapshots??0}</b> 条 · 已评估 <b>${rep.evaluated_outcomes??0}</b> 条<br>`;
  for(const h in bh){const ov=bh[h].overall; if(!ov.n)continue;
    const bull=(bh[h].by_direction||{})['偏多（战术）'];
    st+=`<div style="margin-top:8px"><b>${h}前瞻</b> 全体均值 <span class="${cls(ov.avg_ret_pct)}">${pct(ov.avg_ret_pct)}</span>`;
    if(bull&&bull.n) st+=` · 偏多 <span class="${cls(bull.avg_ret_pct)}">${pct(bull.avg_ret_pct)}</span>（n=${bull.n}，命中 ${bull.hit_rate_pct??'-'}%）`;
    st+=`</div>`;}
  st+=`</div>`;
  $('trackStat').innerHTML = rep.total_snapshots? st : '<div class="loading">还没有快照。点上方「记录今日决策」开始积累，或命令行跑 backfill 立刻出历史战绩。</div>';
  // 最近表
  if(!recent.length){ $('trackTable').innerHTML='<div class="loading">暂无快照</div>'; return; }
  const fmt=(r,ok)=> r==null?'<span class="loading">待评估</span>':`<span class="${cls(r)}">${pct(r)}</span> ${ok===1?'✅':(ok===0?'❌':'·')}`;
  let rows='';
  for(const r of recent){
    rows+=`<tr><td>${r.as_of_date}</td><td>${r.price}</td><td>${r.conviction_score}</td><td>${r.direction}</td><td>${r.position_pct}%</td><td>${fmt(r.r7,r.c7)}</td><td>${fmt(r.r30,r.c30)}</td></tr>`;
  }
  $('trackTable').innerHTML=`<table><thead><tr><th>日期</th><th>价格</th><th>信心</th><th>方向</th><th>仓位</th><th>7天</th><th>30天</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderAttr(d){
  if(!cAttr) cAttr=echarts.init($('cAttr'));
  const attr=(d.attribution||[]).slice().sort((a,b)=>a.contribution-b.contribution);
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  if(!attr.length){
    cAttr.setOption({title:{text:'本次无显式因子贡献（中性）',textStyle:{color:'#8b98a9',fontSize:13},left:'center',top:'center'}},true);
    return;
  }
  cAttr.setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:p=>{const it=p[0];const a=attr[it.dataIndex];return `<b>${a.factor}</b>: ${a.contribution>0?'+':''}${a.contribution}<br><span style="color:#8b98a9">${a.note}</span>`;}},
    grid:{left:120,right:40,top:20,bottom:25},
    xAxis:{type:'value',...ax,axisLabel:{color:'#8b98a9'},name:'对信心分的贡献'},
    yAxis:{type:'category',data:attr.map(a=>a.factor),...ax,axisLabel:{color:'#e6edf3'}},
    series:[{type:'bar',data:attr.map(a=>({value:a.contribution,itemStyle:{color:a.contribution>=0?'#16c784':'#ea3943'}})),
      label:{show:true,position:'right',color:'#8b98a9',formatter:p=>(p.value>0?'+':'')+p.value}}]
  },true);
}

function renderDecision(s){
  const d = s.decision||{}; const fac=s.factor_state||{};
  lastDecision = d;
  renderAttr(d);
  if(cKline) loadKline($('symbol').value);  // 决策更新后重绘 K 线以叠加信号
  const score = d.conviction_score ?? 0;
  if(!gauge) gauge = echarts.init($('gauge'));
  gauge.setOption({
    series:[{type:'gauge',min:-2,max:2,splitNumber:4,radius:'92%',
      axisLine:{lineStyle:{width:14,color:[[0.4,'#ea3943'],[0.6,'#8b98a9'],[1,'#16c784']]}},
      pointer:{width:5}, progress:{show:false},
      axisLabel:{distance:-6,fontSize:9,color:'#8b98a9'}, axisTick:{show:false}, splitLine:{length:10},
      detail:{valueAnimation:true,fontSize:26,offsetCenter:[0,'58%'],formatter:'{value}',color:'#e6edf3'},
      title:{offsetCenter:[0,'82%'],fontSize:12,color:'#8b98a9'},
      data:[{value:score,name:'信心分'}]}]
  });
  const dir = d.direction||'-';
  const color = score>=0.8?'#16c784':(score<=-0.8?'#ea3943':'#8b98a9');
  let html = `<h3>决策：<span class="pill" style="background:${color}22;color:${color}">${dir}</span> &nbsp; 建议仓位 <b>${d.suggested_position_pct??0}%</b></h3>`;
  if(d.suggested_position_pct>0){
    html += `<table style="margin-top:6px">
      <tr><td>入场区间</td><td>${d.entry_zone||'-'}</td></tr>
      <tr><td>硬止损 (-10%)</td><td class="neg">${d.stop_loss||'-'}</td></tr>
      <tr><td>参考止盈 (+8%)</td><td class="pos">${d.take_profit_ref||'-'}</td></tr>
      <tr><td>时间止损</td><td>${d.time_stop_days||'-'} 天</td></tr>
      <tr><td>组合最大风险</td><td class="warnc">≈${d.max_risk_pct||'-'}%</td></tr></table>`;
  }
  html += `<div class="reasons"><b>依据：</b><br>` + (d.reasons||[]).map(r=>'· '+r).join('<br>') + `</div>`;
  $('plan').innerHTML = html;
}

function card(k,v,sub){return `<div class="card"><div class="k">${k}</div><div class="v">${v} ${sub?('<small>'+sub+'</small>'):''}</div></div>`;}

function renderCards(s){
  const f=s.real_data.funding||{}, oi=s.real_data.open_interest||{}, ls=s.real_data.long_short||{},
        fng=s.real_data.fear_greed||{}, ms=s.real_data.market_structure||{}, oc=s.real_data.onchain||{},
        fac=s.factor_state||{};
  let h='';
  h+=card('价格', fac.price??'-', 'USDT');
  h+=card('距高点回撤', (fac.drawdown_from_ath_pct??'-')+'%', fac.above_ma200?'200MA之上':'200MA之下');
  h+=card('资金费率(8h)', (f.last_funding_rate_8h_pct??'-')+'%', f.funding_regime||'');
  h+=card('恐慌贪婪', fng.fng_value??'-', fng.fng_class||'');
  h+=card('全网多空比', ls.global_long_short_ratio??'-', '大户 '+(ls.top_trader_long_short_ratio??'-'));
  h+=card('未平仓OI', oi.open_interest_contracts??'-', '7日 '+(oi.oi_7d_trend_pct??'-')+'%');
  h+=card('BTC市占率', (ms.btc_dominance_pct??'-')+'%', '总市值 '+(ms.total_market_cap_usd_b??'-')+'B');
  if(oc.hashrate_eh_s) h+=card('全网算力', oc.hashrate_eh_s+' EH/s', '内存池 '+(oc.mempool_tx_count??'-'));
  $('cards').innerHTML=h;
  $('foot').innerHTML='数据来源：Binance Futures/Spot、CoinGecko、alternative.me、blockchain.info/mempool.space（真实拉取，非估算）。'+
    '<br>因子 edge 经样本外验证偏弱，本仪表盘刻意以小仓位+硬止损+时间止损控制风险。不构成交易建议。';
}

function renderCharts(d){
  if(!cPrice) cPrice=echarts.init($('chartPrice'));
  if(!cFng) cFng=echarts.init($('chartFng'));
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  cPrice.setOption({title:{text:'价格 & 距高点回撤',textStyle:{color:'#e6edf3',fontSize:13}},
    tooltip:{trigger:'axis'},legend:{textStyle:{color:'#8b98a9'},top:22},grid:{left:55,right:55,top:55,bottom:30},
    xAxis:{type:'category',data:d.dates,...ax,axisLabel:{color:'#8b98a9'}},
    yAxis:[{type:'value',name:'价',...ax,axisLabel:{color:'#8b98a9'},scale:true},
           {type:'value',name:'回撤%',...ax,axisLabel:{color:'#8b98a9'},max:0}],
    series:[{name:'收盘价',type:'line',data:d.close,showSymbol:false,lineStyle:{color:'#3b82f6'}},
            {name:'回撤%',type:'line',yAxisIndex:1,data:d.drawdown_pct,showSymbol:false,areaStyle:{color:'#ea394322'},lineStyle:{color:'#ea3943'}}]});
  cFng.setOption({title:{text:'恐慌贪婪指数',textStyle:{color:'#e6edf3',fontSize:13}},
    tooltip:{trigger:'axis'},grid:{left:45,right:20,top:45,bottom:30},
    xAxis:{type:'category',data:d.dates,...ax,axisLabel:{color:'#8b98a9'}},
    yAxis:{type:'value',min:0,max:100,...ax,axisLabel:{color:'#8b98a9'}},
    visualMap:{show:false,pieces:[{lte:25,color:'#ea3943'},{gt:25,lte:45,color:'#f0b90b'},{gt:45,lte:55,color:'#8b98a9'},{gt:55,lte:75,color:'#7ac74f'},{gt:75,color:'#16c784'}]},
    series:[{name:'F&G',type:'line',data:d.fng,showSymbol:false}]});
}

function renderFactor(f){
  let rows='';
  for(const k in f){
    const lab=k.replace('fng_below_','F&G<');
    for(const h in f[k]){const m=f[k][h];
      rows+=`<tr><td>${lab}</td><td>${h.slice(1)}天</td><td>${m.n_events}</td><td>${m.cond_mean_ret_pct}%</td><td>${m.baseline_mean_ret_pct}%</td><td class="${cls(m.edge_pct)}">${pct(m.edge_pct)}</td><td>${m.cond_win_rate_pct}%</td></tr>`;
    }
  }
  $('factorTable').innerHTML=`<table><thead><tr><th>条件</th><th>持有</th><th>事件数</th><th>条件后收益</th><th>基线</th><th>超额edge</th><th>胜率</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="foot">注：F&G&lt;20 持有 30 天 edge 最佳（胜率显著高于基线）；持有 90 天 edge 转负——极度恐惧是<b>短期反弹</b>信号而非长期持仓。回撤≤-50% 因子经 P4 已判定过拟合，未纳入决策。</div>`;
}

window.addEventListener('resize',()=>{[gauge,cPrice,cFng,cTrack,cKline,cCalib,cAttr].forEach(c=>c&&c.resize());});
loadAll();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯可视化仪表盘")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7899)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
