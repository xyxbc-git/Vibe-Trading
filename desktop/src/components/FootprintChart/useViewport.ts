import { useCallback, useEffect, useRef, useState } from "react";
import type { ViewportState } from "./renderer";
import {
  AXIS_W,
  BASE_BAR_W,
  BASE_ROW_H,
  MAX_ZOOM,
  MIN_ZOOM,
  RIGHT_GAP_BARS_DEFAULT,
  RIGHT_GAP_BARS_MAX,
  STATS_H,
  TIME_H,
  barWOf,
  clamp,
  rowHOf,
} from "./renderer";

export interface ViewportApi {
  vpRef: React.MutableRefObject<ViewportState>;
  /** 作为 ref 回调绑定到画布容器（wheel 需 passive:false 阻止页面滚动） */
  bindTarget: (el: HTMLElement | null) => void;
  resetFollow: () => void;
  /** follow 状态镜像（仅用于「回到最新」按钮显隐，低频更新） */
  following: boolean;
  /** vpRef.follow 被外部（如 stepViewport 惯性吸附）改动后调用，同步按钮显隐 */
  syncFollow: () => void;
  /** 自动适配：纵向装下可见柱价格范围，横向回到默认柱宽并跟随最新 */
  autoFit: () => void;
}

interface GeomRef {
  chartW: number;
  maxScroll: number;
  centerPriceEff: number;
  plotH: number;
  tick: number;
  /** 可见范围价格极值（自动适配用；无数据时 hi<lo） */
  visLo: number;
  visHi: number;
  /** 当前已加载柱数（动态缩放下限「适应全部数据」用） */
  barCount: number;
}

/** 惯性衰减：每帧 ×0.94（60fps 基准，按 dt 换算）；低于 8px/s 停止 */
const FRICTION_PER_FRAME = 0.94;
const MIN_FLING_SPEED = 8;
/** 缩放平滑：每帧向目标推进 26%（60fps 基准），≈180ms 收敛 */
const ZOOM_LERP_PER_FRAME = 0.26;
/** 轴拖拽缩放灵敏度：每 px 的 zoom 指数增量 */
const AXIS_DRAG_SENS = 0.006;
/** 可读性下限：柱宽低于此像素蜡烛/足迹柱已不可辨认，禁止继续缩小 */
const MIN_READABLE_BAR_W = 5;
/** 放大上限：单柱不超过图区宽的 1/3（一屏至少看得到 3 根柱） */
const MAX_BAR_W_RATIO = 1 / 3;
/** 滚轮缩放灵敏度 + 单次事件倍率限幅（降噪：狂滚也不会一滚到底） */
const WHEEL_ZOOM_SENS = 0.008;
const WHEEL_STEP_MAX = 1.25;

export interface ZoomBounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

/**
 * 动态缩放边界（防「缩到不可读 + 整屏空白」）：
 * - X 下限 = max(全部数据恰好填满图区的倍率, 可读柱宽下限)，且封顶 1（默认倍率永远可达）。
 *   数据少时缩到下限正好「适应全部数据」，不会继续缩出空白；数据多时止步于可读柱宽。
 * - X 上限 = 单柱 ≤ 图区宽 1/3（与 MAX_ZOOM 取小）。
 * - Y 下限 = 可见价格范围恰好装满绘图区的倍率（与 autoFit 同口径），封死纵向缩出空白。
 */
export function zoomBoundsOf(g: GeomRef): ZoomBounds {
  let minX = MIN_ZOOM;
  if (g.barCount > 0 && g.chartW > 0) {
    const fitX = g.chartW / (g.barCount * BASE_BAR_W);
    minX = clamp(Math.max(fitX, MIN_READABLE_BAR_W / BASE_BAR_W), MIN_ZOOM, 1);
  }
  const maxX = Math.max(
    minX,
    Math.min(MAX_ZOOM, (g.chartW * MAX_BAR_W_RATIO) / BASE_BAR_W),
  );

  let minY = MIN_ZOOM;
  if (g.visHi > g.visLo && g.plotH > 0) {
    const span = (g.visHi - g.visLo) / Math.max(g.tick, 1e-9) + 4; // 上下各留 2 行
    const fitRowH = clamp(g.plotH / span, 1.2, 64);
    minY = clamp(fitRowH / BASE_ROW_H, MIN_ZOOM, 1);
  }
  return { minX, maxX, minY, maxY: Math.max(minY, MAX_ZOOM) };
}

export function initialViewport(): ViewportState {
  return {
    zoomX: 1,
    zoomY: 1,
    zoomTargetX: 1,
    zoomTargetY: 1,
    anchor: null,
    scrollX: 0,
    velX: 0,
    centerPrice: null,
    follow: true,
    dragging: false,
    rightGapBars: RIGHT_GAP_BARS_DEFAULT,
  };
}

