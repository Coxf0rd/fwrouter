// Shared UI helpers for FWRouter pages. Keep this file framework-free.
(function () {
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const DEFAULT_JOB_POLL_TIMEOUT_MS = 45000;
  const DEFAULT_RESULT_FLASH_MS = 4500;
  const DEFAULT_RESULT_ICON_MS = 120000;

  async function readResponsePayload(response) {
    const contentType = String(response.headers?.get?.("content-type") || "");
    if (contentType.includes("application/json")) {
      const parsed = await response.clone().json().catch(() => null);
      if (parsed !== null) return parsed;
    }
    const text = await response.text().catch(() => "");
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (_) {
      return text;
    }
  }

  function detailItemMessage(item) {
    if (!item || typeof item !== "object") return String(item || "").trim();
    const msg = String(item.msg || item.message || item.detail || "").trim();
    const loc = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
    return [loc, msg].filter(Boolean).join(": ");
  }

  function payloadMessage(value) {
    if (!value) return "";
    if (typeof value === "string") return value.trim();
    if (Array.isArray(value)) {
      return value.map(detailItemMessage).filter(Boolean).join("; ");
    }
    if (typeof value === "object") {
      return String(
        value.message ||
        value.msg ||
        value.detail ||
        value.error_message ||
        ""
      ).trim();
    }
    return String(value).trim();
  }

  function apiErrorMessage(payload, response) {
    if (payload && typeof payload === "object" && !Array.isArray(payload)) {
      return (
        payloadMessage(payload.error) ||
        payloadMessage(payload.detail) ||
        payloadMessage(payload.message) ||
        payloadMessage(payload.result?.message) ||
        payloadMessage(payload.data?.message)
      );
    }
    const fallbackStatus = Number(response?.status || 0);
    const fallbackText = String(response?.statusText || "").trim();
    const fallback = fallbackStatus ? [fallbackStatus, fallbackText].filter(Boolean).join(" ") : "";
    return payloadMessage(payload) || fallback;
  }

  function makeApiError(payload, response) {
    const message = apiErrorMessage(payload, response);
    const error = new Error(message || t("action.failed"));
    error.status = response.status;
    error.payload = payload;
    return error;
  }

  async function fetchJson(url, opts) {
    const response = await fetch(url, opts || {});
    const payload = await readResponsePayload(response);

    if (!response.ok) {
      throw makeApiError(payload, response);
    }

    return payload;
  }

  async function fetchApiV2(path, opts) {
    const response = await fetch(`/api/v2${path}`, opts || {});
    const payload = await readResponsePayload(response);

    if (!response.ok || payload.ok === false) {
      throw makeApiError(payload, response);
    }

    return payload.data || {};
  }

  function actionMessage(error) {
    return translateBackendMessage(
      apiErrorMessage(error?.payload, { status: error?.status || 0, statusText: "" }) ||
      String(error?.message || "").trim() ||
      t("action.failed")
    );
  }

  function translateBackendMessage(message) {
    const text = String(message || "").trim();
    if (!text) return "";

    return window.FwrouterI18n?.translateBackendMessage(text) || text;
  }

  async function pollJob(jobId, options) {
    const opts = options || {};
    const timeoutMs = Number(opts.timeoutMs || DEFAULT_JOB_POLL_TIMEOUT_MS);
    const delayMs = Number(opts.delayMs || 700);
    const startedAt = Date.now();

    while (Date.now() - startedAt < timeoutMs) {
      const data = await fetchApiV2(`/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
      const job = data.job || {};
      const status = String(data.status || job.status || "");

      if (typeof opts.onProgress === "function") {
        opts.onProgress(status, job);
      }

      if (status === "success") return job;
      if (status === "failed" || status === "cancelled") {
        throw new Error(
          payloadMessage(data?.error) ||
          payloadMessage(job?.error_message) ||
          t("job.failed")
        );
      }

      await new Promise((resolve) => window.setTimeout(resolve, delayMs));
    }

    throw new Error(t("job.timeout"));
  }

  async function waitForAppliedState(loadState, isApplied, options) {
    const opts = options || {};
    const timeoutMs = Number(opts.timeoutMs || 15000);
    const delayMs = Number(opts.delayMs || 600);
    const startedAt = Date.now();

    while (Date.now() - startedAt < timeoutMs) {
      await loadState();
      if (isApplied()) return true;
      await new Promise((resolve) => window.setTimeout(resolve, delayMs));
    }

    throw new Error(t("state.apply_unconfirmed"));
  }

  const DATA_STORE_TTL_MS = {
    whoami: 3000,
    servers: 8000,
    routerSummary: 2000,
    settingsWorkspace: 8000,
    settingsInventory: 10000,
    settingsDisplay: 10000,
    externalIp: 1500,
  };
  const dataStoreCache = new Map();

  function dataStoreNow() {
    return Date.now();
  }

  function encodeQuery(params) {
    const query = new URLSearchParams();
    Object.keys(params || {})
      .sort()
      .forEach((key) => {
        const value = params[key];
        if (value === undefined || value === null || value === "") return;
        query.set(key, String(value));
      });
    return query.toString();
  }

  function dataStoreKey(prefix, params) {
    const query = encodeQuery(params || {});
    return query ? `${prefix}:${query}` : prefix;
  }

  function readCached(key, ttlMs, loader, options) {
    const opts = options || {};
    const entry = dataStoreCache.get(key);
    const fresh = entry && entry.payload !== undefined && dataStoreNow() - Number(entry.loadedAt || 0) < ttlMs;

    if (!opts.force && fresh) return Promise.resolve(entry.payload);
    if (!opts.force && entry?.promise) return entry.promise;

    const nextEntry = entry || { payload: undefined, loadedAt: 0, promise: null, requestId: 0 };
    const requestId = Number(nextEntry.requestId || 0) + 1;
    nextEntry.requestId = requestId;
    const promise = Promise.resolve()
      .then(loader)
      .then((payload) => {
        if (nextEntry.requestId === requestId) {
          nextEntry.payload = payload;
          nextEntry.loadedAt = dataStoreNow();
        }
        return payload;
      })
      .finally(() => {
        if (nextEntry.requestId === requestId) {
          nextEntry.promise = null;
        }
      });

    nextEntry.promise = promise;
    dataStoreCache.set(key, nextEntry);
    return promise;
  }

  function invalidateDataStore(keys) {
    if (!keys) {
      dataStoreCache.clear();
      return;
    }

    const wanted = Array.isArray(keys) ? keys : [keys];
    wanted.forEach((item) => {
      const key = String(item || "").trim();
      if (!key) return;
      Array.from(dataStoreCache.keys()).forEach((cachedKey) => {
        if (cachedKey === key || cachedKey.startsWith(`${key}:`)) {
          dataStoreCache.delete(cachedKey);
        }
      });
    });
  }

  function normalizeServersOptions(options) {
    const opts = options || {};
    return {
      inventory_state: opts.inventory_state === undefined ? "active" : opts.inventory_state,
      vpn_auto: opts.vpn_auto,
      global_list: opts.global_list,
      include_virtual_xray_vpn_auto: opts.include_virtual_xray_vpn_auto,
      limit: opts.limit === undefined ? 1000 : opts.limit,
    };
  }

  function normalizeSettingsInventoryParams(params) {
    const opts = params || {};
    return {
      role: opts.role || "all",
      query: opts.query || "",
      limit: opts.limit === undefined ? 200 : opts.limit,
      include_inactive: opts.include_inactive === undefined ? false : Boolean(opts.include_inactive),
      live_observations: opts.live_observations === undefined ? true : Boolean(opts.live_observations),
    };
  }

  const FwrouterDataStore = {
    getWhoami(options) {
      const opts = options || {};
      return readCached(
        "whoami",
        DATA_STORE_TTL_MS.whoami,
        () => fetchApiV2("/ui/whoami", { cache: "no-store" }),
        opts
      );
    },
    getServers(options) {
      const params = normalizeServersOptions(options);
      const key = dataStoreKey("servers", params);
      const query = encodeQuery(params);
      return readCached(
        key,
        DATA_STORE_TTL_MS.servers,
        () => fetchApiV2(`/servers?${query}`, { cache: "no-store" }),
        options || {}
      );
    },
    getRouterSummary(options) {
      const opts = options || {};
      return readCached(
        "routerSummary",
        DATA_STORE_TTL_MS.routerSummary,
        () => fetchApiV2("/ui/router-summary", { cache: "no-store" }),
        opts
      );
    },
    getSettingsWorkspace(options) {
      const opts = options || {};
      return readCached(
        "settingsWorkspace",
        DATA_STORE_TTL_MS.settingsWorkspace,
        () => fetchApiV2("/ui/settings/workspace", { cache: "no-store" }),
        opts
      );
    },
    getSettingsInventory(params) {
      const normalized = normalizeSettingsInventoryParams(params);
      const key = dataStoreKey("settingsInventory", normalized);
      const query = encodeQuery(normalized);
      return readCached(
        key,
        DATA_STORE_TTL_MS.settingsInventory,
        () => fetchApiV2(`/ui/settings/inventory?${query}`, { cache: "no-store", signal: params?.signal }),
        params || {}
      );
    },
    getSettingsDisplay(options) {
      const opts = options || {};
      return readCached(
        "settingsDisplay",
        DATA_STORE_TTL_MS.settingsDisplay,
        () => fetchApiV2("/ui/settings/display", { cache: "no-store" }),
        opts
      );
    },
    getExternalIp(options) {
      const opts = options || {};
      return readCached(
        "externalIp",
        DATA_STORE_TTL_MS.externalIp,
        () => fetchApiV2("/ui/external-ip", { cache: "no-store" }),
        opts
      );
    },
    invalidate: invalidateDataStore,
    clear() {
      dataStoreCache.clear();
    },
  };

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function setText(id, text) {
    const node = document.getElementById(id);
    if (!node) return;

    delete node.dataset.dynamicStatusKey;
    delete node.dataset.dynamicStatusParams;

    const value = text || "";
    node.textContent = value;

    if (node.classList.contains("pill")) {
      node.hidden = !value;
    }
  }

  function _renderDynamicStatus(node) {
    if (!node) return;
    const key = String(node.dataset.dynamicStatusKey || "");
    if (!key) return;
    let params = {};
    try {
      const parsed = JSON.parse(node.dataset.dynamicStatusParams || "{}");
      params = parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      params = {};
    }
    const value = t(key, params);
    node.textContent = value || "";
    if (node.classList.contains("pill")) node.hidden = !value;
  }

  function setDynamicStatus(id, key, params) {
    const node = document.getElementById(id);
    if (!node) return;
    const statusKey = String(key || "");
    if (!statusKey) {
      setText(id, "");
      return;
    }
    node.dataset.dynamicStatusKey = statusKey;
    try {
      node.dataset.dynamicStatusParams = JSON.stringify(params || {});
    } catch (_) {
      node.dataset.dynamicStatusParams = "{}";
    }
    _renderDynamicStatus(node);
  }

  function clearDynamicStatus(id) {
    setText(id, "");
  }

  document.addEventListener("fwrouter:locale", () => {
    document.querySelectorAll("[data-dynamic-status-key]").forEach((node) => {
      _renderDynamicStatus(node);
    });
  });

  function setPendingState(node, pending) {
    if (!node) return;

    if (pending) {
      if (!node.hasAttribute("data-pending-prev-disabled")) {
        node.setAttribute("data-pending-prev-disabled", node.disabled ? "1" : "0");
      }
      node.disabled = true;
      node.classList.add("is-pending");
      node.setAttribute("aria-busy", "true");
      return;
    }

    const prevDisabled = node.getAttribute("data-pending-prev-disabled") === "1";
    node.disabled = prevDisabled;
    node.classList.remove("is-pending");
    node.removeAttribute("aria-busy");
    node.removeAttribute("data-pending-prev-disabled");
  }

  function setPendingStateMany(nodes, pending) {
    (Array.isArray(nodes) ? nodes : []).forEach((node) => setPendingState(node, pending));
  }

  function createPendingHelpers(scopeSelectors, options) {
    const selectors = Array.isArray(scopeSelectors) ? scopeSelectors : [];
    const opts = options || {};
    const resultFlashMs = Number(opts.resultFlashMs || DEFAULT_RESULT_FLASH_MS);
    const resultIconMs = Number(opts.resultIconMs || DEFAULT_RESULT_ICON_MS);

    function findPendingScope(node) {
      if (!node || typeof node.closest !== "function" || !selectors.length) return null;
      return node.closest(selectors.join(", "));
    }

    function setPendingScope(node, pending) {
      const scope = findPendingScope(node);
      if (!scope) return;
      scope.classList.toggle("is-pending-scope", Boolean(pending));
      scope.setAttribute("aria-busy", pending ? "true" : "false");
      if (pending) {
        scope.classList.remove("is-success-scope", "is-error-scope", "has-result-icon");
        scope.removeAttribute("data-result-icon");
      }
    }

    function flashScopeResult(node, tone) {
      const scope = findPendingScope(node);
      if (!scope) return;
      window.clearTimeout(Number(scope.dataset.resultFlashTimer || 0));
      window.clearTimeout(Number(scope.dataset.resultIconTimer || 0));
      scope.classList.remove("is-success-scope", "is-error-scope");
      if (tone !== "success" && tone !== "error") return;
      scope.dataset.resultIcon = tone === "success" ? "✓" : "×";
      scope.classList.add("has-result-icon");
      scope.classList.add(tone === "success" ? "is-success-scope" : "is-error-scope");
      scope.dataset.resultFlashTimer = String(window.setTimeout(() => {
        scope.classList.remove("is-success-scope", "is-error-scope");
        scope.removeAttribute("data-result-flash-timer");
      }, resultFlashMs));
      scope.dataset.resultIconTimer = String(window.setTimeout(() => {
        scope.classList.remove("has-result-icon");
        scope.removeAttribute("data-result-icon");
        scope.removeAttribute("data-result-icon-timer");
      }, resultIconMs));
    }

    return { findPendingScope, setPendingScope, flashScopeResult };
  }

  function trafficMetricLabel(metric, fallbackLabel) {
    const isMetricObject = metric && typeof metric === "object";
    const value = String(isMetricObject ? metric.key : metric || "").trim();
    const label = String(isMetricObject ? metric.label : fallbackLabel || "").trim();
    return ({
      direct_rx_bytes: t("traffic.direct_rx_bytes"),
      direct_tx_bytes: t("traffic.direct_tx_bytes"),
      vpn_rx_bytes: t("traffic.vpn_rx_bytes"),
      vpn_tx_bytes: t("traffic.vpn_tx_bytes"),
    }[value] || label || value || t("traffic.generic"));
  }

  function formatTrafficBytes(bytes) {
    const value = Number(bytes || 0);
    if (!Number.isFinite(value) || value <= 0) return "0 B";

    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = value;
    let idx = 0;
    while (size >= 1024 && idx < units.length - 1) {
      size /= 1024;
      idx += 1;
    }
    const precision = idx <= 1 ? 0 : 1;
    return `${size.toFixed(precision)} ${units[idx]}`;
  }

  function countryCodeToFlagEmoji(code) {
    const value = String(code || "").trim().toUpperCase();
    if (!/^[A-Z]{2}$/.test(value)) return "";

    return Array.from(value)
      .map((char) => String.fromCodePoint(127397 + char.charCodeAt(0)))
      .join("");
  }

  function flagEmojiToCountryCode(text) {
    const chars = Array.from(String(text || "").trim());
    if (chars.length < 2) return "";
    const codes = chars.slice(0, 2).map((char) => char.codePointAt(0) - 127397);
    if (codes.some((code) => code < 65 || code > 90)) return "";
    return String.fromCharCode(...codes).toLowerCase();
  }

  function stripLeadingFlagEmoji(text) {
    return String(text || "").replace(/^\s*[\u{1F1E6}-\u{1F1FF}]{2}\s*/u, "").trim();
  }

  window.FwrouterUI = {
    fetchJson,
    fetchApiV2,
    actionMessage,
    apiErrorMessage,
    translateBackendMessage,
    pollJob,
    waitForAppliedState,
    escapeHtml,
    setText,
    setDynamicStatus,
    clearDynamicStatus,
    setPendingState,
    setPendingStateMany,
    createPendingHelpers,
    trafficMetricLabel,
    formatTrafficBytes,
    countryCodeToFlagEmoji,
    flagEmojiToCountryCode,
    stripLeadingFlagEmoji,
  };
  window.FwrouterDataStore = FwrouterDataStore;
})();
