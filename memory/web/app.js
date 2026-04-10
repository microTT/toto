const SCOPE_FILTERS = [
  { id: "all", label: "All" },
  { id: "global_long_term", label: "Global" },
  { id: "local_recent", label: "Recent" },
  { id: "local_archive", label: "Archive" },
];

const STATUS_FILTERS = [
  { id: "all", label: "All" },
  { id: "open", label: "Open" },
  { id: "active", label: "Active" },
  { id: "closed", label: "Closed" },
  { id: "superseded", label: "Superseded" },
  { id: "deleted", label: "Deleted" },
];

const state = {
  workspaceIndex: null,
  workspaceDetails: new Map(),
  selectedWorkspaceId: null,
  selectedRecordId: null,
  searchTerm: "",
  scopeFilter: "all",
  statusFilter: "all",
};

const elements = {
  healthBadge: document.querySelector("#healthBadge"),
  workspaceCount: document.querySelector("#workspaceCount"),
  visibleCount: document.querySelector("#visibleCount"),
  currentWorkspace: document.querySelector("#currentWorkspace"),
  memoryHomeParent: document.querySelector("#memoryHomeParent"),
  refreshButton: document.querySelector("#refreshButton"),
  searchInput: document.querySelector("#searchInput"),
  scopeFilters: document.querySelector("#scopeFilters"),
  statusFilters: document.querySelector("#statusFilters"),
  workspaceList: document.querySelector("#workspaceList"),
  recordList: document.querySelector("#recordList"),
  recordsMeta: document.querySelector("#recordsMeta"),
  detailPane: document.querySelector("#detailPane"),
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

function formatDateTime(value) {
  if (!value) {
    return "—";
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

function compactPath(value) {
  if (!value) {
    return "—";
  }
  return String(value).replace(/^\/Users\/[^/]+/, "~");
}

function shortText(value, limit = 160) {
  const compact = String(value || "").replace(/\s+/g, " ").trim();
  if (compact.length <= limit) {
    return compact;
  }
  return `${compact.slice(0, limit - 1)}…`;
}

function formatCount(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(errorText || `Request failed: ${response.status}`);
  }
  return response.json();
}

function renderScopeFilters() {
  renderFilterRow(elements.scopeFilters, SCOPE_FILTERS, state.scopeFilter, (filterId) => {
    state.scopeFilter = filterId;
    renderScopeFilters();
    renderRecordList();
    renderDetailPane();
  });
}

function renderStatusFilters() {
  renderFilterRow(elements.statusFilters, STATUS_FILTERS, state.statusFilter, (filterId) => {
    state.statusFilter = filterId;
    renderStatusFilters();
    renderRecordList();
    renderDetailPane();
  });
}

function renderFilterRow(container, filters, activeId, onSelect) {
  container.innerHTML = filters
    .map(
      (filter) => `
        <button
          type="button"
          class="chip-button ${filter.id === activeId ? "is-active" : ""}"
          data-filter-id="${escapeHtml(filter.id)}"
        >
          ${escapeHtml(filter.label)}
        </button>
      `,
    )
    .join("");

  container.querySelectorAll("[data-filter-id]").forEach((button) => {
    button.addEventListener("click", () => onSelect(button.dataset.filterId));
  });
}

function getSelectedWorkspace() {
  if (!state.workspaceIndex || !state.selectedWorkspaceId) {
    return null;
  }
  return state.workspaceIndex.workspaces.find(
    (workspace) => workspace.workspace_instance_id === state.selectedWorkspaceId,
  );
}

function getSelectedWorkspaceDetail() {
  if (!state.selectedWorkspaceId) {
    return null;
  }
  return state.workspaceDetails.get(state.selectedWorkspaceId) || null;
}

function getFilteredRecords() {
  const detail = getSelectedWorkspaceDetail();
  if (!detail) {
    return [];
  }

  const needle = state.searchTerm.trim().toLowerCase();
  return detail.records.filter((record) => {
    if (state.scopeFilter !== "all" && record.scope !== state.scopeFilter) {
      return false;
    }
    if (state.statusFilter !== "all" && record.status !== state.statusFilter) {
      return false;
    }
    if (!needle) {
      return true;
    }
    const corpus = [
      record.subject,
      record.summary,
      record.rationale,
      record.next_use,
      ...(record.tags || []),
      ...(record.source_refs || []),
    ]
      .filter(Boolean)
      .join("\n")
      .toLowerCase();
    return corpus.includes(needle);
  });
}

function renderWorkspaceList() {
  if (!state.workspaceIndex?.workspaces?.length) {
    elements.workspaceList.innerHTML = elements.emptyStateTemplate.innerHTML;
    return;
  }

  elements.workspaceList.innerHTML = state.workspaceIndex.workspaces
    .map((workspace) => {
      const isSelected = workspace.workspace_instance_id === state.selectedWorkspaceId;
      const counts = workspace.counts || {};
      return `
        <button
          type="button"
          class="workspace-card ${isSelected ? "is-selected" : ""}"
          data-workspace-id="${escapeHtml(workspace.workspace_instance_id)}"
        >
          <div class="workspace-card-head">
            <div>
              <h3>${escapeHtml(workspace.label)}</h3>
              <p>${escapeHtml(compactPath(workspace.workspace_root))}</p>
            </div>
            ${workspace.is_current ? '<span class="mini-pill tone-current">Current</span>' : ""}
          </div>
          <div class="metric-row">
            <span class="metric-pill">G ${formatCount(counts.global)}</span>
            <span class="metric-pill">R ${formatCount(counts.recent)}</span>
            <span class="metric-pill">A ${formatCount(counts.archive)}</span>
          </div>
          <div class="workspace-meta">
            <span>Last record ${escapeHtml(formatDateTime(workspace.latest_record_at))}</span>
            <span>Snapshot ${escapeHtml(formatDateTime(workspace.latest_snapshot_at))}</span>
          </div>
        </button>
      `;
    })
    .join("");

  elements.workspaceList.querySelectorAll("[data-workspace-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      await selectWorkspace(button.dataset.workspaceId);
    });
  });
}

