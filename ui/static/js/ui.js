(() => {
  const VIEW_STORAGE_KEY = "fwrouter:view";
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const params = new URLSearchParams(window.location.search);
  const requestedView = (params.get("view") || "").toLowerCase();
  const storedView = (() => {
    try {
      return (window.localStorage.getItem(VIEW_STORAGE_KEY) || "").toLowerCase();
    } catch (_) {
      return "";
    }
  })();

  const isAllowedView = (value) => value === "user" || value === "admin" || value === "settings";
  const initialView = isAllowedView(requestedView)
    ? requestedView
    : (isAllowedView(storedView) ? storedView : "user");

  function persistView(view) {
    try {
      window.localStorage.setItem(VIEW_STORAGE_KEY, view);
    } catch (_) {
      // Ignore storage errors in restricted browsers/webviews.
    }
  }

  function setView(view) {
    if (!isAllowedView(view)) return;
    document.documentElement.dataset.view = view;
    persistView(view);

    $$(".seg__btn[data-view]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.view === view);
    });

    $$("[data-scope]").forEach((node) => {
      node.style.display = node.getAttribute("data-scope") === view ? "" : "none";
    });

    document.dispatchEvent(new CustomEvent("fwrouter:view", { detail: { view } }));
  }

  function init() {
    setView(initialView);
    $$(".seg__btn[data-view]").forEach((btn) => {
      btn.addEventListener("click", () => setView(btn.dataset.view || "user"));
    });
  }

  window.addEventListener("DOMContentLoaded", init);
})();

