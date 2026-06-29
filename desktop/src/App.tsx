import { Routes, Route } from "react-router-dom";
import Layout from "./components/layout/Layout";
import ErrorBoundary from "./components/common/ErrorBoundary";
import Dashboard from "./pages/Dashboard";
import Chart from "./pages/Chart";
import Trading from "./pages/Trading";
import StrategyLab from "./pages/StrategyLab";
import MarketIntel from "./pages/MarketIntel";
import AIChat from "./pages/AIChat";
import ScalperData from "./pages/ScalperData";
import Growth from "./pages/Growth";
import Backtest from "./pages/Backtest";
import Terminal from "./pages/Terminal";
import PriceAlerts from "./pages/PriceAlerts";
import SettingsPage from "./pages/Settings";

function PageGuard({ children, name }: { children: React.ReactNode; name: string }) {
  return <ErrorBoundary fallbackTitle={`${name} 加载异常`}>{children}</ErrorBoundary>;
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<PageGuard name="总览"><Dashboard /></PageGuard>} />
        <Route path="chart" element={<PageGuard name="K线图表"><Chart /></PageGuard>} />
        <Route path="trading" element={<PageGuard name="交易中心"><Trading /></PageGuard>} />
        <Route path="strategy" element={<PageGuard name="策略实验室"><StrategyLab /></PageGuard>} />
        <Route path="backtest" element={<PageGuard name="回测"><Backtest /></PageGuard>} />
        <Route path="market" element={<PageGuard name="市场情报"><MarketIntel /></PageGuard>} />
        <Route path="ai" element={<PageGuard name="AI 助手"><AIChat /></PageGuard>} />
        <Route path="scalper" element={<PageGuard name="短线数据"><ScalperData /></PageGuard>} />
        <Route path="growth" element={<PageGuard name="成长进度"><Growth /></PageGuard>} />
        <Route path="alerts" element={<PageGuard name="价位提醒"><PriceAlerts /></PageGuard>} />
        <Route path="terminal" element={<PageGuard name="后端终端"><Terminal /></PageGuard>} />
        <Route path="settings" element={<PageGuard name="设置"><SettingsPage /></PageGuard>} />
      </Route>
    </Routes>
  );
}
