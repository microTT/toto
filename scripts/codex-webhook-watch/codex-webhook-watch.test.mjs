import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { appendFile, mkdtemp, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const scriptPath = fileURLToPath(
  new URL("./codex-webhook-watch.mjs", import.meta.url),
);

async function runWatcher(lines, extraArgs = []) {
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
      ...extraArgs,
    ]);

    return stdout;
  } finally {
    await rm(rootDir, { recursive: true, force: true });
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function runWatcherWithAppend(initialLines, appendedLines, extraArgs = []) {
  const rootDir = await mkdtemp(path.join(os.tmpdir(), "codex-watch-"));

  try {
    const sessionFile = path.join(rootDir, "session.jsonl");
    await writeFile(sessionFile, `${initialLines.join("\n")}\n`, "utf8");

    const child = spawn(process.execPath, [
      scriptPath,
      "--dry-run",
      "--interval",
      "50",
      "--approval-wait",
      "0",
      "--root",
      rootDir,
      ...extraArgs,
    ]);

    let stdout = "";
    let stderr = "";
    let readyResolve;
    let readyReject;
    const ready = new Promise((resolve, reject) => {
      readyResolve = resolve;
      readyReject = reject;
    });
    const readyTimer = setTimeout(() => {
      readyReject(new Error("watcher did not become ready in time"));
    }, 2000);

    child.stdout.on("data", chunk => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", chunk => {
      stderr += chunk.toString("utf8");
      if (stderr.includes("Watching ")) {
        clearTimeout(readyTimer);
        readyResolve();
      }
    });
    child.once("exit", (code, signal) => {
      clearTimeout(readyTimer);
      readyReject(new Error(`watcher exited before ready: code=${code} signal=${signal}`));
    });

    try {
      await ready;
      await sleep(150);
      await appendFile(sessionFile, `${appendedLines.join("\n")}\n`, "utf8");
      await sleep(400);
    } finally {
      child.kill("SIGTERM");
      await new Promise(resolve => child.once("exit", resolve));
    }

    return { stdout, stderr };
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

function buildTaskCompleteRecords() {
  return [
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.000Z",
      type: "session_meta",
      payload: { id: "sess-1" },
    }),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.100Z",
      type: "event_msg",
      payload: {
        type: "task_complete",
        turn_id: "turn-1",
        last_agent_message: "final answer",
      },
    }),
  ];
}

function buildSessionMetaRecord(extraPayload = {}) {
  return JSON.stringify({
    timestamp: "2026-03-31T00:00:00.000Z",
    type: "session_meta",
    payload: {
      id: "sess-1",
      ...extraPayload,
    },
  });
}

test("does not notify for pending chrome_devtools MCP approvals by default", async () => {
  const stdout = await runWatcher(
    buildBaseRecords("mcp__chrome_devtools__evaluate_script", "call-1"),
  );

  assert.doesNotMatch(stdout, /approval_needed/);
});

test("can opt in to pending chrome_devtools MCP approvals", async () => {
  const stdout = await runWatcher(
    buildBaseRecords("mcp__chrome_devtools__evaluate_script", "call-2"),
    ["--approval-mcp-servers", "chrome_devtools"],
  );

  assert.match(stdout, /approval_needed/);
  assert.match(stdout, /chrome_devtools/);
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

test("notifies for pending sandbox approvals", async () => {
  const stdout = await runWatcher([
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
        name: "exec_command",
        arguments: JSON.stringify({
          cmd: "npm install",
          sandbox_permissions: "require_escalated",
          justification: "Need network access",
        }),
        call_id: "call-4",
      },
    }),
  ]);

  assert.match(stdout, /approval_needed/);
  assert.match(stdout, /npm install/);
});

