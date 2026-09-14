const PAGE_SIZE = 60;
const VIEW_STATE_KEY = "image-reviewer.view-state";

const labels = {
  all: "全部",
  unreviewed: "未处理",
  needs_revision: "需修改",
  modified_pending_review: "已修改",
  approved: "已确认",
  ignored: "忽略",
  delivered: "已交付",
};
const inventoryLabels = {
  all: "全部",
  present: "正常",
  missing: "缺失",
  blocked: "阻塞",
  extra: "名单外",
  invalid_dimensions: "尺寸异常",
  invalid_manifest: "清单无效",
  wrong_path: "路径不符",
  duplicate_module: "模块重复",
  product_exception: "产品异常",
};
const mainViews = new Set(["overview", "review", "revisions", "delivery"]);

const state = {
  assets: [],
  current: null,
  mainView: "overview",
  status: ["all"],
  reviewStatus: ["all"],
  reviewInventoryStatus: ["all"],
  deliveryMode: "deliverable",
  inventoryStatus: ["all"],
  sources: [],
  suggestions: [],
  browsePath: "",
  offset: 0,
  total: 0,
  hasMore: false,
  loadingMore: false,
  requestToken: 0,
  browseToken: 0,
  searchTimer: null,
  scrollSaveFrame: null,
  listController: null,
  saving: false,
  iopaintPending: null,
  toastTimer: null,
  altTextSaveTimer: null,
  altTextSaving: false,
  altTextLastSaved: "",
  iopaintConfig: { enabled: false, editor_url: "http://127.0.0.1:5055" },
  restoreScrollY: 0,
  restoringScroll: false,
};

const $ = (selector) => document.querySelector(selector);

if ("scrollRestoration" in history) history.scrollRestoration = "manual";

function loadViewState() {
  try {
    const saved = JSON.parse(localStorage.getItem(VIEW_STATE_KEY) || "{}");
    if (mainViews.has(saved.mainView)) state.mainView = saved.mainView;
    const savedStatuses = Array.isArray(saved.status) ? saved.status : [saved.status];
    const savedInventoryStatuses = Array.isArray(saved.inventoryStatus) ? saved.inventoryStatus : [saved.inventoryStatus];
    state.status = savedStatuses.filter((value) => Object.hasOwn(labels, value));
    state.inventoryStatus = savedInventoryStatuses.filter((value) => Object.hasOwn(inventoryLabels, value));
    if (!state.status.length) state.status = ["all"];
    if (!state.inventoryStatus.length) state.inventoryStatus = ["all"];
    state.reviewStatus = [...state.status];
    state.reviewInventoryStatus = [...state.inventoryStatus];
    if (["deliverable", "delivered"].includes(saved.deliveryMode)) state.deliveryMode = saved.deliveryMode;
    if (typeof saved.search === "string") $("#search").value = saved.search;
    if (Number.isFinite(saved.scrollY) && saved.scrollY >= 0) state.restoreScrollY = saved.scrollY;
  } catch (_) {
    // 浏览器禁用本地存储时仍保持正常浏览。
  }
}

function saveViewState() {
  try {
    localStorage.setItem(VIEW_STATE_KEY, JSON.stringify({
      mainView: state.mainView,
      status: state.mainView === "review" ? state.status : state.reviewStatus,
      inventoryStatus: state.mainView === "review" ? state.inventoryStatus : state.reviewInventoryStatus,
      deliveryMode: state.deliveryMode,
      search: $("#search").value,
      scrollY: window.scrollY,
    }));
  } catch (_) {
    // 浏览器禁用本地存储时仍保持正常浏览。
  }
}

function scheduleViewStateSave() {
  if (state.scrollSaveFrame !== null) return;
  state.scrollSaveFrame = requestAnimationFrame(() => {
    state.scrollSaveFrame = null;
    saveViewState();
  });
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_) {
      message = (await response.text()) || message;
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function showToast(message, error = false) {
  const toast = $("#toast");
  clearTimeout(state.toastTimer);
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  state.toastTimer = setTimeout(() => { toast.className = "toast"; }, 3500);
}

function imageUrl(asset) {
  const path = encodeURIComponent(asset.relative_path).replaceAll("%2F", "/");
  const version = encodeURIComponent(String(asset.sha256 || "").slice(0, 12));
  return `/images/${path}?v=${version}`;
}

async function init() {
  loadViewState();
  initMainTabs();
  // Reapply a fixed revisions/delivery view on reload rather than accidentally
  // carrying the last free-form review filters into that view.
  applyMainView();
  initTabs();
  initInventoryTabs();
  try {
    state.iopaintConfig = await api("/api/iopaint/config");
  } catch (error) {
    console.error("加载 IOPaint 配置失败", error);
  }
  try {
    await loadSuggestions();
  } catch (error) {
    console.error("加载快捷建议失败", error);
  }
  try {
    await loadSources();
    if (state.mainView === "overview") await loadOverview();
    else await load();
  } catch (error) {
    showListError(error);
  }
  await restoreScrollPosition();
}

async function loadSources() {
  state.sources = await api("/api/sources");
  const active = state.sources.find((source) => source.active);
  $("#sourceSelect").innerHTML = state.sources.map((source) =>
    `<option value="${source.id}" ${source.active ? "selected" : ""}>${escapeHtml(source.name)} (${source.image_count})</option>`,
  ).join("") || "<option value=\"\">暂无目录</option>";

  const list = $("#sourceList");
  list.replaceChildren();
  if (!state.sources.length) {
    list.innerHTML = "<p class=\"muted\">还没有保存的图片目录。</p>";
  }
  state.sources.forEach((source) => {
    const row = document.createElement("div");
    row.className = "source-row";
    const details = document.createElement("span");
    details.innerHTML = `${source.active ? "✅" : "○"} <strong>${escapeHtml(source.name)}</strong><br><small class="muted">${escapeHtml(source.path)} · ${source.image_count} 张${source.exists ? "" : " · 路径不可用"}</small>`;
    row.appendChild(details);
    if (source.active) {
      const current = document.createElement("span");
      current.className = "muted";
      current.textContent = "当前";
      row.appendChild(current);
    } else {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "切换";
      button.addEventListener("click", () => switchSource(source.id));
      row.appendChild(button);
    }
    list.appendChild(row);
  });

  if (active) state.browsePath = active.path;
  else $("#groups").innerHTML = "<p>请先通过“管理目录”添加图片目录。</p>";
}

async function switchSource(sourceId) {
  if (!sourceId) return;
  try {
    await api(`/api/sources/${sourceId}/activate`, { method: "POST" });
    $("#search").value = "";
    saveViewState();
    await loadSources();
    if (state.mainView === "overview") await loadOverview();
    else await load();
  } catch (error) {
    alert(`切换失败：${error.message}`);
  }
}

async function scanNow() {
  const button = $("#scanButton");
  button.disabled = true;
  try {
    await api("/api/scan", { method: "POST" });
    await loadSources();
    if (state.mainView === "overview") await loadOverview();
    else await load();
  } catch (error) {
    alert(`扫描失败：${error.message}`);
  } finally {
    button.disabled = false;
  }
}

function initMainTabs() {
  $("#mainTabs").querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => switchMainView(button.dataset.view));
  });
}

