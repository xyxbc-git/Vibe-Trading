// 盘上合流仪表 HUD（C2 前端最小版，方案《贾维斯-合流仪表-方案-20260813.md》§六）。
//
// 形态：图表容器内左上角浮层（父容器需 relative）。
//   折叠态（常驻单行 ~28px）：[▲环境偏多] [4H] [75] [✓✓◐○] [🔒仅冷静期] [⌄]
//   展开态（w-72 卡片）：四组证据（方向40/结构30/微观20/环境10）+ 页脚
//     （按当前合流开计划 / 当日 N 单 / 新鲜度）；ESC/点外部收回。
// 防噪纪律：分数滞回 <5 不重绘；取数期间保持上一态 + 角落小 spinner，绝不整卡
//   骨架闪；变化 300ms 淡入且尊重 prefers-reduced-motion；禁红色闪烁；低分不用红。
// 诚实纪律：skipped 灰显 + 原因；可用权重 <50 分数灰显挂「证据不足」；演示数据
//   （后端 404 回退）整卡挂「演示数据」角标；永不显示 BUY/SELL 字样。
// 行为闸口（D3 裁决）：冷静期只体现为「开计划」按钮禁用态 + 倒计时与折叠态一枚 🔒，
//   连亏计数不上盘面。一期预填降级：跳导师页（页内抽屉复用 PlanForm 为二期）。
// z-index 共存：本卡 z-10 < TrapReasonCard z-20（点击型优先）< 工具栏弹层 z-50。

import { useEffect, useMemo, useRef, useState } from "react";
import { clsx } from "clsx";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Circle,
  CircleDashed,
  Lock,
  Zap,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { fetchConfluence, type ConfluenceItem, type ConfluenceState } from "@/api/confluence";
import {
  collapsedChecks,
  cooldownRemaining,
  dirMeta,
  fmtCountdown,
  fmtFreshness,
  holdDisplayScore,
  isNarrowContainer,
  matchesScope,
  scoreTone,
} from "@/lib/confluence";

/** 条目状态图标：沿用 ReversalScorePanel 三态惯例 + 琥珀⚠（fail/取数失败类） */
function StateIcon({ state, conflict }: { state: ConfluenceState; conflict?: boolean }) {
  if (conflict) return <Zap size={12} className="text-jarvis-yellow shrink-0 mt-0.5" />;
  if (state === "pass") return <CheckCircle2 size={12} className="text-jarvis-green shrink-0 mt-0.5" />;
  if (state === "warn") return <CircleDashed size={12} className="text-jarvis-yellow/70 shrink-0 mt-0.5" />;
  if (state === "fail") return <AlertTriangle size={12} className="text-jarvis-yellow shrink-0 mt-0.5" />;
  return <Circle size={12} className="text-jarvis-text-secondary/40 shrink-0 mt-0.5" />;
}

/** 折叠态微型勾格（不带文字，title 提示条目名） */
function MiniCheck({ state }: { state: ConfluenceState }) {
  const cls =
    state === "pass"
      ? "bg-jarvis-green"
      : state === "warn"
        ? "bg-jarvis-yellow/70"
        : state === "fail"
          ? "bg-jarvis-yellow"
          : "bg-jarvis-border";
  return <span className={clsx("inline-block w-1.5 h-1.5 rounded-full", cls)} />;
}

const COLLAPSED_CHECK_NAMES = ["多周期一致", "结构突破", "流动性扫单", "FVG/折溢价"];

interface ConfluenceHudProps {
  symbol: string;
  tf: string;
}

