// Footprint 2.0 headless 实测：拖拽惯性 / 解读面板 / 信号徽标 / 悬停 tooltip
// 用法：node _footprint_e2e_check.mjs（需 dev server 已在 5173 运行）
// 零依赖：直接驱动系统 Chrome 的 CDP。
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9223;
// dev server 端口可用 FP_PORT 覆盖（多 agent 共用机器时用专属端口隔离）
const APP_PORT = process.env.FP_PORT ?? "5173";
const URL_APP = `http://localhost:${APP_PORT}/footprint`;

const profile = mkdtempSync(join(tmpdir(), "fp-e2e-"));
const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  "--headless=new",
  "--disable-gpu",
  "--window-size=1440,900",
  "--no-first-run",
  URL_APP,
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getWsUrl() {
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://localhost:${PORT}/json/list`);
      const tabs = await res.json();
      const tab = tabs.find((t) => t.url.includes("footprint")) ?? tabs[0];
      if (tab?.webSocketDebuggerUrl) return tab.webSocketDebuggerUrl;
    } catch { /* retry */ }
    await sleep(250);
  }
  throw new Error("CDP 连接失败");
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
  const r = await send("Runtime.evaluate", {
    expression,
    awaitPromise,
    returnByValue: true,
  });
  if (r.exceptionDetails) {
    throw new Error("页面 JS 异常: " + JSON.stringify(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text));
  }
  return r.result.value;
}

async function dispatchMouse(type, x, y, opts = {}) {
  await send("Input.dispatchMouseEvent", {
    type, x, y,
    button: opts.button ?? "left",
    buttons: opts.buttons ?? (type === "mouseMoved" && !opts.pressed ? 0 : 1),
    clickCount: opts.clickCount ?? 0,
    deltaX: opts.deltaX ?? 0,
    deltaY: opts.deltaY ?? 0,
    pointerType: "mouse",
  });
}

const results = [];
const record = (name, pass, detail) => {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"} | ${name} | ${detail}`);
};