function applyMainView(updateFilters = true) {
  $("#mainTabs").querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === state.mainView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  $("#overviewView").hidden = state.mainView !== "overview";
  $("#assetsView").hidden = state.mainView === "overview";
  if (state.mainView === "overview") return;
  $("#reviewFilters").hidden = state.mainView !== "review";
  $("#inventoryFilters").hidden = state.mainView !== "review";
  $("#deliveryFilters").hidden = state.mainView !== "delivery";
  $("#batchDeliveryButton").hidden = state.mainView !== "delivery";
  $("#queueDimensionRepairsButton").hidden = state.mainView !== "review";
  document.querySelectorAll("[data-delivery-mode]").forEach((button) => button.classList.toggle("active", button.dataset.deliveryMode === state.deliveryMode));
  if (!updateFilters) return;
  if (state.mainView === "review") {
    state.status = [...state.reviewStatus];
    state.inventoryStatus = [...state.reviewInventoryStatus];
  } else if (state.mainView === "revisions") {
    state.status = ["needs_revision", "modified_pending_review"];
    state.inventoryStatus = ["all"];
  } else {
    state.status = ["all"];
    state.inventoryStatus = ["present"];
  }
  // The filter buttons are reused across views. Rebuild their selected state
  // after a view-level filter reset so returning to 图片评审 is unambiguous.
  if ($("#tabs").childElementCount) initTabs();
  if ($("#inventoryTabs").childElementCount) initInventoryTabs();
}

async function switchMainView(view) {
  if (!mainViews.has(view) || view === state.mainView) return;
  if (state.mainView === "review") {
    state.reviewStatus = [...state.status];
    state.reviewInventoryStatus = [...state.inventoryStatus];
  }
  state.mainView = view;
  applyMainView();
  window.scrollTo({ top: 0, behavior: "auto" });
  saveViewState();
  if (view === "overview") await loadOverview();
  else await load();
}

async function loadOverview() {
  const summary = await api("/api/overview");
  const scanned = summary.source.last_scanned_at ? new Date(summary.source.last_scanned_at).toLocaleString() : "尚未扫描";
  const manifestDetail = summary.manifest_error
    ? `清单错误：${summary.manifest_error}`
    : (summary.manifest ? summary.manifest_path : "未使用清单文件");
  $("#summaryGrid").innerHTML = [
    ["当前目录", summary.source.name, summary.source.path],
    ["参考文件", `${summary.reference_images} 张产品主图`, manifestDetail],
    ["图片总量", `${summary.total_images} 张`, `期望 ${summary.expected_images} 张`],
    ["产品数量", `${summary.product_count} 个 SKU`, `${summary.deliverable_skus} 个 SKU 当前可交付`],
    ["最近扫描", scanned, "后台也会周期扫描文件变化"],
  ].map(([label, value, detail]) => `<article class="summary-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small title="${escapeHtml(detail)}">${escapeHtml(detail)}</small></article>`).join("");
  $("#reviewSummary").innerHTML = Object.entries(labels).filter(([key]) => key !== "all").map(([key, label]) =>
    `<div class="summary-status status-${key}"><span>${escapeHtml(label)}</span><strong>${summary.review_counts[key] || 0}</strong></div>`
  ).join("");
  $("#inventorySummary").innerHTML = Object.entries(inventoryLabels).filter(([key]) => key !== "all").map(([key, label]) =>
    `<div class="summary-status inventory-${key}"><span>${escapeHtml(label)}</span><strong>${summary.inventory_counts[key] || 0}</strong></div>`
  ).join("");
}

function toggleFilter(selected, value) {
  if (value === "all") return ["all"];
  const next = selected.filter((item) => item !== "all");
  const index = next.indexOf(value);
  if (index >= 0) next.splice(index, 1);
  else next.push(value);
  return next.length ? next : ["all"];
}

