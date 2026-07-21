// Footprint 真实行情（币安合约）浏览器实连验证：/footprint 全链路。
// 需 dev server 已在 5173 运行。零依赖，直接驱动系统 Chrome 的 CDP。
// 用法：node _realfeed_browser_check.mjs
//   访问币安需代理时：FP_PROXY=http://127.0.0.1:10809 node _realfeed_browser_check.mjs
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9224;

const profile = mkdtempSync(join(tmpdir(), "fp-real-"));
const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  ...(process.env.FP_PROXY
    ? [`--proxy-server=${process.env.FP_PROXY}`, `--proxy-bypass-list=localhost;127.0.0.1;<local>`]
    : []),
  "--headless=new",
  "--disable-gpu",
  "--window-size=1440,900",
  "--no-first-run",
  "http://localhost:5173/",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getWsUrl() {
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://localhost:${PORT}/json/list`);
      const tabs = await res.json();
      // 只认真实页面 tab（headless 也会挂内置扩展的 background_page，不能误连）
      const tab = tabs.find((t) => t.type === "page" && t.url.includes("localhost:5173"));
      if (tab?.webSocketDebuggerUrl) return tab.webSocketDebuggerUrl;
    } catch { /* retry */ }
    await sleep(250);
  }
  throw new Error("CDP 连接失败（未发现 localhost:5173 页面 tab）");
}

let msgId = 0;
const pending = new Map();
let ws;

function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++msgId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evalJs(expression, awaitPromise = true) {
  const r = await send("Runtime.evaluate", { expression, awaitPromise, returnByValue: true });
  if (r.exceptionDetails) {
    throw new Error("页面 JS 异常: " + JSON.stringify(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text));
  }
  return r.result.value;
}

const results = [];
const record = (name, pass, detail) => {
  results.push({ name, pass });
  console.log(`${pass ? "PASS" : "FAIL"} | ${name} | ${detail}`);
};

