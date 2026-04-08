const SESSION_FILTERS = [
  { id: "all", label: "All" },
  { id: "human-led", label: "Human-led" },
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

      if (record.type !== "response_item") {
        return;
      }

      const payload = record.payload ?? {};

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
      ${bodyHtml}
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
    ["Model", turn.turnContextCompact?.model || "unknown"],
    ["Effort", turn.turnContextCompact?.effort || "unknown"],
    ["Approval", turn.turnContextCompact?.approvalPolicy || "unknown"],
    ["Started", formatDateTime(turn.startedAt)],
    ["Finished", formatDateTime(turn.finishedAt)],
    ["Turns in Session", String(session.turns.length)],
    ["Session Path", compactPath(session.filePath)],
    ["CWD", compactPath(session.sessionMeta.cwd || session.filePath)],
  ];

  const tokenEntries = [
    ["Input Tokens", formatNumber(turn.lastTokenSnapshot?.inputTokens ?? 0)],
    ["Output Tokens", formatNumber(turn.lastTokenSnapshot?.outputTokens ?? 0)],
    ["Reasoning Tokens", formatNumber(turn.lastTokenSnapshot?.reasoningTokens ?? 0)],
    ["Tool Calls", String(turn.toolCalls.length)],
    ["Tool Outputs", String(turn.toolOutputs.length)],
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
