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

function tryParseJsonString(value) {
  if (typeof value !== "string") {
    return null;
  }

  const trimmed = value.trim();
  if (!trimmed || (!trimmed.startsWith("{") && !trimmed.startsWith("["))) {
    return null;
  }

  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
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

function normalizeSessionSource(source) {
  if (typeof source === "string" && source) {
    return {
      raw: source,
      kind: source,
      label: source === "cli" ? "CLI" : source,
    };
  }

  const subagent = source?.subagent?.other ?? source?.subagent?.name ?? null;
  if (subagent) {
    if (subagent === "guardian") {
      return {
        raw: source,
        kind: "guardian",
        label: "Guardian",
      };
    }

    return {
      raw: source,
      kind: "subagent",
      label: `Subagent · ${subagent}`,
    };
  }

  return {
    raw: source ?? null,
    kind: "unknown",
    label: "Unknown",
  };
}

function compactCommitHash(value) {
  if (!value) {
    return null;
  }

  return String(value).slice(0, 7);
}

function parseRepositoryLabel(repositoryUrl) {
  if (!repositoryUrl) {
    return null;
  }

  const match = String(repositoryUrl).match(/[:/]([^/:]+\/[^/]+?)(?:\.git)?$/);
  return match?.[1] ?? repositoryUrl;
}

function normalizeGitInfo(git) {
  const repositoryUrl = git?.repository_url ?? null;
  const commitHash = git?.commit_hash ?? null;

  return {
    repositoryUrl,
    repositoryLabel: parseRepositoryLabel(repositoryUrl),
    branch: git?.branch ?? null,
    commitHash,
    commitShort: compactCommitHash(commitHash),
  };
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
    collaborationMode:
      turnContext.collaboration_mode?.mode ??
      turnContext.collaboration_mode ??
      null,
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

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeSkillPath(value) {
  if (!value) {
    return "";
  }

  if (value.startsWith("~/")) {
    return path.join(os.homedir(), value.slice(2));
  }

  return value;
}

function getSkillPathCandidates(skillPath) {
  const normalized = normalizeSkillPath(skillPath);
  const candidates = new Set([skillPath, normalized].filter(Boolean));

  if (normalized.startsWith(os.homedir())) {
    candidates.add(`~/${path.relative(os.homedir(), normalized)}`);
  }

  return [...candidates];
}

function parseSkillCatalogFromText(text) {
  const skills = [];

  text.split("\n").forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed.startsWith("- ")) {
      return;
    }

    const fileMarkerIndex = trimmed.lastIndexOf(" (file: ");
    if (fileMarkerIndex < 0 || !trimmed.endsWith(")")) {
      return;
    }

    const header = trimmed.slice(2, fileMarkerIndex).trim();
    const filePath = trimmed.slice(fileMarkerIndex + 8, -1).trim();
    if (!filePath.endsWith("/SKILL.md")) {
      return;
    }

    const separatorIndex = header.indexOf(": ");
    if (separatorIndex < 0) {
      return;
    }

    const name = header.slice(0, separatorIndex).trim();
    const description = header.slice(separatorIndex + 2).trim();
    if (!name) {
      return;
    }

    skills.push({
      name,
      description,
      path: normalizeSkillPath(filePath),
    });
  });

  return skills;
}

function dedupeSkills(skills) {
  const deduped = new Map();

  skills.forEach((skill) => {
    const key = skill.path || skill.name;
    if (!key || deduped.has(key)) {
      return;
    }

    deduped.set(key, {
      name: skill.name,
      description: skill.description ?? "",
      path: normalizeSkillPath(skill.path),
    });
  });

  return [...deduped.values()];
}

function collectSkillCatalog(messages) {
  return dedupeSkills(
    messages.flatMap((message) => parseSkillCatalogFromText(message.text)),
  );
}

