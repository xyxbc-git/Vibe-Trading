import { render, screen, fireEvent } from "@testing-library/react";
import { PatternExplainCard } from "../PatternExplainCard";
import type { DetectedPattern } from "@/lib/patterns";

// Mock patterns aligned with the real DetectedPattern contract from lib/patterns.ts
const bullish: DetectedPattern = {
  type: "double_bottom",
  nameCn: "双底（W底）",
  direction: "bullish",
  startIndex: 10,
  endIndex: 40,
  keyPoints: [
    { index: 10, ts: "2026-06-01", price: 100, label: "底1 100", note: "第一次探底获得支撑" },
    { index: 25, ts: "2026-06-15", price: 112, label: "颈线点 112", note: "两底之间的反弹高点" },
    { index: 40, ts: "2026-07-01", price: 101, label: "底2 101", note: "第二次探底不破前低" },
  ],
  boundaries: [{ kind: "neckline", label: "颈线", x0: 10, y0: 112, x1: 40, y1: 112 }],
  breakout: 112,
  target: 124,
  stop: 100,
  confidence: 0.75,
  summary: "价格两次回踩获得支撑，形成 W 底；突破颈线后按双底高度量度看涨。",
};

const bearish: DetectedPattern = {
  type: "wedge_rising",
  nameCn: "上升楔形",
  direction: "bearish",
  startIndex: 5,
  endIndex: 45,
  keyPoints: [{ index: 45, ts: "2026-07-05", price: 130, label: "上轨触点 130", note: "触碰上边界后折返" }],
  boundaries: [
    { kind: "upper", label: "上边界", x0: 5, y0: 120, x1: 45, y1: 132 },
    { kind: "lower", label: "下边界", x0: 5, y0: 110, x1: 45, y1: 128 },
  ],
  breakout: 128,
  target: 110,
  stop: 132,
  confidence: 0.55,
  summary: "上涨动能衰竭的楔形，跌破下边界后看跌回落。",
};

const neutral: DetectedPattern = {
  ...bullish,
  type: "rectangle",
  nameCn: "矩形整理（箱体）",
  direction: "neutral",
  startIndex: 20,
  confidence: 0.42,
  summary: "价格在箱体内震荡蓄力，方向待突破。",
};

describe("PatternExplainCard", () => {
  it("renders the empty state when no patterns are detected", () => {
    render(<PatternExplainCard patterns={[]} activeIndex={0} onSelect={() => {}} />);
    expect(screen.getByText("当前区间未发现明显形态")).toBeInTheDocument();
    expect(screen.getByText(/已扫描：楔形/)).toBeInTheDocument();
  });

  it("renders name, direction badge, confidence, summary, key levels and key points", () => {
    render(<PatternExplainCard patterns={[bullish]} activeIndex={0} onSelect={() => {}} />);
    expect(screen.getByText("双底（W底）")).toBeInTheDocument();
    expect(screen.getByText("▲ 看涨")).toBeInTheDocument();
    expect(screen.getByText("置信度 75%")).toBeInTheDocument();
    expect(screen.getByText(/形成 W 底/)).toBeInTheDocument();
    expect(screen.getByText(/突破位/)).toBeInTheDocument();
    expect(screen.getByText(/量度目标/)).toBeInTheDocument();
    expect(screen.getByText(/建议止损/)).toBeInTheDocument();
    // Key point rows: price-stripped label + own price column
    expect(screen.getByText("底1")).toBeInTheDocument();
    expect(screen.getByText("颈线点")).toBeInTheDocument();
    expect(screen.getByText("2026-06-15")).toBeInTheDocument();
    expect(screen.getByText("已叠加到图上")).toBeInTheDocument();
  });

  it("colors the direction badge by semantic token", () => {
    const { rerender } = render(<PatternExplainCard patterns={[bullish]} activeIndex={0} onSelect={() => {}} />);
    expect(screen.getByText("▲ 看涨").className).toContain("text-success");
    rerender(<PatternExplainCard patterns={[bearish]} activeIndex={0} onSelect={() => {}} />);
    expect(screen.getByText("▼ 看跌").className).toContain("text-danger");
    rerender(<PatternExplainCard patterns={[neutral]} activeIndex={0} onSelect={() => {}} />);
    expect(screen.getByText("＝ 中性·待突破").className).toContain("text-muted-foreground");
  });

  it("hides the tab strip for a single pattern and shows it for several", () => {
    const { rerender } = render(<PatternExplainCard patterns={[bullish]} activeIndex={0} onSelect={() => {}} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    rerender(<PatternExplainCard patterns={[bullish, bearish, neutral]} activeIndex={0} onSelect={() => {}} />);
    expect(screen.getAllByRole("button")).toHaveLength(3);
  });

  it("marks the active tab and reports switches via onSelect", () => {
    const onSelect = vi.fn();
    render(<PatternExplainCard patterns={[bullish, bearish]} activeIndex={0} onSelect={onSelect} />);
    const tabs = screen.getAllByRole("button");
    expect(tabs[0]).toHaveAttribute("aria-pressed", "true");
    expect(tabs[1]).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(tabs[1]);
    expect(onSelect).toHaveBeenCalledWith(1);
  });

  it("shows the pattern picked by activeIndex", () => {
    render(<PatternExplainCard patterns={[bullish, bearish]} activeIndex={1} onSelect={() => {}} />);
    expect(screen.getByText("▼ 看跌")).toBeInTheDocument();
    expect(screen.getByText(/上涨动能衰竭/)).toBeInTheDocument();
  });

  it("clamps an out-of-range activeIndex instead of crashing", () => {
    render(<PatternExplainCard patterns={[bullish, bearish]} activeIndex={9} onSelect={() => {}} />);
    expect(screen.getByText("▼ 看跌")).toBeInTheDocument();
  });
});