function initTabs() {
  const tabs = $("#tabs");
  tabs.replaceChildren();
  Object.entries(labels).forEach(([key, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `status-${key} ${state.status.includes(key) ? "active" : ""}`;
    button.textContent = label;
    button.setAttribute("aria-pressed", String(state.status.includes(key)));
    button.addEventListener("click", () => changeStatus(key));
    tabs.appendChild(button);
  });
}

function changeStatus(status) {
  state.status = toggleFilter(state.status, status);
  saveViewState();
  initTabs();
  load();
}

function initInventoryTabs() {
  const tabs = $("#inventoryTabs");
  tabs.replaceChildren();
  Object.entries(inventoryLabels).forEach(([key, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `inventory-${key}${state.inventoryStatus.includes(key) ? " active" : ""}`;
    button.textContent = label;
    button.setAttribute("aria-pressed", String(state.inventoryStatus.includes(key)));
    button.addEventListener("click", () => {
      state.inventoryStatus = toggleFilter(state.inventoryStatus, key);
      saveViewState();
      initInventoryTabs();
      load();
    });
    tabs.appendChild(button);
  });
}

async function load(reset = true) {
  if (!reset && state.loadingMore) return;
  if (reset) {
    state.listController?.abort();
    state.listController = new AbortController();
    state.requestToken += 1;
    state.offset = 0;
    state.hasMore = false;
    state.assets = [];
    $("#groups").innerHTML = "";
    $("#stats").textContent = "加载中…";
  } else if (!state.listController) {
    state.listController = new AbortController();
  }

  const token = state.requestToken;
  const offset = state.offset;
  state.loadingMore = true;
  try {
    const query = encodeURIComponent($("#search").value);
    const page = await api(
      `/api/assets?status=${encodeURIComponent(state.status.join(","))}&inventory_status=${encodeURIComponent(state.inventoryStatus.join(","))}&delivery_status=${state.mainView === "delivery" ? state.deliveryMode : "all"}&q=${query}&limit=${PAGE_SIZE}&offset=${offset}`,
      { signal: state.listController.signal },
    );
    if (token !== state.requestToken || offset !== state.offset) return;

    state.assets = state.assets.concat(page.items);
    state.offset += page.items.length;
    state.hasMore = page.has_more;
    state.total = page.total;
    renderPage(page.items, reset);
    updateStats();
  } catch (error) {
    if (error.name !== "AbortError" && token === state.requestToken) showListError(error);
  } finally {
    if (token === state.requestToken) {
      state.loadingMore = false;
      requestAnimationFrame(loadMoreIfNeeded);
    }
  }
}

function updateStats() {
  $("#stats").textContent = `已加载 ${state.assets.length} / ${state.total} 张`;
}

function showListError(error) {
  $("#groups").innerHTML = `<p>加载失败：${escapeHtml(error.message || "未知错误")}</p>`;
  $("#stats").textContent = "加载失败";
}

function renderPage(items, reset) {
  if (reset) $("#groups").innerHTML = "";
  const grouped = {};
  items.forEach((asset) => (grouped[asset.sku] ??= []).push(asset));

  Object.entries(grouped).forEach(([sku, assets]) => {
    let section = Array.from(document.querySelectorAll(".group"))
      .find((element) => element.dataset.sku === sku);
    if (!section) {
      section = document.createElement("section");
      section.className = "group";
      section.dataset.sku = sku;
      section.innerHTML = `<h2>${escapeHtml(sku)} <small></small><button type="button" class="product-exception-button" data-action="toggle-product-exception"></button><button type="button" class="delivery-button" data-action="create-delivery">创建 A+ 交付版本</button></h2><div class="grid"></div>`;
      $("#groups").appendChild(section);
    }

    const grid = section.querySelector(".grid");
    const isProductException = assets.some((asset) => asset.product_exception);
    const exceptionButton = section.querySelector('[data-action="toggle-product-exception"]');
    exceptionButton.textContent = isProductException ? "解除产品异常" : "标记产品异常";
    exceptionButton.classList.toggle("active", isProductException);
    const deliveryButton = section.querySelector('[data-action="create-delivery"]');
    deliveryButton.hidden = state.mainView === "delivery" && state.deliveryMode === "delivered";
    deliveryButton.disabled = isProductException;
    deliveryButton.title = isProductException ? "解除产品异常后才能创建 A+ 交付版本" : "";
    const reference = assets.find((asset) => asset.asset_role !== "reference" && asset.reference_images?.length)?.reference_images[0];
    if (reference && !grid.querySelector(".reference-card")) grid.insertAdjacentHTML("beforeend", referenceCardHtml(reference));
    assets.forEach((asset) => grid.insertAdjacentHTML("beforeend", cardHtml(asset)));
    section.querySelector("h2 small").textContent = `(${section.querySelectorAll(".card:not(.reference-card)").length})`;
  });
}

function referenceCardHtml(reference) {
  return `
    <article class="card reference-card" data-asset-id="${reference.id}">
      <div class="image-wrap"><img loading="lazy" decoding="async" src="${imageUrl(reference)}" alt="产品主图" data-action="open-review" /><span class="dimensions">${reference.width || "?"} × ${reference.height || "?"}</span></div>
      <div class="info"><strong>产品主图</strong><div class="inventory-badge">参考图</div><div class="actions"><button type="button" data-action="open-review">查看大图</button><button type="button" data-action="refresh-asset">刷新</button></div></div>
    </article>`;
}

function isDimensionRepair(asset) {
  return Boolean(asset.dimension_repair?.required && asset.ai_repairable);
}

function cardHtml(asset) {
  const dimensionRepair = isDimensionRepair(asset);
  const readOnly = asset.inventory_status !== "present" && !dimensionRepair;
  const canPreview = Boolean(asset.id) && !["missing", "blocked"].includes(asset.inventory_status);
  const copyButton = (asset.inventory_status === "present" || dimensionRepair) && asset.status === "needs_revision"
    ? `<button type="button" data-action="copy-task" data-asset-id="${asset.id}">复制意见</button>`
    : "";
  const refreshButton = canPreview ? `<button type="button" data-action="refresh-asset">刷新</button>` : "";
  const iopaintButton = asset.inventory_status === "present" && asset.asset_role !== "reference"
    ? `<a class="button-link iopaint-button" data-action="open-iopaint" href="/api/assets/${asset.id}/edit-in-iopaint" target="_blank" rel="noopener" data-iopaint-sha="${escapeHtml(asset.sha256 || "")}">消除</a>`
    : "";
  const image = asset.inventory_status === "missing" || asset.inventory_status === "blocked"
    ? `<div class="placeholder">${asset.inventory_status === "blocked" ? "⛔<br>生成阻塞" : "⚠️<br>图片缺失"}</div>`
    : `<img loading="lazy" decoding="async" src="${imageUrl(asset)}" alt="${escapeHtml(asset.module)}" data-action="open-review" />`;
  const dimensionQueued = dimensionRepair && asset.status === "needs_revision";
  return `
    <article class="card ${asset.status} inventory-${asset.inventory_status}${dimensionRepair ? " dimension-repair" : ""}" data-asset-id="${asset.id ?? ""}">
      <div class="image-wrap">${image}<span class="dimensions">${asset.width || asset.expected_width || "?"} × ${asset.height || asset.expected_height || "?"}</span></div>
      <div class="info">
        <strong title="${escapeHtml(asset.module)}">${escapeHtml(asset.module)}</strong>
        <div class="inventory-badge">${asset.asset_role === "reference" ? "产品主图" : (inventoryLabels[asset.inventory_status] || "名单外")}${asset.reason ? `：${escapeHtml(asset.reason)}` : ""}</div>
        ${dimensionRepair ? `<div class="dimension-repair-hint">${dimensionQueued ? "已在 AI 尺寸修复队列" : "可加入 AI 尺寸修复队列"}：输出 ${asset.expected_width} × ${asset.expected_height}</div>` : ""}
        ${asset.inventory_status === "invalid_dimensions" ? `<div class="muted">当前 ${asset.width || "?"} × ${asset.height || "?"}；期望 ${asset.expected_width} × ${asset.expected_height}</div>` : ""}
        ${asset.asset_role !== "reference" && !dimensionRepair ? `<div class="alt-text-badge ${asset.alt_text ? "" : "empty"}" title="${escapeHtml(asset.alt_text?.alt_text || "")}">Alt Text：${asset.alt_text ? escapeHtml(asset.alt_text.alt_text) : "未导入"}</div>` : ""}
        ${readOnly ? (canPreview ? `<div class="actions"><button type="button" data-action="open-review">查看大图</button>${refreshButton}</div>` : "") : (dimensionRepair ? `<div class="actions"><button type="button" data-action="open-review">${dimensionQueued ? "查看尺寸修复任务" : "加入尺寸修复队列"}</button>${copyButton}${refreshButton}</div>` : `<div class="actions"><button type="button" data-action="open-review">评审</button><button type="button" class="ok-button" data-action="quick-approve">OK</button>${iopaintButton}${copyButton}${refreshButton}</div>`)}
      </div>
    </article>
  `;
}

const zoomStates = new WeakMap();

function clampPan(viewport, zoom) {
  const minX = viewport.clientWidth * (1 - zoom.scale);
  const minY = viewport.clientHeight * (1 - zoom.scale);
  zoom.x = Math.min(0, Math.max(minX, zoom.x));
  zoom.y = Math.min(0, Math.max(minY, zoom.y));
}

function renderZoom(viewport, zoom) {
  zoom.image.style.transform = `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.scale})`;
  zoom.output.value = `${Math.round(zoom.scale * 100)}%`;
  viewport.classList.toggle("is-zoomed", zoom.scale > 1);
}

function resetZoom(viewport) {
  const zoom = zoomStates.get(viewport);
  if (!zoom) return;
  zoom.scale = 1;
  zoom.x = 0;
  zoom.y = 0;
  zoom.pointerId = null;
  viewport.classList.remove("is-dragging");
  renderZoom(viewport, zoom);
}

function initZoomViewport(viewport) {
  const image = viewport.querySelector(".preview");
  const output = viewport.querySelector(".zoom-level");
  const zoom = { image, output, scale: 1, x: 0, y: 0, pointerId: null, lastX: 0, lastY: 0 };
  zoomStates.set(viewport, zoom);
  renderZoom(viewport, zoom);

  viewport.addEventListener("wheel", (event) => {
    if (image.hidden) return;
    event.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const pointX = event.clientX - rect.left;
    const pointY = event.clientY - rect.top;
    const previousScale = zoom.scale;
    const factor = Math.exp(-event.deltaY * 0.002);
    zoom.scale = Math.min(6, Math.max(1, previousScale * factor));
    const ratio = zoom.scale / previousScale;
    zoom.x = pointX - (pointX - zoom.x) * ratio;
    zoom.y = pointY - (pointY - zoom.y) * ratio;
    clampPan(viewport, zoom);
    renderZoom(viewport, zoom);
  }, { passive: false });

  viewport.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || zoom.scale <= 1 || image.hidden) return;
    zoom.pointerId = event.pointerId;
    zoom.lastX = event.clientX;
    zoom.lastY = event.clientY;
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("is-dragging");
  });

  viewport.addEventListener("pointermove", (event) => {
    if (event.pointerId !== zoom.pointerId) return;
    zoom.x += event.clientX - zoom.lastX;
    zoom.y += event.clientY - zoom.lastY;
    zoom.lastX = event.clientX;
    zoom.lastY = event.clientY;
    clampPan(viewport, zoom);
    renderZoom(viewport, zoom);
  });

  const stopDragging = (event) => {
    if (event.pointerId !== zoom.pointerId) return;
    zoom.pointerId = null;
    viewport.classList.remove("is-dragging");
    if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
  };
  viewport.addEventListener("pointerup", stopDragging);
  viewport.addEventListener("pointercancel", stopDragging);
  viewport.addEventListener("dblclick", () => resetZoom(viewport));
}

