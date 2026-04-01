#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import { homedir } from "node:os";

function printUsage() {
  console.log(`Usage: codex-webhook-watch.mjs --url <webhook-url> [options]

Options:
  --url <url>           Webhook URL. Can also use CODEX_WEBHOOK_URL.
  --root <dir>          Session root. Defaults to ~/.codex/sessions.
  --events <list>       Comma-separated: task_complete,approval_needed
  --at-mobiles <list>   Comma-separated mobile numbers for DingTalk @.
  --at-user-ids <list>  Comma-separated DingTalk user IDs for @.
  --at-all              Mention everyone in DingTalk.
  --approval-mcp-servers <list>
                        Comma-separated MCP server names whose pending tool calls
                        should be treated as approval waits. Default:
                        chrome-devtools,chrome_devtools.
  --approval-wait <ms>  Wait before flagging a pending manual approval. Default: 2500.
  --interval <ms>       Poll interval in milliseconds. Default: 1500.
  --replay              Read existing session content from the beginning.
  --dry-run             Print events instead of POSTing them.
  --once                Scan once and exit.
  -h, --help            Show this help.
`);
}

function parseArgs(argv) {
  const options = {
    url: process.env.CODEX_WEBHOOK_URL || "",
    root: path.join(homedir(), ".codex", "sessions"),
    events: new Set(["task_complete", "approval_needed"]),
    atMobiles: splitList(process.env.CODEX_DINGTALK_AT_MOBILES || ""),
    atUserIds: splitList(process.env.CODEX_DINGTALK_AT_USER_IDS || ""),
    atAll: process.env.CODEX_DINGTALK_AT_ALL === "true",
    approvalMcpServers: new Set(
      splitList(
        process.env.CODEX_APPROVAL_MCP_SERVERS ||
          "chrome-devtools,chrome_devtools",
      ),
    ),
    approvalWaitMs: 2500,
    intervalMs: 1500,
    replay: false,
    dryRun: false,
    once: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--url") {
      options.url = argv[++i] || "";
      continue;
    }
    if (arg === "--root") {
      options.root = argv[++i] || options.root;
      continue;
    }
    if (arg === "--events") {
      const raw = argv[++i] || "";
      const nextEvents = raw
        .split(",")
        .map(item => item.trim())
        .filter(Boolean);
      options.events = new Set(nextEvents);
      continue;
    }
    if (arg === "--at-mobiles") {
      options.atMobiles = splitList(argv[++i] || "");
      continue;
    }
    if (arg === "--at-user-ids") {
      options.atUserIds = splitList(argv[++i] || "");
      continue;
    }
    if (arg === "--at-all") {
      options.atAll = true;
      continue;
    }
    if (arg === "--approval-mcp-servers") {
      options.approvalMcpServers = new Set(splitList(argv[++i] || ""));
      continue;
    }
    if (arg === "--approval-wait") {
      options.approvalWaitMs = Number(argv[++i] || options.approvalWaitMs);
      continue;
    }
    if (arg === "--interval") {
      options.intervalMs = Number(argv[++i] || options.intervalMs);
      continue;
    }
    if (arg === "--replay") {
      options.replay = true;
      continue;
    }
    if (arg === "--dry-run") {
      options.dryRun = true;
      continue;
    }
    if (arg === "--once") {
      options.once = true;
      continue;
    }
    if (arg === "-h" || arg === "--help") {
      printUsage();
      process.exit(0);
    }
    throw new Error(`Unknown argument: ${arg}`);
  }

  if (!options.dryRun && !options.url) {
    throw new Error("Missing webhook URL. Pass --url or set CODEX_WEBHOOK_URL.");
  }

  return options;
}

function splitList(raw) {
  return raw
    .split(",")
    .map(item => item.trim())
    .filter(Boolean);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function listJsonlFiles(rootDir) {
  const output = [];
  const queue = [rootDir];

  while (queue.length > 0) {
    const current = queue.pop();
    let entries;
    try {
      entries = await fs.readdir(current, { withFileTypes: true });
    } catch (error) {
      if (error && error.code === "ENOENT") {
        continue;
      }
      throw error;
    }

    for (const entry of entries) {
      const fullPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        queue.push(fullPath);
        continue;
      }
      if (entry.isFile() && entry.name.endsWith(".jsonl")) {
        output.push(fullPath);
      }
    }
  }

  return output.sort();
}

function safeJsonParse(value) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function parseSessionIdFromPath(filePath) {
  const base = path.basename(filePath, ".jsonl");
  const marker = base.lastIndexOf("-");
  if (marker === -1) {
    return null;
  }
  return base.slice(marker + 1);
}