/**
 * TradingView 手感的视口物理引擎。
 * 状态全存 ref，由主组件的 rAF 循环每帧调用 step(dt) 推进：
 * - 拖拽跟手（指针事件直接写 scrollX，无 React setState）
 * - 松手惯性滑行（velocity 采样 + 指数衰减，可按住打断）
 * - 普通滚轮/触控板双指 = 时间轴平移；Ctrl/⌘+滚轮（含捏合）= 光标锚点缩放，
 *   向 zoomTarget 平滑插值，缩放范围受 zoomBoundsOf 动态边界约束（不可缩出空白）
 * - 图区右拖越过留白锚点时物化为更大的 rightGapBars（TV 式可调留白）
 * - 价格轴上下拖 = 纵向缩放；时间轴左右拖 = 横向缩放；双击轴 = 自动适配
 * - 双击图区 = 回到最新 + 默认缩放（一键复位）
 */
export function useViewport(getGeom: () => GeomRef): ViewportApi {
  const vpRef = useRef<ViewportState>(initialViewport());

  const [following, setFollowing] = useState(true);
  const [el, setEl] = useState<HTMLElement | null>(null);
  const getGeomRef = useRef(getGeom);
  getGeomRef.current = getGeom;

  const syncFollow = useCallback(() => {
    setFollowing(vpRef.current.follow);
  }, []);

  /** 一键复位：回到最新 + 默认缩放（迷失后一步找回） */
  const resetFollow = useCallback(() => {
    const vp = vpRef.current;
    vp.follow = true;
    vp.centerPrice = null;
    vp.velX = 0;
    vp.rightGapBars = RIGHT_GAP_BARS_DEFAULT;
    vp.zoomTargetX = 1;
    vp.zoomTargetY = 1;
    vp.anchor = null;
    syncFollow();
  }, [syncFollow]);

  /** 纵向装下可见价格范围 + 横向回默认倍率并跟随（TV 双击轴/auto-fit 按钮行为） */
  const autoFit = useCallback(() => {
    const vp = vpRef.current;
    const g = getGeomRef.current();
    if (g.visHi > g.visLo) {
      const span = (g.visHi - g.visLo) / Math.max(g.tick, 1e-9) + 4; // 上下各留 2 行
      const rowH = clamp(g.plotH / span, 1.2, 64);
      vp.zoomTargetY = clamp(rowH / 17, MIN_ZOOM, MAX_ZOOM); // BASE_ROW_H=17
      vp.centerPrice = (g.visHi + g.visLo) / 2;
    } else {
      vp.zoomTargetY = 1;
      vp.centerPrice = null;
    }
    vp.anchor = null;
    vp.velX = 0;
    syncFollow();
  }, [syncFollow]);

  const bindTarget = useCallback((node: HTMLElement | null) => {
    setEl(node);
  }, []);

  useEffect(() => {
    if (!el) return;

    // 拖拽速度采样窗（最近 ~80ms）
    let samples: { t: number; x: number }[] = [];
    let drag: {
      x: number;
      y: number;
      pointerId: number;
      /** chart=图区平移 priceAxis=纵向缩放 timeAxis=横向缩放 */
      kind: "chart" | "priceAxis" | "timeAxis";
    } | null = null;

    /** 判定按下位置落在哪个交互区（价格轴 / 时间+统计轴 / 图区） */
    const zoneOf = (px: number, py: number): "chart" | "priceAxis" | "timeAxis" => {
      const r = el.getBoundingClientRect();
      const x = px - r.left;
      const y = py - r.top;
      const chartW = r.width - AXIS_W;
      const plotH = r.height - TIME_H - STATS_H;
      if (x >= chartW) return "priceAxis";
      if (y >= plotH && y < plotH + TIME_H) return "timeAxis";
      return "chart";
    };

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const g = getGeomRef.current();
      const vp = vpRef.current;

      if (!e.ctrlKey && !e.metaKey) {
        // 普通滚轮 / 触控板双指 / Shift+滚轮：沿时间轴平移。
        // 不再默认缩放——滚一下整图缩没是用户反馈的核心痛点
        const delta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        const eff = vp.follow ? g.maxScroll : vp.scrollX;
        const nx = eff + delta;
        vp.scrollX = nx;
        vp.velX = 0;
        vp.follow = nx >= g.maxScroll - 2;
        syncFollow();
        return;
      }
      // Ctrl/⌘+滚轮（含触控板双指捏合）：以光标为锚点 X/Y 同步缩放；
      // 单次事件倍率限幅 + 动态边界 clamp（缩到下限即「适应全部数据」，不再缩出空白）
      const rect = el.getBoundingClientRect();
      const zb = zoomBoundsOf(g);
      const factor = clamp(
        Math.exp(-e.deltaY * WHEEL_ZOOM_SENS),
        1 / WHEEL_STEP_MAX,
        WHEEL_STEP_MAX,
      );
      vp.zoomTargetX = clamp(vp.zoomTargetX * factor, zb.minX, zb.maxX);
      vp.zoomTargetY = clamp(vp.zoomTargetY * factor, zb.minY, zb.maxY);
      vp.anchor = { mx: e.clientX - rect.left, my: e.clientY - rect.top };
      vp.velX = 0;
    };

    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0) return;
      const kind = zoneOf(e.clientX, e.clientY);
      drag = { x: e.clientX, y: e.clientY, pointerId: e.pointerId, kind };
      samples = [{ t: performance.now(), x: e.clientX }];
      el.setPointerCapture(e.pointerId);
      const vp = vpRef.current;
      if (kind === "chart") {
        vp.dragging = true;
        vp.velX = 0; // 按住即打断惯性
      }
    };

    const onPointerMove = (e: PointerEvent) => {
      if (!drag || drag.pointerId !== e.pointerId) return;
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      drag.x = e.clientX;
      drag.y = e.clientY;

      const g = getGeomRef.current();
      const vp = vpRef.current;

      if (drag.kind === "priceAxis") {
        // 价格轴上下拖：纵向缩放（向下拖 = 拉伸格高，与 TV 一致），锚定图区中心价
        const zb = zoomBoundsOf(g);
        vp.zoomTargetY = clamp(vp.zoomTargetY * Math.exp(dy * AXIS_DRAG_SENS), zb.minY, zb.maxY);
        vp.anchor = { mx: g.chartW / 2, my: g.plotH / 2 };
        return;
      }
      if (drag.kind === "timeAxis") {
        // 时间轴左右拖：横向缩放，锚定图区水平中点
        const zb = zoomBoundsOf(g);
        vp.zoomTargetX = clamp(vp.zoomTargetX * Math.exp(dx * AXIS_DRAG_SENS), zb.minX, zb.maxX);
        vp.anchor = { mx: g.chartW / 2, my: g.plotH / 2 };
        return;
      }

      const now = performance.now();
      samples.push({ t: now, x: e.clientX });
      while (samples.length > 2 && now - samples[0].t > 80) samples.shift();

      const rowH = rowHOf(vp.zoomY);
      const barW = barWOf(vp.zoomX);
      const eff = vp.follow ? g.maxScroll : vp.scrollX;
      vp.scrollX = eff - dx;
      vp.centerPrice = g.centerPriceEff + (dy * g.tick) / rowH;
      if (vp.scrollX > g.maxScroll + 1) {
        // 越过留白锚点继续右拖：物化为更大的右侧留白（TV 手感）
        vp.rightGapBars = clamp(
          vp.rightGapBars + (vp.scrollX - g.maxScroll) / barW,
          0,
          RIGHT_GAP_BARS_MAX,
        );
        vp.scrollX = g.maxScroll;
        vp.follow = true;
      } else {
        vp.follow = dx < 0 && vp.scrollX >= g.maxScroll - 2;
      }
      syncFollow();
    };

    const endDrag = (e: PointerEvent) => {
      if (drag?.pointerId !== e.pointerId) return;
      const wasChart = drag.kind === "chart";
      drag = null;
      const vp = vpRef.current;
      vp.dragging = false;
      if (!wasChart) return;

      // 松手速度 = 采样窗内平均速度（px/s），交给 step 做惯性滑行
      const now = performance.now();
      samples.push({ t: now, x: e.clientX });
      const first = samples[0];
      const dt = (now - first.t) / 1000;
      if (dt > 0.016) {
        const v = (e.clientX - first.x) / dt;
        if (Math.abs(v) > 60 && !vp.follow) vp.velX = -v;
      }
      samples = [];
    };

    const onDblClick = (e: MouseEvent) => {
      const kind = zoneOf(e.clientX, e.clientY);
      const vp = vpRef.current;
      if (kind === "priceAxis" || kind === "timeAxis") {
        // 双击轴：自动适配（TV 行为）
        const g = getGeomRef.current();
        if (g.visHi > g.visLo) {
          const span = (g.visHi - g.visLo) / Math.max(g.tick, 1e-9) + 4;
          const rowH = clamp(g.plotH / span, 1.2, 64);
          vp.zoomTargetY = clamp(rowH / 17, MIN_ZOOM, MAX_ZOOM);
          vp.centerPrice = (g.visHi + g.visLo) / 2;
        }
        if (kind === "timeAxis") vp.zoomTargetX = 1;
        vp.anchor = null;
        vp.velX = 0;
        syncFollow();
        return;
      }
      // 双击图区：回到最新 + 默认缩放（一键复位，迷失后一步找回）
      vp.follow = true;
      vp.centerPrice = null;
      vp.velX = 0;
      vp.rightGapBars = RIGHT_GAP_BARS_DEFAULT;
      vp.zoomTargetX = 1;
      vp.zoomTargetY = 1;
      vp.anchor = null;
      syncFollow();
    };

    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("pointermove", onPointerMove);
    el.addEventListener("pointerup", endDrag);
    el.addEventListener("pointercancel", endDrag);
    el.addEventListener("dblclick", onDblClick);
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointermove", onPointerMove);
      el.removeEventListener("pointerup", endDrag);
      el.removeEventListener("pointercancel", endDrag);
      el.removeEventListener("dblclick", onDblClick);
    };
  }, [el, syncFollow]);

  return { vpRef, bindTarget, resetFollow, following, syncFollow, autoFit };
}

