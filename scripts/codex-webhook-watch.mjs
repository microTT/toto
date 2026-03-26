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

function buildApprovalEvent(record, state, filePath) {
  const args = safeJsonParse(record.payload.arguments);
  if (!args || args.sandbox_permissions !== "require_escalated") {
    return null;
  }

  return {
    event: "approval_needed",
    timestamp: record.timestamp,
    session_id: state.sessionId,
    turn_id: state.currentTurnId || null,
    tool_name: record.payload.name,
    command: args.cmd || null,
    workdir: args.workdir || null,
    justification: args.justification || null,
    prefix_rule: Array.isArray(args.prefix_rule) ? args.prefix_rule : null,
    file: filePath,
  };
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
    detailLines = [
      `- 工具: \`${payload.tool_name || "unknown"}\``,
      `- 工作目录: \`${payload.workdir || "unknown"}\``,
      `- 审批原因: ${payload.justification || "unknown"}`,
      `- 命令: \`${truncateText(payload.command || "unknown", 1200)}\``,
    ];

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

async function handleRecord(record, state, options, filePath) {
  if (record?.type === "session_meta" && record.payload?.id) {
    state.sessionId = record.payload.id;
  }

  if (record?.type === "event_msg" && record.payload?.type === "task_started") {
    state.currentTurnId = record.payload.turn_id || state.currentTurnId;
  }

  if (record?.type === "turn_context" && record.payload?.turn_id) {
    state.currentTurnId = record.payload.turn_id;
  }

  let payload = null;

  if (record?.type === "event_msg" && record.payload?.type === "task_complete") {
    if (options.events.has("task_complete")) {
      payload = buildTaskCompleteEvent(record, state, filePath);
    }
  } else if (record?.type === "response_item" && record.payload?.type === "function_call") {
    if (options.events.has("approval_needed")) {
      payload = buildApprovalEvent(record, state, filePath);
    }
  }

  if (!payload) {
    return;
  }

  const eventKey = [
    payload.event,
    payload.session_id || "unknown",
    payload.turn_id || "unknown",
    payload.timestamp || "unknown",
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