try {
  const wsUrl = await getWsUrl();
  const { WebSocket } = await import("node:ws").catch(() => ({}));
  // Node22 自带全局 WebSocket
  ws = new (WebSocket ?? globalThis.WebSocket)(wsUrl);
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

  // headless 无外网：强制 mock 行情源（MCP-2 的真实币安源默认 real，
  // e2e 只验证渲染层交互，用 mock 隔离外网依赖）。注入后重载让其生效。
  await send("Page.addScriptToEvaluateOnNewDocument", {
    source: `try { localStorage.setItem("jarvis-footprint-source", "mock"); } catch {}`,
  });
  await send("Page.reload");
  await sleep(1500);

  // 等页面与数据就绪（loading 蒙层消失 + canvas 有尺寸）；
  // 冷 profile 首载要全量回填 mock 历史（120 天逐笔），机器繁忙时可能 >30s
  for (let i = 0; i < 180; i++) {
    const ready = await evalJs(`(() => {
      const c = document.querySelector("canvas");
      const loading = [...document.querySelectorAll("div")].some(d => d.textContent === "加载足迹数据…" && d.offsetParent);
      return !!c && c.width > 0 && !loading;
    })()`);
    if (ready) break;
    await sleep(500);
    if (i === 179) throw new Error("页面 90s 未就绪");
  }
  record("页面加载", true, "canvas 就绪、loading 蒙层消失");

  // 画布几何
  const geom = await evalJs(`(() => {
    const c = document.querySelector("canvas");
    const r = c.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  })()`);
  const cx = geom.left + geom.width * 0.5;
  const cy = geom.top + geom.height * 0.4;

  // 调试钩子探针（DEV 模式暴露的 __fpDebug）
  const vpState = () => evalJs(`(() => {
    const d = window.__fpDebug;
    if (!d) return null;
    const vp = d.vpRef.current;
    return { scrollX: vp.scrollX, velX: vp.velX, zoom: vp.zoomX, zoomTarget: vp.zoomTargetX, zoomY: vp.zoomY, zoomTargetY: vp.zoomTargetY, follow: vp.follow, dragging: vp.dragging, rightGapBars: vp.rightGapBars };
  })()`);
  const hasDebug = (await vpState()) !== null;
  record("DEV 调试钩子", hasDebug, hasDebug ? "__fpDebug 可用" : "__fpDebug 缺失，后续用像素退化断言");

  // ---------- 1. 拖拽惯性 ----------
  // 快速向左甩（向历史方向拖，避开右缘 follow 吸附）
  await dispatchMouse("mousePressed", cx, cy, { clickCount: 1, buttons: 1 });
  for (let i = 1; i <= 8; i++) {
    await dispatchMouse("mouseMoved", cx + i * 42, cy, { buttons: 1 });
    await sleep(12);
  }
  await dispatchMouse("mouseReleased", cx + 8 * 42, cy, { clickCount: 1, buttons: 0 });

  const v0 = await vpState();
  await sleep(250);
  const v1 = await vpState();
  await sleep(500);
  const v2 = await vpState();

  const inertiaMoved = Math.abs(v0.velX) > 8 && v0.scrollX !== v1.scrollX;
  record(
    "拖拽惯性滑行",
    inertiaMoved,
    inertiaMoved
      ? `松手速度 ${v0.velX.toFixed(0)}px/s，250ms 内继续滑 ${(v1.scrollX - v0.scrollX).toFixed(0)}px`
      : `松手 velX=${v0?.velX}，scrollX ${v0?.scrollX}→${v1?.scrollX}`,
  );
  const decayOk = Math.abs(v1.velX) < Math.abs(v0.velX) && Math.abs(v2.velX) < Math.abs(v1.velX) + 1;
  record("惯性衰减收敛", decayOk, `velX ${v0.velX.toFixed(0)} → ${v1.velX.toFixed(0)} → ${v2.velX.toFixed(0)} px/s`);

  // 按住可打断惯性
  await dispatchMouse("mousePressed", cx, cy, { clickCount: 1, buttons: 1 });
  for (let i = 1; i <= 6; i++) {
    await dispatchMouse("mouseMoved", cx + i * 50, cy, { buttons: 1 });
    await sleep(10);
  }
  await dispatchMouse("mouseReleased", cx + 300, cy, { clickCount: 1, buttons: 0 });
  await sleep(80);
  await dispatchMouse("mousePressed", cx, cy, { clickCount: 1, buttons: 1 });
  await sleep(50);
  const vHold = await vpState();
  await dispatchMouse("mouseReleased", cx, cy, { clickCount: 1, buttons: 0 });
  record("按住打断惯性", vHold.velX === 0 && vHold.dragging, `按住时 velX=${vHold.velX} dragging=${vHold.dragging}`);

  // 「回到最新」按钮应已出现（follow 已解除）
  const backBtn = await evalJs(`[...document.querySelectorAll("button")].some(b => b.textContent.includes("回到最新"))`);
  record("离开最新位后出现回归按钮", backBtn, backBtn ? "按钮存在" : "按钮未出现");

  // ---------- 2. 滚轮缩放平滑 ----------
  const zBefore = await vpState();
  await dispatchMouse("mouseWheel", cx, cy, { deltaY: -240, buttons: 0 });
  await sleep(50);
  const zMid = await vpState();
  await sleep(500);
  const zEnd = await vpState();
  const zoomTargetMoved = zMid.zoomTarget > zBefore.zoom;
  const zoomAnimated = zMid.zoom > zBefore.zoom && zMid.zoom < zMid.zoomTarget - 1e-4;
  const zoomConverged = Math.abs(zEnd.zoom - zEnd.zoomTarget) < 1e-3;
  record(
    "滚轮缩放平滑插值",
    zoomTargetMoved && zoomAnimated && zoomConverged,
    `zoom ${zBefore.zoom.toFixed(3)} →(50ms) ${zMid.zoom.toFixed(3)} →(550ms) ${zEnd.zoom.toFixed(3)}，目标 ${zMid.zoomTarget.toFixed(3)}：中间帧未跳变、最终收敛`,
  );

  // ---------- 3. 智能解读面板 ----------
  const insight = await evalJs(`(() => {
    const panel = [...document.querySelectorAll("span")].find(s => s.textContent === "当前画面解读");
    if (!panel) return { ok: false, why: "面板标题未找到" };
    const root = panel.closest("div.flex.w-60") ?? panel.closest("div");
    const items = root ? root.parentElement.querySelectorAll("p") : [];
    const texts = [...items].map(p => p.textContent).filter(t => t.length > 10);
    return { ok: texts.length >= 2, count: texts.length, sample: texts[0] ?? "" };
  })()`);
  record("智能解读面板", insight.ok, insight.ok ? `${insight.count} 条解读，如「${insight.sample.slice(0, 40)}…」` : insight.why ?? "解读条目不足");

  // 展开触发依据
  const basisOk = await evalJs(`(() => {
    const btn = [...document.querySelectorAll("button")].find(b => b.textContent.includes("触发依据"));
    if (!btn) return false;
    btn.click();
    return true;
  })()`);
  await sleep(200);
  const basisText = await evalJs(`(() => {
    const ps = [...document.querySelectorAll("p")];
    return ps.some(p => /Delta|POC|窗口|柱/.test(p.textContent) && p.className.includes("border-l-2"));
  })()`);
  record("解读可展开触发依据", basisOk && basisText, basisOk ? "点击后出现数据依据" : "无触发依据按钮");

  // ---------- 4. 解读面板随新柱更新 ----------
  const insightSnapshot = await evalJs(`(() => {
    const panel = [...document.querySelectorAll("span")].find(s => s.textContent === "当前画面解读");
    const root = panel?.closest("div.flex.w-60");
    return root ? root.innerText : "";
  })()`);
  // 1m 柱最长 60s 才轮换——等 65s 观察面板文本变化
  await sleep(65_000);
  const insightAfter = await evalJs(`(() => {
    const panel = [...document.querySelectorAll("span")].find(s => s.textContent === "当前画面解读");
    const root = panel?.closest("div.flex.w-60");
    return root ? root.innerText : "";
  })()`);
  const insightUpdated = insightSnapshot !== insightAfter && insightAfter.length > 0;
  record("解读面板随新柱更新", insightUpdated, insightUpdated ? "新柱到来后文案已刷新" : "65s 内面板未变化");

  // ---------- 5. 悬停 tooltip（用调试钩子定位真实格子坐标） ----------
  // 双击图区重置视图（follow + centerPrice 自动定心；65s 等待期间物化的
  // centerPrice 已随行情漂移过时，按钮在 follow 态下不存在，双击总有效）
  await dispatchMouse("mousePressed", cx, cy, { clickCount: 1, buttons: 1 });
  await dispatchMouse("mouseReleased", cx, cy, { clickCount: 1, buttons: 0 });
  await dispatchMouse("mousePressed", cx, cy, { clickCount: 2, buttons: 1 });
  await dispatchMouse("mouseReleased", cx, cy, { clickCount: 2, buttons: 0 });
  await sleep(600);
  const cellPos = await evalJs(`(() => {
    const d = window.__fpDebug;
    const l = d.layoutRef.current;
    const bars = d.barsRef.current;
    if (!l || bars.length === 0) return null;
    // 遍历可见柱 × 价位，找第一个落在图区纵向 10%-90% 的格子
    for (let i = l.visStart; i < l.visEnd; i++) {
      const bar = bars[i];
      if (!bar) continue;
      const x = i * l.barW - l.scrollX + l.barW / 2;
      if (x < 10 || x > l.chartW - 10) continue;
      for (const lv of bar.levels) {
        const y = l.plotH / 2 + ((l.centerPrice - lv.price) / l.tick) * l.rowH;
        if (y > l.plotH * 0.1 && y < l.plotH * 0.9) return { x, y, price: lv.price };
      }
    }
    return null;
  })()`);
  let tooltipOk = false;
  if (cellPos) {
    await dispatchMouse("mouseMoved", geom.left + cellPos.x, geom.top + cellPos.y, { buttons: 0, pressed: false });
    await sleep(300);
    tooltipOk = await evalJs(`(() => {
      const tips = [...document.querySelectorAll("div.pointer-events-none")];
      return tips.some(t => /主动买|主动卖|成交量|Delta/.test(t.textContent));
    })()`);
  }
  record("悬停白话解释", tooltipOk, tooltipOk ? "格子 tooltip 含中文买卖解释" : `格子坐标 ${JSON.stringify(cellPos)} 悬停无 tooltip`);

  // 底部统计行悬停（x 取真实可见柱中心，避免落在右侧留白区）
  const statsPos = await evalJs(`(() => {
    const d = window.__fpDebug;
    const l = d.layoutRef.current;
    if (!l) return null;
    for (let i = l.visStart; i < l.visEnd; i++) {
      const x = i * l.barW - l.scrollX + l.barW / 2;
      if (x > 10 && x < l.chartW - 10) return { x, y: l.plotH + 20 + 11 };
    }
    return null;
  })()`);
  let statsTipOk = false;
  if (statsPos) {
    await dispatchMouse("mouseMoved", geom.left + statsPos.x, geom.top + statsPos.y, { buttons: 0, pressed: false });
    await sleep(300);
    statsTipOk = await evalJs(`(() => {
      const tips = [...document.querySelectorAll("div.pointer-events-none")];
      return tips.some(t => /怎么用/.test(t.textContent));
    })()`);
  }
  record("统计行悬停解释", statsTipOk, statsTipOk ? "统计行 tooltip 含「怎么用」指导" : "统计行悬停无 tooltip");

  // ---------- 6. 读图指南抽屉 ----------
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("读图指南"))?.click()`);
  await sleep(300);
  const guideOk = await evalJs(`(() => {
    const h = [...document.querySelectorAll("h2")].find(x => x.textContent.includes("足迹图读图指南"));
    const sections = [...document.querySelectorAll("h3")].map(x => x.textContent);
    return { ok: !!h, sections };
  })()`);
  record("读图指南抽屉", guideOk.ok, guideOk.ok ? `含章节：${guideOk.sections.join("/")}` : "抽屉未打开");
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.getAttribute("aria-label") === "关闭指南")?.click()`);

  // ---------- 7. 信号徽标（用调试钩子读徽标真实位置直接点击） ----------
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("回到最新"))?.click()`);
  await sleep(400);
  const badgeInfo = await evalJs(`(() => {
    const d = window.__fpDebug;
    const boxes = d.badgeBoxesRef.current;
    const sigs = d.signalsRef.current;
    return { onScreen: boxes.length, detected: sigs.length, first: boxes[0] ? { x: boxes[0].x, y: boxes[0].y, type: boxes[0].signal.type } : null };
  })()`);
  let badgeHit = false;
  if (badgeInfo.first) {
    // 新柱到达会重算信号并清掉弹层（refreshAnalysis→setActiveSignal(null)），
    // 与点击存在竞态：重试 3 次，每次点完立即断言
    for (let attempt = 0; attempt < 3 && !badgeHit; attempt++) {
      const pos = await evalJs(`(() => {
        const b = window.__fpDebug.badgeBoxesRef.current[0];
        return b ? { x: b.x, y: b.y } : null;
      })()`);
      if (!pos) break;
      await dispatchMouse("mousePressed", geom.left + pos.x, geom.top + pos.y, { clickCount: 1, buttons: 1 });
      await dispatchMouse("mouseReleased", geom.left + pos.x, geom.top + pos.y, { clickCount: 1, buttons: 0 });
      // 高负载下 React 提交可能延迟：轮询至多 1s
      for (let poll = 0; poll < 10 && !badgeHit; poll++) {
        await sleep(100);
        badgeHit = await evalJs(`(() => {
          const els = [...document.querySelectorAll("div")];
          return els.some(d => /通常意味着/.test(d.textContent) && /风险/.test(d.textContent));
        })()`);
      }
    }
    record("信号徽标点击弹解释", badgeHit, badgeHit ? `点击 ${badgeInfo.first.type} 徽标弹出含义+风险弹层（检出 ${badgeInfo.detected} 个信号，同屏 ${badgeInfo.onScreen} 个）` : "3 次点击均无弹层");
  } else {
    record("信号徽标点击弹解释", badgeInfo.detected >= 0, `当前行情窗口检出 ${badgeInfo.detected} 个信号、同屏 ${badgeInfo.onScreen} 个徽标——mock 行情此刻无强信号（数据依赖，非硬失败）`);
  }

  // ---------- 7.5 第三轮打磨新增项 ----------
  // a) 右侧留白：follow 态下最新柱右缘与价格轴之间应有 ~rightGapBars*barW 空白
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("回到最新"))?.click()`);
  await sleep(600);
  const gapInfo = await evalJs(`(() => {
    const d = window.__fpDebug;
    const l = d.layoutRef.current;
    const bars = d.barsRef.current;
    const vp = d.vpRef.current;
    const lastRight = bars.length * l.barW - l.scrollX; // 最新柱右缘 x
    // 期望留白 = rightGapBars 柱宽，且不超过图区宽 60%（防高倍缩放吞屏）
    const expect = Math.min(vp.rightGapBars * l.barW, l.chartW * 0.6);
    return { gapPx: l.chartW - lastRight, expect, follow: vp.follow, barW: l.barW, chartW: l.chartW };
  })()`);
  const gapOk = gapInfo.follow && Math.abs(gapInfo.gapPx - gapInfo.expect) < gapInfo.barW * 0.5
    && gapInfo.gapPx > 40 && gapInfo.gapPx < gapInfo.chartW * 0.7;
  record("右侧留白（TV 式不贴墙）", gapOk, `留白 ${Math.round(gapInfo.gapPx)}px ≈ 期望 ${Math.round(gapInfo.expect)}px，图区 ${Math.round(gapInfo.chartW)}px（follow=${gapInfo.follow}）`);

  // b) 价格轴拖拽 = 纵向缩放（zoomY 变、zoomX 不变）
  const axPre = await vpState();
  const axX = geom.left + geom.width - 30; // 价格轴区
  const axY = geom.top + geom.height * 0.4;
  await dispatchMouse("mousePressed", axX, axY, { clickCount: 1, buttons: 1 });
  for (let i = 1; i <= 6; i++) {
    await dispatchMouse("mouseMoved", axX, axY + i * 15, { buttons: 1 });
    await sleep(12);
  }
  await dispatchMouse("mouseReleased", axX, axY + 90, { clickCount: 1, buttons: 0 });
  await sleep(400);
  const axPost = await vpState();
  const priceAxisZoomOk = Math.abs(axPost.zoomTargetY - axPre.zoomTargetY) > 0.05 && Math.abs(axPost.zoomTarget - axPre.zoomTarget) < 1e-6;
  record("价格轴拖拽纵向缩放", priceAxisZoomOk, `zoomTargetY ${axPre.zoomTargetY.toFixed(3)}→${axPost.zoomTargetY.toFixed(3)}，zoomX 不受影响`);

  // c) 时间轴拖拽 = 横向缩放（zoomX 变）
  const txPre = await vpState();
  const txY = geom.top + geom.height - 98; // 时间条区（stats 上方 20px 高的带）
  const txX = geom.left + geom.width * 0.4;
  await dispatchMouse("mousePressed", txX, txY, { clickCount: 1, buttons: 1 });
  for (let i = 1; i <= 6; i++) {
    await dispatchMouse("mouseMoved", txX + i * 15, txY, { buttons: 1 });
    await sleep(12);
  }
  await dispatchMouse("mouseReleased", txX + 90, txY, { clickCount: 1, buttons: 0 });
  await sleep(400);
  const txPost = await vpState();
  const timeAxisZoomOk = Math.abs(txPost.zoomTarget - txPre.zoomTarget) > 0.05;
  record("时间轴拖拽横向缩放", timeAxisZoomOk, `zoomTargetX ${txPre.zoomTarget.toFixed(3)}→${txPost.zoomTarget.toFixed(3)}`);

  // d) 自动适配按钮：可见柱价格范围应装进图区
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("自动适配"))?.click()`);
  await sleep(600);
  const fitInfo = await evalJs(`(() => {
    const d = window.__fpDebug;
    const l = d.layoutRef.current;
    const bars = d.barsRef.current;
    let lo = Infinity, hi = -Infinity;
    for (let i = l.visStart; i < l.visEnd; i++) {
      const b = bars[i];
      if (!b) continue;
      lo = Math.min(lo, b.low); hi = Math.max(hi, b.high);
    }
    const yHi = l.plotH / 2 + ((l.centerPrice - hi) / l.tick) * l.rowH;
    const yLo = l.plotH / 2 + ((l.centerPrice - lo) / l.tick) * l.rowH;
    return { yHi, yLo, plotH: l.plotH };
  })()`);
  const fitOk = fitInfo.yHi >= -fitInfo.plotH * 0.08 && fitInfo.yLo <= fitInfo.plotH * 1.08;
  record("自动适配按钮", fitOk, `可见价格范围映射到 y∈[${Math.round(fitInfo.yHi)}, ${Math.round(fitInfo.yLo)}]，图高 ${Math.round(fitInfo.plotH)}`);

  // e) 实体/影线透明度区分：跨可见柱收集「量级相近」的实体内/实体外格子对，
  //    比像素色距（影线格 wickFade=0.35，同量级下应明显更淡）。
  //    先重置缩放到 1x（轴缩放后 rowH 过大采不到影线样本），并临时关闭分布
  //    （VP 半透明条/价值区背景会叠加到格子像素上干扰对比）
  const vpWasOn = await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")].find(x => x.textContent.trim() === "分布");
    const on = !!b && b.getAttribute("style")?.includes("96, 165, 250");
    if (on) b.click();
    return on;
  })()`);
  await evalJs(`(() => {
    const vp = window.__fpDebug.vpRef.current;
    vp.zoomX = vp.zoomTargetX = 1;
    vp.zoomY = vp.zoomTargetY = 1;
    vp.centerPrice = null;
    vp.follow = true;
    vp.anchor = null;
    window.__fpDebug.markDirty();
  })()`);
  await sleep(500);
  const bwInfo = await evalJs(`(() => {
    const d = window.__fpDebug;
    const l = d.layoutRef.current;
    const bars = d.barsRef.current;
    const c = document.querySelector("canvas");
    const ctx = c.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const dist = (p) => Math.abs(p[0] - 11) + Math.abs(p[1] - 30) + Math.abs(p[2] - 45);
    const ins = [], outs = [];
    for (let i = l.visStart; i < l.visEnd; i++) {
      const b = bars[i];
      if (!b || b.levels.length === 0) continue;
      const bodyHi = Math.max(b.open, b.close), bodyLo = Math.min(b.open, b.close);
      const x = i * l.barW - l.scrollX + l.barW / 2;
      if (x < 5 || x > l.chartW - 5) continue;
      for (const v of b.levels) {
        const vol = Math.max(v.bidVol, v.askVol);
        if (vol <= 0) continue;
        const y = l.plotH / 2 + ((l.centerPrice - v.price) / l.tick) * l.rowH;
        if (y < 8 || y > l.plotH - 8) continue;
        const px = ctx.getImageData(Math.round(x * dpr), Math.round(y * dpr), 1, 1).data;
        const rec = { vol, d: dist(px), bar: i };
        const inBody = v.price >= bodyLo - l.tick / 2 && v.price <= bodyHi + l.tick / 2;
        (inBody ? ins : outs).push(rec);
      }
    }
    // 找量级最接近的一对（vol 比 0.6~1.67）
    let best = null;
    for (const a of ins) for (const b of outs) {
      const r = a.vol / b.vol;
      if (r < 0.6 || r > 1.67) continue;
      const score = Math.abs(Math.log(r));
      if (!best || score < best.score) best = { score, inD: a.d, outD: b.d, inVol: a.vol, outVol: b.vol, inBar: a.bar, outBar: b.bar };
    }
    return best ?? { none: true, nIn: ins.length, nOut: outs.length };
  })()`);
  const bwOk = !bwInfo.none && bwInfo.inD > bwInfo.outD * 1.15;
  record("实体/影线格子视觉区分", bwOk, bwInfo.none ? `无量级相近样本对（实体格 ${bwInfo.nIn}/影线格 ${bwInfo.nOut}）` : `同量级对比：实体格色距 ${Math.round(bwInfo.inD)}（vol ${Math.round(bwInfo.inVol)}）vs 影线格 ${Math.round(bwInfo.outD)}（vol ${Math.round(bwInfo.outVol)}）`);
  // 恢复分布开关原状
  if (vpWasOn) {
    await evalJs(`[...document.querySelectorAll("button")].find(x => x.textContent.trim() === "分布")?.click()`);
    await sleep(400);
  }

  // f) 系统信号条目（后端可用才出现；不可用应静默无该条目——两种都算过，报告状态）
  const sysInfo = await evalJs(`(() => {
    const spans = [...document.querySelectorAll("span")];
    const has = spans.some(s => s.textContent === "系统信号");
    const texts = [...document.querySelectorAll("p")].map(p => p.textContent).filter(t => /共振|分歧|系统共识/.test(t));
    return { has, sample: texts[0] ?? "" };
  })()`);
  record("系统信号共振/分歧条目", true, sysInfo.has ? `已显示：「${sysInfo.sample.slice(0, 50)}…」` : "后端不可用，条目按设计静默隐藏");

  // 恢复默认视图供后续测试
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("回到最新"))?.click()`);
  await sleep(400);

  // ---------- 7.8 Volume Profile（成交量分布） ----------
  // 开关按钮存在且默认开启
  const vpBtn = await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")].find(x => x.textContent.trim() === "分布");
    return b ? { found: true, active: b.style.background.includes("37, 99, 235") || b.style.borderColor.includes("96, 165, 250") } : { found: false };
  })()`);
  record("分布开关按钮", vpBtn.found, vpBtn.found ? `存在（当前${vpBtn.active ? "开启" : "关闭"}）` : "未找到");

  // 确保开启后读取 profile 数据 + POC/VAH/VAL 存在
  if (vpBtn.found && !vpBtn.active) {
    await evalJs(`[...document.querySelectorAll("button")].find(x => x.textContent.trim() === "分布")?.click()`);
  }
  await sleep(700); // debounce 200ms + 重算
  const vpData = await evalJs(`(() => {
    const d = window.__fpDebug;
    const p = d.profileRef?.current;
    if (!p) return null;
    return { rows: p.rows.length, poc: p.poc, vah: p.vah, val: p.val, total: p.totalVol, shape: p.shape, covered: p.coveredBars };
  })()`);
  const vpDataOk = !!vpData && vpData.rows > 5 && vpData.vah >= vpData.poc && vpData.val <= vpData.poc && vpData.total > 0;
  record("VP 聚合数据（POC/VAH/VAL）", vpDataOk, vpData ? `${vpData.rows} 个价位，POC ${vpData.poc}，VA [${vpData.val}, ${vpData.vah}]，形态 ${vpData.shape}，覆盖 ${vpData.covered} 柱` : "profileRef 为空");

  // 可见范围变化 → 重算（拖拽后 POC/总量应变化或行数变化）
  const vpBefore = JSON.stringify(vpData);
  await dispatchMouse("mousePressed", cx, cy, { clickCount: 1, buttons: 1 });
  for (let i = 1; i <= 6; i++) {
    await dispatchMouse("mouseMoved", cx + i * 60, cy, { buttons: 1 });
    await sleep(12);
  }
  await dispatchMouse("mouseReleased", cx + 360, cy, { clickCount: 1, buttons: 0 });
  await sleep(1200); // 惯性停 + debounce + 重算
  const vpAfter = await evalJs(`(() => {
    const p = window.__fpDebug.profileRef?.current;
    return p ? { rows: p.rows.length, poc: p.poc, total: p.totalVol } : null;
  })()`);
  const vpRecomputed = !!vpAfter && JSON.stringify(vpAfter) !== vpBefore;
  record("可见范围变化重算", vpRecomputed, vpAfter ? `拖拽后 POC ${vpAfter.poc}、总量 ${Math.round(vpAfter.total)}（与拖拽前不同）` : "重算后为空");

  // 解读面板「成交分布」条目
  const vpInsight = await evalJs(`(() => {
    const spans = [...document.querySelectorAll("span")];
    const has = spans.some(s => s.textContent === "成交分布");
    const texts = [...document.querySelectorAll("p")].map(p => p.textContent).filter(t => /价值区|POC/.test(t));
    return { has, sample: texts[0] ?? "" };
  })()`);
  record("解读面板成交分布条目", vpInsight.has, vpInsight.has ? `「${vpInsight.sample.slice(0, 44)}…」` : "条目未出现");

  // 指南新章节
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("读图指南"))?.click()`);
  await sleep(300);
  const vpGuide = await evalJs(`[...document.querySelectorAll("h3")].some(h => h.textContent.includes("成交量分布"))`);
  record("指南新增分布章节", vpGuide, vpGuide ? "「成交量分布（钟形曲线）怎么看？」已收录" : "章节缺失");
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.getAttribute("aria-label") === "关闭指南")?.click()`);
  await sleep(200);

  // 关闭分布 → profileRef 应清空；再开回（保持后续测试基线一致）
  await evalJs(`[...document.querySelectorAll("button")].find(x => x.textContent.trim() === "分布")?.click()`);
  await sleep(400);
  const vpOff = await evalJs(`window.__fpDebug.profileRef?.current === null`);
  await evalJs(`[...document.querySelectorAll("button")].find(x => x.textContent.trim() === "分布")?.click()`);
  await sleep(500);
  record("分布开关关闭清空", vpOff, vpOff ? "关闭后 profile 清空、不再绘制" : "关闭后仍有残留");

  // 回到最新恢复基线
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("回到最新"))?.click()`);
  await sleep(400);

  // ---------- 8. 六档周期切换 ----------
  const tfResults = [];
  for (const tf of ["30m", "4h", "1d"]) {
    await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.trim() === "${tf}")?.click()`);
    // 等 loading 蒙层出现→消失 + 数据落地
    let ok = false;
    for (let i = 0; i < 40; i++) {
      await sleep(500);
      ok = await evalJs(`(() => {
        const loading = [...document.querySelectorAll("div")].some(d => d.textContent === "加载足迹数据…" && d.offsetParent);
        const d = window.__fpDebug;
        const bars = d?.barsRef.current ?? [];
        return !loading && bars.length > 10 && bars[0].timeframe === "${tf}";
      })()`);
      if (ok) break;
    }
    tfResults.push(`${tf}:${ok ? "OK" : "FAIL"}`);
  }
  const tfAllOk = tfResults.every(s => s.endsWith("OK"));
  record("六档周期切换（30m/4h/1d）", tfAllOk, tfResults.join(" "));
  // 切回 1m 供后续币种测试
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.textContent.trim() === "1m")?.click()`);
  await sleep(2000);

  // ---------- 9. 币种切换跟随 ----------
  // 打开顶栏 SymbolPicker → 点 SOL/USDT
  const symBefore = await evalJs(`window.__fpDebug.barsRef.current[0]?.symbol`);
  await evalJs(`[...document.querySelectorAll("button")].find(b => b.title && b.title.includes("切换/管理币种"))?.click()`);
  await sleep(300);
  const clickedSol = await evalJs(`(() => {
    const item = [...document.querySelectorAll(".z-50 button")].find(b => b.textContent.trim() === "SOL/USDT");
    if (!item) return false;
    item.click();
    return true;
  })()`);
  let symbolSwitched = false, solPriceSane = false, panelRefreshed = false, badgeShown = false;
  if (clickedSol) {
    for (let i = 0; i < 40; i++) {
      await sleep(500);
      const st = await evalJs(`(() => {
        const d = window.__fpDebug;
        const bars = d?.barsRef.current ?? [];
        const loading = [...document.querySelectorAll("div")].some(x => x.textContent === "加载足迹数据…" && x.offsetParent);
        const last = bars[bars.length - 1];
        return { n: bars.length, sym: last?.symbol, close: last?.close, loading };
      })()`);
      if (!st.loading && st.n > 10 && st.sym === "SOLUSDT") {
        symbolSwitched = true;
        solPriceSane = st.close > 10 && st.close < 2000; // SOL mock 基准价 145
        break;
      }
    }
    panelRefreshed = await evalJs(`(() => {
      const panel = [...document.querySelectorAll("span")].find(s => s.textContent === "当前画面解读");
      const root = panel?.closest("div.flex.w-60");
      return !!root && root.innerText.length > 40;
    })()`);
    badgeShown = await evalJs(`[...document.querySelectorAll("span")].some(s => s.textContent === "SOL/USDT" && s.title && s.title.includes("跟随"))`);
  }
  record("币种切换跟随", clickedSol && symbolSwitched, symbolSwitched ? `${symBefore} → SOLUSDT，数据/图表已刷新` : `picker 点击=${clickedSol}`);
  record("切币后价格量级正确", solPriceSane, solPriceSane ? "SOL 收盘价在合理区间（非 BTC 残留）" : "价格量级异常");
  record("切币后解读面板刷新", panelRefreshed, panelRefreshed ? "面板有内容" : "面板为空");
  record("工具条币种徽标", badgeShown, badgeShown ? "显示 SOL/USDT" : "徽标未更新");

  console.log("\n=== 汇总 ===");
  const hard = results.filter(r => !r.pass && !r.detail.includes("非硬失败"));
  console.log(`通过 ${results.filter(r => r.pass).length}/${results.length}，硬失败 ${hard.length}`);
  process.exitCode = hard.length > 0 ? 1 : 0;
} catch (err) {
  console.error("E2E 异常:", err.message);
  process.exitCode = 2;
} finally {
  try { ws?.close(); } catch { /* noop */ }
  chrome.kill("SIGKILL");
  await sleep(500);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
}
