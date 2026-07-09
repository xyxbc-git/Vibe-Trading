import { useEffect, useMemo, useRef, useState } from "react";
import {
  Calendar,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
} from "lucide-react";
import { clsx } from "clsx";

/* ────────── 日期工具（本地时区；ISO yyyy-MM-dd 为唯一交换格式）────────── */

const ISO_RE = /^\d{4}-\d{2}-\d{2}$/;

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function fmtISO(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** 严格解析 yyyy-MM-dd；2025-02-31 之类回环不一致返回 null */
function parseISO(s: string): Date | null {
  if (!ISO_RE.test(s)) return null;
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  if (dt.getFullYear() !== y || dt.getMonth() !== m - 1 || dt.getDate() !== d) {
    return null;
  }
  return dt;
}

function todayISO(): string {
  return fmtISO(new Date());
}

/** 加 n 个月，月末溢出截断（如 1-31 → 2-28） */
function addMonthsClamped(d: Date, n: number): Date {
  const anchor = new Date(d.getFullYear(), d.getMonth() + n, 1);
  const dim = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0).getDate();
  return new Date(anchor.getFullYear(), anchor.getMonth(), Math.min(d.getDate(), dim));
}

const PRESETS: { key: string; label: string; range: () => [string, string] }[] = [
  { key: "1m", label: "近 1 月", range: () => [fmtISO(addMonthsClamped(new Date(), -1)), todayISO()] },
  { key: "3m", label: "近 3 月", range: () => [fmtISO(addMonthsClamped(new Date(), -3)), todayISO()] },
  { key: "6m", label: "近半年", range: () => [fmtISO(addMonthsClamped(new Date(), -6)), todayISO()] },
  { key: "1y", label: "近 1 年", range: () => [fmtISO(addMonthsClamped(new Date(), -12)), todayISO()] },
  { key: "ytd", label: "今年以来", range: () => [`${new Date().getFullYear()}-01-01`, todayISO()] },
];

const WEEK_HEADER = ["一", "二", "三", "四", "五", "六", "日"];
const PANEL_WIDTH = 540;

/* ────────── 单月网格 ────────── */