function resetAllZooms() {
  document.querySelectorAll("[data-zoom-viewport]").forEach(resetZoom);
}

function updateBodyModalState() {
  const modalOpen = document.querySelector(".modal.open");
  document.body.classList.toggle("modal-open", Boolean(modalOpen));
}

function openReview(assetId) {
  if (state.saving) return;
  const asset = state.assets.find((item) => item.id === assetId)
    || state.assets.flatMap((item) => item.reference_images || []).find((item) => item.id === assetId);
  if (!asset) {
    alert("该图片列表已更新，请刷新后重试。");
    return;
  }
  const dimensionRepair = isDimensionRepair(asset);
  const readOnly = asset.inventory_status !== "present" && !dimensionRepair;
  const isReference = asset.asset_role === "reference";
  const reference = isReference ? null : asset.reference_images?.[0];
  state.current = asset;
  resetAllZooms();
  $("#preview").src = imageUrl(asset);
  const dimensionQueued = dimensionRepair && asset.status === "needs_revision";
  $("#reviewTitle").textContent = `${asset.sku} / ${asset.module}${dimensionRepair ? (dimensionQueued ? "（已在 AI 尺寸修复队列）" : "（可加入 AI 尺寸修复队列）") : (readOnly ? "（只读预览）" : "")}`;
  const productInfo = asset.product_info || {};
  $("#productTitle").textContent = productInfo.title || "暂无商品标题";
  $("#productBullets").textContent = productInfo.bullets || "暂无五点描述";
  $("#productInfo").open = false;
  $("#generatedCaption").textContent = isReference ? "产品主图" : "当前评审图";
  $("#referencePanel").hidden = isReference;
  $("#referencePreview").hidden = !reference;
  $("#referenceMissing").hidden = Boolean(reference);
  if (reference) $("#referencePreview").src = imageUrl(reference);
  $("#reviewControls").hidden = readOnly;
  $("#openIopaintButton").hidden = readOnly || isReference || dimensionRepair;
  $("#openIopaintButton").href = `/api/assets/${asset.id}/edit-in-iopaint`;
  $("#openIopaintButton").dataset.assetId = asset.id;
  $("#openIopaintButton").dataset.iopaintSha = asset.sha256 || "";
  const savedComments = asset.comments || "";
  $("#comments").value = savedComments;
  $("#comments").placeholder = dimensionRepair
    ? "可补充尺寸修复或画面调整意见；系统会自动附加 970×600 修复要求"
    : "补充具体修改意见；每行一条";
  $("#altText").value = asset.alt_text?.alt_text || "";
  state.altTextLastSaved = normalizeAltText(asset.alt_text?.alt_text || "");
  $("#altTextStatus").textContent = asset.alt_text ? "已自动保存，将随此图片一并交付" : "尚未导入；修改后将自动保存";
  document.querySelector(".alt-text-editor").hidden = readOnly || isReference || dimensionRepair;
  renderSuggestionButtons(readOnly);
  document.querySelectorAll("[data-review-status]").forEach((button) => {
    if (dimensionRepair) {
      button.hidden = button.dataset.reviewStatus !== "needs_revision";
      button.textContent = dimensionQueued ? "更新尺寸修复意见" : "加入尺寸修复队列";
    } else {
      button.hidden = false;
      button.textContent = { needs_revision: "需修改", approved: "OK", ignored: "忽略" }[button.dataset.reviewStatus];
    }
  });
  $("#reviewModal").classList.add("open");
  updateBodyModalState();
}