function renderRecordList() {
  const workspace = getSelectedWorkspace();
  const records = getFilteredRecords();
  elements.visibleCount.textContent = formatCount(records.length);

  if (!workspace) {
    elements.recordList.innerHTML = elements.emptyStateTemplate.innerHTML;
    elements.recordsMeta.textContent = "选择 workspace 后显示所有记忆记录。";
    return;
  }

  elements.recordsMeta.textContent = `${workspace.label} · ${formatCount(records.length)} 条可见记录`;

  if (!records.length) {
    elements.recordList.innerHTML = elements.emptyStateTemplate.innerHTML;
    return;
  }

  elements.recordList.innerHTML = records
    .map((record) => {
      const isSelected = record.id === state.selectedRecordId;
      return `
        <button
          type="button"
          class="record-card ${isSelected ? "is-selected" : ""}"
          data-record-id="${escapeHtml(record.id)}"
        >
          <div class="record-card-head">
            <div class="badge-row">
              <span class="mini-pill tone-scope">${escapeHtml(record.scope.replace("local_", "").replace("_long_term", ""))}</span>
              <span class="mini-pill tone-status">${escapeHtml(record.status)}</span>
              <span class="mini-pill tone-type">${escapeHtml(record.type)}</span>
            </div>
            <span class="record-date">${escapeHtml(formatDateTime(record.updated_at || record.created_at))}</span>
          </div>
          <h3>${escapeHtml(record.subject)}</h3>
          <p>${escapeHtml(shortText(record.summary, 220))}</p>
          <div class="tag-row">
            ${(record.tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
          </div>
        </button>
      `;
    })
    .join("");

  elements.recordList.querySelectorAll("[data-record-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedRecordId = button.dataset.recordId;
      renderRecordList();
      renderDetailPane();
    });
  });
}

function renderSnapshot(snapshot) {
  const globalCount = Array.isArray(snapshot?.global_records) ? snapshot.global_records.length : 0;
  const localCount = Array.isArray(snapshot?.local_records) ? snapshot.local_records.length : 0;
  return `
    <section class="detail-section">
      <div class="detail-section-head">
        <h3>Latest Snapshot</h3>
        <span class="mini-pill tone-current">${escapeHtml(snapshot?.source || "runtime")}</span>
      </div>
      <div class="snapshot-grid">
        <div class="snapshot-card">
          <span>Built At</span>
          <strong>${escapeHtml(formatDateTime(snapshot?.built_at))}</strong>
        </div>
        <div class="snapshot-card">
          <span>Global</span>
          <strong>${formatCount(globalCount)}</strong>
        </div>
        <div class="snapshot-card">
          <span>Local</span>
          <strong>${formatCount(localCount)}</strong>
        </div>
        <div class="snapshot-card">
          <span>Session</span>
          <strong>${escapeHtml(snapshot?.session_id || "rebuilt")}</strong>
        </div>
      </div>
      <pre class="code-block">${escapeHtml(snapshot?.rendered_text || "")}</pre>
    </section>
  `;
}

