(function () {
  const sharedCache = new Map();
  const pickers = new Set();
  const preloadedFlagCodes = new Set();

  function closeAllPickers(except) {
    pickers.forEach((picker) => {
      if (picker !== except) picker.close();
    });
  }

  function createTablePicker(options) {
    const root = options && options.root;
    const placeholder = (options && options.placeholder) || "Выберите";
    const columns = Array.isArray(options && options.columns) ? options.columns : [];
    const alwaysOpen = Boolean(options && options.alwaysOpen);
    if (!root) return null;

    root.classList.add("picklist", "server-table", "server-table--compact");
    if (alwaysOpen) root.classList.add("picklist--always-open");
    root.innerHTML = `
      <button type="button" class="picklist__trigger" aria-haspopup="listbox" aria-expanded="false">
        <span class="picklist__trigger-text"></span>
        <span class="picklist__trigger-arrow">▾</span>
      </button>
      <div class="picklist__menu server-table__menu" hidden>
        <div class="picklist__head server-table__head"></div>
        <div class="picklist__body server-table__body" role="listbox"></div>
      </div>
    `;

    const trigger = root.querySelector(".picklist__trigger");
    const triggerText = root.querySelector(".picklist__trigger-text");
    const menu = root.querySelector(".picklist__menu");
    const head = root.querySelector(".picklist__head");
    const body = root.querySelector(".picklist__body");

    let items = [];
    let value = "";
    let currentValue = "";
    let sortKey = "";
    let sortDir = "asc";

    function headerLabel(col) {
      if (!col.sortable) return escapeHtml(col.label || "");
      const active = sortKey === col.key;
      const arrow = active ? (sortDir === "asc" ? " ↑" : " ↓") : "";
      return `<button type="button" class="picklist__sort ${active ? "is-active" : ""}" data-sort-key="${escapeHtml(col.key || "")}">${escapeHtml(col.label || "")}${arrow}</button>`;
    }

    function renderHead() {
      head.innerHTML = columns.map((col) => (
        `<div class="picklist__cell server-table__cell server-table__cell--head picklist__cell--head ${col.className || ""}">${headerLabel(col)}</div>`
      )).join("");
    }

    function normalizedSortValue(item, key) {
      const sort = item && item.sort ? item.sort : {};
      return sort[key];
    }

    function sortedItems() {
      const out = items.slice();
      if (!sortKey) return out;
      out.sort((left, right) => {
        const a = normalizedSortValue(left, sortKey);
        const b = normalizedSortValue(right, sortKey);
        if (a == null && b == null) return 0;
        if (a == null) return 1;
        if (b == null) return -1;
        if (typeof a === "number" && typeof b === "number") {
          return sortDir === "asc" ? a - b : b - a;
        }
        const aa = String(a);
        const bb = String(b);
        return sortDir === "asc" ? aa.localeCompare(bb, "ru") : bb.localeCompare(aa, "ru");
      });
      return out;
    }

    function updateTrigger() {
      const current = items.find((item) => item.value === value);
      triggerText.textContent = current ? (current.triggerLabel || current.primary || current.value) : placeholder;
      trigger.title = triggerText.textContent;
    }

    function currentBadge() {
      return '<span class="picklist__badge" data-role="current" style="margin-left:8px;padding:1px 7px;font-size:10px;line-height:1.6;white-space:nowrap">сейчас</span>';
    }

    function renderItems() {
      renderHead();
      body.innerHTML = sortedItems().map((item) => {
        const isSelected = item.value === value;
        const isCurrent = item.value === currentValue;

        const cells = (item.cells || [item.primary || item.value, item.secondary || ""]).map((cell, index) => {
          const col = columns[index] || {};
          const cls = col.className || "";

          if (isCurrent && index === 0) {
            return `<div class="picklist__cell server-table__cell ${cls}">
              <span style="display:inline-flex;align-items:center;min-width:0;max-width:100%">
                ${cell || ""}
                ${currentBadge()}
              </span>
            </div>`;
          }

          return `<div class="picklist__cell server-table__cell ${cls}">${cell || ""}</div>`;
        }).join("");

        const classes = [
          "picklist__row",
          "server-table__row",
          isSelected ? "is-selected" : "",
          isCurrent ? "is-current" : "",
        ].filter(Boolean).join(" ");

        return `<button
          type="button"
          class="${classes}"
          role="option"
          data-value="${escapeHtml(item.value)}"
          aria-selected="${isSelected ? "true" : "false"}"
          ${isCurrent ? 'aria-current="true"' : ""}
        >${cells}</button>`;
      }).join("");

      updateTrigger();
    }

    function setItems(nextItems) {
      items = Array.isArray(nextItems) ? nextItems.slice() : [];
      if (value && !items.some((item) => item.value === value)) value = "";
      if (currentValue && !items.some((item) => item.value === currentValue)) currentValue = "";
      renderItems();
    }

    function setValue(nextValue) {
      value = nextValue || "";
      renderItems();
    }

    function getValue() {
      return value || "";
    }

    function setCurrentValue(nextValue) {
      currentValue = nextValue || "";
      renderItems();
    }

    function getCurrentValue() {
      return currentValue || "";
    }

    function getCount() {
      return items.length;
    }

    function open() {
      if (alwaysOpen) {
        menu.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        root.classList.add("is-open");
        return;
      }
      closeAllPickers(api);
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      root.classList.add("is-open");
      const card = root.closest(".card");
      if (card) card.classList.add("has-open-select");
      root.dispatchEvent(new CustomEvent("pickopen", { bubbles: true }));
    }

    function close() {
      if (alwaysOpen) {
        menu.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        root.classList.add("is-open");
        return;
      }
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      root.classList.remove("is-open");
      const card = root.closest(".card");
      if (card) card.classList.remove("has-open-select");
    }

    function toggle() {
      if (menu.hidden) open();
      else close();
    }

    trigger.addEventListener("click", () => toggle());
    trigger.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " " || ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        open();
      } else if (ev.key === "Escape") {
        close();
      }
    });

    body.addEventListener("click", (ev) => {
      const row = ev.target.closest(".picklist__row");
      if (!row) return;
      value = row.dataset.value || "";
      renderItems();
      if (!alwaysOpen) close();
      root.dispatchEvent(new Event("change", { bubbles: true }));
    });

    head.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-sort-key]");
      if (!btn) return;
      ev.stopPropagation();
      const nextKey = btn.dataset.sortKey || "";
      if (!nextKey) return;
      if (sortKey === nextKey) {
        sortDir = sortDir === "asc" ? "desc" : "asc";
      } else {
        sortKey = nextKey;
        sortDir = "asc";
      }
      renderItems();
    });

    const api = {
      root,
      setItems,
      setValue,
      getValue,
      setCurrentValue,
      getCurrentValue,
      getCount,
      open,
      close,
      trigger: trigger,
    };

    pickers.add(api);
    renderHead();
    updateTrigger();
    if (alwaysOpen) open();
    return api;
  }

  function bindLazyPingSelect(options) {
    const target = options && options.target;
    const cooldownMs = Number(options && options.cooldownMs) || 180000;
    const loadData = options && options.loadData;
    const applyData = options && options.applyData;
    const getCacheKey = options && options.getCacheKey;
    const autoTrigger = !(options && options.autoTrigger === false);

    if (!target || typeof loadData !== "function" || typeof applyData !== "function" || typeof getCacheKey !== "function") {
      return { trigger: async () => {}, reset: () => {} };
    }

    const preloadTarget = target.querySelector(".picklist__trigger") || target;

    async function trigger(force) {
      const key = String(getCacheKey() || "");
      if (!key) return null;

      const now = Date.now();
      const cached = sharedCache.get(key);

      if (!force && cached && cached.data && now - cached.loadedAt < cooldownMs) {
        applyData(cached.data);
        return cached.data;
      }

      if (cached && cached.loading) {
        const data = await cached.loading;
        applyData(data);
        return data;
      }

      const loading = (async () => {
        try {
          const data = await loadData();
          sharedCache.set(key, { data, loadedAt: Date.now(), loading: null });
          return data;
        } finally {
          const next = sharedCache.get(key);
          if (next && next.loading) next.loading = null;
        }
      })();

      sharedCache.set(key, { data: null, loadedAt: 0, loading });
      const data = await loading;
      applyData(data);
      return data;
    }

    if (autoTrigger) {
      target.addEventListener("pickopen", () => { trigger(false); });
      preloadTarget.addEventListener("pointerdown", () => { trigger(false); });
      preloadTarget.addEventListener("focusin", () => { trigger(false); });
      preloadTarget.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          trigger(false);
        }
      });
    }

    return {
      trigger,
      applyCached() {
        const key = String(getCacheKey() || "");
        if (!key) return null;
        const cached = sharedCache.get(key);
        if (cached && cached.data) {
          applyData(cached.data);
          return cached.data;
        }
        return null;
      },
      reset() {
        const key = String(getCacheKey() || "");
        if (key) sharedCache.delete(key);
      },
    };
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  function flagEmojiToCountryCode(flag) {
    const chars = Array.from(String(flag || ""));
    if (chars.length !== 2) return "";
    const base = 0x1F1E6;
    const letters = chars.map((char) => {
      const point = char.codePointAt(0);
      if (point == null || point < base || point > base + 25) return "";
      return String.fromCharCode(65 + (point - base));
    }).join("");
    return /^[A-Z]{2}$/.test(letters) ? letters.toLowerCase() : "";
  }

  function renderFlaggedName(name) {
    const text = String(name || "");
    if (/^proxy(?:\s|$|\d)/i.test(text)) {
      return `<span class="picklist__label picklist__label--proxy"><span class="picklist__flag picklist__flag--proxy" aria-hidden="true">🔌</span><span class="picklist__label-text">${escapeHtml(text)}</span></span>`;
    }

    const match = text.match(/^([\u{1F1E6}-\u{1F1FF}]{2})\s*(.*)$/u);
    if (!match) return escapeHtml(text);

    const flag = match[1];
    const rest = match[2] || "";
    const code = flagEmojiToCountryCode(flag);

    const flagNode = code
      ? `<span class="picklist__flag-wrap"><img class="picklist__flag-img" src="/static/flags/${code}.svg" alt="${escapeHtml(code.toUpperCase())}" loading="eager" decoding="async" onerror="this.style.display='none';if(this.nextElementSibling){this.nextElementSibling.style.display='inline-flex';}" /><span class="picklist__flag picklist__flag--fallback" style="display:none">${escapeHtml(flag)}</span></span>`
      : `<span class="picklist__flag">${escapeHtml(flag)}</span>`;

    return `<span class="picklist__label">${flagNode}<span class="picklist__label-text">${escapeHtml(rest)}</span></span>`;
  }

  function preloadFlagsFromNames(names) {
    const list = Array.isArray(names) ? names : [];
    list.forEach((name) => {
      const text = String(name || "");
      const match = text.match(/^([\u{1F1E6}-\u{1F1FF}]{2})/u);
      if (!match) return;

      const code = flagEmojiToCountryCode(match[1]);
      if (!code || preloadedFlagCodes.has(code)) return;

      preloadedFlagCodes.add(code);
      const img = new Image();
      img.decoding = "async";
      img.src = `/static/flags/${code}.svg`;
    });
  }

  document.addEventListener("click", (ev) => {
    pickers.forEach((picker) => {
      if (!picker.root.contains(ev.target)) picker.close();
    });
  });

  window.FwrouterPingSelect = {
    createTablePicker,
    bindLazyPingSelect,
    renderFlaggedName,
    preloadFlagsFromNames,
  };
})();
