import type { NamespaceCard, WhoAmI } from "../types";

export default function Overview(_props: {
  who: WhoAmI;
  namespaces: NamespaceCard[];
}) {
  return <div className="p-6 text-sm text-slate-500">Overview (Task 11)</div>;
}
