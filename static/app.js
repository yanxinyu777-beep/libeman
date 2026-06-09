const state = {
  page: 1,
  pageSize: 50,
  total: 0,
  rows: [],
  query: "",
  tags: new Set(),
  format: "",
  status: "active",
  sort: "updated",
  order: "desc",
  selectionMode: "ids",
  selectedIds: new Set(),
  excludedIds: new Set(),
  pendingDelete: null,
};

let booksRequestSequence = 0;
let facetsRequestSequence = 0;

const el = {
  stats: document.querySelector("#stats"),
  scanRoot: document.querySelector("#scanRoot"),
  chooseRoot: document.querySelector("#chooseRoot"),
  extractText: document.querySelector("#extractText"),
  startScan: document.querySelector("#startScan"),
  searchInput: document.querySelector("#searchInput"),
  sortField: document.querySelector("#sortField"),
  formatFilters: document.querySelector("#formatFilters"),
  tagFilters: document.querySelector("#tagFilters"),
  selectPage: document.querySelector("#selectPage"),
  selectAllMatching: document.querySelector("#selectAllMatching"),
  clearSelection: document.querySelector("#clearSelection"),
  manualTags: document.querySelector("#manualTags"),
  addTags: document.querySelector("#addTags"),
  copyDesktop: document.querySelector("#copyDesktop"),
  deleteSelected: document.querySelector("#deleteSelected"),
  selectionInfo: document.querySelector("#selectionInfo"),
  jobs: document.querySelector("#jobs"),
  bookRows: document.querySelector("#bookRows"),
  prevPage: document.querySelector("#prevPage"),
  nextPage: document.querySelector("#nextPage"),
  pageInfo: document.querySelector("#pageInfo"),
  deleteDialog: document.querySelector("#deleteDialog"),
  deleteSummary: document.querySelector("#deleteSummary"),
  deleteSample: document.querySelector("#deleteSample"),
  deleteConfirmText: document.querySelector("#deleteConfirmText"),
  confirmDelete: document.querySelector("#confirmDelete"),
  toast: document.querySelector("#toast"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "请求失败");
  }
  return payload;
}

function filtersPayload() {
  return {
    query: state.query,
    tags: [...state.tags],
    format: state.format,
    status: state.status,
    sort: state.sort,
    order: state.order,
  };
}

function selectionPayload(extra = {}) {
  return {
    selection: {
      allMatching: state.selectionMode === "all",
      ids: [...state.selectedIds],
      excludeIds: [...state.excludedIds],
    },
    filters: filtersPayload(),
    ...extra,
  };
}

function selectedCount() {
  if (state.selectionMode === "all") {
    return Math.max(0, state.total - state.excludedIds.size);
  }
  return state.selectedIds.size;
}

function resetSelection() {
  state.selectionMode = "ids";
  state.selectedIds.clear();
  state.excludedIds.clear();
  renderSelection();
}

function setFilterChanged() {
  state.page = 1;
  resetSelection();
  loadBooks();
  loadFacets();
}

async function loadBooks() {
  const requestSequence = ++booksRequestSequence;
  const params = new URLSearchParams({
    query: state.query,
    tags: [...state.tags].join(","),
    format: state.format,
    status: state.status,
    sort: state.sort,
    order: state.order,
    page: String(state.page),
    page_size: String(state.pageSize),
  });
  const payload = await api(`/api/books?${params.toString()}`);
  if (requestSequence !== booksRequestSequence) return;
  state.rows = payload.items;
  state.total = payload.total;
  state.page = payload.page;
  state.pageSize = payload.page_size;
  renderRows();
  renderPager();
  renderSelection();
}

async function loadFacets() {
  const requestSequence = ++facetsRequestSequence;
  const [tags, formats, stats] = await Promise.all([
    api("/api/tags"),
    api("/api/formats"),
    api("/api/stats"),
  ]);
  if (requestSequence !== facetsRequestSequence) return;
  renderTags(tags.tags);
  renderFormats(formats.formats);
  renderStats(stats);
}