function buildTaskCompleteEvent(record, state, filePath) {
  return {
    event: "task_complete",
    timestamp: record.timestamp,
    session_id: state.sessionId,
    turn_id: record.payload.turn_id || state.currentTurnId || null,
    last_agent_message: record.payload.last_agent_message || "",
    file: filePath,
  };
}

function extractApprovedPrefixRules(record) {
  if (record?.type !== "response_item" || record.payload?.type !== "message" || record.payload?.role !== "developer") {
    return null;
  }

  const content = Array.isArray(record.payload.content) ? record.payload.content : [];
  const collected = [];

  for (const item of content) {
    if (item?.type !== "input_text" || typeof item.text !== "string") {
      continue;
    }

    const match = item.text.match(
      /The following prefix rules have already been approved:\s*([\s\S]*?)(?:\n\s*The writable roots are|\n<\/permissions instructions>)/,
    );
    if (!match) {
      continue;
    }

    const lines = match[1].split("\n");
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("- [")) {
        continue;
      }
      const arrayStart = trimmed.indexOf("[");
      const jsonText = trimmed.slice(arrayStart);
      const parsed = safeJsonParse(jsonText);
      if (Array.isArray(parsed) && parsed.every(part => typeof part === "string")) {
        collected.push(parsed);
      }
    }
  }

  return collected.length > 0 ? collected : null;
}

function arrayStartsWith(source, prefix) {
  if (!Array.isArray(source) || !Array.isArray(prefix) || prefix.length > source.length) {
    return false;
  }

  for (let i = 0; i < prefix.length; i += 1) {
    if (source[i] !== prefix[i]) {
      return false;
    }
  }
  return true;
}

function isAlreadyApproved(prefixRule, command, approvedPrefixRules) {
  if (!Array.isArray(approvedPrefixRules) || approvedPrefixRules.length === 0) {
    return false;
  }

  for (const approved of approvedPrefixRules) {
    if (arrayStartsWith(prefixRule, approved)) {
      return true;
    }
    const approvedText = approved.join(" ");
    if (approvedText && typeof command === "string" && command.startsWith(approvedText)) {
      return true;
    }
  }

  return false;
}

function parseMcpToolName(toolName) {
  if (typeof toolName !== "string" || !toolName.startsWith("mcp__")) {
    return null;
  }

  const parts = toolName.split("__");
  if (parts.length < 3 || parts[0] !== "mcp") {
    return null;
  }

  const [, server, ...toolParts] = parts;
  if (!server || toolParts.length === 0) {
    return null;
  }

  return {
    server,
    tool: toolParts.join("__"),
  };
}

function stringifyMcpArgumentsPreview(rawArguments) {
  const parsed = safeJsonParse(rawArguments);
  const value = parsed ?? rawArguments;

  try {
    return truncateText(JSON.stringify(value), 1200);
  } catch {
    return truncateText(String(value ?? ""), 1200);
  }
}

function rememberApprovalCall(record, state, filePath, options) {
  const toolName = record?.payload?.name;
  if (typeof toolName !== "string") {
    return null;
  }

  const args = safeJsonParse(record.payload.arguments);
  if (args && args.sandbox_permissions === "require_escalated") {
    state.pendingApprovals.set(record.payload.call_id, {
      approval_kind: "sandbox_command",
      call_id: record.payload.call_id,
      timestamp: record.timestamp,
      createdAtMs: Date.parse(record.timestamp),
      turn_id: state.currentTurnId || null,
      tool_name: toolName,
      command: args.cmd || null,
      workdir: args.workdir || state.currentCwd || null,
      justification: args.justification || null,
      prefix_rule: Array.isArray(args.prefix_rule) ? args.prefix_rule : null,
      alreadyApproved: isAlreadyApproved(
        Array.isArray(args.prefix_rule) ? args.prefix_rule : [],
        args.cmd || "",
        state.approvedPrefixRules,
      ),
      guardianSeen: false,
      notified: false,
      file: filePath,
      mcp_server: null,
      mcp_tool: null,
      mcp_arguments_preview: null,
    });
    return null;
  }

  const mcpTool = parseMcpToolName(toolName);
  if (
    !mcpTool ||
    !options.approvalMcpServers.has(mcpTool.server)
  ) {
    return null;
  }

  state.pendingApprovals.set(record.payload.call_id, {
    approval_kind: "mcp_tool",
    call_id: record.payload.call_id,
    timestamp: record.timestamp,
    createdAtMs: Date.parse(record.timestamp),
    turn_id: state.currentTurnId || null,
    tool_name: toolName,
    command: null,
    workdir: state.currentCwd || null,
    justification: null,
    prefix_rule: null,
    alreadyApproved: false,
    guardianSeen: false,
    notified: false,
    file: filePath,
    mcp_server: mcpTool.server,
    mcp_tool: mcpTool.tool,
    mcp_arguments_preview: stringifyMcpArgumentsPreview(record.payload.arguments),
  });
  return null;
}