test("does not notify sandbox approvals already covered by approved prefix rules", async () => {
  const stdout = await runWatcher([
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.000Z",
      type: "session_meta",
      payload: { id: "sess-1" },
    }),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.050Z",
      type: "response_item",
      payload: {
        type: "message",
        role: "developer",
        content: [
          {
            type: "input_text",
            text:
              "<permissions instructions>\n" +
              "The following prefix rules have already been approved:\n" +
              '- ["npm", "install"]\n' +
              "The writable roots are /tmp\n" +
              "</permissions instructions>",
          },
        ],
      },
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
        name: "exec_command",
        arguments: JSON.stringify({
          cmd: "npm install",
          sandbox_permissions: "require_escalated",
          prefix_rule: ["npm", "install"],
          justification: "Need network access",
        }),
        call_id: "call-5",
      },
    }),
  ]);

  assert.doesNotMatch(stdout, /approval_needed/);
});

test("does not notify sandbox approvals that enter guardian assessment", async () => {
  const stdout = await runWatcher([
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
        name: "exec_command",
        arguments: JSON.stringify({
          cmd: "npm install",
          sandbox_permissions: "require_escalated",
          justification: "Need network access",
        }),
        call_id: "call-6",
      },
    }),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.250Z",
      type: "event_msg",
      payload: {
        type: "guardian_assessment",
        id: "call-6",
        status: "in_progress",
        turn_id: "turn-1",
      },
    }),
  ]);

  assert.doesNotMatch(stdout, /approval_needed/);
});

test("notifies for task completion after quiet period", async () => {
  const stdout = await runWatcher(buildTaskCompleteRecords(), [
    "--task-complete-wait",
    "0",
  ]);

  assert.match(stdout, /task_complete/);
  assert.match(stdout, /Codex 任务完成/);
  assert.match(stdout, /final answer/);
});

test("suppresses task completion when a new user message arrives", async () => {
  const stdout = await runWatcher([
    ...buildTaskCompleteRecords(),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.200Z",
      type: "event_msg",
      payload: {
        type: "user_message",
        message: "follow up",
        images: [],
        local_images: [],
        text_elements: [],
      },
    }),
  ]);

  assert.doesNotMatch(stdout, /task_complete/);
});

test("suppresses task completion when the next task already started", async () => {
  const stdout = await runWatcher([
    ...buildTaskCompleteRecords(),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.200Z",
      type: "event_msg",
      payload: {
        type: "task_started",
        turn_id: "turn-2",
      },
    }),
  ]);

  assert.doesNotMatch(stdout, /task_complete/);
});

test("does not notify for guardian subagent task completion", async () => {
  const stdout = await runWatcher([
    buildSessionMetaRecord({
      source: {
        subagent: {
          other: "guardian",
        },
      },
    }),
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.100Z",
      type: "event_msg",
      payload: {
        type: "task_complete",
        turn_id: "turn-1",
        last_agent_message: "{\"risk_level\":\"low\"}",
      },
    }),
  ], [
    "--task-complete-wait",
    "0",
  ]);

  assert.doesNotMatch(stdout, /task_complete/);
});

test("keeps ignoring subagent sessions when attached after file creation", async () => {
  const { stdout } = await runWatcherWithAppend([
    buildSessionMetaRecord({
      source: {
        subagent: {
          other: "guardian",
        },
      },
    }),
  ], [
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.100Z",
      type: "event_msg",
      payload: {
        type: "task_complete",
        turn_id: "turn-1",
        last_agent_message: "{\"risk_level\":\"low\"}",
      },
    }),
  ], [
    "--task-complete-wait",
    "0",
  ]);

  assert.doesNotMatch(stdout, /task_complete/);
});

test("still notifies for main session task completion after initial attach", async () => {
  const { stdout } = await runWatcherWithAppend([
    buildSessionMetaRecord(),
  ], [
    JSON.stringify({
      timestamp: "2026-03-31T00:00:00.100Z",
      type: "event_msg",
      payload: {
        type: "task_complete",
        turn_id: "turn-1",
        last_agent_message: "final answer",
      },
    }),
  ], [
    "--task-complete-wait",
    "0",
  ]);

  assert.match(stdout, /task_complete/);
  assert.match(stdout, /final answer/);
});
