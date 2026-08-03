import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import CarPage from "./pages/CarPage";
import AlertsPage from "./pages/AlertsPage";
import DrivingPage from "./pages/DrivingPage";
import MaintenancePage from "./pages/MaintenancePage";
import OverviewPage from "./pages/OverviewPage";
import SettingsPage from "./pages/SettingsPage";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<OverviewPage />} />
        <Route path="/cars/:id" element={<CarPage />} />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="/maintenance" element={<MaintenancePage />} />
        <Route path="/driving" element={<DrivingPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}