/**
 * 每帧物理推进（由主组件 rAF 调用）。返回 true 表示状态有变化需要重绘。
 * dt 单位秒。
 */
export function stepViewport(
  vp: ViewportState,
  g: GeomRef,
  dt: number,
  onFollowChange?: () => void,
): boolean {
  let changed = false;
  const frames = Math.max(0.25, dt * 60); // 换算为 60fps 基准帧数
  const k = 1 - Math.pow(1 - ZOOM_LERP_PER_FRAME, frames);

  const needX = Math.abs(vp.zoomTargetX - vp.zoomX) > 1e-4;
  const needY = Math.abs(vp.zoomTargetY - vp.zoomY) > 1e-4;
  if (needX || needY) {
    const mx = vp.anchor?.mx ?? g.chartW / 2;
    const my = vp.anchor?.my ?? g.plotH / 2;

    if (needX) {
      let next = vp.zoomX + (vp.zoomTargetX - vp.zoomX) * k;
      if (Math.abs(vp.zoomTargetX - next) < 1e-4) next = vp.zoomTargetX;
      const bw0 = barWOf(vp.zoomX);
      const bw1 = barWOf(next);
      // 横向：锚定光标下的柱索引（follow 时贴右不动，布局层自动处理）
      if (!vp.follow) {
        const worldBar = (vp.scrollX + mx) / bw0;
        vp.scrollX = worldBar * bw1 - mx;
      }
      vp.zoomX = next;
    }
    if (needY) {
      let next = vp.zoomY + (vp.zoomTargetY - vp.zoomY) * k;
      if (Math.abs(vp.zoomTargetY - next) < 1e-4) next = vp.zoomTargetY;
      const rh0 = rowHOf(vp.zoomY);
      const rh1 = rowHOf(next);
      // 纵向：锚定光标下的价格。基准中心优先用物理状态 vp.centerPrice——
      // layout 是上一帧的快照，autoFit 等外部改写后用它会把新中心覆盖回旧值
      const baseCenter = vp.centerPrice ?? g.centerPriceEff;
      const anchorPrice = baseCenter - ((my - g.plotH / 2) * g.tick) / rh0;
      vp.centerPrice = anchorPrice + ((my - g.plotH / 2) * g.tick) / rh1;
      vp.zoomY = next;
    }
    if (vp.zoomX === vp.zoomTargetX && vp.zoomY === vp.zoomTargetY) vp.anchor = null;
    changed = true;
  }

  // 2. 惯性滑行
  if (!vp.dragging && Math.abs(vp.velX) > MIN_FLING_SPEED) {
    vp.scrollX += vp.velX * dt;
    vp.velX *= Math.pow(FRICTION_PER_FRAME, frames);

    // 边界：滑到留白锚点吸附 follow；滑出左界急停
    if (vp.scrollX >= g.maxScroll) {
      vp.scrollX = g.maxScroll;
      if (vp.velX > 0) {
        vp.velX = 0;
        if (!vp.follow) {
          vp.follow = true;
          onFollowChange?.();
        }
      }
    }
    const leftLimit = Math.min(g.maxScroll, 0) - g.chartW * 0.25;
    if (vp.scrollX <= leftLimit) {
      vp.scrollX = leftLimit;
      vp.velX = 0;
    }
    if (Math.abs(vp.velX) <= MIN_FLING_SPEED) vp.velX = 0;
    changed = true;
  }

  return changed;
}
