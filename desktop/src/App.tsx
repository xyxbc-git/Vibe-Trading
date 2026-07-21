import { Routes, Route } from "react-router-dom";
import Layout from "./components/layout/Layout";
import ErrorBoundary from "./components/common/ErrorBoundary";
import Dashboard from "./pages/Dashboard";
import Chart from "./pages/Chart";
import Trading from "./pages/Trading";
import FundingArb from "./pages/FundingArb";
import StrategyLab from "./pages/StrategyLab";
import AIStrategy from "./pages/AIStrategy";
import StrategyEvolve from "./pages/StrategyEvolve";
import MarketIntel from "./pages/MarketIntel";
import AIChat from "./pages/AIChat";
import ScalperData from "./pages/ScalperData";
import Growth from "./pages/Growth";
import Backtest from "./pages/Backtest";
import Terminal from "./pages/Terminal";
import PriceAlerts from "./pages/PriceAlerts";
import TradeRecords from "./pages/TradeRecords";
import SettingsPage from "./pages/Settings";
import SignalHistory from "./pages/SignalHistory";
import DepthView from "./pages/DepthView";
import FootprintChart from "./components/FootprintChart/FootprintChart";

function PageGuard({ children, name }: { children: React.ReactNode; name: string }) {
  return <ErrorBoundary fallbackTitle={`${name} 加载异常`}>{children}</ErrorBoundary>;
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<PageGuard name="总览"><Dashboard /></PageGuard>} />
        <Route path="chart" element={<PageGuard name="K线图表"><Chart /></PageGuard>} />
        <Route path="depth" element={<PageGuard name="盘口透视"><DepthView /></PageGuard>} />
        <Route path="footprint" element={<PageGuard name="足迹图"><div className="h-full min-h-0"><FootprintChart /></div></PageGuard>} />
        <Route path="trading" element={<PageGuard name="交易中心"><Trading /></PageGuard>} />
        <Route path="trades" element={<PageGuard name="交易记录"><TradeRecords /></PageGuard>} />
        <Route path="funding-arb" element={<PageGuard name="费率套利"><FundingArb /></PageGuard>} />
        <Route path="strategy" element={<PageGuard name="策略实验室"><StrategyLab /></PageGuard>} />
        <Route path="ai-strategy" element={<PageGuard name="AI 策略工坊"><AIStrategy /></PageGuard>} />
        <Route path="strategy-evolve" element={<PageGuard name="策略自动进化"><StrategyEvolve /></PageGuard>} />
        <Route path="backtest" element={<PageGuard name="回测"><Backtest /></PageGuard>} />
        <Route path="market" element={<PageGuard name="市场情报"><MarketIntel /></PageGuard>} />
        <Route path="ai" element={<PageGuard name="AI 助手"><AIChat /></PageGuard>} />
        <Route path="scalper" element={<PageGuard name="短线数据"><ScalperData /></PageGuard>} />
        <Route path="signal-history" element={<PageGuard name="信号历史"><SignalHistory /></PageGuard>} />
        <Route path="growth" element={<PageGuard name="成长进度"><Growth /></PageGuard>} />
        <Route path="alerts" element={<PageGuard name="价位提醒"><PriceAlerts /></PageGuard>} />
        <Route path="terminal" element={<PageGuard name="后端终端"><Terminal /></PageGuard>} />
        <Route path="settings" element={<PageGuard name="设置"><SettingsPage /></PageGuard>} />
      </Route>
    </Routes>
  );
}
