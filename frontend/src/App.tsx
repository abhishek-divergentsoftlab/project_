import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "@/components/Layout";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { SidebarDataProvider } from "@/context/SidebarDataContext";

// Route-level code splitting: each page (and everything only it imports --
// Leaflet, the deal room, the AI chat) is its own chunk, fetched on first
// visit instead of in one 790 kB bundle before the login screen can paint.
const Dashboard = lazy(() => import("@/pages/Dashboard").then((m) => ({ default: m.Dashboard })));
const Login = lazy(() => import("@/pages/Login").then((m) => ({ default: m.Login })));
const Marketplace = lazy(() =>
  import("@/pages/Marketplace").then((m) => ({ default: m.Marketplace })),
);
const Matches = lazy(() => import("@/pages/Matches").then((m) => ({ default: m.Matches })));
const Profile = lazy(() => import("@/pages/Profile").then((m) => ({ default: m.Profile })));
const RFQList = lazy(() => import("@/pages/RFQList").then((m) => ({ default: m.RFQList })));
const RFQNew = lazy(() => import("@/pages/RFQNew").then((m) => ({ default: m.RFQNew })));
const Messages = lazy(() => import("@/pages/Messages").then((m) => ({ default: m.Messages })));
const AIChat = lazy(() => import("@/pages/AIChat").then((m) => ({ default: m.AIChat })));
const Signup = lazy(() => import("@/pages/Signup").then((m) => ({ default: m.Signup })));

function PageFallback() {
  return (
    <div className="loading-state" role="status" aria-live="polite">
      <span className="spinner" />
      Loading…
    </div>
  );
}

/** Suspense per route so the shell (sidebar, top bar) stays put while a page chunk loads. */
function Page({ children }: { children: ReactNode }) {
  return <Suspense fallback={<PageFallback />}>{children}</Suspense>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Page><Login /></Page>} />
      <Route path="/signup" element={<Page><Signup /></Page>} />

      <Route
        element={
          <ProtectedRoute>
            <SidebarDataProvider>
              <Layout />
            </SidebarDataProvider>
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Page><Dashboard /></Page>} />
        <Route path="/marketplace" element={<Page><Marketplace /></Page>} />
        <Route path="/rfqs" element={<Page><RFQList /></Page>} />
        <Route path="/rfqs/new" element={<Page><RFQNew /></Page>} />
        {/* Same form in edit mode: PATCH /rfqs/{id} existed but nothing called it. */}
        <Route path="/rfqs/:rfqId/edit" element={<Page><RFQNew /></Page>} />
        <Route path="/rfqs/:rfqId/matches" element={<Page><Matches /></Page>} />
        <Route path="/messages" element={<Page><Messages /></Page>} />
        <Route path="/ai-chat" element={<Page><AIChat /></Page>} />
        <Route path="/profile" element={<Page><Profile /></Page>} />
      </Route>


      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
