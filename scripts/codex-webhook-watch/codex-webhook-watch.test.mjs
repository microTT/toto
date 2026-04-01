import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const scriptPath = fileURLToPath(
  new URL("./codex-webhook-watch.mjs", import.meta.url),
);

async function runWatcher(lines) {
  const rootDir = await mkdtemp(path.join(os.tmpdir(), "codex-watch-"));

  try {
    const sessionFile = path.join(rootDir, "session.jsonl");
    await writeFile(sessionFile, `${lines.join("\n")}\n`, "utf8");

    const { stdout } = await execFileAsync(process.execPath, [
      scriptPath,
      "--dry-run",
      "--once",
      "--replay",
      "--approval-wait",
      "0",
      "--root",
      rootDir,
    ]);

    return stdout;
  } finally {
    await rm(rootDir, { recursive: true, force: true });
  }
}

function buildBaseRecords(functionName, callId) {
  return [
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.000Z",
      type: "session_meta",
      payload: { id: "sess-1" },
    }),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.100Z",
      type: "turn_context",
      payload: {
        turn_id: "turn-1",
        cwd: "/tmp/project",
      },
    }),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.200Z",
      type: "response_item",
      payload: {
        type: "function_call",
        name: functionName,
        arguments: JSON.stringify({
          function: "() => document.body.innerText",
        }),
        call_id: callId,
      },
    }),
  ];
}

test("notifies for pending chrome_devtools MCP approvals", async () => {
  const stdout = await runWatcher(
    buildBaseRecords("mcp__chrome_devtools__evaluate_script", "call-1"),
  );

  assert.match(stdout, /approval_needed/);
  assert.match(stdout, /chrome_devtools/);
  assert.match(stdout, /evaluate_script/);
});

test("notifies for pending chrome-devtools MCP approvals", async () => {
  const stdout = await runWatcher(
    buildBaseRecords("mcp__chrome-devtools__evaluate_script", "call-2"),
  );

  assert.match(stdout, /approval_needed/);
  assert.match(stdout, /chrome-devtools/);
  assert.match(stdout, /evaluate_script/);
});

test("does not notify when MCP call already completed", async () => {
  const stdout = await runWatcher([
    ...buildBaseRecords("mcp__chrome_devtools__evaluate_script", "call-3"),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.300Z",
      type: "response_item",
      payload: {
        type: "function_call_output",
        call_id: "call-3",
        output: "[]",
      },
    }),
  ]);

  assert.doesNotMatch(stdout, /approval_needed/);
});
