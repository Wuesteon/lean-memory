import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { listNamespaces } from "./api";
import type { NamespaceCard } from "./types";
import Layout from "./components/Layout";
import Overview from "./pages/Overview";
import Memories from "./pages/Memories";
import Episodes from "./pages/Episodes";
import Activity from "./pages/Activity";

function LoginScreen() {
  const { login, error } = useAuth();
  const [key, setKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50">
      <form
        className="w-80 space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
        onSubmit={async (e) => {
          e.preventDefault();
          setSubmitting(true);
          try {
            await login(key.trim());
          } finally {
            setSubmitting(false);
          }
        }}
      >
        <h1 className="text-sm font-semibold">lean-memory console</h1>
        <p className="text-xs text-slate-500">
          Enter the API key (LM_API_KEY) for this container.
        </p>
        <input
          type="password"
          autoFocus
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="API key"
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
        />
        {error && <p className="text-xs text-red-600">{error}</p>}
        <button
          type="submit"
          disabled={submitting || key.trim() === ""}
          className="w-full rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-40"
        >
          {submitting ? "Checking…" : "Connect"}
        </button>
      </form>
    </div>
  );
}

function ErrorScreen({ message }: { message: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
      <div className="max-w-md rounded-lg border border-red-200 bg-white p-6 text-center shadow-sm">
        <h1 className="text-sm font-semibold text-red-700">Cannot connect</h1>
        <p className="mt-2 text-xs text-slate-600">{message}</p>
      </div>
    </div>
  );
}

function Shell() {
  const { status, whoami, error } = useAuth();
  const [namespaces, setNamespaces] = useState<NamespaceCard[]>([]);
  const [activeNs, setActiveNs] = useState<string | null>(null);
  const [nsError, setNsError] = useState<string | null>(null);

  useEffect(() => {
    if (status !== "ready") return;
    let cancelled = false;
    listNamespaces()
      .then((ns) => {
        if (cancelled) return;
        setNamespaces(ns);
        setActiveNs((prev) => prev ?? (ns.length ? ns[0].name : null));
      })
      .catch((e) => !cancelled && setNsError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [status]);

  if (status === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-400">
        Loading…
      </div>
    );
  }
  if (status === "needs-login") return <LoginScreen />;
  if (status === "error" || !whoami) {
    return <ErrorScreen message={error ?? "Unknown error"} />;
  }

  const ns = activeNs ?? "";
  return (
    <Layout
      who={whoami}
      namespaces={namespaces}
      activeNs={activeNs}
      onNsChange={setActiveNs}
    >
      {nsError && (
        <div className="p-4 text-sm text-red-600">
          Failed to list namespaces: {nsError}
        </div>
      )}
      <Routes>
        <Route
          path="/"
          element={<Overview who={whoami} namespaces={namespaces} />}
        />
        <Route path="/memories" element={<Memories ns={ns} />} />
        <Route path="/episodes" element={<Episodes ns={ns} />} />
        <Route path="/activity" element={<Activity ns={ns} />} />
      </Routes>
    </Layout>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}
