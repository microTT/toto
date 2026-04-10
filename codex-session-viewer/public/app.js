const SESSION_FILTERS = [
  { id: "all", label: "All" },
  { id: "human-led", label: "Human-led" },
  { id: "guardian", label: "Guardian" },
  { id: "long-context", label: "Long Context" },
  { id: "tool-heavy", label: "Tool-heavy" },
  { id: "system-like", label: "System-like" },
];

const SYSTEM_PROMPT_MARKERS = [
  "the following is the codex agent history whose request action you are assessing",
  ">>> approval request start",
  "your final message must be strict json",
  "\"risk_level\"",
  "\"risk_score\"",
];

const INJECTED_USER_PREFIX = "# AGENTS.md instructions for ";
const MEMORY_PREFIX = "[MEMORY LOADED]";
const RUNTIME_DEVELOPER_MARKERS = [
  "<permissions instructions>",
  "<collaboration_mode>",
  "<apps_instructions>",
  "<skills_instructions>",
  "<plugins_instructions>",
];

const LONG_CONTEXT_THRESHOLD = 50_000;
const TOOL_HEAVY_THRESHOLD = 4;
const DEEP_THREAD_THRESHOLD = 6;

const state = {
  groupedSessions: [],
  sessionDetails: new Map(),
  selectedSessionId: null,
  selectedMessageId: null,
  searchTerm: "",
  sessionsRoot: "",
  kindFilter: "all",
};