function buildApprovalEvent(pending, state) {
  return {
    event: "approval_needed",
    timestamp: pending.timestamp,
    session_id: state.sessionId,
    turn_id: pending.turn_id || state.currentTurnId || null,
    call_id: pending.call_id,
    tool_name: pending.tool_name,
    command: pending.command || null,
    workdir: pending.workdir || null,
    justification: pending.justification,
    prefix_rule: pending.prefix_rule,
    file: pending.file,
    approval_kind: pending.approval_kind || "sandbox_command",
    mcp_server: pending.mcp_server || null,
    mcp_tool: pending.mcp_tool || null,
    mcp_arguments_preview: pending.mcp_arguments_preview || null,
  };
}

function cleanupApprovalState(record, state) {
  if (record?.type === "response_item" && record.payload?.type === "function_call_output") {
    state.pendingApprovals.delete(record.payload.call_id);
    return;
  }

  if (record?.type === "event_msg" && record.payload?.type === "exec_command_end") {
    state.pendingApprovals.delete(record.payload.call_id);
    return;
  }

  if (record?.type === "event_msg" && record.payload?.type === "guardian_assessment") {
    const pending = state.pendingApprovals.get(record.payload.id);
    if (pending) {
      pending.guardianSeen = true;
      pending.turn_id = record.payload.turn_id || pending.turn_id;
      pending.workdir = pending.workdir || record.payload.action?.cwd || null;
    }
    if (record.payload?.status && record.payload.status !== "in_progress") {
      state.pendingApprovals.delete(record.payload.id);
    }
  }
}

async function postWebhook(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`Webhook POST failed: ${response.status} ${response.statusText} ${body}`.trim());
  }
}

function logEvent(payload, dryRun) {
  const prefix = dryRun ? "DRY_RUN" : "POST";
  console.log(`${new Date().toISOString()} ${prefix} ${payload.event} session=${payload.session_id ?? "unknown"} turn=${payload.turn_id ?? "unknown"}`);
}

function truncateText(text, limit) {
  if (!text || text.length <= limit) {
    return text || "";
  }
  return `${text.slice(0, limit - 1)}…`;
}

function quoteMarkdown(text) {
  const normalized = (text || "(empty)")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .split("\n")
    .map(line => `> ${line || " "}`)
    .join("\n");
  return normalized;
}

function buildMentionLine(options) {
  if (options.atAll) {
    return "@all";
  }

  const mentions = [
    ...options.atMobiles.map(item => `@${item}`),
    ...options.atUserIds.map(item => `@${item}`),
  ];

  return mentions.join(" ");
}

function buildDingTalkBody(payload, options) {
  const mentionLine = buildMentionLine(options);
  const commonLines = [
    "codex",
    mentionLine,
    `- 事件: \`${payload.event}\``,
    `- Session: \`${payload.session_id || "unknown"}\``,
    `- Turn: \`${payload.turn_id || "unknown"}\``,
    `- 事件时间: \`${payload.timestamp || "unknown"}\``,
    `- 观测文件: \`${payload.file}\``,
  ].filter(Boolean);

  let title = "Codex 通知";
  let detailLines = [];

  if (payload.event === "task_complete") {
    title = "Codex 任务完成";
    detailLines = [
      "#### 最终回复",
      quoteMarkdown(truncateText(payload.last_agent_message, 3500)),
    ];
  } else if (payload.event === "approval_needed") {
    title = "Codex 需要审批";
    if (payload.approval_kind === "mcp_tool") {
      detailLines = [
        `- 工具: \`${payload.tool_name || "unknown"}\``,
        `- MCP Server: \`${payload.mcp_server || "unknown"}\``,
        `- MCP Tool: \`${payload.mcp_tool || "unknown"}\``,
        `- 工作目录: \`${payload.workdir || "unknown"}\``,
        `- 参数: \`${payload.mcp_arguments_preview || "unknown"}\``,
      ];
    } else {
      detailLines = [
        `- 工具: \`${payload.tool_name || "unknown"}\``,
        `- 工作目录: \`${payload.workdir || "unknown"}\``,
        `- 审批原因: ${payload.justification || "unknown"}`,
        `- 命令: \`${truncateText(payload.command || "unknown", 1200)}\``,
      ];
    }

    if (Array.isArray(payload.prefix_rule) && payload.prefix_rule.length > 0) {
      detailLines.push(`- Prefix Rule: \`${payload.prefix_rule.join(" ")}\``);
    }
  }

  const markdownText = [
    `#### ${title}`,
    ...commonLines,
    ...detailLines,
  ].join("\n");

  return {
    msgtype: "markdown",
    markdown: {
      title,
      text: markdownText,
    },
    at: {
      atMobiles: options.atMobiles,
      atUserIds: options.atUserIds,
      isAtAll: options.atAll,
    },
  };
}