function extractToolTraceEntries(records) {
  return records
    .flatMap((record) => {
      if (record.type !== "response_item") {
        return [];
      }

      const payload = record.payload ?? {};

      if (payload.type === "function_call") {
        return [
          {
            timestamp: record.timestamp,
            kind: "function_call",
            toolName: payload.name ?? "unknown",
            callId: payload.call_id ?? null,
            inputText: payload.arguments ?? "",
          },
        ];
      }

      if (payload.type === "custom_tool_call") {
        return [
          {
            timestamp: record.timestamp,
            kind: "custom_tool_call",
            toolName: payload.name ?? "custom_tool",
            callId: payload.call_id ?? null,
            inputText: payload.input ?? "",
          },
        ];
      }

      return [];
    })
    .filter((entry) => entry.inputText);
}

function extractObservedSkillPaths(toolEntries) {
  const observed = [];
  const pattern = /(?:~\/|\/)[^\s"'`]+\/SKILL\.md/g;

  toolEntries.forEach((entry) => {
    for (const match of entry.inputText.matchAll(pattern)) {
      const rawPath = match[0];
      const normalizedPath = normalizeSkillPath(rawPath);
      observed.push({
        name: path.basename(path.dirname(normalizedPath)),
        description: "",
        path: normalizedPath,
      });
    }
  });

  return dedupeSkills(observed);
}

function isDistinctiveSkillName(name) {
  return /[:/._-]/.test(name) || name.length >= 8;
}

function hasExplicitSkillMention(text, skillName) {
  if (!text || !skillName) {
    return false;
  }

  const lowerText = text.toLowerCase();
  const lowerName = skillName.toLowerCase();

  if (lowerText.includes(`$${lowerName}`) || lowerText.includes(`\`${lowerName}\``)) {
    return true;
  }

  if (isDistinctiveSkillName(skillName) && lowerText.includes(lowerName)) {
    return true;
  }

  const escapedName = escapeRegExp(lowerName);
  return (
    new RegExp(`(?:^|\\W)skill\\s+${escapedName}(?:\\W|$)`, "i").test(text) ||
    new RegExp(`${escapedName}\\s+skill(?:\\W|$)`, "i").test(text)
  );
}

function hasPluginMention(text, pluginName) {
  if (!text || !pluginName) {
    return false;
  }

  const lowerText = text.toLowerCase();
  const lowerPlugin = pluginName.toLowerCase();

  if (lowerText.includes(`[$${lowerPlugin}](`) || lowerText.includes(`\`${lowerPlugin}\``)) {
    return true;
  }

  if (isDistinctiveSkillName(pluginName)) {
    return lowerText.includes(lowerPlugin);
  }

  return new RegExp(
    `(^|[^a-z0-9])${escapeRegExp(lowerPlugin)}([^a-z0-9]|$)`,
    "i",
  ).test(text);
}

function extractEvidenceSnippet(text, terms = []) {
  const lines = String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  const matchedLine =
    lines.find((line) =>
      terms.some((term) => term && line.toLowerCase().includes(term.toLowerCase())),
    ) ?? lines[0] ?? "";

  return shortText(matchedLine || text, 220);
}

function isSkillFileReadTrace(text, skillPath) {
  if (!text || !skillPath) {
    return false;
  }

  const pathCandidates = getSkillPathCandidates(skillPath);
  if (!pathCandidates.some((candidate) => text.includes(candidate))) {
    return false;
  }

  const lowerText = text.toLowerCase();
  const readPatterns = [
    /\bcat\b/i,
    /\bsed\b/i,
    /\brg\b/i,
    /\bgrep\b/i,
    /\bhead\b/i,
    /\btail\b/i,
    /\bless\b/i,
    /\breadfile\s*\(/i,
  ];
  const writePatterns = [
    /\bwritefile\s*\(/i,
    /\bappendfile\s*\(/i,
    /\bcp\b/i,
    /\bmv\b/i,
    /\brm\b/i,
    /\bmkdir\b/i,
  ];

  return (
    readPatterns.some((pattern) => pattern.test(lowerText)) &&
    !writePatterns.some((pattern) => pattern.test(lowerText))
  );
}

function buildSkillTrace(turn, fallbackCatalog = []) {
  const currentCatalog = collectSkillCatalog(turn.runtimeDeveloperMessages);
  const visibleCatalog = currentCatalog.length ? currentCatalog : fallbackCatalog;
  const catalogSource = currentCatalog.length
    ? "current_turn"
    : fallbackCatalog.length
      ? "prior_turn"
      : "none";

  const toolEntries = extractToolTraceEntries(turn.records);
  const observedSkills = extractObservedSkillPaths(toolEntries);
  const candidateSkills = dedupeSkills([...visibleCatalog, ...observedSkills]);
  const matchedSkills = [];

  candidateSkills.forEach((skill) => {
    const explicitReasons = turn.humanUserMessages
      .filter((message) => hasExplicitSkillMention(message.text, skill.name))
      .map((message) => ({
        type: "explicit",
        source: "user",
        timestamp: message.timestamp,
        evidence: extractEvidenceSnippet(message.text, [skill.name]),
      }));

    const assistantMentions = turn.assistantMessages
      .filter((message) => hasExplicitSkillMention(message.text, skill.name))
      .map((message) => ({
        type: "semantic",
        source: "assistant",
        timestamp: message.timestamp,
        evidence: extractEvidenceSnippet(message.text, [skill.name]),
      }));

    const readEvidence = toolEntries
      .filter((entry) => isSkillFileReadTrace(entry.inputText, skill.path))
      .map((entry) => ({
        source: entry.kind === "custom_tool_call" ? "custom_tool" : "tool",
        toolName: entry.toolName,
        timestamp: entry.timestamp,
        evidence: extractEvidenceSnippet(entry.inputText, [
          skill.path,
          path.basename(path.dirname(skill.path)),
          "SKILL.md",
        ]),
      }));

    if (!explicitReasons.length && !assistantMentions.length && !readEvidence.length) {
      return;
    }

    const pluginName = skill.name.includes(":") ? skill.name.split(":")[0] : "";
    const priorityReasons =
      pluginName && (explicitReasons.length || assistantMentions.length || readEvidence.length)
        ? turn.humanUserMessages
            .filter((message) => hasPluginMention(message.text, pluginName))
            .map((message) => ({
              type: "priority",
              source: "user",
              timestamp: message.timestamp,
              evidence: extractEvidenceSnippet(message.text, [pluginName]),
            }))
        : [];

    const reasons = [
      ...explicitReasons,
      ...(explicitReasons.length ? [] : assistantMentions),
      ...priorityReasons,
    ];

    if (!reasons.length && readEvidence.length) {
      reasons.push({
        type: "semantic",
        source: readEvidence[0].source,
        timestamp: readEvidence[0].timestamp,
        evidence: readEvidence[0].evidence,
      });
    }

    matchedSkills.push({
      name: skill.name,
      description: skill.description,
      path: skill.path,
      status: readEvidence.length ? "verified" : "inferred",
      actuallyRead: readEvidence.length > 0,
      reasons,
      readEvidence,
    });
  });

  const mergedMatchedSkills = [...matchedSkills
    .reduce((map, skill) => {
      const existing = map.get(skill.name);
      if (!existing) {
        map.set(skill.name, {
          ...skill,
          reasons: [...skill.reasons],
          readEvidence: [...skill.readEvidence],
        });
        return map;
      }

      existing.status =
        existing.actuallyRead || skill.actuallyRead ? "verified" : existing.status;
      existing.actuallyRead = existing.actuallyRead || skill.actuallyRead;
      existing.reasons = [...existing.reasons, ...skill.reasons].filter(
        (reason, index, array) =>
          array.findIndex(
            (item) =>
              item.type === reason.type &&
              item.source === reason.source &&
              item.timestamp === reason.timestamp &&
              item.evidence === reason.evidence,
          ) === index,
      );
      existing.readEvidence = [...existing.readEvidence, ...skill.readEvidence].filter(
        (entry, index, array) =>
          array.findIndex(
            (item) =>
              item.toolName === entry.toolName &&
              item.timestamp === entry.timestamp &&
              item.evidence === entry.evidence,
          ) === index,
      );

      if (!existing.description && skill.description) {
        existing.description = skill.description;
      }

      if (skill.path && !existing.path) {
        existing.path = skill.path;
      }

      return map;
    }, new Map())
    .values()];

  return {
    catalogInjected: currentCatalog.length > 0,
    catalogSource,
    availableSkillCount: visibleCatalog.length,
    catalogPreview: visibleCatalog.slice(0, 6).map((skill) => skill.name),
    matchedSkillCount: mergedMatchedSkills.length,
    matchedSkills: mergedMatchedSkills,
    notes: [
      currentCatalog.length
        ? "Current turn includes a visible available-skills catalog."
        : fallbackCatalog.length
          ? "Current turn has no visible skills catalog; reusing the latest catalog seen earlier in this session."
          : "No skills catalog was found in this turn or earlier turns.",
      mergedMatchedSkills.length
        ? "Matched skills require direct evidence from user text, assistant text, or explicit SKILL.md reads."
        : "No direct skill-hit evidence was found in this turn's logs.",
    ],
  };
}

function buildPromptEnvelope(turn) {
  const placeholders = turn.userMessageEvents
    .flatMap((message) =>
      (message.textElements ?? []).map((item) => ({
        placeholder: item.placeholder ?? "",
        start: item.byteRange?.start ?? null,
        end: item.byteRange?.end ?? null,
      })),
    )
    .filter((item) => item.placeholder)
    .filter(
      (item, index, array) =>
        array.findIndex(
          (candidate) =>
            candidate.placeholder === item.placeholder &&
            candidate.start === item.start &&
            candidate.end === item.end,
        ) === index,
    );

  const imageCount = turn.userMessageEvents.reduce(
    (sum, message) => sum + (message.images?.length ?? 0),
    0,
  );
  const localImageCount = turn.userMessageEvents.reduce(
    (sum, message) => sum + (message.localImages?.length ?? 0),
    0,
  );

  return {
    messageCount: turn.userMessageEvents.length,
    imageCount,
    localImageCount,
    placeholderCount: placeholders.length,
    placeholders,
  };
}

function buildApprovalTrace(turn) {
  const grouped = new Map();
  const mcpResultsById = new Map(
    turn.mcpToolCallResults.map((item) => [item.callId, item]),
  );
  const patchResultsById = new Map(
    turn.patchApplyEvents.map((item) => [item.callId, item]),
  );
  const commandResultsById = new Map(
    turn.commandResults.map((item) => [item.callId, item]),
  );
  const toolCallsById = new Map(
    turn.toolCalls
      .filter((item) => item.callId)
      .map((item) => [item.callId, item]),
  );
  const toolOutputsById = new Map(
    turn.toolOutputs
      .filter((item) => item.callId)
      .map((item) => [item.callId, item]),
  );

  turn.guardianAssessments.forEach((item) => {
    if (!grouped.has(item.id)) {
      grouped.set(item.id, {
        id: item.id,
        action: item.action ?? null,
        events: [],
      });
    }

    const group = grouped.get(item.id);
    group.action = item.action ?? group.action;
    group.events.push(item);
  });

  const items = [...grouped.values()]
    .map((group) => {
      const events = [...group.events].sort((a, b) =>
        String(a.timestamp).localeCompare(String(b.timestamp)),
      );
      const latest = events.at(-1) ?? null;

      return {
        id: group.id,
        action: group.action,
        finalStatus: latest?.status ?? "unknown",
        riskScore: latest?.riskScore ?? null,
        riskLevel: latest?.riskLevel ?? null,
        rationale: latest?.rationale ?? "",
        events: events.map((item) => ({
          timestamp: item.timestamp,
          status: item.status,
        })),
        toolCall: toolCallsById.get(group.id) ?? null,
        toolOutput: toolOutputsById.get(group.id) ?? null,
        mcpResult: mcpResultsById.get(group.id) ?? null,
        patchResult: patchResultsById.get(group.id) ?? null,
        commandResult: commandResultsById.get(group.id) ?? null,
      };
    })
    .sort((a, b) =>
      String(a.events.at(0)?.timestamp ?? "").localeCompare(
        String(b.events.at(0)?.timestamp ?? ""),
      ),
    );

  return {
    count: items.length,
    approvedCount: items.filter((item) => item.finalStatus === "approved").length,
    deniedCount: items.filter((item) => item.finalStatus === "denied").length,
    pendingCount: items.filter((item) => item.finalStatus === "in_progress").length,
    items,
  };
}

function extractCompactionEntryText(entry) {
  if (!entry || typeof entry !== "object") {
    return serializeValue(entry);
  }

  if (entry.type === "message") {
    return extractTextFromContent(entry.content);
  }

  return serializeValue(entry);
}

function extractCompactionEntryRole(entry) {
  if (!entry || typeof entry !== "object") {
    return "unknown";
  }

  if (entry.type === "message") {
    return entry.role ?? "message";
  }

  return entry.type ?? "unknown";
}

function buildCompactionTranscript(replacementHistory = []) {
  return replacementHistory
    .map((entry) => {
      const role = extractCompactionEntryRole(entry);
      const phaseLabel = entry?.phase ? ` · ${entry.phase}` : "";
      const text = extractCompactionEntryText(entry).trim() || "(empty)";
      return `[${role.toUpperCase()}${phaseLabel}]\n${text}`;
    })
    .join("\n\n");
}

function buildCompactionTrace(turn) {
  const items = turn.compactionEvents.map((event) => ({
    timestamp: event.timestamp,
    replacementCount: event.replacementCount,
    roleCounts: event.roleCounts,
    preview: event.preview,
    transcript: event.transcript,
    summaryMessage: event.summaryMessage,
  }));

  return {
    count: items.length,
    replacedEntryTotal: items.reduce((sum, item) => sum + item.replacementCount, 0),
    items,
  };
}

function buildTurnMechanics(turn) {
  const lastTokenSnapshot = turn.lastTokenSnapshot ?? null;
  const lastRateLimitSnapshot =
    [...turn.tokenSnapshots].reverse().find((item) => item.rateLimits) ?? null;
  const modelContextWindow =
    turn.taskStarted?.modelContextWindow ??
    lastTokenSnapshot?.modelContextWindow ??
    null;
  const contextCompactedCount = Math.max(
    turn.contextCompactedSignals ?? 0,
    turn.compactionEvents.length,
  );

  return {
    modelContextWindow,
    collaborationModeKind:
      turn.taskStarted?.collaborationModeKind ??
      turn.turnContextCompact?.collaborationMode ??
      null,
    cachedInputTokens: lastTokenSnapshot?.cachedInputTokens ?? 0,
    inputTokens: lastTokenSnapshot?.inputTokens ?? 0,
    outputTokens: lastTokenSnapshot?.outputTokens ?? 0,
    reasoningTokens: lastTokenSnapshot?.reasoningTokens ?? 0,
    totalTokens: lastTokenSnapshot?.totalTokens ?? null,
    contextUtilization:
      modelContextWindow && lastTokenSnapshot?.inputTokens
        ? lastTokenSnapshot.inputTokens / modelContextWindow
        : null,
    contextCompactedCount,
    compactionRecordCount: turn.compactionEvents.length,
    errorCount: turn.errors.length,
    rateLimits: lastRateLimitSnapshot?.rateLimits ?? null,
  };
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
  const commentaryMessages = [];
  const userMessageEvents = [];
  const guardianAssessments = [];
  const mcpToolCallResults = [];
  const patchApplyEvents = [];
  const commandResults = [];
  const webSearchEvents = [];
  const compactionEvents = [];
  const errors = [];
  const rawMessages = [];
  let taskStarted = null;
  let taskCompleted = null;
  let contextCompactedSignals = 0;

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
        sourceType: "function",
      });
      continue;
    }

    if (payload.type === "custom_tool_call") {
      toolCalls.push({
        timestamp: record.timestamp,
        name: payload.name ?? "custom_tool",
        argumentsText: serializeValue(payload.input ?? ""),
        callId: payload.call_id ?? null,
        status: payload.status ?? null,
        sourceType: "custom",
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
        sourceType: "function",
      });
      continue;
    }

    if (payload.type === "custom_tool_call_output") {
      const parsedOutput = tryParseJsonString(payload.output);
      const outputValue = parsedOutput ?? payload.output;
      const outputText = serializeValue(outputValue);
      toolOutputs.push({
        timestamp: record.timestamp,
        callId: payload.call_id ?? null,
        outputText,
        preview: shortText(outputText, 320),
        metadata:
          parsedOutput && typeof parsedOutput === "object"
            ? parsedOutput.metadata ?? null
            : null,
        sourceType: "custom",
      });
    }
  }

  for (const record of eventMessages) {
    const payload = record.payload ?? {};

    if (payload.type === "agent_message") {
      commentaryMessages.push({
        timestamp: record.timestamp,
        phase: payload.phase ?? null,
        message: payload.message ?? "",
        memoryCitation: payload.memory_citation ?? null,
      });
      continue;
    }

    if (payload.type === "user_message") {
      userMessageEvents.push({
        timestamp: record.timestamp,
        message: payload.message ?? "",
        images: payload.images ?? [],
        localImages: payload.local_images ?? [],
        textElements: (payload.text_elements ?? []).map((item) => ({
          placeholder: item.placeholder ?? "",
          byteRange: item.byte_range ?? null,
        })),
      });
      continue;
    }

    if (payload.type === "guardian_assessment") {
      guardianAssessments.push({
        id: payload.id ?? null,
        timestamp: record.timestamp,
        status: payload.status ?? "unknown",
        riskScore: payload.risk_score ?? null,
        riskLevel: payload.risk_level ?? null,
        rationale: payload.rationale ?? "",
        action: payload.action ?? null,
      });
      continue;
    }

    if (payload.type === "mcp_tool_call_end") {
      const errorText = payload.result?.Err ?? null;
      const okText = payload.result?.Ok ?? null;
      mcpToolCallResults.push({
        callId: payload.call_id ?? null,
        timestamp: record.timestamp,
        invocation: payload.invocation ?? null,
        duration: payload.duration ?? null,
        ok: !errorText,
        resultText: errorText || serializeValue(okText ?? payload.result ?? ""),
      });
      continue;
    }

    if (payload.type === "patch_apply_end") {
      patchApplyEvents.push({
        callId: payload.call_id ?? null,
        timestamp: record.timestamp,
        success: payload.success ?? false,
        status: payload.status ?? null,
        stdout: payload.stdout ?? "",
        stderr: payload.stderr ?? "",
        changes: payload.changes ?? {},
      });
      continue;
    }

    if (payload.type === "exec_command_end") {
      commandResults.push({
        callId: payload.call_id ?? null,
        timestamp: record.timestamp,
        processId: payload.process_id ?? null,
        turnId: payload.turn_id ?? null,
        command: payload.command ?? [],
        cwd: payload.cwd ?? null,
        parsedCmd: payload.parsed_cmd ?? [],
        source: payload.source ?? null,
        exitCode: payload.exit_code ?? null,
        duration: payload.duration ?? null,
        status: payload.status ?? null,
        output: payload.aggregated_output ?? payload.stdout ?? "",
      });
      continue;
    }

    if (payload.type === "task_started") {
      taskStarted = {
        timestamp: record.timestamp,
        turnId: payload.turn_id ?? null,
        modelContextWindow: payload.model_context_window ?? null,
        collaborationModeKind: payload.collaboration_mode_kind ?? null,
      };
      continue;
    }

    if (payload.type === "task_complete") {
      taskCompleted = {
        timestamp: record.timestamp,
        turnId: payload.turn_id ?? null,
        lastAgentMessage: payload.last_agent_message ?? null,
      };
      continue;
    }

    if (payload.type === "context_compacted") {
      contextCompactedSignals += 1;
      continue;
    }

    if (payload.type === "error") {
      errors.push({
        timestamp: record.timestamp,
        message: payload.message ?? "unknown error",
        code: payload.codex_error_info ?? null,
      });
      continue;
    }

    if (payload.type === "web_search_call" || payload.type === "web_search_end") {
      webSearchEvents.push({
        timestamp: record.timestamp,
        type: payload.type,
        callId: payload.call_id ?? null,
        query: payload.query ?? payload.action?.query ?? "",
        action: payload.action ?? null,
      });
    }
  }

  for (const record of records) {
    if (record.type !== "compacted") {
      continue;
    }

    const payload = record.payload ?? {};
    const replacementHistory = Array.isArray(payload.replacement_history)
      ? payload.replacement_history
      : [];
    const roleCounts = replacementHistory.reduce((accumulator, entry) => {
      const role = extractCompactionEntryRole(entry);
      accumulator[role] = (accumulator[role] ?? 0) + 1;
      return accumulator;
    }, {});
    const transcript = buildCompactionTranscript(replacementHistory);

    compactionEvents.push({
      timestamp: record.timestamp,
      summaryMessage: payload.message ?? "",
      replacementCount: replacementHistory.length,
      roleCounts,
      preview: shortText(transcript, 220),
      transcript,
    });
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
        cachedInputTokens: usage.cached_input_tokens ?? 0,
        outputTokens: usage.output_tokens ?? 0,
        reasoningTokens: usage.reasoning_output_tokens ?? 0,
        totalTokens: usage.total_tokens ?? 0,
        modelContextWindow: record.payload?.info?.model_context_window ?? null,
        rateLimits: record.payload?.rate_limits ?? null,
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
    commentaryMessages,
    userMessageEvents,
    toolCalls,
    toolOutputs,
    guardianAssessments,
    mcpToolCallResults,
    patchApplyEvents,
    commandResults,
    webSearchEvents,
    compactionEvents,
    errors,
    taskStarted,
    taskCompleted,
    contextCompactedSignals,
    reasoningCount,
    tokenSnapshots,
    lastTokenSnapshot,
    promptEnvelope: null,
    approvalTrace: null,
    compactionTrace: null,
    turnMechanics: null,
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
    const sliceStart = index === 0 ? 0 : startIndex;
    const endIndex =
      index + 1 < turnContextIndexes.length
        ? turnContextIndexes[index + 1]
        : records.length;

    return {
      turnContextRecord: records[startIndex],
      records: records.slice(sliceStart, endIndex),
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
    sourceKind: sessionMeta.sourceKind,
    sourceLabel: sessionMeta.sourceLabel,
    git: sessionMeta.git,
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
  const sourceMeta = normalizeSessionSource(metaPayload.source);
  const sessionMeta = {
    id: metaPayload.id ?? path.basename(filePath, ".jsonl"),
    timestamp: metaPayload.timestamp ?? metaRecord?.timestamp ?? null,
    cwd: metaPayload.cwd ?? null,
    source: sourceMeta.raw,
    sourceKind: sourceMeta.kind,
    sourceLabel: sourceMeta.label,
    originator: metaPayload.originator ?? null,
    cliVersion: metaPayload.cli_version ?? null,
    modelProvider: metaPayload.model_provider ?? null,
    model: metaPayload.model ?? null,
    git: normalizeGitInfo(metaPayload.git ?? {}),
    baseInstructions: metaPayload.base_instructions?.text ?? "",
  };

  const turns = groupedTurns.map((group, index) =>
    parseTurn(group, index + 1, sessionMeta),
  );

  let latestSkillCatalog = [];
  turns.forEach((turn, index) => {
    const currentCatalog = collectSkillCatalog(turn.runtimeDeveloperMessages);
    turn.skillTrace = buildSkillTrace(turn, latestSkillCatalog);
    turn.promptEnvelope = buildPromptEnvelope(turn);
    turn.approvalTrace = buildApprovalTrace(turn);
    turn.compactionTrace = buildCompactionTrace(turn);
    turn.turnMechanics = buildTurnMechanics(turn);
    if (currentCatalog.length) {
      latestSkillCatalog = currentCatalog;
    }
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
  const sourceMeta = normalizeSessionSource(metaPayload.source);
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
    sourceKind: sourceMeta.kind,
    sourceLabel: sourceMeta.label,
    git: normalizeGitInfo(metaPayload.git ?? {}),
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