function MonthGrid({
  year,
  month,
  draftStart,
  draftEnd,
  maxISO,
  onPick,
}: {
  year: number;
  /** 0-based */
  month: number;
  draftStart: string;
  draftEnd: string;
  maxISO: string;
  onPick: (iso: string) => void;
}) {
  const offset = (new Date(year, month, 1).getDay() + 6) % 7; // 周一起始
  const dim = new Date(year, month + 1, 0).getDate();
  const today = todayISO();

  return (
    <div className="flex-1">
      <p className="text-xs font-medium text-jarvis-text text-center mb-1.5">
        {year} 年 {month + 1} 月
      </p>
      <div className="grid grid-cols-7 gap-y-0.5">
        {WEEK_HEADER.map((w) => (
          <span
            key={w}
            className="w-8 h-6 flex items-center justify-center text-[10px] text-jarvis-text-secondary/70"
          >
            {w}
          </span>
        ))}
        {Array.from({ length: offset }, (_, i) => (
          <span key={`blank-${i}`} />
        ))}
        {Array.from({ length: dim }, (_, i) => {
          const iso = `${year}-${pad2(month + 1)}-${pad2(i + 1)}`;
          const disabled = iso > maxISO;
          const isEdge = iso === draftStart || iso === draftEnd;
          const inRange = iso > draftStart && iso < draftEnd;
          return (
            <button
              key={iso}
              disabled={disabled}
              onClick={() => onPick(iso)}
              className={clsx(
                "w-8 h-7 text-xs font-mono rounded-md transition-colors",
                disabled
                  ? "text-jarvis-text-secondary/25 cursor-not-allowed"
                  : isEdge
                    ? "bg-jarvis-blue text-white"
                    : inRange
                      ? "bg-jarvis-blue/15 text-jarvis-text"
                      : "text-jarvis-text hover:bg-white/10",
                iso === today && !isEdge && "ring-1 ring-inset ring-jarvis-blue/50",
              )}
            >
              {i + 1}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ────────── 主组件 ────────── */

export interface DateRangePickerProps {
  /** ISO yyyy-MM-dd */
  start: string;
  end: string;
  onChange: (start: string, end: string) => void;
  /** 最大可选日期（ISO），默认今天 */
  max?: string;
  className?: string;
}

/**
 * 暗色主题日期范围选择器：快捷预设 chips + 双月日历点选起止 + 键盘输入。
 * 预设点击即生效并收起；日历/输入为草稿态，「应用」后生效。
 */
export default function DateRangePicker({
  start,
  end,
  onChange,
  max,
  className,
}: DateRangePickerProps) {
  const maxISO = max ?? todayISO();
  const [open, setOpen] = useState(false);
  const [alignRight, setAlignRight] = useState(false);
  const [draftStart, setDraftStart] = useState(start);
  const [draftEnd, setDraftEnd] = useState(end);
  const [pickingEnd, setPickingEnd] = useState(false);
  const [startText, setStartText] = useState(start);
  const [endText, setEndText] = useState(end);
  const [view, setView] = useState(() => {
    const d = parseISO(start) ?? new Date();
    return { y: d.getFullYear(), m: d.getMonth() };
  });
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const openPanel = () => {
    setDraftStart(start);
    setDraftEnd(end);
    setStartText(start);
    setEndText(end);
    setPickingEnd(false);
    const d = parseISO(start) ?? new Date();
    setView({ y: d.getFullYear(), m: d.getMonth() });
    const rect = triggerRef.current?.getBoundingClientRect();
    setAlignRight(!!rect && rect.left + PANEL_WIDTH > window.innerWidth - 16);
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const activePreset = useMemo(() => {
    const hit = PRESETS.find((p) => {
      const [s, e] = p.range();
      return s === start && e === end;
    });
    return hit?.key ?? "custom";
  }, [start, end]);

  const shiftView = (months: number) => {
    setView((v) => {
      const d = new Date(v.y, v.m + months, 1);
      return { y: d.getFullYear(), m: d.getMonth() };
    });
  };

  const pickDay = (iso: string) => {
    if (!pickingEnd) {
      setDraftStart(iso);
      setDraftEnd(iso);
      setStartText(iso);
      setEndText(iso);
      setPickingEnd(true);
    } else {
      const [s, e] = iso < draftStart ? [iso, draftStart] : [draftStart, iso];
      setDraftStart(s);
      setDraftEnd(e);
      setStartText(s);
      setEndText(e);
      setPickingEnd(false);
    }
  };

  /** 键盘输入提交：非法还原草稿；超上限截断；起止倒置自动交换 */
  const commitText = (which: "start" | "end", text: string) => {
    const d = parseISO(text.trim());
    if (!d) {
      if (which === "start") setStartText(draftStart);
      else setEndText(draftEnd);
      return;
    }
    let iso = fmtISO(d);
    if (iso > maxISO) iso = maxISO;
    let s = which === "start" ? iso : draftStart;
    let e = which === "end" ? iso : draftEnd;
    if (s > e) [s, e] = [e, s];
    setDraftStart(s);
    setDraftEnd(e);
    setStartText(s);
    setEndText(e);
  };

  const nextView = new Date(view.y, view.m + 1, 1);
  const inputCls =
    "w-28 px-2 py-1 text-xs font-mono select-text bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text focus:outline-none focus:border-jarvis-blue";

  return (
    <div ref={rootRef} className={clsx("relative", className)}>
      <button
        ref={triggerRef}
        onClick={() => (open ? setOpen(false) : openPanel())}
        className="flex items-center gap-1.5 bg-jarvis-bg border border-jarvis-border rounded-md px-2.5 py-1.5 text-sm font-mono text-jarvis-text hover:border-jarvis-blue focus:outline-none focus:border-jarvis-blue cursor-pointer whitespace-nowrap"
        title="选择回测时间范围"
      >
        <Calendar size={13} className="text-jarvis-text-secondary shrink-0" />
        {start}
        <span className="text-jarvis-text-secondary">→</span>
        {end}
      </button>

      {open && (
        <div
          className={clsx(
            "absolute top-full mt-1.5 z-50 bg-jarvis-card border border-jarvis-border rounded-lg shadow-2xl p-3",
            alignRight ? "right-0" : "left-0",
          )}
          style={{ width: PANEL_WIDTH }}
        >
          {/* 快捷预设 */}
          <div className="flex flex-wrap gap-1.5 mb-3">
            {PRESETS.map((p) => (
              <button
                key={p.key}
                onClick={() => {
                  const [s, e] = p.range();
                  onChange(s, e);
                  setOpen(false);
                }}
                className={clsx(
                  "px-2.5 py-1 text-xs rounded-full border transition-colors",
                  activePreset === p.key
                    ? "bg-jarvis-blue/15 border-jarvis-blue text-jarvis-blue"
                    : "bg-jarvis-bg border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
                )}
              >
                {p.label}
              </button>
            ))}
            <span
              className={clsx(
                "px-2.5 py-1 text-xs rounded-full border",
                activePreset === "custom"
                  ? "bg-jarvis-blue/15 border-jarvis-blue text-jarvis-blue"
                  : "bg-jarvis-bg border-jarvis-border text-jarvis-text-secondary/60",
              )}
            >
              自定义
            </span>
          </div>

          {/* 双月日历 + 翻页 */}
          <div className="flex items-start gap-2">
            <div className="flex flex-col gap-0.5 pt-0.5">
              <button
                onClick={() => shiftView(-12)}
                className="p-1 rounded text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/10"
                title="上一年"
                aria-label="上一年"
              >
                <ChevronsLeft size={14} />
              </button>
              <button
                onClick={() => shiftView(-1)}
                className="p-1 rounded text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/10"
                title="上一月"
                aria-label="上一月"
              >
                <ChevronLeft size={14} />
              </button>
            </div>

            <div className="flex-1 flex gap-4">
              <MonthGrid
                year={view.y}
                month={view.m}
                draftStart={draftStart}
                draftEnd={draftEnd}
                maxISO={maxISO}
                onPick={pickDay}
              />
              <MonthGrid
                year={nextView.getFullYear()}
                month={nextView.getMonth()}
                draftStart={draftStart}
                draftEnd={draftEnd}
                maxISO={maxISO}
                onPick={pickDay}
              />
            </div>

            <div className="flex flex-col gap-0.5 pt-0.5">
              <button
                onClick={() => shiftView(12)}
                className="p-1 rounded text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/10"
                title="下一年"
                aria-label="下一年"
              >
                <ChevronsRight size={14} />
              </button>
              <button
                onClick={() => shiftView(1)}
                className="p-1 rounded text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/10"
                title="下一月"
                aria-label="下一月"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>

          {/* 键盘输入 + 应用 */}
          <div className="flex items-center gap-2 mt-3 pt-3 border-t border-jarvis-border/60">
            <input
              value={startText}
              onChange={(e) => setStartText(e.target.value)}
              onBlur={() => commitText("start", startText)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitText("start", startText);
              }}
              placeholder="2025-01-01"
              className={inputCls}
              spellCheck={false}
            />
            <span className="text-jarvis-text-secondary text-xs">→</span>
            <input
              value={endText}
              onChange={(e) => setEndText(e.target.value)}
              onBlur={() => commitText("end", endText)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitText("end", endText);
              }}
              placeholder="2026-06-01"
              className={inputCls}
              spellCheck={false}
            />
            <span className="flex-1 text-[10px] text-jarvis-text-secondary/70 leading-tight">
              {pickingEnd ? "再点一天作为结束日期" : "日历点选起止，或直接输入"}
            </span>
            <button
              onClick={() => setOpen(false)}
              className="px-3 py-1 text-xs rounded-md border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
            >
              取消
            </button>
            <button
              onClick={() => {
                onChange(draftStart, draftEnd);
                setOpen(false);
              }}
              className="px-3 py-1 text-xs rounded-md bg-jarvis-blue text-white hover:bg-jarvis-blue/80 transition-colors"
            >
              应用
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
