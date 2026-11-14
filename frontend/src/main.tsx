import "./styles.css";
import { StrictMode, Suspense, lazy, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { PlainShell, ProjectLayout } from "./components/Layout";
import { Loading } from "./components/ui";
import { SessionProvider, ScopeProvider, useSession } from "./lib/session";
import Admin from "./pages/Admin";
import Assistant from "./pages/Assistant";
import AuthCallback from "./pages/AuthCallback";
import Conflicts from "./pages/Conflicts";
import Dashboard from "./pages/Dashboard";
import Explorer from "./pages/Explorer";
import Jobs from "./pages/Jobs";
import Login from "./pages/Login";
import Projects from "./pages/Projects";
import Review from "./pages/Review";
import Targets from "./pages/Targets";
import Field from "./pages/Field";
import Upload from "./pages/Upload";

// MapLibre is large; load it only when the map is opened (low-bandwidth, table-first navigation).
const MapPage = lazy(() => import("./pages/MapPage"));

function RequireUser({ children }: { children: ReactNode }) {
  const { me, loading } = useSession();
  if (loading) return <Loading what="Signing in" />;
  if (!me) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route path="/projects" element={<RequireUser><PlainShell><Projects /></PlainShell></RequireUser>} />
      <Route path="/p/:pid" element={<RequireUser><ScopeProvider><ProjectLayout /></ScopeProvider></RequireUser>}>
        <Route index element={<Dashboard />} />
        <Route path="upload" element={<Upload />} />
        <Route path="review" element={<Review />} />
        <Route path="explorer" element={<Explorer />} />
        <Route path="map" element={<Suspense fallback={<Loading what="Loading map" />}><MapPage /></Suspense>} />
        <Route path="assistant" element={<Assistant />} />
        <Route path="conflicts" element={<Conflicts />} />
        <Route path="targets" element={<Targets />} />
        <Route path="field" element={<Field />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="admin" element={<Admin />} />
      </Route>
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <SessionProvider>
        <App />
      </SessionProvider>
    </BrowserRouter>
  </StrictMode>,
);
