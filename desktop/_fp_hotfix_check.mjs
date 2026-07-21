// 白屏 hotfix 专项验证：real 源不可达（headless 无代理 = 天然断网仿真）时
// ①10s 内出现兜底卡片（具体错误+重试+降级按钮）②降级按钮切 mock 并恢复渲染
// ③VP 开关开/关不崩。用法：FP_PORT=5199 node _fp_hotfix_check.mjs
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9230;
const APP_PORT = process.env.FP_PORT ?? "5173";
const URL_APP = `http://localhost:${APP_PORT}/footprint`;

const profile = mkdtempSync(join(tmpdir(), "fp-hotfix-"));
const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  "--headless=new", "--disable-gpu", "--window-size=1440,900", "--no-first-run",
  URL_APP,
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, msgId = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++msgId;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? "eval error");
  return r.result.value;
};

const results = [];
const record = (name, pass, detail) => {
  results.push({ name, pass });
  console.log(`${pass ? "PASS" : "FAIL"} | ${name} | ${detail}`);
};

try {
  let wsUrl;
  for (let i = 0; i < 40 && !wsUrl; i++) {
    try {
      const tabs = await (await fetch(`http://localhost:${PORT}/json/list`)).json();
      wsUrl = tabs.find((t) => t.url.includes("footprint"))?.webSocketDebuggerUrl ?? tabs[0]?.webSocketDebuggerUrl;
    } catch { /* retry */ }
    if (!wsUrl) await sleep(250);
  }
  ws = new globalThis.WebSocket(wsUrl);
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

  // 场景A：real 源（默认）+ fapi.binance.com 不可达 → 兜底卡片
  // headless 新 profile localStorage 空 → getFootprintSource() = real ✓
  const t0 = Date.now();
  let cardAt = -1;
  for (let i = 0; i < 60; i++) {
    await sleep(500);
    const card = await evalJs(`document.body.innerText.includes("行情数据加载异常")`);
    if (card) { cardAt = Date.now() - t0; break; }
  }
  record("断网兜底卡片出现", cardAt > 0 && cardAt < 25_000, cardAt > 0 ? `${(cardAt / 1000).toFixed(1)}s 出现（阈值 10s + 页面加载耗时）` : "30s 未出现");

  const cardDetail = await evalJs(`(() => {
    const txt = document.body.innerText;
    return {
      hasReason: txt.includes("币安行情") && (txt.includes("代理") || txt.includes("网络")),
      hasRetry: [...document.querySelectorAll("button")].some(b => b.textContent.includes("重试")),
      hasDowngrade: [...document.querySelectorAll("button")].some(b => b.textContent.includes("切换到模拟行情")),
    };
  })()`);
  record("卡片含具体原因", cardDetail.hasReason, "提及币安/网络/代理");
  record("卡片含重试按钮", cardDetail.hasRetry, "重试可点");
  record("卡片含降级按钮", cardDetail.hasDowngrade, "一键切模拟行情");

  // 场景B：点重试 → 卡片先消失（进入重载）→ 仍连不上 → 再次出现
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("重试"))?.click()`);
  await sleep(800);
  const goneAfterRetry = await evalJs(`!document.body.innerText.includes("行情数据加载异常")`);
  let cardBack = false;
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    cardBack = await evalJs(`document.body.innerText.includes("行情数据加载异常")`);
    if (cardBack) break;
  }
  record("重试按钮工作", goneAfterRetry && cardBack, `点击后卡片消失=${goneAfterRetry}，持续失败后再现=${cardBack}`);

  // 场景C：点降级 → localStorage 切 mock + reload → 数据正常渲染
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("切换到模拟行情"))?.click()`);
  await sleep(2000);
  let mockOk = false;
  for (let i = 0; i < 120; i++) {
    await sleep(500);
    try {
      mockOk = await evalJs(`(() => {
        const d = window.__fpDebug;
        const loading = [...document.querySelectorAll("div")].some(x => x.textContent === "加载足迹数据…" && x.offsetParent);
        const err = document.body.innerText.includes("行情数据加载异常");
        return !loading && !err && (d?.barsRef.current.length ?? 0) > 10;
      })()`);
    } catch { mockOk = false; } // reload 期间 eval 可能瞬断
    if (mockOk) break;
  }
  const srcNow = await evalJs(`localStorage.getItem("jarvis-footprint-source")`);
  record("一键降级恢复渲染", mockOk && srcNow === "mock", `source=${srcNow}，柱数 ${await evalJs(`window.__fpDebug?.barsRef.current.length ?? 0`)}`);

  // 场景D：VP 开关开/关连点不崩 + 图表继续响应
  for (let i = 0; i < 4; i++) {
    await evalJs(`[...document.querySelectorAll("button")].find(x => x.textContent.trim() === "分布")?.click()`);
    await sleep(350);
  }
  const alive = await evalJs(`(() => {
    const d = window.__fpDebug;
    return !!d && d.barsRef.current.length > 10 && !document.body.innerText.includes("加载异常");
  })()`);
  record("VP 开关连点不崩", alive, "4 次快速切换后页面存活、数据在位");

  console.log("\n=== 汇总 ===");
  const fails = results.filter((r) => !r.pass).length;
  console.log(`通过 ${results.length - fails}/${results.length}`);
  process.exitCode = fails > 0 ? 1 : 0;
} catch (err) {
  console.error("hotfix 验证异常:", err.message);
  process.exitCode = 2;
} finally {
  try { ws?.close(); } catch { /* noop */ }
  chrome.kill("SIGKILL");
  await sleep(500);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
}