function renderStats(stats) {
  const active = stats.statuses.active || 0;
  const remote = stats.remote_lookup || {};
  const rated = remote.rated || 0;
  const remoteTotal = remote.total || active;
  const due = remote.due || 0;
  const pauseText = stats.remote_pause?.active
    ? `<span>联网补齐暂停至 ${formatDateTime(stats.remote_pause.until)}：${escapeHtml(stats.remote_pause.reason || "等待重试")}</span>`
    : "";
  el.stats.innerHTML = `
    <span>在库 ${active.toLocaleString()} 本</span>
    <span>总大小 ${formatBytes(stats.active_size || 0)}</span>
    <span>豆瓣评分 ${rated.toLocaleString()}/${remoteTotal.toLocaleString()}</span>
    <span>待联网补齐 ${due.toLocaleString()} 本</span>
    ${pauseText}
  `;
}

function renderTags(tags) {
  if (!tags.length) {
    el.tagFilters.innerHTML = `<div class="filter-count">暂无标签</div>`;
    return;
  }
  el.tagFilters.innerHTML = tags
    .map((item) => {
      const active = state.tags.has(item.tag) ? " active" : "";
      return `<button class="filter-item${active}" data-tag="${escapeAttr(item.tag)}">
        <span>${escapeHtml(item.tag)}</span><span class="filter-count">${item.count}</span>
      </button>`;
    })
    .join("");
  el.tagFilters.querySelectorAll("[data-tag]").forEach((button) => {
    button.addEventListener("click", () => {
      const tag = button.dataset.tag;
      if (state.tags.has(tag)) state.tags.delete(tag);
      else state.tags.add(tag);
      setFilterChanged();
    });
  });
}

function renderFormats(formats) {
  const allActive = state.format ? "" : " active";
  const items = [
    `<button class="filter-item${allActive}" data-format=""><span>全部格式</span><span class="filter-count"></span></button>`,
    ...formats.map((item) => {
      const active = state.format === item.ext ? " active" : "";
      return `<button class="filter-item${active}" data-format="${escapeAttr(item.ext)}">
        <span>${escapeHtml(item.ext.replace(".", "").toUpperCase())}</span><span class="filter-count">${item.count}</span>
      </button>`;
    }),
  ];
  el.formatFilters.innerHTML = items.join("");
  el.formatFilters.querySelectorAll("[data-format]").forEach((button) => {
    button.addEventListener("click", () => {
      state.format = button.dataset.format || "";
      setFilterChanged();
    });
  });
}