function renderRecordDetail(record) {
  return `
    <section class="detail-section">
      <div class="detail-section-head">
        <h3>${escapeHtml(record.subject)}</h3>
        <div class="badge-row">
          <span class="mini-pill tone-scope">${escapeHtml(record.scope)}</span>
          <span class="mini-pill tone-status">${escapeHtml(record.status)}</span>
          <span class="mini-pill tone-type">${escapeHtml(record.type)}</span>
        </div>
      </div>
      <dl class="detail-grid">
        <div><dt>ID</dt><dd>${escapeHtml(record.id)}</dd></div>
        <div><dt>Section</dt><dd>${escapeHtml(record.section)}</dd></div>
        <div><dt>Updated</dt><dd>${escapeHtml(formatDateTime(record.updated_at))}</dd></div>
        <div><dt>Created</dt><dd>${escapeHtml(formatDateTime(record.created_at))}</dd></div>
        <div><dt>Workspace</dt><dd>${escapeHtml(compactPath(record.workspace_root))}</dd></div>
        <div><dt>Source File</dt><dd>${escapeHtml(compactPath(record.path))}</dd></div>
      </dl>
      <div class="rich-block">
        <h4>Summary</h4>
        <p>${escapeHtml(record.summary || "—")}</p>
      </div>
      <div class="rich-block">
        <h4>Rationale</h4>
        <p>${escapeHtml(record.rationale || "—")}</p>
      </div>
      <div class="rich-block">
        <h4>Next Use</h4>
        <p>${escapeHtml(record.next_use || "—")}</p>
      </div>
      <div class="rich-block">
        <h4>Tags</h4>
        <div class="tag-row">
          ${(record.tags || []).length ? record.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("") : "<span>—</span>"}
        </div>
      </div>
      <div class="rich-block">
        <h4>Source Refs</h4>
        <pre class="code-block">${escapeHtml((record.source_refs || []).join("\n") || "—")}</pre>
      </div>
    </section>
  `;
}

function renderDetailPane() {
  const detail = getSelectedWorkspaceDetail();
  const records = getFilteredRecords();
  const selectedRecord = records.find((record) => record.id === state.selectedRecordId) || null;
  const workspace = getSelectedWorkspace();

  if (!detail || !workspace) {
    elements.detailPane.innerHTML = elements.emptyStateTemplate.innerHTML;
    elements.detailMeta.textContent = "选择一条记忆查看完整字段与当前 snapshot。";
    return;
  }

  elements.detailMeta.textContent = selectedRecord
    ? `${workspace.label} · ${selectedRecord.id}`
    : `${workspace.label} · 当前 snapshot`;

  elements.detailPane.innerHTML = `
    ${renderSnapshot(detail.snapshot)}
    ${selectedRecord ? renderRecordDetail(selectedRecord) : ""}
  `;
}

async function loadWorkspaceIndex() {
  const payload = await fetchJson("/api/workspaces");
  state.workspaceIndex = payload;
  if (!state.selectedWorkspaceId) {
    state.selectedWorkspaceId = payload.current_workspace_id;
  }
  elements.workspaceCount.textContent = formatCount(payload.workspace_count);
  elements.memoryHomeParent.textContent = compactPath(payload.memory_home_parent);
  const current = payload.workspaces.find(
    (workspace) => workspace.workspace_instance_id === payload.current_workspace_id,
  );
  elements.currentWorkspace.textContent = current ? current.label : "—";
  renderWorkspaceList();
}

async function selectWorkspace(workspaceId) {
  state.selectedWorkspaceId = workspaceId;
  if (!state.workspaceDetails.has(workspaceId)) {
    const detail = await fetchJson(`/api/workspaces/${encodeURIComponent(workspaceId)}`);
    state.workspaceDetails.set(workspaceId, detail);
  }
  const currentDetail = state.workspaceDetails.get(workspaceId);
  state.selectedRecordId = currentDetail?.records?.[0]?.id || null;
  renderWorkspaceList();
  renderRecordList();
  renderDetailPane();
}

async function refreshAll() {
  elements.healthBadge.textContent = "Refreshing";
  state.workspaceDetails.clear();
  await loadWorkspaceIndex();
  if (state.selectedWorkspaceId) {
    await selectWorkspace(state.selectedWorkspaceId);
  }
  elements.healthBadge.textContent = "Ready";
}

async function boot() {
  renderScopeFilters();
  renderStatusFilters();

  elements.refreshButton.addEventListener("click", async () => {
    await refreshAll();
  });
  elements.searchInput.addEventListener("input", (event) => {
    state.searchTerm = event.target.value || "";
    renderRecordList();
    renderDetailPane();
  });

  const health = await fetchJson("/api/health");
  elements.healthBadge.textContent = health.ok ? "Healthy" : "Degraded";

  await refreshAll();
}

boot().catch((error) => {
  elements.healthBadge.textContent = "Error";
  elements.detailPane.innerHTML = `
    <section class="detail-section">
      <h3>加载失败</h3>
      <pre class="code-block">${escapeHtml(error.message || String(error))}</pre>
    </section>
  `;
});