// reusable liquid-glass select
(function () {
  const SELECTOR = "select[data-lg-select], select.input";

  const state = {
    opened: null,
    menu: null,
    items: [],
    focusedIndex: -1,
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

  function getOptionLabel(option) {
    return option ? (option.textContent || option.value || "").trim() : "";
  }

  function closeSelect() {
    if (!state.opened) return;

    state.opened.classList.remove("is-open");
    const trigger = state.opened.querySelector(".lg-select__trigger");
    if (trigger) trigger.setAttribute("aria-expanded", "false");

    if (state.menu) {
      state.menu.remove();
      state.menu = null;
    }

    state.opened = null;
    state.items = [];
    state.focusedIndex = -1;
  }

  function syncSelect(wrapper) {
    if (!wrapper) return;

    const select = wrapper.querySelector("select");
    const value = wrapper.querySelector(".lg-select__value");
    if (!select || !value) return;

    const option = select.options[select.selectedIndex];
    value.textContent = getOptionLabel(option) || "—";
    wrapper.dataset.value = select.value || "";
  }

  function positionMenu(wrapper, menu) {
    const trigger = wrapper.querySelector(".lg-select__trigger");
    if (!trigger || !menu) return;

    const rect = trigger.getBoundingClientRect();
    const gap = 6;
    const viewportPadding = 8;

    menu.style.minWidth = `${rect.width}px`;
    menu.style.width = `${rect.width}px`;

    let left = rect.left;
    let top = rect.bottom + gap;

    const estimatedHeight = Math.min(menu.scrollHeight || 280, 280);
    const bottomOverflow = top + estimatedHeight + viewportPadding - window.innerHeight;

    if (bottomOverflow > 0 && rect.top > estimatedHeight + gap) {
      top = rect.top - estimatedHeight - gap;
      menu.style.transformOrigin = "bottom center";
    } else {
      menu.style.transformOrigin = "top center";
    }

    left = Math.max(viewportPadding, Math.min(left, window.innerWidth - rect.width - viewportPadding));
    top = Math.max(viewportPadding, Math.min(top, window.innerHeight - viewportPadding));

    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
  }

  function setFocused(index) {
    state.focusedIndex = index;

    state.items.forEach((item, itemIndex) => {
      item.classList.toggle("is-focused", itemIndex === index);
      if (itemIndex === index) {
        item.scrollIntoView({ block: "nearest" });
      }
    });
  }

  function chooseOption(wrapper, option) {
    const select = wrapper.querySelector("select");
    if (!select || !option || option.disabled) return;

    select.value = option.value;
    syncSelect(wrapper);

    select.dispatchEvent(new Event("change", { bubbles: true }));

    closeSelect();
    wrapper.querySelector(".lg-select__trigger")?.focus();
  }

  function openSelect(wrapper) {
    if (!wrapper) return;

    if (state.opened === wrapper) {
      closeSelect();
      return;
    }

    closeSelect();

    const select = wrapper.querySelector("select");
    const trigger = wrapper.querySelector(".lg-select__trigger");
    if (!select || !trigger || select.disabled) return;

    const menu = document.createElement("div");
    menu.className = "lg-select__menu";
    menu.setAttribute("role", "listbox");

    const options = Array.from(select.options);

    menu.innerHTML = options.map((option, index) => {
      const selected = option.value === select.value;
      const disabled = option.disabled;

      return `<div
        class="lg-select__option${selected ? " is-selected" : ""}${disabled ? " is-disabled" : ""}"
        role="option"
        aria-selected="${selected ? "true" : "false"}"
        data-value="${escapeHtml(option.value)}"
        data-index="${index}"
      >${escapeHtml(getOptionLabel(option))}</div>`;
    }).join("");

    document.body.appendChild(menu);

    state.opened = wrapper;
    state.menu = menu;
    state.items = Array.from(menu.querySelectorAll(".lg-select__option"));
    state.focusedIndex = Math.max(0, options.findIndex((option) => option.value === select.value));

    wrapper.classList.add("is-open");
    trigger.setAttribute("aria-expanded", "true");

    positionMenu(wrapper, menu);
    setFocused(state.focusedIndex);

    menu.addEventListener("mousedown", (event) => {
      event.preventDefault();
    });

    menu.addEventListener("click", (event) => {
      const item = event.target.closest(".lg-select__option");
      if (!item) return;

      const option = select.options[Number(item.dataset.index)];
      chooseOption(wrapper, option);
    });
  }

  function enhanceSelect(select) {
    if (!select || select.dataset.lgSelectReady === "1") {
      if (select && select.closest(".lg-select")) syncSelect(select.closest(".lg-select"));
      return;
    }

    const wrapper = document.createElement("div");
    wrapper.className = "lg-select";
    wrapper.dataset.lgSelect = "ready";

    const trigger = document.createElement("button");
    trigger.className = "lg-select__trigger";
    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");

    trigger.innerHTML = `
      <span class="lg-select__value"></span>
      <span class="lg-select__arrow" aria-hidden="true"></span>
    `;

    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);
    wrapper.appendChild(trigger);

    select.dataset.lgSelectReady = "1";
    select.classList.add("lg-select__native");

    trigger.addEventListener("click", () => openSelect(wrapper));

    trigger.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
        event.preventDefault();

        if (!wrapper.classList.contains("is-open")) {
          openSelect(wrapper);
          return;
        }

        setFocused(Math.min(state.items.length - 1, state.focusedIndex + 1));
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();

        if (!wrapper.classList.contains("is-open")) {
          openSelect(wrapper);
          return;
        }

        setFocused(Math.max(0, state.focusedIndex - 1));
        return;
      }

      if (event.key === "Escape") {
        event.preventDefault();
        closeSelect();
        return;
      }

      if (event.key === "Tab") {
        closeSelect();
        return;
      }

      if (event.key === "Enter" && wrapper.classList.contains("is-open")) {
        event.preventDefault();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (!state.opened || state.opened !== wrapper) return;

      if (event.key === "Enter") {
        event.preventDefault();
        const selectNode = wrapper.querySelector("select");
        const option = selectNode?.options[state.focusedIndex];
        chooseOption(wrapper, option);
      }
    });

    select.addEventListener("change", () => syncSelect(wrapper));

    syncSelect(wrapper);
  }

  function enhance(root) {
    const scope = root || document;
    const selects = Array.from(scope.querySelectorAll(SELECTOR));

    selects.forEach((select) => {
      if (select.classList.contains("user-hidden-select")) return;
      enhanceSelect(select);
    });
  }

  document.addEventListener("click", (event) => {
    if (!state.opened) return;

    const clickedInsideWrapper = event.target.closest(".lg-select") === state.opened;
    const clickedInsideMenu = state.menu && state.menu.contains(event.target);

    if (!clickedInsideWrapper && !clickedInsideMenu) {
      closeSelect();
    }
  });

  window.addEventListener("scroll", () => {
    if (state.opened && state.menu) positionMenu(state.opened, state.menu);
  }, true);

  window.addEventListener("resize", () => {
    if (state.opened && state.menu) positionMenu(state.opened, state.menu);
  });

  window.FwrouterLiquidSelect = {
    enhance,
    enhanceSelect,
    refresh(root) {
      const scope = root || document;
      Array.from(scope.querySelectorAll(".lg-select")).forEach(syncSelect);
      enhance(scope);
    },
    close: closeSelect,
  };

  window.addEventListener("DOMContentLoaded", () => {
    enhance(document);
  });
})();