const elements = {
  healthBadge: document.querySelector("#healthBadge"),
  sessionCount: document.querySelector("#sessionCount"),
  turnCount: document.querySelector("#turnCount"),
  peakTokens: document.querySelector("#peakTokens"),
  sessionsRoot: document.querySelector("#sessionsRoot"),
  refreshButton: document.querySelector("#refreshButton"),
  searchInput: document.querySelector("#searchInput"),
  kindFilters: document.querySelector("#kindFilters"),
  activeLens: document.querySelector("#activeLens"),
  sessionsList: document.querySelector("#sessionsList"),
  turnsList: document.querySelector("#turnsList"),
  turnDetail: document.querySelector("#turnDetail"),
  turnsMeta: document.querySelector("#turnsMeta"),
  detailMeta: document.querySelector("#detailMeta"),
  emptyStateTemplate: document.querySelector("#emptyStateTemplate"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return new Intl.NumberFormat("zh-CN").format(value ?? 0);
}

function formatDateTime(value) {
  if (!value) {
    return "unknown";
  }

  const date = new Date(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function shortText(value, limit = 160) {
  const compact = String(value || "").replace(/\s+/g, " ").trim();
  if (compact.length <= limit) {
    return compact;
  }
  return `${compact.slice(0, limit - 1)}…`;
}

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
  return text.startsWith(INJECTED_USER_PREFIX) || text.includes("<environment_context>");
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

function compactPath(value) {
  if (!value) {
    return "unknown";
  }

  const compact = value.replace(/^\/Users\/[^/]+/, "~");
  if (compact.length <= 54) {
    return compact;
  }

  const parts = compact.split("/").filter(Boolean);
  if (parts.length <= 4) {
    return compact;
  }

  return `${parts.slice(0, 2).join("/")}/…/${parts.slice(-2).join("/")}`;
}

function getSessionSourceKind(session) {
  return session.sessionMeta?.sourceKind || session.sourceKind || "unknown";
}

function getSessionSourceLabel(session) {
  return session.sessionMeta?.sourceLabel || session.sourceLabel || "Unknown";
}

function getSessionGit(session) {
  return session.sessionMeta?.git || session.git || {};
}

function getSessionRepoLabel(session) {
  return getSessionGit(session).repositoryLabel || "unknown repo";
}

function getSessionBranchLabel(session) {
  return getSessionGit(session).branch || "unknown branch";
}

function serializeSignals(signals) {
  return signals.map((signal) => signal.label).join(" · ");
}

function getSessionPromptCorpus(session) {
  const turnPrompts = Array.isArray(session.turns)
    ? session.turns.map((turn) => turn.promptFocus || turn.promptPreview || "").join("\n")
    : "";

  return [session.firstPrompt, session.latestPrompt, turnPrompts]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
}

function getSessionTurnCount(session) {
  if (Array.isArray(session.turns)) {
    return session.turns.length;
  }
  return typeof session.turnCount === "number" ? session.turnCount : null;
}

function getSessionToolCount(session) {
  if (Array.isArray(session.turns)) {
    return session.turns.reduce((sum, turn) => sum + turn.toolCalls.length, 0);
  }
  return typeof session.toolCallCount === "number" ? session.toolCallCount : null;
}

function getSessionPeakTokens(session) {
  if (Array.isArray(session.turns)) {
    return Math.max(
      0,
      ...session.turns.map((turn) => turn.lastTokenSnapshot?.inputTokens ?? 0),
    );
  }
  return typeof session.peakInputTokens === "number" ? session.peakInputTokens : null;
}

function isSystemLikeSession(session) {
  return SYSTEM_PROMPT_MARKERS.some((marker) =>
    getSessionPromptCorpus(session).includes(marker),
  );
}

function isLongContextSession(session) {
  return (getSessionPeakTokens(session) ?? 0) >= LONG_CONTEXT_THRESHOLD;
}

function isToolHeavySession(session) {
  return (getSessionToolCount(session) ?? 0) >= TOOL_HEAVY_THRESHOLD;
}

function isDeepThreadSession(session) {
  return (getSessionTurnCount(session) ?? 0) >= DEEP_THREAD_THRESHOLD;
}

function matchesFilter(session, filterId) {
  if (filterId === "all") {
    return true;
  }
  if (filterId === "human-led") {
    return !isSystemLikeSession(session);
  }
  if (filterId === "guardian") {
    return getSessionSourceKind(session) === "guardian";
  }
  if (filterId === "long-context") {
    return isLongContextSession(session);
  }
  if (filterId === "tool-heavy") {
    return isToolHeavySession(session);
  }
  if (filterId === "system-like") {
    return isSystemLikeSession(session);
  }
  return true;
}

function getSessionSignals(session) {
  const signals = [];

  if (getSessionSourceKind(session) === "guardian") {
    signals.push({ tone: "warning", label: "Guardian" });
  }

  if (isSystemLikeSession(session)) {
    signals.push({ tone: "warning", label: "System-like" });
  } else {
    signals.push({ tone: "human", label: "Human-led" });
  }

  if (isLongContextSession(session)) {
    signals.push({ tone: "accent", label: "Long context" });
  }

  if (isToolHeavySession(session)) {
    signals.push({ tone: "info", label: "Tool-heavy" });
  }

  if (isDeepThreadSession(session)) {
    signals.push({ tone: "muted", label: "Deep thread" });
  }

  if ((session.parseErrorCount ?? 0) > 0) {
    signals.push({ tone: "danger", label: "Parse issue" });
  }

  return signals;
}

function getTurnSignals(turn) {
  const signals = [];
  const inputTokens = turn.lastTokenSnapshot?.inputTokens ?? 0;

  if (inputTokens >= LONG_CONTEXT_THRESHOLD) {
    signals.push({ tone: "accent", label: "Long context" });
  }

  if (turn.toolCalls.length >= 1) {
    signals.push({ tone: "info", label: `${turn.toolCalls.length} tool call${turn.toolCalls.length > 1 ? "s" : ""}` });
  }

  if (turn.memoryMessages.length >= 1) {
    signals.push({ tone: "muted", label: `${turn.memoryMessages.length} memory inject` });
  }

  if (turn.reasoningCount >= 1) {
    signals.push({ tone: "warning", label: `${turn.reasoningCount} reasoning` });
  }

  if ((turn.skillTrace?.matchedSkillCount ?? 0) >= 1) {
    signals.push({
      tone: turn.skillTrace.matchedSkills.some((skill) => skill.actuallyRead)
        ? "accent"
        : "warning",
      label: `${turn.skillTrace.matchedSkillCount} skill hit${turn.skillTrace.matchedSkillCount > 1 ? "s" : ""}`,
    });
  }

  if ((turn.approvalTrace?.count ?? 0) >= 1) {
    signals.push({
      tone: turn.approvalTrace.deniedCount ? "danger" : "muted",
      label: `${turn.approvalTrace.count} approval${turn.approvalTrace.count > 1 ? "s" : ""}`,
    });
  }

  if ((turn.turnMechanics?.contextCompactedCount ?? 0) >= 1) {
    signals.push({
      tone: "warning",
      label: `${turn.turnMechanics.contextCompactedCount} compacted`,
    });
  }

  if ((turn.errors?.length ?? 0) >= 1) {
    signals.push({
      tone: "danger",
      label: `${turn.errors.length} error${turn.errors.length > 1 ? "s" : ""}`,
    });
  }

  return signals;
}

function renderSignalRow(signals) {
  if (!signals.length) {
    return "";
  }

  return `
    <div class="signal-row">
      ${signals
        .map(
          (signal) => `
            <span class="signal-chip tone-${escapeHtml(signal.tone)}">${escapeHtml(signal.label)}</span>
          `,
        )
        .join("")}
    </div>
  `;
}

function getSkillReasonMeta(reasonType) {
  if (reasonType === "explicit") {
    return { label: "Explicit", tone: "human" };
  }
  if (reasonType === "semantic") {
    return { label: "Semantic", tone: "warning" };
  }
  if (reasonType === "priority") {
    return { label: "Priority", tone: "info" };
  }
  return { label: "Observed", tone: "muted" };
}

function renderSkillTraceSection(turn) {
  const trace = turn.skillTrace;

  if (!trace) {
    return renderDetailSection(
      "Skill Trace",
      `<p class="empty-note">当前 turn 没有 skill trace 数据。</p>`,
      {
        tone: "info",
        kicker: "Skill routing",
        meta: "No parsed data",
      },
    );
  }

  const summarySignals = [
    {
      tone: trace.catalogInjected ? "info" : "muted",
      label: trace.catalogInjected ? "Catalog in turn" : "Catalog reused",
    },
    {
      tone: trace.matchedSkillCount ? "accent" : "muted",
      label: `${trace.matchedSkillCount} matched`,
    },
    {
      tone: "muted",
      label: `${trace.availableSkillCount} visible`,
    },
  ];

  const notesHtml = `
    <div class="skill-trace-notes">
      ${trace.notes.map((note) => `<p>${escapeHtml(note)}</p>`).join("")}
    </div>
  `;

  if (!trace.matchedSkills.length) {
    return renderDetailSection(
      "Skill Trace",
      `
        ${renderSignalRow(summarySignals)}
        ${notesHtml}
      `,
      {
        tone: "info",
        kicker: "Skill routing",
        meta:
          `${trace.availableSkillCount} skills visible` +
          (trace.catalogPreview?.length
            ? ` · sample: ${trace.catalogPreview.join(", ")}`
            : ""),
      },
    );
  }

  const cardsHtml = trace.matchedSkills
    .map((skill) => {
      const reasonMetaSignals = [...new Map(
        skill.reasons.map((reason) => {
          const meta = getSkillReasonMeta(reason.type);
          return [reason.type, { tone: meta.tone, label: meta.label }];
        }),
      ).values()];
      const reasonSignals = [
        {
          tone: skill.status === "verified" ? "accent" : "warning",
          label: skill.status === "verified" ? "Verified" : "Inferred",
        },
        {
          tone: skill.actuallyRead ? "accent" : "muted",
          label: skill.actuallyRead ? "Read SKILL.md" : "No direct file read",
        },
        ...reasonMetaSignals,
      ];

      const reasonItems = skill.reasons.length
        ? skill.reasons
            .map((reason) => {
              const meta = getSkillReasonMeta(reason.type);
              return `
                <article class="skill-evidence-item">
                  <div class="skill-evidence-topline">
                    <strong>${escapeHtml(meta.label)}</strong>
                    <span>${escapeHtml(reason.source)}</span>
                    <span>${escapeHtml(formatDateTime(reason.timestamp))}</span>
                  </div>
                  <p>${escapeHtml(reason.evidence)}</p>
                </article>
              `;
            })
            .join("")
        : `<p class="empty-note">没有记录到命中原因。</p>`;

      const readItems = skill.readEvidence.length
        ? skill.readEvidence
            .map(
              (entry) => `
                <article class="skill-evidence-item">
                  <div class="skill-evidence-topline">
                    <strong>Read</strong>
                    <span>${escapeHtml(entry.toolName || entry.source)}</span>
                    <span>${escapeHtml(formatDateTime(entry.timestamp))}</span>
                  </div>
                  <p>${escapeHtml(entry.evidence)}</p>
                </article>
              `,
            )
            .join("")
        : "";

      return `
        <article class="skill-card">
          <div class="skill-card-header">
            <div>
              <h4>${escapeHtml(skill.name)}</h4>
              <p class="skill-card-path" title="${escapeHtml(skill.path)}">${escapeHtml(
                compactPath(skill.path),
              )}</p>
            </div>
          </div>
          ${skill.description ? `<p class="skill-card-description">${escapeHtml(skill.description)}</p>` : ""}
          ${renderSignalRow(reasonSignals)}
          <div class="skill-evidence-stack">
            ${reasonItems}
            ${readItems}
          </div>
        </article>
      `;
    })
    .join("");

  return renderDetailSection(
    "Skill Trace",
    `
      ${renderSignalRow(summarySignals)}
      <div class="skill-card-list">
        ${cardsHtml}
      </div>
      ${notesHtml}
    `,
    {
      tone: "info",
      kicker: "Skill routing",
      meta:
        `${trace.matchedSkillCount} matched / ${trace.availableSkillCount} visible` +
        (trace.catalogPreview?.length
          ? ` · sample: ${trace.catalogPreview.join(", ")}`
          : ""),
    },
  );
}

function formatPercent(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return `${Number(value).toFixed(digits)}%`;
}

function formatDuration(duration) {
  if (!duration) {
    return "—";
  }

  const millis = (duration.secs ?? 0) * 1000 + (duration.nanos ?? 0) / 1_000_000;
  if (millis < 1000) {
    return `${millis >= 100 ? Math.round(millis) : millis.toFixed(1)} ms`;
  }

  const seconds = millis / 1000;
  return `${seconds >= 10 ? seconds.toFixed(0) : seconds.toFixed(1)} s`;
}

function getApprovalStatusMeta(status) {
  if (status === "approved") {
    return { label: "Approved", tone: "accent" };
  }
  if (status === "denied") {
    return { label: "Denied", tone: "danger" };
  }
  if (status === "in_progress") {
    return { label: "In Progress", tone: "warning" };
  }
  return { label: status || "Unknown", tone: "muted" };
}

function getApprovalActionLabel(item) {
  const action = item.action || {};

  if (action.tool === "mcp_tool_call") {
    return `${action.server}.${action.tool_name}`;
  }

  if (Array.isArray(action.command) && action.command.length) {
    return shortText(action.command.join(" "), 108);
  }

  if (typeof action.command === "string" && action.command) {
    return shortText(action.command, 108);
  }

  if (Array.isArray(action.files) && action.files.length) {
    return `${action.tool || "guarded action"} · ${action.files.length} file${action.files.length > 1 ? "s" : ""}`;
  }

  if (item.toolCall?.name) {
    return item.toolCall.name;
  }

  return action.tool || "guarded action";
}

function renderPromptEnvelopeSection(turn) {
  const envelope = turn.promptEnvelope;

  if (!envelope) {
    return "";
  }

  const summarySignals = [
    {
      tone: envelope.placeholderCount ? "accent" : "muted",
      label: `${envelope.placeholderCount} placeholder${envelope.placeholderCount > 1 ? "s" : ""}`,
    },
    {
      tone: envelope.localImageCount || envelope.imageCount ? "info" : "muted",
      label: `${envelope.imageCount + envelope.localImageCount} attachment${envelope.imageCount + envelope.localImageCount > 1 ? "s" : ""}`,
    },
  ];

  const placeholderHtml = envelope.placeholders.length
    ? `
        <div class="timeline-list">
          ${envelope.placeholders
            .map(
              (item) => `
                <article class="timeline-card">
                  <div class="timeline-meta">
                    <strong>${escapeHtml(item.placeholder)}</strong>
                    <span>${escapeHtml(
                      item.start !== null && item.end !== null
                        ? `bytes ${item.start}-${item.end}`
                        : "range unknown",
                    )}</span>
                  </div>
                </article>
              `,
            )
            .join("")}
        </div>
      `
    : `<p class="empty-note">这一轮没有结构化 prompt placeholder 或附件标记。</p>`;

  return renderDetailSection(
    "Prompt Envelope",
    `
      ${renderSignalRow(summarySignals)}
      ${placeholderHtml}
    `,
    {
      tone: "default",
      kicker: "Input envelope",
      meta:
        `${envelope.messageCount} user_message event${envelope.messageCount > 1 ? "s" : ""}` +
        ` · ${envelope.localImageCount} local image${envelope.localImageCount > 1 ? "s" : ""}`,
    },
  );
}

function renderProgressTraceSection(turn) {
  const items = (turn.commentaryMessages || []).filter(
    (item) => item.phase === "commentary" && item.message,
  );

  if (!items.length) {
    return "";
  }

  return renderDetailSection(
    "Progress Trace",
    renderTimelineList(
      items,
      (item) => `
        <article class="timeline-card">
          <div class="timeline-meta">
            <strong>Commentary</strong>
            <span>${escapeHtml(formatDateTime(item.timestamp))}</span>
          </div>
          ${renderTextBlock(item.message, {
            className: "timeline-block",
            maxLines: 5,
            maxChars: 720,
            summaryLabel: "展开进度消息",
          })}
        </article>
      `,
      "这一轮没有中间进度消息。",
    ),
    {
      tone: "info",
      kicker: "User-visible process",
      meta: `${items.length} commentary update${items.length > 1 ? "s" : ""}`,
    },
  );
}

function renderApprovalTraceSection(turn) {
  const trace = turn.approvalTrace;

  if (!trace?.count) {
    return "";
  }

  const summarySignals = [
    {
      tone: trace.approvedCount ? "accent" : "muted",
      label: `${trace.approvedCount} approved`,
    },
    {
      tone: trace.deniedCount ? "danger" : "muted",
      label: `${trace.deniedCount} denied`,
    },
    {
      tone: trace.pendingCount ? "warning" : "muted",
      label: `${trace.pendingCount} pending`,
    },
  ];

  return renderDetailSection(
    "Approval Trace",
    `
      ${renderSignalRow(summarySignals)}
      ${renderTimelineList(
        trace.items,
        (item) => {
          const statusMeta = getApprovalStatusMeta(item.finalStatus);
          const extraSignals = [
            { tone: statusMeta.tone, label: statusMeta.label },
          ];

          if (item.riskLevel) {
            extraSignals.push({
              tone:
                item.riskLevel === "low"
                  ? "muted"
                  : item.riskLevel === "medium"
                    ? "warning"
                    : "danger",
              label: `Risk ${item.riskLevel}`,
            });
          }

          if (item.patchResult?.success) {
            extraSignals.push({
              tone: "accent",
              label: `${Object.keys(item.patchResult.files || item.patchResult.changes || {}).length || item.patchResult.fileCount || 0} file patch`,
            });
          }

          const resultText =
            item.mcpResult?.resultText ||
            item.commandResult?.output ||
            item.toolOutput?.outputText ||
            "";

          return `
            <article class="timeline-card">
              <div class="timeline-meta">
                <strong>${escapeHtml(getApprovalActionLabel(item))}</strong>
                <span>${escapeHtml(formatDateTime(item.events.at(-1)?.timestamp))}</span>
                <span>${escapeHtml(item.id)}</span>
              </div>
              ${renderSignalRow(extraSignals)}
              ${item.rationale
                ? renderTextBlock(item.rationale, {
                    className: "timeline-block",
                    maxLines: 4,
                    maxChars: 560,
                    summaryLabel: "展开审批理由",
                  })
                : ""}
              ${resultText
                ? renderTextBlock(resultText, {
                    className: "timeline-block",
                    maxLines: 4,
                    maxChars: 560,
                    summaryLabel: "展开调用结果",
                  })
                : ""}
            </article>
          `;
        },
        "这一轮没有 guardrail / approval 记录。",
      )}
    `,
    {
      tone: "warning",
      kicker: "Guardrail decisions",
      meta: `${trace.count} guarded action${trace.count > 1 ? "s" : ""}`,
    },
  );
}

function renderTurnMechanicsSection(turn) {
  const mechanics = turn.turnMechanics;

  if (!mechanics) {
    return "";
  }

  const entries = [
    ["Context Window", formatNumber(mechanics.modelContextWindow)],
    ["Context Load", mechanics.contextUtilization !== null ? formatPercent(mechanics.contextUtilization * 100) : "—"],
    ["Cached Input", formatNumber(mechanics.cachedInputTokens)],
    ["Compactions", formatNumber(mechanics.contextCompactedCount)],
    ["Mode", mechanics.collaborationModeKind || "unknown"],
  ];

  if (mechanics.rateLimits?.plan_type) {
    entries.push(["Plan", mechanics.rateLimits.plan_type]);
  }

  if (mechanics.rateLimits?.primary?.used_percent !== undefined) {
    entries.push([
      "Primary Limit",
      `${formatPercent(mechanics.rateLimits.primary.used_percent)} · reset ${formatDateTime(
        (mechanics.rateLimits.primary.resets_at ?? 0) * 1000,
      )}`,
    ]);
  }

  if (mechanics.rateLimits?.secondary?.used_percent !== undefined) {
    entries.push([
      "Secondary Limit",
      `${formatPercent(mechanics.rateLimits.secondary.used_percent)} · reset ${formatDateTime(
        (mechanics.rateLimits.secondary.resets_at ?? 0) * 1000,
      )}`,
    ]);
  }

  const errorHtml = (turn.errors || []).length
    ? `
        <div class="timeline-list inline-section">
          ${turn.errors
            .map(
              (item) => `
                <article class="timeline-card">
                  <div class="timeline-meta">
                    <strong>${escapeHtml(item.code || "Codex error")}</strong>
                    <span>${escapeHtml(formatDateTime(item.timestamp))}</span>
                  </div>
                  <p class="inline-note">${escapeHtml(item.message)}</p>
                </article>
              `,
            )
            .join("")}
        </div>
      `
    : "";

  return renderDetailSection(
    "Turn Mechanics",
    `
      <div class="section-stack">
        ${renderKeyValueList(entries)}
        ${errorHtml}
      </div>
    `,
    {
      tone: "default",
      kicker: "Runtime mechanics",
      meta:
        `${formatNumber(mechanics.inputTokens)} in · ${formatNumber(mechanics.outputTokens)} out · ` +
        `${formatNumber(mechanics.reasoningTokens)} reasoning`,
    },
  );
}

function renderProvenanceSection(session, turn) {
  const git = getSessionGit(session);
  const entries = [
    ["Source", getSessionSourceLabel(session)],
    ["Originator", session.sessionMeta.originator || "unknown"],
    ["Model Provider", session.sessionMeta.modelProvider || "unknown"],
    ["CLI Version", session.sessionMeta.cliVersion || "unknown"],
    ["Repo", git.repositoryLabel || "unknown"],
    ["Branch", git.branch || "unknown"],
    ["Commit", git.commitShort || "unknown"],
    ["CWD", compactPath(session.sessionMeta.cwd || session.filePath)],
  ];

  if (turn.turnContextCompact?.collaborationMode) {
    entries.push(["Turn Mode", turn.turnContextCompact.collaborationMode]);
  }

  return renderDetailSection(
    "Session Provenance",
    renderKeyValueList(entries),
    {
      tone: "default",
      kicker: "Session origin",
      meta: git.repositoryUrl || session.filePath,
    },
  );
}

function renderCompactionTraceSection(turn) {
  const trace = turn.compactionTrace;

  if (!trace?.count) {
    return "";
  }

  const summarySignals = [
    {
      tone: "warning",
      label: `${trace.count} compaction${trace.count > 1 ? "s" : ""}`,
    },
    {
      tone: "muted",
      label: `${trace.replacedEntryTotal} replaced item${trace.replacedEntryTotal > 1 ? "s" : ""}`,
    },
  ];

  return renderDetailSection(
    "Compaction Trace",
    `
      ${renderSignalRow(summarySignals)}
      ${renderTimelineList(
        trace.items,
        (item) => {
          const roleSignals = Object.entries(item.roleCounts || {})
            .sort((left, right) => left[0].localeCompare(right[0]))
            .map(([role, count]) => ({
              tone: role === "user" || role === "assistant" ? "info" : "muted",
              label: `${count} ${role}`,
            }));

          return `
            <article class="timeline-card">
              <div class="timeline-meta">
                <strong>${escapeHtml(`${item.replacementCount} items compacted`)}</strong>
                <span>${escapeHtml(formatDateTime(item.timestamp))}</span>
              </div>
              ${renderSignalRow(roleSignals)}
              ${item.summaryMessage
                ? renderTextBlock(item.summaryMessage, {
                    className: "timeline-block",
                    maxLines: 4,
                    maxChars: 560,
                    summaryLabel: "展开压缩摘要",
                  })
                : `<p class="inline-note">这一轮没有额外 compaction summary，只保留 replacement history。</p>`}
              ${renderTextBlock(item.transcript || item.preview || "(empty)", {
                className: "timeline-block",
                maxLines: 8,
                maxChars: 1000,
                summaryLabel: "展开被压缩的历史消息",
              })}
            </article>
          `;
        },
        "这一轮没有上下文压缩记录。",
      )}
    `,
    {
      tone: "warning",
      kicker: "Prompt history compression",
      meta: "这些记录代表 Codex 为腾出上下文窗口而压缩掉的旧消息。",
    },
  );
}

function getVisibleGroups() {
  const term = state.searchTerm.trim().toLowerCase();

  return state.groupedSessions
    .map((group) => ({
      ...group,
      sessions: group.sessions.filter((session) => {
        if (!matchesFilter(session, state.kindFilter)) {
          return false;
        }

        if (!term) {
          return true;
        }

        const corpus = [
          session.id,
          session.cwd,
          session.firstPrompt,
          session.latestPrompt,
          session.model,
          getSessionSourceLabel(session),
          getSessionRepoLabel(session),
          getSessionBranchLabel(session),
          serializeSignals(getSessionSignals(session)),
        ]
          .filter(Boolean)
          .join("\n")
          .toLowerCase();

        return corpus.includes(term);
      }),
    }))
    .filter((group) => group.sessions.length);
}

function getVisibleSessions() {
  return getVisibleGroups().flatMap((group) => group.sessions);
}

function pickInitialSessionId(sessions) {
  return sessions.find((session) => !isSystemLikeSession(session))?.id ?? sessions[0]?.id ?? null;
}

function renderKindFilters() {
  const sessions = state.groupedSessions.flatMap((group) => group.sessions);

  elements.kindFilters.innerHTML = SESSION_FILTERS.map((filter) => {
    const count = sessions.filter((session) => matchesFilter(session, filter.id)).length;
    const className =
      filter.id === state.kindFilter ? "filter-chip is-active" : "filter-chip";

    return `
      <button class="${className}" data-filter-id="${escapeHtml(filter.id)}">
        <span>${escapeHtml(filter.label)}</span>
        <strong>${formatNumber(count)}</strong>
      </button>
    `;
  }).join("");
}

function renderActiveLens() {
  const visibleSessions = getVisibleSessions();
  const totalSessions = state.groupedSessions.flatMap((group) => group.sessions).length;
  const lensLabel =
    SESSION_FILTERS.find((filter) => filter.id === state.kindFilter)?.label ?? "All";
  const searchLabel = state.searchTerm.trim() ? ` · Search: “${state.searchTerm.trim()}”` : "";

  elements.activeLens.textContent =
    `Showing ${formatNumber(visibleSessions.length)} / ${formatNumber(totalSessions)} sessions` +
    ` · Lens: ${lensLabel}${searchLabel}`;
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
}

async function loadHealth() {
  const payload = await fetchJson("/api/health");
  state.sessionsRoot = payload.sessionsRoot;
  elements.sessionsRoot.textContent = payload.sessionsRoot;
  elements.healthBadge.textContent = "Ready";
}

function refreshAggregateMetrics() {
  const sessions = state.groupedSessions.flatMap((group) => group.sessions);
  const knownTurnCounts = sessions
    .map((session) => getSessionTurnCount(session))
    .filter((value) => typeof value === "number");
  const knownPeakTokens = sessions
    .map((session) => getSessionPeakTokens(session))
    .filter((value) => typeof value === "number");

  elements.sessionCount.textContent = formatNumber(sessions.length);
  elements.turnCount.textContent = formatNumber(
    knownTurnCounts.length
      ? knownTurnCounts.reduce((sum, value) => sum + value, 0)
      : null,
  );
  elements.peakTokens.textContent = formatNumber(
    knownPeakTokens.length ? Math.max(...knownPeakTokens) : null,
  );
}

async function loadSummaries({ preserveSelection = true } = {}) {
  const payload = await fetchJson("/api/sessions");
  state.groupedSessions = payload.groups;
  state.sessionsRoot = payload.sessionsRoot;
  elements.sessionsRoot.textContent = payload.sessionsRoot;

  const sessions = payload.groups.flatMap((group) => group.sessions);
  refreshAggregateMetrics();
  renderKindFilters();
  renderActiveLens();

  if (!preserveSelection || !sessions.some((session) => session.id === state.selectedSessionId)) {
    state.selectedSessionId = pickInitialSessionId(sessions);
    state.selectedMessageId = null;
  }

  renderSessions();

  if (state.selectedSessionId) {
    await loadSessionDetail(state.selectedSessionId, { preserveMessage: preserveSelection });
  } else {
    renderMessages();
    renderMessageDetail();
  }
}

async function loadSessionDetail(sessionId, { preserveMessage = true } = {}) {
  if (!sessionId) {
    return;
  }

  let session = state.sessionDetails.get(sessionId);
  if (!session) {
    session = await fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}`);
    state.sessionDetails.set(sessionId, session);
  }

  state.selectedSessionId = sessionId;

  const messages = buildSessionMessages(session);
  if (!preserveMessage || !messages.some((message) => message.id === state.selectedMessageId)) {
    state.selectedMessageId = messages.at(-1)?.id ?? null;
  }

  for (const group of state.groupedSessions) {
    const summary = group.sessions.find((item) => item.id === sessionId);
    if (summary) {
      summary.turnCount = session.turns.length;
      summary.toolCallCount = session.turns.reduce(
        (sum, turn) => sum + turn.toolCalls.length,
        0,
      );
      summary.peakInputTokens = Math.max(
        0,
        ...session.turns.map((turn) => turn.lastTokenSnapshot?.inputTokens ?? 0),
      );
      summary.latestPrompt = session.turns.at(-1)?.promptPreview ?? summary.latestPrompt;
      summary.firstPrompt = session.turns[0]?.promptPreview ?? summary.firstPrompt;
      break;
    }
  }

  refreshAggregateMetrics();
  renderKindFilters();
  renderActiveLens();
  renderSessions();
  renderMessages();
  renderMessageDetail();
}

async function syncSelectionToLens() {
  const visibleSessions = getVisibleSessions();

  renderKindFilters();
  renderActiveLens();
  renderSessions();

  if (!visibleSessions.length) {
    state.selectedSessionId = null;
    state.selectedMessageId = null;
    renderMessages();
    renderMessageDetail();
    return;
  }

  if (!visibleSessions.some((session) => session.id === state.selectedSessionId)) {
    await loadSessionDetail(visibleSessions[0].id, { preserveMessage: false });
    return;
  }

  renderMessages();
  renderMessageDetail();
}

function renderEmptyState(title, detail, actionLabel = "") {
  return `
    <div class="empty-state">
      <h3>${escapeHtml(title)}</h3>
      <p>${escapeHtml(detail)}</p>
      ${actionLabel ? `<button class="ghost-button small" data-reset-lens="true">${escapeHtml(actionLabel)}</button>` : ""}
    </div>
  `;
}

function renderSessions() {
  const groups = getVisibleGroups();

  if (!groups.length) {
    const emptyText = state.searchTerm.trim() || state.kindFilter !== "all"
      ? renderEmptyState("没有匹配的 session", "试着清空检索词，或切回更宽的 lens。", "清空筛选")
      : renderEmptyState("还没有可展示的数据", "确认 `~/.codex/sessions` 下存在 `.jsonl` 会话文件。");

    elements.sessionsList.innerHTML = emptyText;
    return;
  }

  elements.sessionsList.innerHTML = groups
    .map(
      (group) => `
        <section class="day-group">
          <div class="day-group-header">
            <h3>${escapeHtml(group.day)}</h3>
            <span>${group.sessions.length} sessions</span>
          </div>
          <div class="session-cards">
            ${group.sessions
              .map((session) => {
                const selectedClass =
                  session.id === state.selectedSessionId ? "session-card selected" : "session-card";
                const signals = renderSignalRow(getSessionSignals(session));

                return `
                  <button class="${selectedClass}" data-session-id="${escapeHtml(session.id)}" title="${escapeHtml(session.latestPrompt || session.firstPrompt || session.id)}">
                    <div class="session-topline">
                      <strong>${escapeHtml(session.model || "unknown model")}</strong>
                      <span>${escapeHtml(formatDateTime(session.timestamp))}</span>
                    </div>
                    <p class="session-title">${escapeHtml(shortText(session.latestPrompt, 96))}</p>
                    <p class="session-subtitle">${escapeHtml(compactPath(session.cwd || session.filePath))}</p>
                    <p class="session-subtitle session-subtitle-secondary">${escapeHtml(
                      `${getSessionSourceLabel(session)} · ${getSessionRepoLabel(session)} · ${getSessionBranchLabel(session)}`,
                    )}</p>
                    ${signals}
                    <div class="session-metrics">
                      <span>${formatNumber(getSessionTurnCount(session))} turns</span>
                      <span>${formatNumber(getSessionToolCount(session))} tools</span>
                      <span>peak ${formatNumber(getSessionPeakTokens(session))}</span>
                    </div>
                  </button>
                `;
              })
              .join("")}
          </div>
        </section>
      `,
    )
    .join("");
}

function getSelectedSession() {
  return state.sessionDetails.get(state.selectedSessionId) ?? null;
}

function getMessageRoleMeta(record) {
  if (record.kind === "user") {
    return { label: "User", tone: "human" };
  }
  if (record.kind === "assistant") {
    return { label: "Assistant", tone: "accent" };
  }
  if (record.kind === "memory") {
    return { label: "Memory", tone: "warning" };
  }
  if (record.kind === "runtime") {
    return { label: "Runtime", tone: "warning" };
  }
  if (record.kind === "developer") {
    return { label: "Developer", tone: "muted" };
  }
  if (record.kind === "injected-user") {
    return { label: "Injected User", tone: "muted" };
  }
  if (record.kind === "tool-call") {
    return { label: "Tool Call", tone: "info" };
  }
  if (record.kind === "tool-output") {
    return { label: "Tool Output", tone: "info" };
  }
  if (record.kind === "commentary") {
    return { label: "Progress", tone: "info" };
  }
  if (record.kind === "error") {
    return { label: "Error", tone: "danger" };
  }
  return { label: "Message", tone: "muted" };
}

function buildSessionMessages(session) {
  if (session.messageFeed) {
    return session.messageFeed;
  }

  const messages = [];
  let sequence = 0;

  session.turns.forEach((turn) => {
    turn.rawRecords.forEach((record, recordIndex) => {
      const base = {
        id: `${turn.id}:${recordIndex}`,
        turnId: turn.id,
        turnIndex: turn.index,
        timestamp: record.timestamp,
        sequence: sequence + 1,
      };

      const payload = record.payload ?? {};

      if (record.type === "event_msg" && payload.type === "error") {
        const text = (payload.message || "unknown error").trim();
        messages.push({
          ...base,
          kind: "error",
          role: "system",
          title: "Error",
          text,
          preview: shortText(text, 108),
          code: payload.codex_error_info || null,
        });
        sequence += 1;
        return;
      }

      if (record.type !== "response_item") {
        return;
      }

      if (payload.type === "message") {
        const text = extractTextFromContent(payload.content).trim();
        if (!text) {
          return;
        }

        let kind = payload.role;
        if (payload.role === "user" && looksLikeInjectedUserMessage(text)) {
          kind = "injected-user";
        }
        if (payload.role === "developer") {
          kind = classifyDeveloperText(text);
        }

        messages.push({
          ...base,
          kind,
          role: payload.role,
          title: getMessageRoleMeta({ kind }).label,
          text,
          preview: shortText(text, 108),
        });
        sequence += 1;
        return;
      }

      if (payload.type === "function_call") {
        const text = payload.arguments ?? "";
        messages.push({
          ...base,
          kind: "tool-call",
          role: "tool",
          title: payload.name ? `Tool Call · ${payload.name}` : "Tool Call",
          name: payload.name ?? "unknown",
          callId: payload.call_id ?? null,
          text,
          preview: payload.name
            ? `${payload.name} · ${shortText(text || "(no arguments)", 76)}`
            : shortText(text || "(no arguments)", 92),
        });
        sequence += 1;
        return;
      }

      if (payload.type === "custom_tool_call") {
        const text = serializeValue(payload.input ?? "");
        messages.push({
          ...base,
          kind: "tool-call",
          role: "tool",
          title: payload.name ? `Tool Call · ${payload.name}` : "Tool Call",
          name: payload.name ?? "custom_tool",
          callId: payload.call_id ?? null,
          text,
          preview: payload.name
            ? `${payload.name} · ${shortText(text || "(no input)", 76)}`
            : shortText(text || "(no input)", 92),
        });
        sequence += 1;
        return;
      }

      if (payload.type === "function_call_output") {
        const text = serializeValue(payload.output);
        messages.push({
          ...base,
          kind: "tool-output",
          role: "tool",
          title: payload.call_id ? `Tool Output · ${payload.call_id}` : "Tool Output",
          callId: payload.call_id ?? null,
          text,
          preview: shortText(text || "(empty output)", 92),
        });
        sequence += 1;
        return;
      }

      if (payload.type === "custom_tool_call_output") {
        const text = serializeValue(payload.output);
        messages.push({
          ...base,
          kind: "tool-output",
          role: "tool",
          title: payload.call_id ? `Tool Output · ${payload.call_id}` : "Tool Output",
          callId: payload.call_id ?? null,
          text,
          preview: shortText(text || "(empty output)", 92),
        });
        sequence += 1;
      }
    });
  });

  session.messageFeed = messages;
  return messages;
}

function getSelectedMessage(session) {
  const messages = buildSessionMessages(session);
  return (
    messages.find((item) => item.id === state.selectedMessageId) ??
    messages.at(-1) ??
    null
  );
}

function renderMessages() {
  const session = getSelectedSession();

  if (!session) {
    elements.turnsMeta.textContent = "选择一个 session 后显示消息流。";
    elements.turnsList.innerHTML = renderEmptyState(
      "没有活动 session",
      "左侧列表里选择一个 session，或者调整当前的筛选 lens。",
    );
    return;
  }

  const messages = buildSessionMessages(session);
  elements.turnsMeta.textContent =
    `${compactPath(session.sessionMeta.cwd || session.filePath)} · ` +
    `${messages.length} messages · ${session.turns.length} turns`;

  let lastTurnIndex = null;
  const items = [];

  messages.forEach((message) => {
    if (message.turnIndex !== lastTurnIndex) {
      items.push(`
        <div class="message-divider">
          <span>Turn ${escapeHtml(String(message.turnIndex))}</span>
        </div>
      `);
      lastTurnIndex = message.turnIndex;
    }

    const selectedClass =
      message.id === state.selectedMessageId ? "turn-card message-card selected" : "turn-card message-card";
    const roleMeta = getMessageRoleMeta(message);

    items.push(`
      <button class="${selectedClass} role-${escapeHtml(message.kind)}" data-message-id="${escapeHtml(message.id)}" title="${escapeHtml(message.text || message.preview || "")}">
        <div class="turn-card-topline">
          <strong>${escapeHtml(roleMeta.label)}</strong>
          <span>#${escapeHtml(String(message.sequence))}</span>
          <span>${escapeHtml(formatDateTime(message.timestamp))}</span>
        </div>
        <p class="turn-title">${escapeHtml(message.preview || "(empty message)")}</p>
        <div class="turn-badges">
          <span>${escapeHtml(message.role || "message")}</span>
          <span>Turn ${escapeHtml(String(message.turnIndex))}</span>
          ${message.name ? `<span>${escapeHtml(message.name)}</span>` : ""}
          ${message.callId ? `<span>${escapeHtml(message.callId)}</span>` : ""}
        </div>
      </button>
    `);
  });

  elements.turnsList.innerHTML = `
    <div class="message-list">
      ${items.join("")}
    </div>
  `;
}

function renderKeyValueList(entries) {
  return `
    <dl class="key-value-grid">
      ${entries
        .map(
          ([label, value]) => `
            <div>
              <dt>${escapeHtml(label)}</dt>
              <dd>${escapeHtml(value ?? "—")}</dd>
            </div>
          `,
        )
        .join("")}
    </dl>
  `;
}

function getTextMeta(value) {
  const text = value || "";
  const lines = text ? text.split("\n").length : 0;
  return `${formatNumber(lines)} lines · ${formatNumber(text.length)} chars`;
}

function truncateTextBlock(value, { maxLines = 10, maxChars = 900 } = {}) {
  const text = value || "";
  if (!text) {
    return { preview: "(empty)", isTruncated: false };
  }

  const lines = text.split("\n");
  let preview = text;
  let isTruncated = false;

  if (lines.length > maxLines) {
    preview = lines.slice(0, maxLines).join("\n");
    isTruncated = true;
  }

  if (preview.length > maxChars) {
    preview = `${preview.slice(0, maxChars).trimEnd()}\n…`;
    isTruncated = true;
  } else if (isTruncated) {
    preview = `${preview.trimEnd()}\n…`;
  }

  return { preview, isTruncated };
}

function renderTextBlock(
  value,
  {
    id = "",
    className = "copy-block",
    maxLines = 10,
    maxChars = 900,
    summaryLabel = "展开完整内容",
    titleOnPreview = false,
  } = {},
) {
  const text = value || "";
  const { preview, isTruncated } = truncateTextBlock(text, { maxLines, maxChars });
  const titleAttr = titleOnPreview ? ` title="${escapeHtml(text)}"` : "";

  if (!isTruncated) {
    const idAttr = id ? ` id="${id}"` : "";
    return `<pre${idAttr} class="${className}"${titleAttr}>${escapeHtml(text || "(empty)")}</pre>`;
  }

  const idAttr = id ? ` id="${id}"` : "";

  return `
    <div class="expandable-block">
      <pre class="${className} is-preview"${titleAttr}>${escapeHtml(preview)}</pre>
      <details class="expand-toggle">
        <summary>${escapeHtml(summaryLabel)}</summary>
        <pre${idAttr} class="${className} is-full">${escapeHtml(text || "(empty)")}</pre>
      </details>
    </div>
  `;
}

function renderCopyableSection(title, value, options = {}) {
  const {
    tone = "default",
    kicker = "",
    note = "",
    tall = false,
    maxLines = 10,
    maxChars = 900,
    summaryLabel = "展开完整内容",
  } = options;
  const sectionId = `section-${Math.random().toString(36).slice(2)}`;
  const text = value || "";
  const meta = note ? `${getTextMeta(text)} · ${note}` : getTextMeta(text);

  return `
    <section class="detail-section tone-${tone}">
      <div class="detail-section-header">
        <div>
          ${kicker ? `<p class="section-kicker">${escapeHtml(kicker)}</p>` : ""}
          <h3>${escapeHtml(title)}</h3>
          <p class="section-meta">${escapeHtml(meta)}</p>
        </div>
        <button class="ghost-button small" data-copy-target="${sectionId}">复制</button>
      </div>
      ${renderTextBlock(text, {
        id: sectionId,
        className: `copy-block ${tall ? "copy-block-tall" : ""}`.trim(),
        maxLines,
        maxChars,
        summaryLabel,
      })}
    </section>
  `;
}

function renderDetailSection(title, bodyHtml, options = {}) {
  const { tone = "default", kicker = "", meta = "" } = options;

  return `
    <section class="detail-section tone-${tone}">
      <div class="detail-section-header">
        <div>
          ${kicker ? `<p class="section-kicker">${escapeHtml(kicker)}</p>` : ""}
          <h3>${escapeHtml(title)}</h3>
          ${meta ? `<p class="section-meta">${escapeHtml(meta)}</p>` : ""}
        </div>
      </div>
      <div class="detail-section-body">
        ${bodyHtml}
      </div>
    </section>
  `;
}

function renderToggleSection(title, bodyHtml, options = {}) {
  const { tone = "default", kicker = "", meta = "", open = false } = options;

  return `
    <details class="detail-section detail-toggle tone-${tone}" ${open ? "open" : ""}>
      <summary class="detail-section-header detail-toggle-summary">
        <div>
          ${kicker ? `<p class="section-kicker">${escapeHtml(kicker)}</p>` : ""}
          <h3>${escapeHtml(title)}</h3>
          ${meta ? `<p class="section-meta">${escapeHtml(meta)}</p>` : ""}
        </div>
        <span class="detail-toggle-label">更多</span>
      </summary>
      <div class="detail-toggle-body">
        ${bodyHtml}
      </div>
    </details>
  `;
}

function renderTimelineList(items, renderer, emptyLabel = "无") {
  if (!items.length) {
    return `<p class="empty-note">${escapeHtml(emptyLabel)}</p>`;
  }

  return `
    <div class="timeline-list">
      ${items.map(renderer).join("")}
    </div>
  `;
}

function renderMessageThread(messages, selectedMessageId) {
  if (!messages.length) {
    return `<p class="empty-note">当前 session 没有可展示的消息。</p>`;
  }

  return `
    <div class="message-thread">
      ${messages
        .map((message) => {
          const roleMeta = getMessageRoleMeta(message);
          const className =
            message.id === selectedMessageId
              ? `thread-bubble selected role-${message.kind}`
              : `thread-bubble role-${message.kind}`;

          return `
            <article class="${className}">
              <div class="thread-meta">
                <strong>${escapeHtml(roleMeta.label)}</strong>
                <span>#${escapeHtml(String(message.sequence))}</span>
                <span>Turn ${escapeHtml(String(message.turnIndex))}</span>
                <span>${escapeHtml(formatDateTime(message.timestamp))}</span>
              </div>
              ${renderTextBlock(message.text || "(empty message)", {
                className: "thread-text",
                maxLines: 6,
                maxChars: 560,
                summaryLabel: "展开这条消息",
                titleOnPreview: true,
              })}
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderMessageDetail() {
  const session = getSelectedSession();

  if (!session) {
    elements.detailMeta.textContent = "选择一条 message 查看细节。";
    elements.turnDetail.innerHTML = renderEmptyState(
      "没有活动 message",
      "当前筛选 lens 下没有可展示的消息。",
    );
    return;
  }

  const selectedMessage = getSelectedMessage(session);
  const turn = session.turns.find((item) => item.id === selectedMessage?.turnId) ?? session.turns.at(-1);
  const sessionMessages = buildSessionMessages(session);

  if (!selectedMessage || !turn) {
    elements.detailMeta.textContent = "当前 session 没有消息数据。";
    elements.turnDetail.innerHTML = renderEmptyState(
      "没有消息数据",
      "该 session 文件没有可解析的消息记录。",
    );
    return;
  }

  const selectedPosition = sessionMessages.findIndex((item) => item.id === selectedMessage.id) + 1;
  elements.detailMeta.textContent =
    `Session ${session.id} · Message ${selectedPosition}/${sessionMessages.length} · ${getMessageRoleMeta(selectedMessage).label}`;

  const sessionSignals = getSessionSignals(session);
  const turnSignals = getTurnSignals(turn);
  const roleMeta = getMessageRoleMeta(selectedMessage);
  const humanPrompt = turn.humanUserMessages.map((item) => item.text).join("\n\n");
  const injectedPrompt = [...turn.memoryMessages, ...turn.runtimeDeveloperMessages]
    .map((item) => item.text)
    .join("\n\n---\n\n");
  const assistantTranscript = turn.assistantMessages.map((message) => message.text).join("\n\n---\n\n");
  const turnMessages = sessionMessages.filter((message) => message.turnId === turn.id);

  const sessionFacts = [
    ["Session ID", session.id],
    ["Message ID", selectedMessage.id],
    ["Message Position", `${selectedPosition} / ${sessionMessages.length}`],
    ["Turn ID", turn.id],
    ["Selected Role", roleMeta.label],
    ["Source", getSessionSourceLabel(session)],
    ["Model", turn.turnContextCompact?.model || "unknown"],
    ["Effort", turn.turnContextCompact?.effort || "unknown"],
    ["Approval", turn.turnContextCompact?.approvalPolicy || "unknown"],
    ["Repo", getSessionRepoLabel(session)],
    ["Branch", getSessionBranchLabel(session)],
    ["Commit", getSessionGit(session).commitShort || "unknown"],
    ["Started", formatDateTime(turn.startedAt)],
    ["Finished", formatDateTime(turn.finishedAt)],
    ["Turns in Session", String(session.turns.length)],
    ["Session Path", compactPath(session.filePath)],
    ["CWD", compactPath(session.sessionMeta.cwd || session.filePath)],
  ];

  const tokenEntries = [
    ["Input Tokens", formatNumber(turn.lastTokenSnapshot?.inputTokens ?? 0)],
    ["Cached Input", formatNumber(turn.lastTokenSnapshot?.cachedInputTokens ?? 0)],
    ["Output Tokens", formatNumber(turn.lastTokenSnapshot?.outputTokens ?? 0)],
    ["Reasoning Tokens", formatNumber(turn.lastTokenSnapshot?.reasoningTokens ?? 0)],
    ["Tool Calls", String(turn.toolCalls.length)],
    ["Tool Outputs", String(turn.toolOutputs.length)],
    ["Approvals", String(turn.approvalTrace?.count ?? 0)],
    ["Compactions", String(turn.turnMechanics?.contextCompactedCount ?? 0)],
    ["Memory Injects", String(turn.memoryMessages.length)],
    ["Runtime Injects", String(turn.runtimeDeveloperMessages.length)],
    ["Reasoning Items", String(turn.reasoningCount)],
  ];

  elements.turnDetail.innerHTML = `
    <section class="hero-card">
      <div class="hero-copy">
        <p class="eyebrow">Selected Message</p>
        <h3>${escapeHtml(selectedMessage.preview || "(empty message)")}</h3>
        <p class="hero-summary">
          先按时间顺序查看整场 session 的消息流，再落到当前选中的单条 message。下方保留该
          message 所属 turn 的 prompt packet，继续做输入取证。
        </p>
        ${renderSignalRow([
          { tone: roleMeta.tone, label: roleMeta.label },
          ...sessionSignals,
          ...turnSignals,
        ])}
      </div>
      <div class="hero-metrics">
        ${renderKeyValueList(tokenEntries)}
      </div>
    </section>

    <section class="detail-grid">
      ${renderCopyableSection("Selected Message", selectedMessage.text, {
        tone: roleMeta.tone,
        kicker: `${roleMeta.label} · Message ${selectedPosition}`,
        note: "当前选中的完整消息正文。",
        maxLines: 12,
        maxChars: 1200,
        summaryLabel: "展开完整消息",
      })}
      ${renderDetailSection(
        "Parent Turn Thread",
        renderMessageThread(turnMessages, selectedMessage.id),
        {
          tone: "default",
          kicker: "Turn-local transcript",
          meta: `${formatNumber(turnMessages.length)} messages in turn ${turn.index}`,
        },
      )}
    </section>

    <section class="detail-grid">
      ${renderProvenanceSection(session, turn)}
      ${renderSkillTraceSection(turn)}
    </section>

    <section class="detail-grid">
      ${renderPromptEnvelopeSection(turn)}
      ${renderTurnMechanicsSection(turn)}
    </section>

    <section class="detail-grid">
      ${renderCompactionTraceSection(turn)}
      ${renderProgressTraceSection(turn)}
      ${renderApprovalTraceSection(turn)}
    </section>

    <section class="detail-grid">
      ${renderCopyableSection("Human Prompt", humanPrompt, {
        tone: "human",
        kicker: "Prompt inputs · human payload",
        note: "所属 turn 里的人类输入。",
        maxLines: 8,
        maxChars: 880,
        summaryLabel: "展开完整 human prompt",
      })}
      ${renderCopyableSection("Memory / Runtime Prompt", injectedPrompt, {
        tone: "warning",
        kicker: "Prompt inputs · injected context",
        note: "所属 turn 的 memory/runtime/developer 注入。",
        maxLines: 8,
        maxChars: 880,
        summaryLabel: "展开注入上下文",
      })}
      ${renderCopyableSection("User Instructions", turn.turnContext?.user_instructions || turn.injectedUserMessages.map((item) => item.text).join("\n\n"), {
        tone: "default",
        kicker: "Prompt inputs · user instructions",
        note: "来自 turn_context 或注入的用户级约束。",
        maxLines: 8,
        maxChars: 880,
        summaryLabel: "展开用户指令",
      })}
      ${renderCopyableSection("Base Instructions", session.sessionMeta.baseInstructions, {
        tone: "default",
        kicker: "Prompt inputs · session root policy",
        note: "整场 session 的底层基准指令。",
        maxLines: 8,
        maxChars: 880,
        summaryLabel: "展开基础指令",
      })}
    </section>

    ${renderCopyableSection("Approx Full Prompt", turn.approxPrompt, {
      tone: "accent",
      kicker: "Reconstructed model input",
      note: "按 base instructions → context → prior turns → current user messages 近似重建。",
      tall: true,
      maxLines: 14,
      maxChars: 1800,
      summaryLabel: "展开完整 reconstructed prompt",
    })}

    ${renderToggleSection(
      "Execution Trail",
      `
        <div class="detail-grid">
          <section class="detail-section tone-info">
            <div class="detail-section-header">
              <div>
                <p class="section-kicker">Execution trail</p>
                <h3>Tool Calls</h3>
                <p class="section-meta">${turn.toolCalls.length} items</p>
              </div>
            </div>
            ${renderTimelineList(
              turn.toolCalls,
              (item) => `
                <article class="timeline-card">
                  <div class="timeline-meta">
                    <strong>${escapeHtml(item.name)}</strong>
                    <span>${escapeHtml(formatDateTime(item.timestamp))}</span>
                  </div>
                  ${renderTextBlock(item.argumentsText || "(no arguments)", {
                    className: "timeline-block",
                    maxLines: 7,
                    maxChars: 720,
                    summaryLabel: "展开调用参数",
                  })}
                </article>
              `,
              "这轮没有 tool call。",
            )}
          </section>

          <section class="detail-section tone-info">
            <div class="detail-section-header">
              <div>
                <p class="section-kicker">Execution trail</p>
                <h3>Tool Outputs</h3>
                <p class="section-meta">${turn.toolOutputs.length} items</p>
              </div>
            </div>
            ${renderTimelineList(
              turn.toolOutputs,
              (item) => `
                <article class="timeline-card">
                  <div class="timeline-meta">
                    <strong>${escapeHtml(item.callId || "output")}</strong>
                    <span>${escapeHtml(formatDateTime(item.timestamp))}</span>
                  </div>
                  ${renderTextBlock(item.outputText, {
                    className: "timeline-block",
                    maxLines: 7,
                    maxChars: 720,
                    summaryLabel: "展开工具输出",
                  })}
                </article>
              `,
              "这轮没有 tool output。",
            )}
          </section>
        </div>
      `,
      {
        tone: "info",
        kicker: "Secondary context",
        meta: `${turn.toolCalls.length} calls · ${turn.toolOutputs.length} outputs`,
      },
    )}

    ${renderToggleSection(
      "Advanced Diagnostics",
      `
        <div class="detail-grid">
          ${renderCopyableSection("Conversation Carry-over", turn.historyTranscript, {
            tone: "default",
            kicker: "Prior turns",
            note: "这一轮带入模型上下文的历史对话摘要。",
            maxLines: 8,
            maxChars: 860,
            summaryLabel: "展开历史摘要",
          })}
          ${renderCopyableSection("Assistant Messages", assistantTranscript, {
            tone: "default",
            kicker: "Visible assistant output",
            note: "当前 turn 内 assistant 的消息序列。",
            maxLines: 8,
            maxChars: 860,
            summaryLabel: "展开 assistant messages",
          })}
          ${renderCopyableSection("Turn Context", JSON.stringify(turn.turnContext, null, 2), {
            tone: "muted",
            kicker: "Raw context payload",
            note: "原始 turn_context，包含 sandbox、approval、cwd、日期等环境字段。",
            maxLines: 8,
            maxChars: 860,
            summaryLabel: "展开 turn_context",
          })}
          ${renderCopyableSection("Session Facts", JSON.stringify(Object.fromEntries(sessionFacts), null, 2), {
            tone: "muted",
            kicker: "Reference facts",
            note: "用于定位 turn、session 和文件来源。",
            maxLines: 8,
            maxChars: 860,
            summaryLabel: "展开 session facts",
          })}
        </div>
        <div class="detail-grid">
          ${renderCopyableSection("Raw Turn Records", JSON.stringify(turn.rawRecords, null, 2), {
            tone: "muted",
            kicker: "Low-level diagnostics",
            note: "保留原始事件记录，便于核对解析器是否遗漏信息。",
            maxLines: 8,
            maxChars: 860,
            summaryLabel: "展开 raw records",
          })}
        </div>
      `,
      {
        tone: "muted",
        kicker: "Low-level data",
        meta: "默认折叠，避免打断主线阅读。",
      },
    )}
  `;
}

async function handleRefresh() {
  elements.healthBadge.textContent = "Refreshing…";
  state.sessionDetails.clear();
  await loadHealth();
  await loadSummaries({ preserveSelection: false });
}

async function resetLens() {
  state.searchTerm = "";
  state.kindFilter = "all";
  elements.searchInput.value = "";
  await syncSelectionToLens();
}

function attachEvents() {
  elements.refreshButton.addEventListener("click", () => {
    handleRefresh().catch((error) => {
      console.error(error);
      elements.healthBadge.textContent = "Error";
    });
  });

  elements.searchInput.addEventListener("input", async (event) => {
    state.searchTerm = event.target.value;
    try {
      await syncSelectionToLens();
    } catch (error) {
      console.error(error);
      elements.healthBadge.textContent = "Error";
    }
  });

  elements.kindFilters.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-filter-id]");
    if (!button) {
      return;
    }

    state.kindFilter = button.dataset.filterId || "all";
    try {
      await syncSelectionToLens();
    } catch (error) {
      console.error(error);
      elements.healthBadge.textContent = "Error";
    }
  });

  elements.sessionsList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-session-id]");
    if (!button) {
      return;
    }

    loadSessionDetail(button.dataset.sessionId, { preserveMessage: false }).catch((error) => {
      console.error(error);
      elements.healthBadge.textContent = "Error";
    });
  });

  elements.turnsList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-message-id]");
    if (!button) {
      return;
    }

    state.selectedMessageId = button.dataset.messageId;
    renderMessages();
    renderMessageDetail();
  });

  document.body.addEventListener("click", async (event) => {
    const copyButton = event.target.closest("[data-copy-target]");
    if (copyButton) {
      const target = document.getElementById(copyButton.dataset.copyTarget);
      if (target) {
        await navigator.clipboard.writeText(target.textContent || "");
        copyButton.textContent = "已复制";
        setTimeout(() => {
          copyButton.textContent = "复制";
        }, 1200);
      }
      return;
    }

    const resetButton = event.target.closest("[data-reset-lens]");
    if (resetButton) {
      await resetLens();
    }
  });
}

async function bootstrap() {
  try {
    await loadHealth();
    await loadSummaries({ preserveSelection: false });
    elements.healthBadge.textContent = "Ready";
  } catch (error) {
    console.error(error);
    elements.healthBadge.textContent = "Failed";
    elements.turnDetail.innerHTML = renderEmptyState(
      "加载失败",
      error instanceof Error ? error.message : String(error),
    );
  }
}

attachEvents();
bootstrap();
