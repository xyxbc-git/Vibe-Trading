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
  Zap,
  Sprout,
  LineChart,
  Bell,
  Terminal,
  Settings,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";

interface NavItem {
  to: string;
  label: string;
  icon: React.ReactNode;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "总览", icon: <LayoutDashboard size={20} /> },
  { to: "/chart", label: "K线", icon: <CandlestickChart size={20} /> },
  { to: "/trading", label: "交易", icon: <ArrowLeftRight size={20} /> },
  { to: "/strategy", label: "策略", icon: <FlaskConical size={20} /> },
  { to: "/backtest", label: "回测", icon: <LineChart size={20} /> },
  { to: "/market", label: "情报", icon: <Globe size={20} /> },
  { to: "/ai", label: "AI助手", icon: <Bot size={20} /> },
  { to: "/scalper", label: "短线", icon: <Zap size={20} /> },
  { to: "/growth", label: "成长", icon: <Sprout size={20} /> },
  { to: "/alerts", label: "提醒", icon: <Bell size={20} /> },
  { to: "/terminal", label: "终端", icon: <Terminal size={20} /> },
];

export default function Sidebar() {
  const [expanded, setExpanded] = useState(false);

  return (
    <aside
      className={clsx(
        "flex flex-col h-full bg-jarvis-card border-r border-jarvis-border transition-all duration-200",
        expanded ? "w-48" : "w-16",
      )}
    >
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center justify-center h-14 text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
      >
        {expanded ? <PanelLeftClose size={20} /> : <PanelLeftOpen size={20} />}
      </button>

      <nav className="flex-1 flex flex-col gap-1 px-2">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
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
        ))}
      </nav>

      <div className="px-2 pb-4">
        <NavLink
          to="/settings"
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