function renderRows() {
  if (!state.rows.length) {
    el.bookRows.innerHTML = `<tr><td colspan="7" class="empty">没有符合条件的书籍</td></tr>`;
    return;
  }
  el.bookRows.innerHTML = state.rows
    .map((book) => {
      const checked = isSelected(book.id) ? "checked" : "";
      const tags = (book.tags || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
      const desc = `<div class="book-desc">${escapeHtml(book.description || "暂无简介")}</div>`;
      const author = book.authors ? `<div class="book-author">${escapeHtml(book.authors)}</div>` : "";
      const rating = renderDoubanRating(book);
      const cover = book.cover_url ? `<img class="book-cover" src="${escapeAttr(book.cover_url)}" alt="">` : `<div class="book-cover placeholder"></div>`;
      const publisher = book.publisher ? escapeHtml(book.publisher) : `<span class="muted-text">未知</span>`;
      return `
        <tr>
          <td class="select-col"><input type="checkbox" data-select-id="${book.id}" ${checked} /></td>
          <td>
            <div class="book-main">
              ${cover}
              <div class="book-meta">
                <div class="book-title">${escapeHtml(book.title || book.filename)}</div>
                ${author}
                ${desc}
              </div>
            </div>
          </td>
          <td class="rating-cell">${rating}</td>
          <td class="publisher-cell">${publisher}</td>
          <td><div class="tags">${tags}</div></td>
          <td><span class="format">${escapeHtml(book.ext.replace(".", ""))}</span></td>
          <td>${escapeHtml(book.size_label)}</td>
        </tr>
      `;
    })
    .join("");
  el.bookRows.querySelectorAll("[data-select-id]").forEach((box) => {
    box.addEventListener("change", () => toggleRowSelection(Number(box.dataset.selectId), box.checked));
    });
}

function renderDoubanRating(book) {
  if (book.douban_rating) {
    const score = escapeHtml(book.douban_rating);
    if (book.douban_url) {
      return `<a class="rating-score" href="${escapeAttr(book.douban_url)}" target="_blank" rel="noreferrer">${score}</a>`;
    }
    return `<span class="rating-score">${score}</span>`;
  }
  return `<span class="rating-status">${escapeHtml(book.douban_rating_label || "待补全")}</span>`;
}

function isSelected(id) {
  if (state.selectionMode === "all") {
    return !state.excludedIds.has(Number(id));
  }
  return state.selectedIds.has(Number(id));
}

function toggleRowSelection(id, checked) {
  if (state.selectionMode === "all") {
    if (checked) state.excludedIds.delete(id);
    else state.excludedIds.add(id);
  } else if (checked) {
    state.selectedIds.add(id);
  } else {
    state.selectedIds.delete(id);
  }
  renderSelection();
}

function renderSelection() {
  const count = selectedCount();
  if (state.selectionMode === "all") {
    el.selectionInfo.textContent = `已选择当前筛选结果 ${count.toLocaleString()} 本；取消勾选的本页项目会被排除。`;
  } else if (count) {
    el.selectionInfo.textContent = `已选择 ${count.toLocaleString()} 本。`;
  } else {
    el.selectionInfo.textContent = `当前筛选结果 ${state.total.toLocaleString()} 本。`;
  }
  const disabled = count === 0;
  el.addTags.disabled = disabled;
  el.copyDesktop.disabled = disabled;
  el.deleteSelected.disabled = disabled;
}

function renderPager() {
  const maxPage = Math.max(1, Math.ceil(state.total / state.pageSize));
  el.pageInfo.textContent = `第 ${state.page} / ${maxPage} 页`;
  el.prevPage.disabled = state.page <= 1;
  el.nextPage.disabled = state.page >= maxPage;
}

async function pollJobs() {
  const payload = await api("/api/jobs");
  const activeBefore = el.jobs.dataset.active === "1";
  const activeNow = payload.jobs.some((job) => job.status === "running" || job.status === "queued");
  const scanActive = payload.jobs.some((job) => job.kind === "scan" && (job.status === "running" || job.status === "queued"));
  el.jobs.dataset.active = activeNow ? "1" : "0";
  el.startScan.disabled = scanActive;
  renderJobs(payload.jobs);
  if (activeBefore && !activeNow) {
    await loadBooks();
    await loadFacets();
  }
}

function renderJobs(jobList) {
  const visible = jobList.filter((job) => Date.now() / 1000 - job.updated_at < 180 || job.status === "running" || job.status === "queued");
  if (!visible.length) {
    el.jobs.innerHTML = "";
    return;
  }
  el.jobs.innerHTML = visible
    .map((job) => {
      const total = Number(job.total || job.payload?.count || 0);
      const processed = Number(job.processed || 0);
      const pct = total ? Math.min(100, Math.round((processed / total) * 100)) : job.status === "finished" ? 100 : 20;
      const kind = job.kind === "metadata" ? "联网补齐" : "扫描";
      return `<div class="job">
        <strong>${kind}</strong>
        <div>
          <div>${escapeHtml(job.message || "")}</div>
          <div class="progress"><div style="width:${pct}%"></div></div>
        </div>
    <span>${escapeHtml(job.status)}</span>
      </div>`;
    })
    .join("");
}

async function startScan() {
  const root = el.scanRoot.value.trim();
  if (!root) {
    showToast("请先填写扫描位置");
    return;
  }
  await api("/api/scan/start", {
    method: "POST",
    body: JSON.stringify({ root, extract_text: el.extractText.checked }),
  });
  showToast("扫描任务已开始");
  pollJobs();
}

async function chooseRoot() {
  el.chooseRoot.disabled = true;
  el.chooseRoot.textContent = "选择中";
  try {
    const result = await api("/api/dialog/folder", {
      method: "POST",
      body: JSON.stringify({ initial_dir: el.scanRoot.value.trim() }),
    });
    if (result.path) {
      el.scanRoot.value = result.path;
      showToast("已选择扫描位置");
    }
  } finally {
    el.chooseRoot.disabled = false;
    el.chooseRoot.textContent = "选择位置";
  }
}

async function addTags() {
  const tags = el.manualTags.value.trim();
  if (!tags) {
    showToast("请输入要添加的标签");
    return;
  }
  const result = await api("/api/tags/add", {
    method: "POST",
    body: JSON.stringify(selectionPayload({ tags })),
  });
  el.manualTags.value = "";
  showToast(`已更新 ${result.changed} 本书的标签`);
  await loadBooks();
  await loadFacets();
}

async function copyDesktop() {
  const count = selectedCount();
  if (!count) return;
  if (!window.confirm(`将 ${count.toLocaleString()} 本书复制到桌面 BookVault_Copies 文件夹？`)) return;
  const result = await api("/api/copy-desktop", {
    method: "POST",
    body: JSON.stringify(selectionPayload()),
  });
  showToast(`已复制 ${result.copied} 本到 ${result.target}`);
}

async function openDeleteDialog() {
  const preview = await api("/api/delete/preview", {
    method: "POST",
    body: JSON.stringify(selectionPayload()),
  });
  state.pendingDelete = preview;
  el.deleteSummary.innerHTML = `
    将移动 ${preview.count.toLocaleString()} 本书到桌面安全备份区，占用 ${escapeHtml(preview.total_size_label)}。
    确认短语：<strong>${escapeHtml(preview.confirm_phrase)}</strong>
  `;
  el.deleteSample.innerHTML = preview.sample
    .map((book) => `<div><strong>${escapeHtml(book.title)}</strong><br>${escapeHtml(book.path)}</div>`)
    .join("");
  el.deleteConfirmText.value = "";
  el.deleteDialog.showModal();
}

async function confirmDelete(event) {
  event.preventDefault();
  if (!state.pendingDelete?.operation_id) {
    showToast("安全删除预览已失效，请重新预览");
    return;
  }
  const confirmText = el.deleteConfirmText.value;
  const result = await api("/api/delete/execute", {
    method: "POST",
    body: JSON.stringify({
      operation_id: state.pendingDelete.operation_id,
      confirm_text: confirmText,
    }),
  });
  el.deleteDialog.close();
  state.pendingDelete = null;
  resetSelection();
  showToast(`已移动 ${result.moved} 本到安全备份区：${result.quarantine}`);
  await loadBooks();
  await loadFacets();
}

function bindEvents() {
  let searchTimer = null;
  el.searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = el.searchInput.value.trim();
      setFilterChanged();
    }, 250);
  });
  el.sortField.addEventListener("change", () => {
    state.sort = el.sortField.value;
    setFilterChanged();
  });
  el.scanRoot.addEventListener("click", () => chooseRoot().catch(showError));
  el.chooseRoot.addEventListener("click", () => chooseRoot().catch(showError));
  el.startScan.addEventListener("click", () => startScan().catch(showError));
  el.selectPage.addEventListener("click", () => {
    if (state.selectionMode !== "ids") resetSelection();
    state.rows.forEach((book) => state.selectedIds.add(Number(book.id)));
    renderRows();
    renderSelection();
  });
  el.selectAllMatching.addEventListener("click", () => {
    state.selectionMode = "all";
    state.selectedIds.clear();
    state.excludedIds.clear();
    renderRows();
    renderSelection();
  });
  el.clearSelection.addEventListener("click", () => {
    resetSelection();
    renderRows();
  });
  el.addTags.addEventListener("click", () => addTags().catch(showError));
  el.copyDesktop.addEventListener("click", () => copyDesktop().catch(showError));
  el.deleteSelected.addEventListener("click", () => openDeleteDialog().catch(showError));
  el.confirmDelete.addEventListener("click", confirmDelete);
  el.prevPage.addEventListener("click", () => {
    state.page -= 1;
    loadBooks().catch(showError);
  });
  el.nextPage.addEventListener("click", () => {
    state.page += 1;
    loadBooks().catch(showError);
  });
}

function showError(error) {
  showToast(error.message || String(error));
}

let toastTimer = null;
function showToast(message) {
  clearTimeout(toastTimer);
  el.toast.textContent = message;
  el.toast.classList.add("show");
  toastTimer = setTimeout(() => el.toast.classList.remove("show"), 4200);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function formatBytes(size) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(size || 0);
  for (const unit of units) {
    if (value < 1024 || unit === units.at(-1)) {
      return unit === "B" ? `${value} ${unit}` : `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return "0 B";
}

function formatDateTime(seconds) {
  if (!seconds) return "";
  return new Date(Number(seconds) * 1000).toLocaleString("zh-CN", { hour12: false });
}

bindEvents();
loadBooks().catch(showError);
loadFacets().catch(showError);
pollJobs().catch(showError);
setInterval(() => pollJobs().catch(showError), 2200);