try {
  ws = new WebSocket(await getWsUrl());
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? reject(new Error(m.error.message)) : resolve(m.result);
    }
  };
  await send("Runtime.enable");
  await send("Page.enable");

  // 预设：ETHUSDT + real 源（默认 real，这里显式固化），再进足迹页。
  // 首个 tab 可能尚未离开 about:blank（无 localStorage 权限）——重试到进入 5173 源。
  let preset = false;
  for (let i = 0; i < 20 && !preset; i++) {
    preset = await evalJs(`(() => {
      try {
        if (!location.origin.includes("localhost:5173")) return false;
        localStorage.setItem("jarvis.symbol", "ETHUSDT");
        localStorage.setItem("jarvis-footprint-source", "real");
        return true;
      } catch { return false; }
    })()`, false).catch(() => false);
    if (!preset) await sleep(500);
  }
  if (!preset) throw new Error("localStorage 预设失败（页面未进入 5173 源）");
  await send("Page.navigate", { url: "http://localhost:5173/footprint" });

  // 等数据就绪（真实回填含 60 页 aggTrades，给足 90s）
  let ready = false;
  for (let i = 0; i < 180; i++) {
    await sleep(500);
    ready = await evalJs(`(() => {
      const d = window.__fpDebug;
      const bars = d?.barsRef?.current ?? [];
      const loading = [...document.querySelectorAll("div")].some(x => x.textContent === "加载足迹数据…" && x.offsetParent);
      return !loading && bars.length > 50;
    })()`).catch(() => false);
    if (ready) break;
  }
  record("页面加载(real 源)", ready, ready ? "足迹数据就绪" : "90s 未就绪");
  if (!ready) throw new Error("页面未就绪");

  // 1. 价格区间：ETH 合约现价应在真实区间（~1800±），绝非 mock 的 3400±
  const px = await evalJs(`(() => {
    const bars = window.__fpDebug.barsRef.current;
    const last = bars[bars.length - 1];
    return { n: bars.length, close: last.close, time: last.time, sym: last.symbol, tf: last.timeframe };
  })()`);
  const priceReal = px.close > 1000 && px.close < 3000;
  record("ETHUSDT 现价真实区间", priceReal, `close=${px.close} bars=${px.n} (${px.sym} ${px.tf})`);

  // 2. 近窗柱有足迹格
  const fp = await evalJs(`(() => {
    const bars = window.__fpDebug.barsRef.current;
    const withLv = bars.filter(b => b.levels.length > 0);
    const last = withLv[withLv.length - 1];
    return { withLevels: withLv.length, lastLevels: last ? last.levels.length : 0, ohlcOnly: bars.filter(b => b.ohlcOnly).length };
  })()`);
  record("近窗真足迹格", fp.withLevels > 5 && fp.lastLevels > 3, `足迹柱 ${fp.withLevels} 根（末柱 ${fp.lastLevels} 价位层）/ ohlcOnly ${fp.ohlcOnly} 根`);

  // 3. WS 实时推送更新末柱
  const t0 = await evalJs(`(() => { const b = window.__fpDebug.barsRef.current; const l = b[b.length-1]; return { time: l.time, vol: l.totalVol, close: l.close }; })()`);
  await sleep(10_000);
  const t1 = await evalJs(`(() => { const b = window.__fpDebug.barsRef.current; const l = b[b.length-1]; return { time: l.time, vol: l.totalVol, close: l.close }; })()`);
  const updated = t1.time > t0.time || t1.vol > t0.vol || t1.close !== t0.close;
  record("WS 实时更新末柱", updated, `10s 内 vol ${t0.vol}→${t1.vol}, close ${t0.close}→${t1.close}${t1.time > t0.time ? "（新柱）" : ""}`);

  // 4. 切 4h：远期 ohlcOnly（levels 空）+ 数据量足
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.trim() === "4h")?.click(); "ok"`, false);
  let tf4h = null;
  for (let i = 0; i < 60; i++) {
    await sleep(500);
    tf4h = await evalJs(`(() => {
      const bars = window.__fpDebug?.barsRef?.current ?? [];
      if (bars.length < 30 || bars[0].timeframe !== "4h") return null;
      const last = bars[bars.length - 1];
      return { n: bars.length, ohlcOnly: bars.filter(b => b.ohlcOnly).length, lastClose: last.close, lastFresh: Date.now() - last.time < 14_400_000 + 300_000 };
    })()`).catch(() => null);
    if (tf4h) break;
  }
  record("4h 周期(ohlcOnly 蜡烛)", !!tf4h && tf4h.ohlcOnly > 20 && tf4h.lastClose > 1000 && tf4h.lastClose < 3000 && tf4h.lastFresh,
    tf4h ? `bars=${tf4h.n} ohlcOnly=${tf4h.ohlcOnly} close=${tf4h.lastClose} 新鲜=${tf4h.lastFresh}` : "4h 数据未就绪");

  // 5. 回切 1m 正常（IndexedDB 缓存 + 会话内运行时复用）
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.trim() === "1m")?.click(); "ok"`, false);
  let back1m = false;
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    back1m = await evalJs(`(() => {
      const bars = window.__fpDebug?.barsRef?.current ?? [];
      return bars.length > 50 && bars[0].timeframe === "1m";
    })()`).catch(() => false);
    if (back1m) break;
  }
  record("回切 1m", back1m, back1m ? "正常" : "未恢复");

  console.log("\n=== 汇总 ===");
  const failed = results.filter(r => !r.pass);
  console.log(`通过 ${results.length - failed.length}/${results.length}`);
  process.exitCode = failed.length > 0 ? 1 : 0;
} catch (err) {
  console.error("检查异常:", err.message);
  process.exitCode = 2;
} finally {
  try { ws?.close(); } catch { /* noop */ }
  chrome.kill("SIGKILL");
  await sleep(400);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
}
