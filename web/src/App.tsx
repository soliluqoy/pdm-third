import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import Layout from "./components/Layout";
import { useAuth } from "./auth";
import CarPage from "./pages/CarPage";
import AlertsPage from "./pages/AlertsPage";
import DrivingPage from "./pages/DrivingPage";
import LoginPage from "./pages/LoginPage";
import MaintenancePage from "./pages/MaintenancePage";
import OverviewPage from "./pages/OverviewPage";
import SettingsPage from "./pages/SettingsPage";

function FullScreenLoader() {
  return (
    <div className="min-h-dvh flex items-center justify-center">
      <div className="w-8 h-8 rounded-full border-2 border-accent/30 border-t-accent animate-spin" />
    </div>
  );
}

// Guards the app: shows a spinner while the auth state loads, then either the
// app shell or the login screen (when the server requires a password).
function Protected() {
  const { loading, authenticated } = useAuth();
  const location = useLocation();
  if (loading) return <FullScreenLoader />;
  if (!authenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <Layout />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<Protected />}>
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