const REFRESH_INTERVAL_MS = 20_000;

const FILTERS = [
  { id: "curated", label: "精选" },
  { id: "all", label: "All" },
  { id: "web", label: "Web UI" },
  { id: "named", label: "Named" },
  { id: "local", label: "仅本机" },
  { id: "unlabeled", label: "未命名" },
];

const state = {
  services: [],
  summary: null,
  searchTerm: "",
  activeFilter: "curated",
  configPath: "",
  hostUser: "",
  generatedAt: "",
  dialogPort: null,
  refreshTimer: null,
};

const elements = {
  refreshButton: document.querySelector("#refreshButton"),
  healthBadge: document.querySelector("#healthBadge"),
  totalCount: document.querySelector("#totalCount"),
  webCount: document.querySelector("#webCount"),
  namedCount: document.querySelector("#namedCount"),
  updatedAt: document.querySelector("#updatedAt"),
  searchInput: document.querySelector("#searchInput"),
  filterChips: document.querySelector("#filterChips"),
  serviceGrid: document.querySelector("#serviceGrid"),
  configPath: document.querySelector("#configPath"),
  hostUser: document.querySelector("#hostUser"),
  aliasDialog: document.querySelector("#aliasDialog"),
  aliasForm: document.querySelector("#aliasForm"),
  dialogTitle: document.querySelector("#dialogTitle"),
  closeDialogButton: document.querySelector("#closeDialogButton"),
  clearAliasButton: document.querySelector("#clearAliasButton"),
  aliasName: document.querySelector("#aliasName"),
  aliasGroup: document.querySelector("#aliasGroup"),
  aliasDescription: document.querySelector("#aliasDescription"),
  aliasTags: document.querySelector("#aliasTags"),
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
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function formatNumber(value) {
  if (!Number.isFinite(value)) {
    return "—";
  }
  return new Intl.NumberFormat("zh-CN").format(value);
}

function filterMatches(service) {
  const query = state.searchTerm.trim().toLowerCase();
  if (!query) {
    return true;
  }

  const haystack = [
    service.displayName,
    service.displayGroup,
    service.description,
    service.command,
    service.port,
    service.bindLabel,
    service.probe?.title,
    ...(service.tags || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  return haystack.includes(query);
}

function filterByLens(service) {
  switch (state.activeFilter) {
    case "curated":
      return Boolean(service.alias?.name || (service.probe?.ok && service.probe?.isPageLike));
    case "web":
      return Boolean(service.probe?.ok && service.probe?.isPageLike);
    case "named":
      return Boolean(service.alias?.name);
    case "local":
      return service.reachability === "仅本机";
    case "unlabeled":
      return !service.alias?.name;
    default:
      return true;
  }
}

function getVisibleServices() {
  return state.services.filter((service) => filterMatches(service) && filterByLens(service));
}

function renderFilters() {
  elements.filterChips.innerHTML = FILTERS.map(
    (filter) => `
      <button
        type="button"
        class="filter-chip ${filter.id === state.activeFilter ? "active" : ""}"
        data-filter="${escapeHtml(filter.id)}"
      >
        ${escapeHtml(filter.label)}
      </button>
    `,
  ).join("");
}

function buildProbeTag(service) {
  if (!service.probe?.isHttp) {
    return '<span class="chip muted">No HTTP</span>';
  }

  return `<span class="chip ${service.probe.ok ? "ok" : ""}">HTTP ${escapeHtml(String(service.probe.status))}</span>`;
}

function buildOpenButton(service) {
  if (!service.probe?.isHttp) {
    return "";
  }

  return `
    <a class="primary-button" href="${escapeHtml(service.probe.url)}" target="_blank" rel="noreferrer">
      打开服务
    </a>
  `;
}

function renderServices() {
  const services = getVisibleServices();

  if (services.length === 0) {
    elements.serviceGrid.innerHTML = "";
    elements.serviceGrid.append(
      elements.emptyStateTemplate.content.cloneNode(true),
    );
    return;
  }

  elements.serviceGrid.innerHTML = services
    .map(
      (service) => `
        <article class="service-card">
          <header class="service-head">
            <div>
              <p class="service-group">${escapeHtml(service.displayGroup)}</p>
              <h2>${escapeHtml(service.displayName)}</h2>
            </div>
            <div class="port-pill">:${escapeHtml(String(service.port))}</div>
          </header>

          <div class="chip-row">
            ${buildProbeTag(service)}
            <span class="chip">${escapeHtml(service.reachability)}</span>
            <span class="chip">${escapeHtml(service.command)}</span>
          </div>

          <p class="service-description">${escapeHtml(service.description)}</p>

          <dl class="service-meta">
            <div>
              <dt>绑定</dt>
              <dd>${escapeHtml(service.bindLabel)}</dd>
            </div>
            <div>
              <dt>进程</dt>
              <dd>${escapeHtml(`${service.command} · pid ${service.pid}`)}</dd>
            </div>
            <div>
              <dt>识别标题</dt>
              <dd>${escapeHtml(service.probe?.title || "—")}</dd>
            </div>
            <div>
              <dt>标签</dt>
              <dd>${escapeHtml((service.tags || []).join(", ") || "—")}</dd>
            </div>
          </dl>

          <footer class="card-actions">
            ${buildOpenButton(service)}
            <button class="ghost-button" type="button" data-edit-port="${escapeHtml(String(service.port))}">
              编辑别名
            </button>
          </footer>
        </article>
      `,
    )
    .join("");
}

function renderSummary() {
  elements.totalCount.textContent = formatNumber(state.summary?.total ?? 0);
  elements.webCount.textContent = formatNumber(state.summary?.web ?? 0);
  elements.namedCount.textContent = formatNumber(state.summary?.named ?? 0);
  elements.updatedAt.textContent = formatDateTime(state.generatedAt);
  elements.configPath.textContent = state.configPath || "—";
  elements.hostUser.textContent = state.hostUser || "—";
}

function renderAll() {
  renderFilters();
  renderSummary();
  renderServices();
}

async function loadServices() {
  elements.healthBadge.textContent = "扫描中";
  const response = await fetch("/api/services", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to load services: ${response.status}`);
  }

  const payload = await response.json();
  state.services = payload.services || [];
  state.summary = payload.summary || {};
  state.configPath = payload.configPath || "";
  state.hostUser = payload.hostUser || "";
  state.generatedAt = payload.generatedAt || "";
  renderAll();
  elements.healthBadge.textContent = `已更新 ${formatDateTime(state.generatedAt)}`;
}

function openAliasDialog(port) {
  const service = state.services.find((item) => item.port === port);
  state.dialogPort = port;
  elements.dialogTitle.textContent = `编辑端口 :${port} 的别名`;
  elements.aliasName.value = service?.alias?.name || "";
  elements.aliasGroup.value = service?.alias?.group || "";
  elements.aliasDescription.value = service?.alias?.description || "";
  elements.aliasTags.value = (service?.alias?.tags || []).join(", ");
  elements.aliasDialog.showModal();
}

async function saveAlias(port, payload) {
  const response = await fetch(`/api/aliases/${port}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Failed to save alias: ${response.status}`);
  }

  await loadServices();
}

function scheduleRefresh() {
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => {
    loadServices().catch((error) => {
      console.error(error);
      elements.healthBadge.textContent = "刷新失败";
    });
  }, REFRESH_INTERVAL_MS);
}

function attachEvents() {
  elements.refreshButton.addEventListener("click", () => {
    loadServices().catch((error) => {
      console.error(error);
      elements.healthBadge.textContent = "刷新失败";
    });
  });

  elements.searchInput.addEventListener("input", (event) => {
    state.searchTerm = event.target.value || "";
    renderServices();
  });

  elements.filterChips.addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) {
      return;
    }
    state.activeFilter = button.dataset.filter || "all";
    renderAll();
  });

  elements.serviceGrid.addEventListener("click", (event) => {
    const button = event.target.closest("[data-edit-port]");
    if (!button) {
      return;
    }
    openAliasDialog(Number(button.dataset.editPort));
  });

  elements.closeDialogButton.addEventListener("click", () => {
    elements.aliasDialog.close();
  });

  elements.clearAliasButton.addEventListener("click", async () => {
    if (state.dialogPort === null) {
      return;
    }
    try {
      await saveAlias(state.dialogPort, {});
      elements.aliasDialog.close();
    } catch (error) {
      console.error(error);
      elements.healthBadge.textContent = "保存失败";
    }
  });

  elements.aliasForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.dialogPort === null) {
      return;
    }

    const payload = {
      name: elements.aliasName.value,
      group: elements.aliasGroup.value,
      description: elements.aliasDescription.value,
      tags: elements.aliasTags.value,
    };

    try {
      await saveAlias(state.dialogPort, payload);
      elements.aliasDialog.close();
    } catch (error) {
      console.error(error);
      elements.healthBadge.textContent = "保存失败";
    }
  });
}

attachEvents();
scheduleRefresh();
loadServices().catch((error) => {
  console.error(error);
  elements.healthBadge.textContent = "加载失败";
});
