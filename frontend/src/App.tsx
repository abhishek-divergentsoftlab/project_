import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "@/components/Layout";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { SidebarDataProvider } from "@/context/SidebarDataContext";
import { Dashboard } from "@/pages/Dashboard";
import { Login } from "@/pages/Login";
import { Marketplace } from "@/pages/Marketplace";
import { Matches } from "@/pages/Matches";
import { Profile } from "@/pages/Profile";
import { RFQList } from "@/pages/RFQList";
import { RFQNew } from "@/pages/RFQNew";
import { Messages } from "@/pages/Messages";
import { AIChat } from "@/pages/AIChat";
import { Signup } from "@/pages/Signup";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/signup" element={<Signup />} />

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
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/marketplace" element={<Marketplace />} />
        <Route path="/rfqs" element={<RFQList />} />
        <Route path="/rfqs/new" element={<RFQNew />} />
        {/* Same form in edit mode: PATCH /rfqs/{id} existed but nothing called it. */}
        <Route path="/rfqs/:rfqId/edit" element={<RFQNew />} />
        <Route path="/rfqs/:rfqId/matches" element={<Matches />} />
        <Route path="/messages" element={<Messages />} />
        <Route path="/ai-chat" element={<AIChat />} />
        <Route path="/profile" element={<Profile />} />
      </Route>


      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
