import {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { setAuthFailureHandler, tokenStore } from "@/api/client";
import { auth, type SignupPayload } from "@/api/endpoints";
import type { User } from "@/types";

interface AuthContextValue {
  user: User | null;
  /** True until the stored token has been checked against the API. */
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (payload: SignupPayload) => Promise<void>;
  logout: () => void;
  refreshUser: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const logout = useCallback(() => {
    auth.logout();
    setUser(null);
  }, []);

  const refreshUser = useCallback(async () => {
    const me = await auth.me();
    setUser(me);
  }, []);

  // Lets the axios interceptor drop the session when a refresh fails.
  useEffect(() => {
    setAuthFailureHandler(() => setUser(null));
  }, []);

  // A stored token may be expired or revoked, so it is validated against the
  // API before the app treats anyone as signed in.
  useEffect(() => {
    let cancelled = false;
    async function restore() {
      if (!tokenStore.access()) {
        setLoading(false);
        return;
      }
      try {
        const me = await auth.me();
        if (!cancelled) setUser(me);
      } catch {
        if (!cancelled) {
          tokenStore.clear();
          setUser(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void restore();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    await auth.login(email, password);
    setUser(await auth.me());
  }, []);

  const signup = useCallback(async (payload: SignupPayload) => {
    await auth.signup(payload);
    setUser(await auth.me());
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, signup, logout, refreshUser }),
    [user, loading, login, signup, logout, refreshUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
