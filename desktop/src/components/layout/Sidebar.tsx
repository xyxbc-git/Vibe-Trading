import { useState } from "react";
import { NavLink } from "react-router-dom";
import { clsx } from "clsx";
import {
  LayoutDashboard,
  CandlestickChart,
  ArrowLeftRight,
  FlaskConical,
  Globe,
  Bot,
  Wand2,
  Dna,
  Zap,
  Sprout,
  LineChart,
  Bell,
  History,
  Terminal,
  Settings,
  Scale,
  PanelLeftClose,
  PanelLeftOpen,
  BookOpenCheck,
  GitCommitHorizontal,
  Footprints,
} from "lucide-react";

interface NavItem {
  to: string;
  label: string;
  icon: React.ReactNode;
}

interface NavGroup {
  title: string;
  items: NavItem[];
}

// 按使用频率组织：驾驶舱/图表/交易为高频入口，靠前排列
const NAV_GROUPS: NavGroup[] = [
  {
    title: "驾驶舱",
    items: [{ to: "/", label: "总览", icon: <LayoutDashboard size={20} /> }],
  },
  {
    title: "图表",
    items: [
      { to: "/chart", label: "K线", icon: <CandlestickChart size={20} /> },
      { to: "/depth", label: "盘口", icon: <BookOpenCheck size={20} /> },
      { to: "/footprint", label: "足迹", icon: <Footprints size={20} /> },
    ],
  },
  {
    title: "交易",
    items: [
      { to: "/trading", label: "交易", icon: <ArrowLeftRight size={20} /> },
      { to: "/trades", label: "记录", icon: <History size={20} /> },
      { to: "/funding-arb", label: "套利", icon: <Scale size={20} /> },
      { to: "/scalper", label: "短线", icon: <Zap size={20} /> },
      { to: "/alerts", label: "提醒", icon: <Bell size={20} /> },
    ],
  },
  {
    title: "智能",
    items: [
      { to: "/ai", label: "AI助手", icon: <Bot size={20} /> },
      { to: "/ai-strategy", label: "AI策略", icon: <Wand2 size={20} /> },
      { to: "/strategy-evolve", label: "自动进化", icon: <Dna size={20} /> },
      { to: "/strategy", label: "策略", icon: <FlaskConical size={20} /> },
      { to: "/backtest", label: "回测", icon: <LineChart size={20} /> },
    ],
  },
  {
    title: "数据",
    items: [
      { to: "/market", label: "情报", icon: <Globe size={20} /> },
      { to: "/signal-history", label: "信号历史", icon: <GitCommitHorizontal size={20} /> },
      { to: "/growth", label: "成长", icon: <Sprout size={20} /> },
      { to: "/terminal", label: "终端", icon: <Terminal size={20} /> },
    ],
  },
];

function NavEntry({ item, expanded }: { item: NavItem; expanded: boolean }) {
  return (
    <NavLink
      to={item.to}
      end={item.to === "/"}
      title={expanded ? undefined : item.label}
      className={({ isActive }) =>
        clsx(
          "flex items-center gap-3 px-3 py-2.5 rounded-lg transition-colors",
          isActive
            ? "bg-jarvis-blue/15 text-jarvis-blue"
            : "text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/5",
        )
      }
    >
      {item.icon}
      {expanded && (
        <span className="text-sm font-medium whitespace-nowrap">
          {item.label}
        </span>
      )}
    </NavLink>
  );
}

export default function Sidebar() {
  const [expanded, setExpanded] = useState(false);

  return (
    <aside
      className={clsx(
        "flex flex-col h-full bg-jarvis-card border-r border-jarvis-border transition-all duration-200",
        expanded ? "w-48" : "w-16",
      )}
    >
      {/* macOS 红绿灯预留区：高度与主进程 trafficLightPosition(y:18) 匹配，兼作窗口拖拽条 */}
      <div
        className="h-12 shrink-0"
        style={{ WebkitAppRegion: "drag" } as React.CSSProperties}
        aria-hidden
      />
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center justify-center h-11 text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
        aria-label={expanded ? "收起侧栏" : "展开侧栏"}
      >
        {expanded ? <PanelLeftClose size={20} /> : <PanelLeftOpen size={20} />}
      </button>

      <nav className="flex-1 flex flex-col px-2 overflow-y-auto">
        {NAV_GROUPS.map((group, gi) => (
          <div key={group.title} className={clsx(gi > 0 && "mt-1")}>
            {expanded ? (
              <p className="text-[10px] font-medium text-jarvis-text-secondary/70 uppercase tracking-wider px-3 pt-2 pb-1">
                {group.title}
              </p>
            ) : (
              gi > 0 && <div className="border-t border-jarvis-border/50 mx-2 my-1.5" />
            )}
            <div className="flex flex-col gap-1">
              {group.items.map((item) => (
                <NavEntry key={item.to} item={item} expanded={expanded} />
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="px-2 pb-4">
        {expanded && (
          <p className="text-[10px] font-medium text-jarvis-text-secondary/70 uppercase tracking-wider px-3 pt-2 pb-1">
            设置
          </p>
        )}
        <NavLink
          to="/settings"
          title={expanded ? undefined : "设置"}
          className={({ isActive }) =>
            clsx(
              "flex items-center gap-3 px-3 py-2.5 rounded-lg transition-colors",
              isActive
                ? "bg-jarvis-blue/15 text-jarvis-blue"
                : "text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/5",
            )
          }
        >
          <Settings size={20} />
          {expanded && (
            <span className="text-sm font-medium whitespace-nowrap">
              设置
            </span>
          )}
        </NavLink>
      </div>
    </aside>
  );
}