function closeReview() {
  if (!state.saving) {
    void flushAltTextSave();
    $("#reviewModal").classList.remove("open");
    updateBodyModalState();
  }
}

async function loadSuggestions() {
  state.suggestions = await api("/api/suggestions");
  renderSuggestionManager();
  if (state.current) renderSuggestionButtons($("#reviewControls").hidden);
}

function renderSuggestionButtons(readOnly = false) {
  const container = $("#suggestions");
  container.replaceChildren();
  if (readOnly) return;
  if (!state.suggestions.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "还没有快捷评论，可点击右侧按钮添加。";
    container.appendChild(empty);
    return;
  }
  state.suggestions.forEach((suggestion) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = suggestion.title;
    button.addEventListener("click", () => addSuggestion(suggestion.id));
    container.appendChild(button);
  });
}

function addSuggestion(id) {
  const suggestion = state.suggestions.find((item) => item.id === id);
  if (!suggestion) return;
  const textarea = $("#comments");
  textarea.value += (textarea.value ? "\n" : "") + suggestion.content;
}

function renderSuggestionManager() {
  const list = $("#suggestionList");
  if (!list) return;
  list.replaceChildren();
  if (!state.suggestions.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "还没有快捷评论，请在下方添加。";
    list.appendChild(empty);
    return;
  }
  state.suggestions.forEach((suggestion, index) => {
    const item = document.createElement("div");
    item.className = "suggestion-item";
    const title = document.createElement("strong");
    title.textContent = suggestion.title;
    const content = document.createElement("p");
    content.textContent = suggestion.content;
    const actions = document.createElement("div");
    actions.className = "suggestion-item-actions";
    [
      ["上移", "move-up", index === 0],
      ["下移", "move-down", index === state.suggestions.length - 1],
      ["编辑", "edit", false],
      ["删除", "delete", false],
    ].forEach(([label, action, disabled]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.dataset.suggestionAction = action;
      button.dataset.suggestionId = suggestion.id;
      button.disabled = disabled;
      if (action === "delete") button.className = "danger";
      actions.appendChild(button);
    });
    item.append(title, content, actions);
    list.appendChild(item);
  });
}

function resetSuggestionForm() {
  $("#suggestionForm").reset();
  $("#suggestionId").value = "";
  $("#suggestionFormTitle").textContent = "添加快捷评论";
  $("#saveSuggestionButton").textContent = "添加";
  $("#cancelSuggestionEditButton").hidden = true;
}

function openSuggestions() {
  renderSuggestionManager();
  resetSuggestionForm();
  $("#suggestionModal").classList.add("open");
  updateBodyModalState();
}

function closeSuggestions() {
  $("#suggestionModal").classList.remove("open");
  updateBodyModalState();
  resetSuggestionForm();
}

function editSuggestion(id) {
  const suggestion = state.suggestions.find((item) => item.id === id);
  if (!suggestion) return;
  $("#suggestionId").value = suggestion.id;
  $("#suggestionName").value = suggestion.title;
  $("#suggestionContent").value = suggestion.content;
  $("#suggestionFormTitle").textContent = "编辑快捷评论";
  $("#saveSuggestionButton").textContent = "保存修改";
  $("#cancelSuggestionEditButton").hidden = false;
  $("#suggestionName").focus();
}

async function saveSuggestion(event) {
  event.preventDefault();
  const id = Number($("#suggestionId").value) || null;
  const title = $("#suggestionName").value.trim();
  const content = $("#suggestionContent").value.trim();
  if (!title || !content) return;
  const button = $("#saveSuggestionButton");
  button.disabled = true;
  try {
    await api(id ? `/api/suggestions/${id}` : "/api/suggestions", {
      method: id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, content }),
    });
    await loadSuggestions();
    resetSuggestionForm();
  } catch (error) {
    alert(`保存快捷评论失败：${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function deleteSuggestion(id) {
  const suggestion = state.suggestions.find((item) => item.id === id);
  if (!suggestion || !confirm(`确定删除快捷评论“${suggestion.title}”吗？\n历史评审中的文字不会受到影响。`)) return;
  try {
    await api(`/api/suggestions/${id}`, { method: "DELETE" });
    await loadSuggestions();
    if (Number($("#suggestionId").value) === id) resetSuggestionForm();
  } catch (error) {
    alert(`删除快捷评论失败：${error.message}`);
  }
}

async function moveSuggestion(id, direction) {
  try {
    state.suggestions = await api(`/api/suggestions/${id}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction }),
    });
    renderSuggestionManager();
    if (state.current) renderSuggestionButtons($("#reviewControls").hidden);
  } catch (error) {
    alert(`调整快捷评论顺序失败：${error.message}`);
  }
}

