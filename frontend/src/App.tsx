import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "@/components/Layout";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Login } from "@/pages/Login";
import { Matches } from "@/pages/Matches";
import { Profile } from "@/pages/Profile";
import { RFQList } from "@/pages/RFQList";
import { RFQNew } from "@/pages/RFQNew";
import { Search } from "@/pages/Search";
import { Signup } from "@/pages/Signup";
import { Messages } from "@/pages/Messages";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/signup" element={<Signup />} />

      <Route
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route path="/rfqs" element={<RFQList />} />
        <Route path="/rfqs/new" element={<RFQNew />} />
        {/* Same form in edit mode: PATCH /rfqs/{id} existed but nothing called it. */}
        <Route path="/rfqs/:rfqId/edit" element={<RFQNew />} />
        <Route path="/rfqs/:rfqId/matches" element={<Matches />} />
        <Route path="/messages" element={<Messages />} />
        <Route path="/search" element={<Search />} />
        <Route path="/profile" element={<Profile />} />
      </Route>

      <Route path="*" element={<Navigate to="/rfqs" replace />} />
    </Routes>
  );
}
