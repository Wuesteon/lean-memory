import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import type { WhoAmI } from "./types";
import { ApiError, bootLocalToken, setBearerKey, whoami as probeWhoami } from "./api";

type Status = "loading" | "ready" | "needs-login" | "error";

interface AuthState {
  status: Status;
  whoami: WhoAmI | null;
  error: string | null;
  login: (key: string) => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<Status>("loading");
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [error, setError] = useState<string | null>(null);

  const probe = useCallback(async () => {
    try {
      const w = await probeWhoami();
      setWho(w);
      if (w.authenticated) {
        setStatus("ready");
        setError(null);
      } else if (w.mode === "docker") {
        setStatus("needs-login");
      } else {
        // local + unauthenticated: no login screen, plain error (§7).
        setStatus("error");
        setError("No valid session token. Re-open the console from the URL it printed.");
      }
    } catch (e) {
      const msg = e instanceof ApiError ? `whoami failed (${e.status})` : "whoami failed";
      setStatus("error");
      setError(msg);
    }
  }, []);

  useEffect(() => {
    bootLocalToken();
    void probe();
  }, [probe]);

  const login = useCallback(
    async (key: string) => {
      setBearerKey(key);
      setStatus("loading");
      await probe();
    },
    [probe],
  );

  return (
    <AuthContext.Provider value={{ status, whoami: who, error, login }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
