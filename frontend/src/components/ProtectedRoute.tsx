import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "@/context/useAuth";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  // Without this gate the redirect would fire before the stored token has
  // been validated, bouncing a signed-in user to the login page on reload.
  if (loading) return <div className="centered muted">Loading...</div>;

  if (!user) {
    // The whole location, not just the pathname: a deep link with a query
    // string came back stripped after signing in.
    const from = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to="/login" replace state={{ from }} />;
  }

  return <>{children}</>;
}
