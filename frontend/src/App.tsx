import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { AdaptiveEngine } from "./pages/AdaptiveEngine";
import { AIUsage } from "./pages/AIUsage";
import { AutonomyCenter } from "./pages/AutonomyCenter";
import { Backtest } from "./pages/Backtest";
import { Evaluation } from "./pages/Evaluation";
import { Robot } from "./pages/Robot";
import { Settings } from "./pages/Settings";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Robot />} />
        {/* Páginas técnicas preservadas, fora da navegação principal */}
        <Route path="/adaptive" element={<AdaptiveEngine />} />
        <Route path="/autonomy" element={<AutonomyCenter />} />
        <Route path="/ai-usage" element={<AIUsage />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/backtest" element={<Backtest />} />
        <Route path="/evaluation" element={<Evaluation />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
