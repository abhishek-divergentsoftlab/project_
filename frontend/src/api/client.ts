import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";

import type { TokenPair } from "@/types";

const ACCESS_KEY = "mp.access_token";
const REFRESH_KEY = "mp.refresh_token";

/**
 * Tokens live in localStorage, which is readable by any script on the page.
 * That is acceptable for development; moving refresh tokens to httpOnly
 * cookies belongs with the rest of the hardening work.
 */
export const tokenStore = {
  access: () => localStorage.getItem(ACCESS_KEY),
  refresh: () => localStorage.getItem(REFRESH_KEY),
  save(tokens: TokenPair) {
    localStorage.setItem(ACCESS_KEY, tokens.access_token);
    localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "/api/v1",
  headers: { "Content-Type": "application/json" },
});

api.interceptors.request.use((config) => {
  const token = tokenStore.access();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

/** Set by AuthContext so a failed refresh can clear the session. */
let onAuthFailure: (() => void) | null = null;
export function setAuthFailureHandler(handler: () => void) {
  onAuthFailure = handler;
}

// A single in-flight refresh shared by every 401'd request, so a burst of
// parallel calls triggers one refresh rather than a stampede.
let refreshInFlight: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  const refreshToken = tokenStore.refresh();
  if (!refreshToken) throw new Error("no refresh token");

  // A bare axios call: using `api` would recurse through this interceptor.
  const { data } = await axios.post<TokenPair>(
    `${api.defaults.baseURL}/auth/refresh`,
    { refresh_token: refreshToken },
    { headers: { "Content-Type": "application/json" } },
  );
  tokenStore.save(data);
  return data.access_token;
}

interface RetriableRequest extends InternalAxiosRequestConfig {
  _retried?: boolean;
}

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const request = error.config as RetriableRequest | undefined;

    const shouldRefresh =
      error.response?.status === 401 &&
      request &&
      !request._retried &&
      // Never try to refresh the refresh call itself, or a login attempt.
      !request.url?.includes("/auth/refresh") &&
      !request.url?.includes("/auth/login");

    if (!shouldRefresh) return Promise.reject(error);

    request._retried = true;
    try {
      refreshInFlight ??= refreshAccessToken().finally(() => {
        refreshInFlight = null;
      });
      const token = await refreshInFlight;
      request.headers.Authorization = `Bearer ${token}`;
      return api(request);
    } catch (refreshError) {
      tokenStore.clear();
      onAuthFailure?.();
      return Promise.reject(refreshError);
    }
  },
);

/** Pulls FastAPI's error shape into a single displayable string. */
export function errorMessage(error: unknown, fallback = "Something went wrong"): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    // 422 from Pydantic: a list of per-field errors.
    if (Array.isArray(detail)) {
      return detail
        .map((item: { loc?: (string | number)[]; msg?: string }) => {
          const field = item.loc?.filter((part) => part !== "body").join(".");
          return field ? `${field}: ${item.msg}` : item.msg;
        })
        .filter(Boolean)
        .join("; ");
    }
    if (error.message) return error.message;
  }
  if (error instanceof Error) return error.message;
  return fallback;
}
