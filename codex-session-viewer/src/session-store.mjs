import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

const DEFAULT_SESSIONS_ROOT = path.join(os.homedir(), ".codex", "sessions");
const SUMMARY_HEAD_BYTES = 256 * 1024;
const SUMMARY_TAIL_BYTES = 256 * 1024;
const INJECTED_USER_PREFIX = "# AGENTS.md instructions for ";
const MEMORY_PREFIX = "[MEMORY LOADED]";
const RUNTIME_DEVELOPER_MARKERS = [
  "<permissions instructions>",
  "<collaboration_mode>",
  "<apps_instructions>",
  "<skills_instructions>",
  "<plugins_instructions>",
];

function extractTextFromContent(content) {
  if (!Array.isArray(content)) {
    return "";
  }

  return content
    .map((item) => {
      if (!item || typeof item !== "object") {
        return "";
      }

      if (typeof item.text === "string") {
        return item.text;
      }

      if (typeof item.content === "string") {
        return item.content;
      }

      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function shortText(text, limit = 140) {
  const compact = text.replace(/\s+/g, " ").trim();
  if (compact.length <= limit) {
    return compact;
  }
  return `${compact.slice(0, limit - 1)}…`;
}

function serializeValue(value) {
  if (typeof value === "string") {
    return value;
  }

  if (value === undefined || value === null) {
    return "";
  }

  return JSON.stringify(value, null, 2);
}

function looksLikeInjectedUserMessage(text) {
  return (
    text.startsWith(INJECTED_USER_PREFIX) || text.includes("<environment_context>")
  );
}

function classifyDeveloperText(text) {
  if (text.startsWith(MEMORY_PREFIX)) {
    return "memory";
  }

  if (RUNTIME_DEVELOPER_MARKERS.some((marker) => text.includes(marker))) {
    return "runtime";
  }

  return "developer";
}

function compactTurnContext(turnContext) {
  if (!turnContext) {
    return null;
  }

  return {
    turnId: turnContext.turn_id ?? null,
    cwd: turnContext.cwd ?? null,
    currentDate: turnContext.current_date ?? null,
    timezone: turnContext.timezone ?? null,
    model: turnContext.model ?? null,
    effort: turnContext.effort ?? null,
    personality: turnContext.personality ?? null,
    approvalPolicy: turnContext.approval_policy ?? null,
    sandboxPolicy: turnContext.sandbox_policy ?? null,
    collaborationMode: turnContext.collaboration_mode ?? null,
    realtimeActive: turnContext.realtime_active ?? false,
    userInstructions: turnContext.user_instructions ?? "",
  };
}

function formatHistoryEntry(role, text) {
  return `[${role.toUpperCase()}]\n${text.trim()}`;
}

function buildHistoryTranscript(previousTurns) {
  const chunks = [];

  previousTurns.forEach((turn) => {
    const lines = [`# Turn ${turn.index}`];

    turn.memoryMessages.forEach((message) => {
      lines.push(formatHistoryEntry("memory", message.text));
    });

    turn.humanUserMessages.forEach((message) => {
      lines.push(formatHistoryEntry("user", message.text));
    });

    turn.assistantMessages.forEach((message) => {
      lines.push(formatHistoryEntry("assistant", message.text));
    });

    turn.toolCalls.forEach((call) => {
      const callBody = `${call.name}\n${call.argumentsText}`.trim();
      lines.push(formatHistoryEntry("tool_call", callBody));
    });

    turn.toolOutputs.forEach((output) => {
      lines.push(formatHistoryEntry("tool_output", output.preview));
    });

    chunks.push(lines.join("\n\n"));
  });

  return chunks.join("\n\n");
}

async function readFileChunk(filePath, { fromStart, byteCount }) {
  const handle = await fs.open(filePath, "r");
  try {
    const stats = await handle.stat();
    const size = stats.size;
    const length = Math.min(byteCount, size);
    const buffer = Buffer.alloc(length);
    const position = fromStart ? 0 : Math.max(0, size - length);
    await handle.read(buffer, 0, length, position);

    return {
      size,
      content: buffer.toString("utf8"),
      truncatedAtStart: !fromStart && size > length,
      truncatedAtEnd: fromStart && size > length,
    };
  } finally {
    await handle.close();
  }
}

function normalizeChunkLines(chunk, { trimLeadingPartial, trimTrailingPartial }) {
  let content = chunk;

  if (trimLeadingPartial) {
    const firstNewline = content.indexOf("\n");
    content = firstNewline >= 0 ? content.slice(firstNewline + 1) : "";
  }

  if (trimTrailingPartial) {
    const lastNewline = content.lastIndexOf("\n");
    content = lastNewline >= 0 ? content.slice(0, lastNewline) : "";
  }

  return content.split("\n").filter(Boolean);
}

function tryParseJsonLine(line) {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

function findUserPrompt(records, { reverse = false } = {}) {
  const iterable = reverse ? [...records].reverse() : records;
  let fallback = "";

  for (const record of iterable) {
    if (
      record?.type !== "response_item" ||
      record.payload?.type !== "message" ||
      record.payload?.role !== "user"
    ) {
      continue;
    }

    const text = extractTextFromContent(record.payload.content);
    if (!fallback && text) {
      fallback = text;
    }

    if (text && !looksLikeInjectedUserMessage(text)) {
      return text;
    }
  }

  return fallback;
}

function buildApproxPrompt({ sessionMeta, turn, previousTurns }) {
  const parts = [
    "## Base Instructions",
    sessionMeta?.baseInstructions ?? "",
    "## Turn Context",
    serializeValue(turn.turnContextCompact),
  ];

  if (turn.turnContext?.collaboration_mode?.settings?.developer_instructions) {
    parts.push(
      "## Collaboration Developer Instructions",
      turn.turnContext.collaboration_mode.settings.developer_instructions,
    );
  }

  if (turn.runtimeDeveloperMessages.length) {
    parts.push(
      "## Runtime Developer Messages",
      turn.runtimeDeveloperMessages.map((item) => item.text).join("\n\n---\n\n"),
    );
  }

  if (turn.memoryMessages.length) {
    parts.push(
      "## Memory Messages",
      turn.memoryMessages.map((item) => item.text).join("\n\n---\n\n"),
    );
  }

  if (turn.turnContext?.user_instructions) {
    parts.push("## User Instructions", turn.turnContext.user_instructions);
  }

  if (previousTurns.length) {
    parts.push("## Prior Conversation", buildHistoryTranscript(previousTurns));
  }

  if (turn.humanUserMessages.length) {
    parts.push(
      "## Current User Messages",
      turn.humanUserMessages.map((item) => item.text).join("\n\n"),
    );
  } else if (turn.userMessages.length) {
    parts.push(
      "## Current User Messages",
      turn.userMessages.map((item) => item.text).join("\n\n"),
    );
  }

  return parts.filter(Boolean).join("\n\n");
}

function parseTurn(turnRecord, index, sessionMeta) {
  const { turnContextRecord, records } = turnRecord;
  const turnContext = turnContextRecord?.payload ?? {};
  const responseItems = records.filter((record) => record.type === "response_item");
  const eventMessages = records.filter((record) => record.type === "event_msg");

  const userMessages = [];
  const injectedUserMessages = [];
  const humanUserMessages = [];
  const developerMessages = [];
  const runtimeDeveloperMessages = [];
  const memoryMessages = [];
  const assistantMessages = [];
  const toolCalls = [];
  const toolOutputs = [];
  const rawMessages = [];

  for (const record of responseItems) {
    const payload = record.payload ?? {};
    if (payload.type === "message") {
      const text = extractTextFromContent(payload.content);
      const message = {
        timestamp: record.timestamp,
        text,
        content: payload.content ?? [],
      };

      rawMessages.push({
        role: payload.role,
        timestamp: record.timestamp,
        text,
      });

      if (payload.role === "user") {
        userMessages.push(message);
        if (looksLikeInjectedUserMessage(text)) {
          injectedUserMessages.push(message);
        } else {
          humanUserMessages.push(message);
        }
      } else if (payload.role === "developer") {
        developerMessages.push(message);
        const classification = classifyDeveloperText(text);
        if (classification === "runtime") {
          runtimeDeveloperMessages.push(message);
        } else if (classification === "memory") {
          memoryMessages.push(message);
        }
      } else if (payload.role === "assistant") {
        assistantMessages.push(message);
      }
      continue;
    }

    if (payload.type === "function_call") {
      toolCalls.push({
        timestamp: record.timestamp,
        name: payload.name ?? "unknown",
        argumentsText: payload.arguments ?? "",
        callId: payload.call_id ?? null,
      });
      continue;
    }

    if (payload.type === "function_call_output") {
      const outputText = serializeValue(payload.output);
      toolOutputs.push({
        timestamp: record.timestamp,
        callId: payload.call_id ?? null,
        outputText,
        preview: shortText(outputText, 320),
      });
    }
  }

  const reasoningCount = responseItems.filter(
    (record) => record.payload?.type === "reasoning",
  ).length;

  const tokenSnapshots = eventMessages
    .filter((record) => record.payload?.type === "token_count")
    .map((record) => {
      const usage = record.payload?.info?.last_token_usage ?? {};
      return {
        timestamp: record.timestamp,
        inputTokens: usage.input_tokens ?? 0,
        outputTokens: usage.output_tokens ?? 0,
        reasoningTokens: usage.reasoning_output_tokens ?? 0,
      };
    });

  const lastTokenSnapshot = tokenSnapshots.at(-1) ?? null;
  const promptFocus = (
    humanUserMessages.map((message) => message.text).join("\n\n") ||
    userMessages.at(-1)?.text ||
    developerMessages.at(-1)?.text ||
    ""
  ).trim();

  return {
    id: turnContext.turn_id ?? `turn-${index}`,
    index,
    startedAt: turnContextRecord?.timestamp ?? records[0]?.timestamp ?? null,
    finishedAt: records.at(-1)?.timestamp ?? turnContextRecord?.timestamp ?? null,
    promptFocus,
    promptPreview: shortText(promptFocus || "(empty turn)", 120),
    userMessages,
    humanUserMessages,
    injectedUserMessages,
    developerMessages,
    runtimeDeveloperMessages,
    memoryMessages,
    assistantMessages,
    toolCalls,
    toolOutputs,
    reasoningCount,
    tokenSnapshots,
    lastTokenSnapshot,
    rawMessages,
    records,
    turnContext,
    turnContextCompact: compactTurnContext(turnContext),
    sessionBaseInstructions: sessionMeta?.baseInstructions ?? "",
  };
}

function groupTurnRecords(records) {
  const turnContextIndexes = [];

  records.forEach((record, index) => {
    if (record.type === "turn_context") {
      turnContextIndexes.push(index);
    }
  });

  if (!turnContextIndexes.length) {
    return [
      {
        turnContextRecord: null,
        records,
      },
    ];
  }

  return turnContextIndexes.map((startIndex, index) => {
    const endIndex =
      index + 1 < turnContextIndexes.length
        ? turnContextIndexes[index + 1]
        : records.length;

    return {
      turnContextRecord: records[startIndex],
      records: records.slice(startIndex, endIndex),
    };
  });
}

function buildSessionSummary(filePath, parsedSession) {
  const { sessionMeta, turns, parseErrors } = parsedSession;
  const firstTurnWithPrompt = turns.find((turn) => turn.promptFocus);
  const lastTurnWithPrompt = [...turns].reverse().find((turn) => turn.promptFocus);
  const peakInputTokens = Math.max(
    0,
    ...turns.map((turn) => turn.lastTokenSnapshot?.inputTokens ?? 0),
  );
  const toolCallCount = turns.reduce((sum, turn) => sum + turn.toolCalls.length, 0);

  return {
    id: sessionMeta.id,
    day: (sessionMeta.timestamp ?? "").slice(0, 10),
    timestamp: sessionMeta.timestamp,
    modelProvider: sessionMeta.modelProvider,
    cliVersion: sessionMeta.cliVersion,
    cwd: sessionMeta.cwd,
    filePath,
    turnCount: turns.length,
    toolCallCount,
    parseErrorCount: parseErrors.length,
    firstPrompt: firstTurnWithPrompt?.promptPreview ?? "(no prompt found)",
    latestPrompt: lastTurnWithPrompt?.promptPreview ?? "(no prompt found)",
    peakInputTokens,
    model:
      turns.at(-1)?.turnContext?.model ??
      turns[0]?.turnContext?.model ??
      sessionMeta.model ??
      null,
  };
}

export async function findSessionFiles(rootDir = DEFAULT_SESSIONS_ROOT) {
  const files = [];

  async function walk(currentDir) {
    const entries = await fs.readdir(currentDir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(currentDir, entry.name);
      if (entry.isDirectory()) {
        await walk(fullPath);
      } else if (entry.isFile() && entry.name.endsWith(".jsonl")) {
        files.push(fullPath);
      }
    }
  }

  await walk(rootDir);
  return files.sort().reverse();
}

export async function parseSessionFile(filePath) {
  const raw = await fs.readFile(filePath, "utf8");
  const lines = raw.split("\n").filter(Boolean);
  const records = [];
  const parseErrors = [];

  lines.forEach((line, index) => {
    try {
      records.push(JSON.parse(line));
    } catch (error) {
      parseErrors.push({
        lineNumber: index + 1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });

  const metaRecord = records.find((record) => record.type === "session_meta");
  const metaPayload = metaRecord?.payload ?? {};
  const groupedTurns = groupTurnRecords(records);
  const sessionMeta = {
    id: metaPayload.id ?? path.basename(filePath, ".jsonl"),
    timestamp: metaPayload.timestamp ?? metaRecord?.timestamp ?? null,
    cwd: metaPayload.cwd ?? null,
    source: metaPayload.source ?? null,
    originator: metaPayload.originator ?? null,
    cliVersion: metaPayload.cli_version ?? null,
    modelProvider: metaPayload.model_provider ?? null,
    model: metaPayload.model ?? null,
    baseInstructions: metaPayload.base_instructions?.text ?? "",
  };

  const turns = groupedTurns.map((group, index) =>
    parseTurn(group, index + 1, sessionMeta),
  );

  turns.forEach((turn, index) => {
    turn.historyTranscript = buildHistoryTranscript(turns.slice(0, index));
    turn.approxPrompt = buildApproxPrompt({
      sessionMeta,
      turn,
      previousTurns: turns.slice(0, index),
    });
  });

  return {
    sessionMeta,
    parseErrors,
    filePath,
    lineCount: lines.length,
    recordCount: records.length,
    turns,
    rawRecords: records,
  };
}

const summaryCache = new Map();

async function summarizeSessionFile(filePath) {
  const [headChunk, tailChunk] = await Promise.all([
    readFileChunk(filePath, { fromStart: true, byteCount: SUMMARY_HEAD_BYTES }),
    readFileChunk(filePath, { fromStart: false, byteCount: SUMMARY_TAIL_BYTES }),
  ]);

  const headLines = normalizeChunkLines(headChunk.content, {
    trimLeadingPartial: false,
    trimTrailingPartial: headChunk.truncatedAtEnd,
  });
  const tailLines = normalizeChunkLines(tailChunk.content, {
    trimLeadingPartial: tailChunk.truncatedAtStart,
    trimTrailingPartial: false,
  });

  const headRecords = headLines.map(tryParseJsonLine).filter(Boolean);
  const tailRecords = tailLines.map(tryParseJsonLine).filter(Boolean);
  const metaRecord = headRecords.find((record) => record.type === "session_meta");
  const metaPayload = metaRecord?.payload ?? {};
  const latestTurnContext = [...tailRecords]
    .reverse()
    .find((record) => record.type === "turn_context");

  const firstPromptText = findUserPrompt(headRecords);
  const latestPromptText = findUserPrompt(tailRecords, { reverse: true });

  return {
    id: metaPayload.id ?? path.basename(filePath, ".jsonl"),
    day:
      (metaPayload.timestamp ?? metaRecord?.timestamp ?? "").slice(0, 10) ||
      path.basename(path.dirname(path.dirname(filePath))),
    timestamp: metaPayload.timestamp ?? metaRecord?.timestamp ?? null,
    modelProvider: metaPayload.model_provider ?? null,
    cliVersion: metaPayload.cli_version ?? null,
    cwd: metaPayload.cwd ?? null,
    filePath,
    turnCount: null,
    toolCallCount: null,
    parseErrorCount: 0,
    firstPrompt: shortText(firstPromptText || "(no prompt found)", 120),
    latestPrompt: shortText(latestPromptText || firstPromptText || "(no prompt found)", 120),
    peakInputTokens: null,
    model:
      latestTurnContext?.payload?.model ??
      metaPayload.model ??
      null,
  };
}

export async function loadSessionSummaries(rootDir = DEFAULT_SESSIONS_ROOT) {
  const files = await findSessionFiles(rootDir);
  const summaries = [];

  for (const filePath of files) {
    try {
      const stats = await fs.stat(filePath);
      const cached = summaryCache.get(filePath);
      if (
        cached &&
        cached.mtimeMs === stats.mtimeMs &&
        cached.size === stats.size
      ) {
        summaries.push(cached.summary);
        continue;
      }

      const summary = await summarizeSessionFile(filePath);
      summaryCache.set(filePath, {
        mtimeMs: stats.mtimeMs,
        size: stats.size,
        summary,
      });
      summaries.push(summary);
    } catch (error) {
      summaries.push({
        id: path.basename(filePath, ".jsonl"),
        day: "unknown",
        timestamp: null,
        cwd: null,
        filePath,
        turnCount: null,
        toolCallCount: null,
        parseErrorCount: 1,
        firstPrompt: "(failed to parse session)",
        latestPrompt: "(failed to parse session)",
        peakInputTokens: null,
        modelProvider: null,
        cliVersion: null,
        model: null,
        loadError: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return summaries;
}

export function groupSummariesByDay(summaries) {
  const groups = new Map();

  summaries.forEach((summary) => {
    const day = summary.day || "unknown";
    if (!groups.has(day)) {
      groups.set(day, []);
    }
    groups.get(day).push(summary);
  });

  return [...groups.entries()]
    .sort(([left], [right]) => right.localeCompare(left))
    .map(([day, items]) => ({
      day,
      sessions: items.sort((left, right) =>
        (right.timestamp ?? "").localeCompare(left.timestamp ?? ""),
      ),
    }));
}

export function getDefaultSessionsRoot() {
  return DEFAULT_SESSIONS_ROOT;
}