/** 盘上合流仪表：父容器需 relative；数据 60s 轮询（隐藏页自动暂停，useApi 内建）。 */
export default function ConfluenceHud({ symbol, tf }: ConfluenceHudProps) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const { data, loading, error } = usePolling(
    () => fetchConfluence(symbol, tf),
    60_000,
    [symbol, tf],
  );

  // 旧响应守卫 + 保持上一态：切 symbol/tf 后旧口径数据立即失效（显示占位），
  // 同口径刷新期间保持旧数据渲染（角落 spinner），绝不整卡骨架闪。
  const resp = useMemo(
    () => (matchesScope(data, symbol, tf) ? data : null),
    [data, symbol, tf],
  );

  // 分数滞回：变化 <5 分不更新显示值（防缓存刷新心电图效应）
  const displayRef = useRef<number | null>(null);
  const display = holdDisplayScore(displayRef.current, resp?.score ?? null);
  displayRef.current = display;

  // 冷静期倒计时（本地每秒走秒；数据刷新校准）
  const [nowSec, setNowSec] = useState(() => Date.now() / 1000);
  const cooldownSec = cooldownRemaining(resp?.cooldownUntil, nowSec);
  useEffect(() => {
    if (!resp?.cooldownUntil) return;
    const t = setInterval(() => setNowSec(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [resp?.cooldownUntil]);

  // 窄容器降级：按图表容器（offsetParent）宽度而非 viewport
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const host = rootRef.current?.offsetParent;
    if (!(host instanceof HTMLElement)) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      setNarrow(isNarrowContainer(w));
    });
    ro.observe(host);
    return () => ro.disconnect();
  }, []);

  // ESC / 点外部收回展开态
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && e.target instanceof Node && !rootRef.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  const dir = dirMeta(resp?.direction ?? "neutral");
  const tone = scoreTone(display, {
    insufficient: resp?.insufficient,
    mock: resp?.mock,
  });
  const checks = collapsedChecks(resp);
  const refreshing = loading && resp != null;
  const failed = Boolean(error) && resp == null;

  return (
    <div ref={rootRef} className="absolute top-2 left-2 z-10 select-none">
      {/* ── 折叠态：常驻单行（触屏命中区 ≥44px 由 padding 扩展，视觉 ~28px）── */}
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={
          failed
            ? "合流数据获取失败，保持重试中"
            : `环境合流分（setup 质量，非下单信号）· 点击${open ? "收起" : "展开"}证据`
        }
        className={clsx(
          "flex items-center gap-1.5 h-7 px-2 rounded-md border text-xs",
          "bg-jarvis-card/85 backdrop-blur-sm shadow-sm transition-colors duration-300",
          "motion-reduce:transition-none",
          dir.borderCls,
        )}
      >
        <span className={clsx("font-medium", dir.textCls)}>
          {dir.arrow}
          {!narrow && ` ${dir.label}`}
        </span>
        {!narrow && (
          <span className="px-1 rounded bg-jarvis-border/40 text-jarvis-text-secondary font-mono text-[10px]">
            {tf}
          </span>
        )}
        <span
          className={clsx(
            "font-mono font-semibold transition-colors duration-300 motion-reduce:transition-none",
            tone.textCls,
          )}
          title={
            resp?.insufficient
              ? "可用证据权重不足 50%，分数仅供参考"
              : "环境合流分 0-100（预登记权重，可点开逐条审计）"
          }
        >
          {display == null ? "--" : display}
        </span>
        {!narrow && (
          <span className="flex items-center gap-0.5" title={COLLAPSED_CHECK_NAMES.join(" / ")}>
            {checks.map((s, i) => (
              <MiniCheck key={i} state={s} />
            ))}
          </span>
        )}
        {cooldownSec > 0 && (
          <Lock size={11} className="text-jarvis-yellow" aria-label="冷静期中" />
        )}
        {refreshing && (
          <span className="inline-block w-2.5 h-2.5 border border-jarvis-blue border-t-transparent rounded-full animate-spin motion-reduce:animate-none" />
        )}
        {resp?.mock && (
          <span className="px-1 rounded bg-jarvis-yellow/15 text-jarvis-yellow text-[9px]">
            演示
          </span>
        )}
        <ChevronDown
          size={12}
          className={clsx(
            "text-jarvis-text-secondary transition-transform duration-300 motion-reduce:transition-none",
            open && "rotate-180",
          )}
        />
      </button>

      {/* ── 展开态：证据卡（窄容器改宽幅底部卡；max-h 限制 + 内滚动）── */}
      {open && (
        <div
          className={clsx(
            "mt-1 rounded-lg border border-jarvis-border bg-jarvis-card/85 backdrop-blur-sm shadow-lg",
            "p-3 overflow-y-auto",
            narrow ? "w-[calc(100vw-2rem)] max-w-sm max-h-[50vh]" : "w-72 max-h-[420px]",
          )}
        >
          {failed ? (
            <p className="text-xs text-jarvis-text-secondary py-2">
              合流数据获取失败（{error}）；轮询保持中，恢复后自动更新。
            </p>
          ) : !resp ? (
            <p className="text-xs text-jarvis-text-secondary py-2">正在获取当前口径数据…</p>
          ) : (
            <>
              {/* 头部：定位声明（防 75 分=可以梭的误读）+ 灰显原因 */}
              <p className="text-[10px] text-jarvis-text-secondary leading-relaxed pb-2 border-b border-jarvis-border/60">
                合流分是<span className="text-jarvis-text">环境分</span>不是下单信号，点位请自行确认。
                {resp.insufficient && (
                  <span className="ml-1 text-jarvis-yellow">证据不足（可用权重 {resp.availableWeight}/100）。</span>
                )}
                {resp.direction === "neutral" && <span className="ml-1">当前无方向共识，整卡仅供观察。</span>}
                {resp.mock && (
                  <span className="ml-1 text-jarvis-yellow">
                    演示数据：后端 /api/confluence 未就绪，本卡不可用于决策。
                  </span>
                )}
              </p>

              {/* 四组证据 */}
              <div className="space-y-2.5 py-2">
                {resp.groups.map((g) => (
                  <div key={g.key}>
                    <div className="flex items-center justify-between mb-1">
                      <p className="text-[10px] font-medium text-jarvis-text-secondary">
                        {g.name}
                        <span className="ml-1 font-mono">
                          {Math.round(g.earned)}/{g.available}
                          {g.available < g.weight && (
                            <span title={`满权重 ${g.weight}，缺失数据源不计分母`}>*</span>
                          )}
                        </span>
                      </p>
                      {/* 组内得分微条：宽度按满权重比例，填充按可用内得分比例 */}
                      <span
                        className="h-1 rounded bg-jarvis-border/50 overflow-hidden"
                        style={{ width: `${g.weight * 2}px` }}
                      >
                        <span
                          className="block h-full bg-jarvis-blue/70 transition-all duration-300 motion-reduce:transition-none"
                          style={{
                            width: g.available > 0 ? `${(g.earned / g.available) * 100}%` : 0,
                          }}
                        />
                      </span>
                    </div>
                    <div className="space-y-1">
                      {g.items.map((it: ConfluenceItem) => (
                        <div key={it.key} className="flex items-start gap-1.5">
                          <StateIcon state={it.state} conflict={it.conflict} />
                          <div className="min-w-0">
                            <p
                              className={clsx(
                                "text-[11px] leading-tight",
                                it.state === "skipped"
                                  ? "text-jarvis-text-secondary/50"
                                  : "text-jarvis-text",
                              )}
                            >
                              {it.name}
                              {it.conflict && (
                                <span className="ml-1 text-[9px] text-jarvis-yellow">⚡冲突</span>
                              )}
                            </p>
                            <p className="text-[10px] text-jarvis-text-secondary leading-relaxed">
                              {it.note}
                            </p>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              {/* 成本红标（不进分但必须可见）+ 估算徽章 + 格子战绩 */}
              {(resp.costWarning || resp.costEstimate || resp.gridStats) && (
                <div className="pt-1.5 border-t border-jarvis-border/60 space-y-1">
                  {resp.costWarning && (
                    <p className="text-[10px] text-jarvis-red flex items-center gap-1">
                      <AlertTriangle size={11} className="shrink-0" />⚠ 成本不可行：{resp.costWarning}
                    </p>
                  )}
                  {resp.costEstimate && !resp.costWarning && (
                    <p className="text-[10px] text-jarvis-text-secondary">{resp.costEstimate}</p>
                  )}
                  {resp.gridStats && (
                    <p className="text-[10px] text-jarvis-text-secondary">{resp.gridStats}</p>
                  )}
                </div>
              )}

              {/* 页脚：行动闸口 + 当日单数 + 新鲜度 */}
              <div className="mt-2 pt-2 border-t border-jarvis-border/60 flex items-center gap-2">
                <button
                  onClick={() => navigate("/mentor")}
                  disabled={cooldownSec > 0 || Boolean(resp.mock)}
                  title={
                    cooldownSec > 0
                      ? `冷静期中，还剩 ${fmtCountdown(cooldownSec)}——闸门是为了下一单的你`
                      : resp.mock
                        ? "演示数据不可用于开计划"
                        : "去导师页开计划（完整红黄绿裁决后才算数）"
                  }
                  className={clsx(
                    "px-2.5 py-1.5 rounded-md text-[11px] font-medium border transition-colors",
                    "motion-reduce:transition-none",
                    cooldownSec > 0 || resp.mock
                      ? "border-jarvis-border text-jarvis-text-secondary/60 cursor-not-allowed"
                      : "border-jarvis-blue/60 text-jarvis-blue hover:bg-jarvis-blue/10",
                  )}
                >
                  {cooldownSec > 0 ? (
                    <span className="flex items-center gap-1">
                      <Lock size={11} />
                      冷静期 {fmtCountdown(cooldownSec)}
                    </span>
                  ) : (
                    "按当前合流开计划"
                  )}
                </button>
                <div className="ml-auto text-right">
                  {resp.todayPlans != null && resp.todayPlans > 0 && (
                    <p className="text-[9px] text-jarvis-text-secondary">当日已提交 {resp.todayPlans} 单</p>
                  )}
                  <p className="text-[9px] text-jarvis-text-secondary/70">
                    {fmtFreshness(resp.updatedAt)}
                  </p>
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
