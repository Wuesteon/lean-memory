import type React from "react";
import { NavLink } from "react-router-dom";
import type { NamespaceCard, WhoAmI } from "../types";

const navItems = [
  { to: "/", label: "Overview", end: true },
  { to: "/memories", label: "Memories", end: false },
  { to: "/episodes", label: "Episodes", end: false },
  { to: "/activity", label: "Activity", end: false },
];

export default function Layout({
  who,
  namespaces,
  activeNs,
  onNsChange,
  children,
}: {
  who: WhoAmI;
  namespaces: NamespaceCard[];
  activeNs: string | null;
  onNsChange: (ns: string) => void;
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-full bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-6 py-3">
          <span className="text-sm font-semibold tracking-tight">
            lean-memory console
          </span>
          <nav className="flex gap-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `rounded px-3 py-1.5 text-sm ${
                    isActive
                      ? "bg-slate-900 text-white"
                      : "text-slate-600 hover:bg-slate-100"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <label className="text-xs text-slate-500">namespace</label>
            <select
              className="rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={activeNs ?? ""}
              onChange={(e) => onNsChange(e.target.value)}
              disabled={namespaces.length === 0}
            >
              {namespaces.length === 0 && <option value="">no namespaces</option>}
              {namespaces.map((n) => (
                <option key={n.name} value={n.name}>
                  {n.name}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="mx-auto max-w-6xl px-6 pb-2 text-xs text-slate-400">
          {who.mode} mode · root {who.data_root}
        </div>
      </header>
      <main className="mx-auto max-w-6xl">{children}</main>
    </div>
  );
}