function setReviewButtonsDisabled(disabled) {
document.querySelectorAll("[data-review-status]").forEach((button) => {
  button.disabled = disabled;
});
}

function normalizeAltText(value) {
  return value.trim().replace(/\s+/g, " ");
}

function scheduleAltTextSave() {
  if (!state.current || $(".alt-text-editor").hidden) return;
  clearTimeout(state.altTextSaveTimer);
  $("#altTextStatus").textContent = "将在停止输入后自动保存";
  state.altTextSaveTimer = setTimeout(saveAltText, 500);
}

async function flushAltTextSave() {
  clearTimeout(state.altTextSaveTimer);
  if (state.current && normalizeAltText($("#altText").value) !== state.altTextLastSaved) await saveAltText();
}

async function saveAltText() {
  if (!state.current || state.altTextSaving) return;
  const asset = state.current;
  const altText = normalizeAltText($("#altText").value);
  if (altText === state.altTextLastSaved) return;
  state.altTextSaving = true;
  $("#altTextStatus").textContent = "正在自动保存…";
  try {
    const result = await api(`/api/assets/${asset.id}/alt-text`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alt_text: altText, revision: asset.revision }),
    });
    asset.alt_text = result.alt_text;
    state.altTextLastSaved = normalizeAltText(result.alt_text?.alt_text || "");
    $("#altTextStatus").textContent = state.altTextLastSaved ? "已自动保存，将随此图片一并交付" : "已清空；交付前需补充 Alt Text";
  } catch (error) {
    $("#altTextStatus").textContent = "自动保存失败；请继续编辑后重试";
    showToast(`Alt Text 自动保存失败：${error.message}`, true);
    if (error.status === 409 || error.status === 410) await load();
  } finally {
    state.altTextSaving = false;
    if (state.current === asset && normalizeAltText($("#altText").value) !== state.altTextLastSaved) scheduleAltTextSave();
  }
}

async function saveReview(status) {
  if (!state.current || state.saving) return;
  const asset = state.current;
  state.saving = true;
  setReviewButtonsDisabled(true);
  try {
    await flushAltTextSave();
    await api(`/api/assets/${asset.id}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, comments: $("#comments").value, revision: asset.revision }),
    });
    $("#reviewModal").classList.remove("open");
    updateBodyModalState();
    await load();
  } catch (error) {
    alert(`保存失败：${error.message}`);
    if (error.status === 409 || error.status === 410) await load();
  } finally {
    state.saving = false;
    setReviewButtonsDisabled(false);
  }
}

function assetMatchesCurrentFilters(asset) {
  if (asset.asset_role === "reference") return state.inventoryStatus.includes("all") || state.inventoryStatus.includes("extra");
  if (!state.status.includes("all") && !state.status.includes(asset.status)) return false;
  if (!state.inventoryStatus.includes("all") && !state.inventoryStatus.includes(asset.inventory_status)) return false;
  const query = $("#search").value.trim().toLowerCase();
  return !query || [asset.sku, asset.module, asset.relative_path]
    .some((value) => String(value || "").toLowerCase().includes(query));
}

function updateSectionCount(section) {
  if (!section) return;
  const count = section.querySelectorAll(".card:not(.reference-card)").length;
  if (!count) section.remove();
  else section.querySelector("h2 small").textContent = `(${count})`;
}


async function refreshAsset(button, assetId, showError = true) {
  if (!assetId || button?.disabled) return null;
  const card = button?.closest(".card") || document.querySelector(`.card[data-asset-id="${assetId}"]`);
  const section = card?.closest(".group");
  const isReferenceCard = card?.classList.contains("reference-card");
  const originalText = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "刷新中…";
  }
  try {
    const result = await api(`/api/assets/${assetId}/refresh`, { method: "POST" });
    if (result.removed) {
      const index = state.assets.findIndex((item) => item.id === assetId);
      if (index >= 0) state.assets.splice(index, 1);
      state.assets.forEach((item) => {
        item.reference_images = (item.reference_images || []).filter((reference) => reference.id !== assetId);
      });
      card?.remove();
      if (!isReferenceCard) state.total = Math.max(0, state.total - 1);
      updateSectionCount(section);
      updateStats();
      return null;
    }

    const refreshed = result.asset;
    if (refreshed.asset_role === "reference") {
      state.assets.forEach((item) => {
        const index = (item.reference_images || []).findIndex((reference) => reference.id === assetId);
        if (index >= 0) item.reference_images[index] = refreshed;
      });
      if (card) card.outerHTML = referenceCardHtml(refreshed);
      return refreshed;
    }

    const index = state.assets.findIndex((item) => item.id === assetId || item.relative_path === refreshed.relative_path);
    const wasListed = index >= 0;
    if (assetMatchesCurrentFilters(refreshed)) {
      if (wasListed) state.assets[index] = refreshed;
      if (card) card.outerHTML = cardHtml(refreshed);
    } else {
      if (wasListed) state.assets.splice(index, 1);
      card?.remove();
      state.total = Math.max(0, state.total - 1);
      updateSectionCount(section);
      updateStats();
    }
    if (state.current?.id === assetId) {
      state.current = refreshed;
      $("#preview").src = imageUrl(refreshed);
    }
    return refreshed;
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
    if (showError) alert(`刷新图片失败：${error.message}`);
    else showToast(`检查 IOPaint 编辑结果失败：${error.message}`, true);
    return null;
  }
}

function rememberIopaintEdit(link, assetId, clickEvent) {
  if (!assetId || !state.iopaintConfig.enabled) {
    clickEvent.preventDefault();
    showToast("IOPaint 集成未启用", true);
    return;
  }
  state.iopaintPending = { assetId, sha256: link.dataset.iopaintSha || "" };
  showToast("正在新标签页打开 IOPaint；保存后回到这里会自动刷新该图片");
}

async function checkIopaintResult() {
  const pending = state.iopaintPending;
  if (!pending) return;
  state.iopaintPending = null;
  const card = document.querySelector(`.card[data-asset-id="${pending.assetId}"]`);
  const button = card?.querySelector('[data-action="refresh-asset"]') || null;
  const refreshed = await refreshAsset(button, pending.assetId, false);
  if (!refreshed) return;
  if (refreshed.sha256 !== pending.sha256) {
    showToast("已检测到 IOPaint 保存的新版本，图片已刷新并进入“已修改”状态");
  } else {
    showToast("未检测到图片变化；如仍在编辑，请保存后再次回到本页");
    state.iopaintPending = pending;
  }
}

async function queueAllDimensionRepairs() {
  const button = $("#queueDimensionRepairsButton");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "正在加入队列…";
  try {
    const result = await api("/api/dimension-repair-queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ all_eligible: true }),
    });
    await load();
    const queued = result.queued?.length || 0;
    const existing = result.existing?.length || 0;
    const skipped = result.skipped?.length || 0;
    showToast(`已加入尺寸修复队列：${queued} 张；已在队列：${existing} 张；跳过：${skipped} 张`, skipped > 0);
  } catch (error) {
    alert(`加入尺寸修复队列失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "将所有可修复尺寸异常加入队列";
  }
}

async function batchCreateAndSyncDeliveries() {
  const button = $("#batchDeliveryButton");
  if (button.disabled) return;
  const confirmed = confirm(
    "将检查当前图片目录中的全部 SKU，并仅对满足交付条件且已在商品 CSV 维护 ASIN 的 SKU 创建交付版本并同步到 A+ Tool。\n\n不满足条件的 SKU 会跳过，其他 SKU 会继续处理。是否开始？",
  );
  if (!confirmed) return;
  button.disabled = true;
  button.textContent = "正在批量同步…";
  try {
    const result = await api("/api/aplus/batch-sync", { method: "POST" });
    await load();
    const lines = [
      `检查 SKU：${result.total_skus}`,
      `成功同步：${result.synced.length}`,
      `跳过：${result.skipped.length}`,
      `失败：${result.failed.length}`,
    ];
    const details = [...result.skipped, ...result.failed]
      .slice(0, 12)
      .map((item) => `${item.sku}：${item.reason}`);
    if (details.length) lines.push("", "未处理明细：", ...details);
    if (result.skipped.length + result.failed.length > details.length) lines.push("其余明细请按条件修复后再次执行批量同步。");
    alert(lines.join("\n"));
  } catch (error) {
    alert(`批量创建并同步失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "批量创建并同步 A+";
  }
}

async function quickApprove(button, assetId) {
  const asset = state.assets.find((item) => item.id === assetId);
  if (!asset || button.disabled) return;
  button.disabled = true;
  try {
    await api(`/api/assets/${assetId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "approved", comments: "", revision: asset.revision }),
    });
    await load();
  } catch (error) {
    alert(`确认失败：${error.message}`);
    if (error.status === 409 || error.status === 410) await load();
  } finally {
    button.disabled = false;
  }
}