async function emitPayload(payload, state, options) {
  if (!payload) {
    return;
  }

  const eventKey = [
    payload.event,
    payload.session_id || "unknown",
    payload.turn_id || "unknown",
    payload.timestamp || "unknown",
    payload.call_id || "",
    payload.command || "",
  ].join("|");

  if (state.emitted.has(eventKey)) {
    return;
  }
  state.emitted.add(eventKey);

  logEvent(payload, options.dryRun);
  const dingTalkBody = buildDingTalkBody(payload, options);

  if (options.dryRun) {
    console.log(JSON.stringify(dingTalkBody, null, 2));
    return;
  }

  await postWebhook(options.url, dingTalkBody);
}

async function maybeEmitPendingApprovals(state, options) {
  if (!options.events.has("approval_needed")) {
    return;
  }

  const nowMs = Date.now();
  for (const pending of state.pendingApprovals.values()) {
    if (pending.notified || pending.guardianSeen || pending.alreadyApproved) {
      continue;
    }
    if (!Number.isFinite(pending.createdAtMs)) {
      continue;
    }
    if (nowMs - pending.createdAtMs < options.approvalWaitMs) {
      continue;
    }

    pending.notified = true;
    await emitPayload(buildApprovalEvent(pending, state), state, options);
  }
}

async function handleRecord(record, state, options, filePath) {
  const approvedPrefixRules = extractApprovedPrefixRules(record);
  if (approvedPrefixRules) {
    state.approvedPrefixRules = approvedPrefixRules;
  }

  cleanupApprovalState(record, state);

  if (record?.type === "session_meta" && record.payload?.id) {
    state.sessionId = record.payload.id;
  }

  if (record?.type === "event_msg" && record.payload?.type === "task_started") {
    state.currentTurnId = record.payload.turn_id || state.currentTurnId;
  }

  if (record?.type === "turn_context" && record.payload?.turn_id) {
    state.currentTurnId = record.payload.turn_id;
    state.currentCwd = record.payload.cwd || state.currentCwd || null;
  }

  let payload = null;

  if (record?.type === "event_msg" && record.payload?.type === "task_complete") {
    if (options.events.has("task_complete")) {
      payload = buildTaskCompleteEvent(record, state, filePath);
    }
  } else if (record?.type === "response_item" && record.payload?.type === "function_call") {
    if (options.events.has("approval_needed")) {
      payload = rememberApprovalCall(record, state, filePath, options);
    }
  }

  await emitPayload(payload, state, options);
}

async function readDelta(filePath, start, end) {
  const handle = await fs.open(filePath, "r");
  try {
    const size = end - start;
    const buffer = Buffer.alloc(size);
    await handle.read(buffer, 0, size, start);
    return buffer.toString("utf8");
  } finally {
    await handle.close();
  }
}

async function processFile(filePath, state, options) {
  let stats;
  try {
    stats = await fs.stat(filePath);
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return;
    }
    throw error;
  }

  if (!state.initialized) {
    state.offset = options.replay ? 0 : stats.size;
    state.initialized = true;
  }

  if (stats.size < state.offset) {
    state.offset = 0;
    state.remainder = "";
  }

  if (stats.size === state.offset) {
    await maybeEmitPendingApprovals(state, options);
    return;
  }

  const chunk = await readDelta(filePath, state.offset, stats.size);
  state.offset = stats.size;

  const text = state.remainder + chunk;
  const lines = text.split("\n");
  state.remainder = text.endsWith("\n") ? "" : lines.pop() || "";

  for (const line of lines) {
    if (!line.trim()) {
      continue;
    }
    const record = safeJsonParse(line);
    if (!record) {
      continue;
    }
    await handleRecord(record, state, options, filePath);
  }

  await maybeEmitPendingApprovals(state, options);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const states = new Map();

  console.error(
    `Watching ${options.root} for ${Array.from(options.events).join(", ")} ` +
      `(interval=${options.intervalMs}ms, replay=${options.replay}, dryRun=${options.dryRun})`,
  );

  while (true) {
    const files = await listJsonlFiles(options.root);

    for (const filePath of files) {
      if (!states.has(filePath)) {
        states.set(filePath, {
          sessionId: parseSessionIdFromPath(filePath),
          currentTurnId: null,
          currentCwd: null,
          approvedPrefixRules: [],
          pendingApprovals: new Map(),
          offset: 0,
          remainder: "",
          initialized: false,
          emitted: new Set(),
        });
      }

      const state = states.get(filePath);
      await processFile(filePath, state, options);
    }

    if (options.once) {
      return;
    }
    await sleep(options.intervalMs);
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
