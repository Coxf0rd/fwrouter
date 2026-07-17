// Shared UI helpers for FWRouter pages. Keep this file framework-free.
(function () {
  const DEFAULT_JOB_POLL_TIMEOUT_MS = 45000;
  const DEFAULT_RESULT_FLASH_MS = 4500;
  const DEFAULT_RESULT_ICON_MS = 120000;

  async function fetchJson(url, opts) {
    const response = await fetch(url, opts || {});
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      const error = new Error(payload.detail || payload.error || `${response.status} ${response.statusText}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }

    return payload;
  }

  async function fetchApiV2(path, opts) {
    const response = await fetch(`/api/v2${path}`, opts || {});
    const payload = await response.json().catch(() => ({}));

    if (!response.ok || payload.ok === false) {
      const message = payload?.error?.message || payload?.detail || payload?.error || `${response.status} ${response.statusText}`;
      const error = new Error(message);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }

    return payload.data || {};
  }

  function actionMessage(error) {
    return String(
      error?.payload?.error?.message ||
      error?.message ||
      "Операция не выполнена"
    ).trim();
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
        throw new Error(data?.error?.message || job?.error_message || "Операция завершилась ошибкой");
      }

      await new Promise((resolve) => window.setTimeout(resolve, delayMs));
    }

    throw new Error("Таймаут ожидания применения");
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

    throw new Error("Изменение не подтвердилось в applied state");
  }

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

    const value = text || "";
    node.textContent = value;

    if (node.classList.contains("pill")) {
      node.hidden = !value;
    }
  }

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

  function trafficMetricLabel(key) {
    const value = String(key || "").trim();
    return ({
      direct_rx_bytes: "DIRECT вход",
      direct_tx_bytes: "DIRECT выход",
      vpn_rx_bytes: "VPN вход",
      vpn_tx_bytes: "VPN выход",
    }[value] || value || "Traffic");
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
    return String(text || "").replace(/^\s*(?:[\uD83C][\uDDE6-\uDDFF]){2}\s*/u, "").trim();
  }

  window.FwrouterUI = {
    fetchJson,
    fetchApiV2,
    actionMessage,
    pollJob,
    waitForAppliedState,
    escapeHtml,
    setText,
    setPendingState,
    setPendingStateMany,
    createPendingHelpers,
    trafficMetricLabel,
    formatTrafficBytes,
    countryCodeToFlagEmoji,
    flagEmojiToCountryCode,
    stripLeadingFlagEmoji,
  };
})();