async function toggleProductException(button, sku) {
  if (!sku || button.disabled) return;
  const isException = button.classList.contains("active");
  button.disabled = true;
  try {
    await api(`/api/products/${encodeURIComponent(sku)}/exception`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !isException }),
    });
    await load();
    showToast(isException ? `已解除 ${sku} 的产品异常` : `已将 ${sku} 标记为产品异常`);
  } catch (error) {
    alert(`更新产品异常状态失败：${error.message}`);
    button.disabled = false;
  }
}

async function createDelivery(button, sku) {
  if (button.disabled) return;
  const skuAssets = state.assets.filter((asset) => asset.sku === sku && asset.asset_role !== "reference");
  const defaultAsin = skuAssets.find((asset) => asset.product_info?.asin)?.product_info.asin || "";
  const asinsInput = defaultAsin
    ? defaultAsin
    : prompt("未在商品 CSV 找到 ASIN。填写关联 ASIN；多个 ASIN 用英文逗号分隔。留空可创建交付草稿，但后续不能提交 Amazon。");
  if (asinsInput === null) return;
  const asins = asinsInput.split(",").map((value) => value.trim()).filter(Boolean);
  button.disabled = true;
  try {
    const result = await api(`/api/aplus/deliveries/${encodeURIComponent(sku)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asins }),
    });
    showToast(result.created ? `已创建 ${sku} 的交付版本 v${result.delivery.version}` : `已存在相同交付版本 v${result.delivery.version}`);
    if (!asins.length) {
      showToast("交付草稿已创建；补充 ASIN 后才能同步 A+ Tool", true);
      return;
    }
    const syncResult = await api(`/api/aplus/deliveries/${result.delivery.id}/sync`, { method: "POST" });
    await load();
    showToast(`已同步 ${sku} 的交付版本到 A+ Tool；${syncResult.delivered_assets || 0} 张图片已标记为已交付`);
  } catch (error) {
    const detail = typeof error.message === "string" ? error.message : "未知错误";
    alert(`无法创建或同步交付版本：${detail}`);
  } finally {
    button.disabled = false;
  }
}

async function copyTask(assetId) {
  const asset = state.assets.find((item) => item.id === assetId);
  if (!asset) return;
  try {
    await navigator.clipboard.writeText(`${asset.sku} / ${asset.module}\n${asset.relative_path}\n${asset.comments}`);
    alert("修改任务已复制");
  } catch (error) {
    alert("复制失败，请检查浏览器剪贴板权限。");
  }
}


async function openSources() {
  $("#sourceModal").classList.add("open");
  updateBodyModalState();
  try {
    await loadSources();
    await browse(state.browsePath);
  } catch (error) {
    alert(`目录加载失败：${error.message}`);
  }
}

function closeSources() {
  $("#sourceModal").classList.remove("open");
  updateBodyModalState();
}

async function browse(path) {
  const token = ++state.browseToken;
  try {
    const data = await api(`/api/directories?path=${encodeURIComponent(path || "")}`);
    if (token !== state.browseToken) return;
    state.browsePath = data.path;
    $("#dirPath").value = data.path;
    const browser = $("#browser");
    browser.replaceChildren();

    const header = document.createElement("div");
    header.className = "row";
    const parentButton = document.createElement("button");
    parentButton.type = "button";
    parentButton.disabled = !data.parent;
    parentButton.textContent = "返回上级";
    parentButton.addEventListener("click", () => browse(data.parent || ""));
    const pathLabel = document.createElement("span");
    pathLabel.className = "muted";
    pathLabel.textContent = data.path;
    header.append(parentButton, pathLabel);
    browser.appendChild(header);

    const directoryList = document.createElement("div");
    directoryList.className = "dirlist";
    if (!data.directories.length) directoryList.innerHTML = "<div class=\"diritem muted\">没有子目录</div>";
    data.directories.forEach((directory) => {
      const item = document.createElement("div");
      item.className = "diritem";
      const name = document.createElement("span");
      name.textContent = `📁 ${directory.name}`;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "进入";
      button.addEventListener("click", () => browse(directory.path));
      item.append(name, button);
      directoryList.appendChild(item);
    });
    browser.appendChild(directoryList);
  } catch (error) {
    if (token === state.browseToken) alert(`目录不可访问：${error.message}`);
  }
}

async function addCurrentDir() {
  if (!state.browsePath) return;
  const defaultName = state.browsePath.split("/").filter(Boolean).pop() || "图片目录";
  const name = prompt("给这个图片目录起个名称", defaultName);
  if (!name) return;
  try {
    await api("/api/sources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path: state.browsePath }),
    });
    await loadSources();
    alert("目录已添加");
  } catch (error) {
    alert(`添加失败：${error.message}`);
  }
}

async function restoreScrollPosition() {
  const target = state.restoreScrollY;
  if (!target) return;
  state.restoreScrollY = 0;
  state.restoringScroll = true;
  try {
    let attempts = 0;
    while (state.hasMore && window.scrollY < target && attempts < 100) {
      const previousOffset = state.offset;
      await load(false);
      if (state.offset === previousOffset) break;
      attempts += 1;
    }
    requestAnimationFrame(() => window.scrollTo({ top: target, behavior: "auto" }));
  } finally {
    state.restoringScroll = false;
  }
}

function loadMoreIfNeeded() {
  const sentinel = $("#loadMoreSentinel");
  if (!state.restoringScroll && state.hasMore && !state.loadingMore && sentinel.getBoundingClientRect().top < window.innerHeight + 500) {
    load(false);
  }
}

new IntersectionObserver(loadMoreIfNeeded, { rootMargin: "500px" }).observe($("#loadMoreSentinel"));
document.querySelectorAll("[data-zoom-viewport]").forEach(initZoomViewport);

$("#sourceSelect").addEventListener("change", (event) => switchSource(event.target.value));
$("#manageSourcesButton").addEventListener("click", openSources);
$("#scanButton").addEventListener("click", scanNow);
$("#batchDeliveryButton").addEventListener("click", batchCreateAndSyncDeliveries);
$("#queueDimensionRepairsButton").addEventListener("click", queueAllDimensionRepairs);
document.querySelectorAll("[data-delivery-mode]").forEach((button) => button.addEventListener("click", () => {
  state.deliveryMode = button.dataset.deliveryMode;
  applyMainView();
  saveViewState();
  load();
}));
$("#manageSuggestionsButton").addEventListener("click", openSuggestions);
$("#altText").addEventListener("input", scheduleAltTextSave);
$("#suggestionForm").addEventListener("submit", saveSuggestion);
$("#cancelSuggestionEditButton").addEventListener("click", resetSuggestionForm);
$("#suggestionModal").addEventListener("click", (event) => {
  if (event.target.dataset.action === "close-suggestions") closeSuggestions();
  const button = event.target.closest("[data-suggestion-action]");
  if (!button) return;
  const id = Number(button.dataset.suggestionId);
  if (button.dataset.suggestionAction === "edit") editSuggestion(id);
  if (button.dataset.suggestionAction === "delete") deleteSuggestion(id);
  if (button.dataset.suggestionAction === "move-up") moveSuggestion(id, "up");
  if (button.dataset.suggestionAction === "move-down") moveSuggestion(id, "down");
});
$("#browseButton").addEventListener("click", () => browse($("#dirPath").value));
$("#addSourceButton").addEventListener("click", addCurrentDir);
$("#groups").addEventListener("click", (event) => {
  const action = event.target.closest("[data-action]");
  if (!action) return;
  if (action.dataset.action === "create-delivery") {
    createDelivery(action, action.closest(".group")?.dataset.sku);
    return;
  }
  if (action.dataset.action === "toggle-product-exception") {
    toggleProductException(action, action.closest(".group")?.dataset.sku);
    return;
  }
  const card = action.closest("[data-asset-id]");
  if (!card) return;
  const assetId = Number(card.dataset.assetId);
  if (action.dataset.action === "open-review") openReview(assetId);
  if (action.dataset.action === "quick-approve") quickApprove(action, assetId);
  if (action.dataset.action === "copy-task") copyTask(assetId);
  if (action.dataset.action === "refresh-asset") refreshAsset(action, assetId);
  if (action.dataset.action === "open-iopaint") rememberIopaintEdit(action, assetId, event);
});
$("#reviewModal").addEventListener("click", (event) => {
  if (event.target.dataset.action === "close-review") closeReview();
  const status = event.target.dataset.reviewStatus;
  if (status) saveReview(status);
  if (event.target.dataset.action === "open-iopaint" && state.current) rememberIopaintEdit(event.target, state.current.id, event);
});
$("#sourceModal").addEventListener("click", (event) => {
  if (event.target.dataset.action === "close-sources") closeSources();
});

$("#search").addEventListener("input", () => {
  saveViewState();
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => load(true), 250);
});
window.addEventListener("scroll", scheduleViewStateSave, { passive: true });
window.addEventListener("pagehide", saveViewState);
window.addEventListener("focus", () => {
  if (state.iopaintPending) setTimeout(checkIopaintResult, 250);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if ($("#suggestionModal").classList.contains("open")) closeSuggestions();
    else {
      closeReview();
      closeSources();
    }
  }
});

init();
