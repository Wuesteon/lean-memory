import type { Mode } from "../types";

function Snippet({ title, code }: { title: string; code: string }) {
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium text-slate-600">{title}</div>
      <pre className="overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
        <code>{code}</code>
      </pre>
    </div>
  );
}

export default function ConnectSnippets({
  mode,
  dataRoot,
}: {
  mode: Mode;
  dataRoot: string;
}) {
  return (
    <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-6">
      <div>
        <h2 className="text-sm font-semibold">No memories yet</h2>
        <p className="mt-1 text-xs text-slate-500">
          Connect an agent to write memories into{" "}
          <code className="rounded bg-slate-100 px-1">{dataRoot}</code>. The
          console is read-only over whatever your agent stores.
        </p>
      </div>
      <Snippet
        title="Install the Claude Code plugin"
        code={
          "/plugin marketplace add <owner>/lean-memory-console\n" +
          "/plugin install lean-memory"
        }
      />
      <Snippet
        title="Or add the observing MCP directly"
        code={"claude mcp add lean-memory -- uvx lean-memory-console mcp"}
      />
      {mode === "docker" && (
        <Snippet
          title="Docker (HTTP MCP)"
          code={
            "claude mcp add --transport http lean-memory http://<host>:8377/mcp \\\n" +
            '  --header "Authorization: Bearer $LM_API_KEY"'
          }
        />
      )}
    </div>
  );
}
