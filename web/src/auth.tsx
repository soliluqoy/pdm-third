// PREDICT — optional single-admin auth provider.
// Calls /api/v1/auth/me on load to learn whether the server requires login.
// When auth is disabled server-side, /me returns authenticated=true and the
// app behaves exactly as before.
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";
import { api } from "./api";

interface AuthContextValue {
  /** True once the initial /me check has completed. */
  loading: boolean;
  /** Whether the current session is authenticated (always true when auth is off). */
  authenticated: boolean;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  loading: true,
  authenticated: false,
  login: async () => {},
  logout: async () => {},
});

export const useAuth = () => useContext(AuthContext);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((state) => {
        if (!cancelled) setAuthenticated(state.authenticated);
      })
      .catch(() => {
        if (!cancelled) setAuthenticated(false);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (password: string) => {
    const state = await api.login(password);
    setAuthenticated(state.authenticated);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setAuthenticated(false);
    }
  }, []);

  const value = useMemo(
    () => ({ loading, authenticated, login, logout }),
    [loading, authenticated, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}