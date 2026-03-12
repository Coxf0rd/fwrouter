(function () {
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const params = new URLSearchParams(window.location.search);
  const forcedHaEmbed = (params.get("embed") || "").toLowerCase() === "ha";
  const isEmbedded = forcedHaEmbed || window.self !== window.top;
  const isHaUi = forcedHaEmbed;
  const requestedView = (params.get("view") || "").toLowerCase();
  const requestedPanel = (params.get("panel") || "").toLowerCase();
  const initialView = (requestedView === "admin" || requestedView === "user") ? requestedView : "user";
  const isHaMobile = isHaUi && window.matchMedia("(max-width: 760px)").matches;
  const panelState = { user: "all", admin: "all" };
  const panelAllowed = {
    user: new Set(["all", "server", "routing", "subscription", "stats"]),
    admin: new Set(["all", "autolist", "selective", "devices", "rules"]),
  };

  function applyPanelFilter() {
    const view = document.documentElement.dataset.view || "user";
    const activePanel = panelState[view] || "all";

    $$("section.card[data-scope][data-section]").forEach((card) => {
      const scope = card.getAttribute("data-scope");
      const section = card.getAttribute("data-section");
      if (scope !== view) {
        card.style.display = "none";
        return;
      }
      card.style.display = (activePanel === "all" || section === activePanel) ? "" : "none";
    });

    $$("button[data-panel-view][data-panel]").forEach((btn) => {
      const on = btn.dataset.panelView === view && btn.dataset.panel === activePanel;
      btn.classList.toggle("is-active", on);
    });
  }

  function setPanel(view, panel) {
    if (!panelAllowed[view] || !panelAllowed[view].has(panel)) return;
    panelState[view] = panel;
    applyPanelFilter();
  }

  function setView(view) {
    document.documentElement.dataset.view = view;

    $$(".seg__btn[data-view]").forEach(b => b.classList.toggle("is-active", b.dataset.view === view));
    $$('[data-scope]').forEach(el => {
      const scope = el.getAttribute('data-scope');
      el.style.display = (scope === view) ? '' : 'none';
    });
    if (isHaUi) applyPanelFilter();
    document.dispatchEvent(new CustomEvent("fwrouter:view", { detail: { view } }));
  }

  function initGlassSelects() {
    const selector = "select.input";

    function closeAllGlass(except) {
      $$(".glass-select").forEach((wrap) => {
        if (except && wrap === except) return;
        wrap.classList.remove("is-open");
        const card = wrap.closest("section.card");
        if (card) card.classList.remove("has-open-select");
        const btn = wrap.querySelector(".glass-select__trigger");
        if (btn) btn.setAttribute("aria-expanded", "false");
      });
    }

    function createGlassSelect(select) {
      if (!select || select.dataset.glassInit === "1") return;
      select.dataset.glassInit = "1";

      const wrap = document.createElement("div");
      wrap.className = "glass-select";

      const trigger = document.createElement("button");
      trigger.type = "button";
      trigger.className = "glass-select__trigger";
      trigger.setAttribute("aria-expanded", "false");

      const triggerLabel = document.createElement("span");
      triggerLabel.className = "glass-select__label";
      const triggerArrow = document.createElement("span");
      triggerArrow.className = "glass-select__arrow";
      triggerArrow.textContent = "▾";
      trigger.appendChild(triggerLabel);
      trigger.appendChild(triggerArrow);

      const menu = document.createElement("div");
      menu.className = "glass-select__menu";

      select.classList.add("glass-select__native");
      const parent = select.parentNode;
      parent.insertBefore(wrap, select);
      wrap.appendChild(select);
      wrap.appendChild(trigger);
      wrap.appendChild(menu);

      function rebuildMenu() {
        menu.innerHTML = "";
        const items = Array.from(select.options || []);
        items.forEach((opt) => {
          const item = document.createElement("button");
          item.type = "button";
          item.className = "glass-select__option";
          item.textContent = opt.textContent || opt.label || opt.value;
          item.dataset.value = opt.value;
          if (opt.disabled) item.disabled = true;
          if (opt.value === select.value) item.classList.add("is-selected");
          item.addEventListener("click", () => {
            if (opt.disabled) return;
            select.value = opt.value;
            select.dispatchEvent(new Event("change", { bubbles: true }));
            syncFromSelect();
            closeAllGlass();
          });
          menu.appendChild(item);
        });
      }

      function syncFromSelect() {
        const current = select.options[select.selectedIndex];
        triggerLabel.textContent = current ? (current.textContent || current.value) : "";
        menu.querySelectorAll(".glass-select__option").forEach((node) => {
          node.classList.toggle("is-selected", node.dataset.value === select.value);
        });
        trigger.disabled = !!select.disabled;
      }

      trigger.addEventListener("click", () => {
        if (trigger.disabled) return;
        const open = !wrap.classList.contains("is-open");
        closeAllGlass(wrap);
        wrap.classList.toggle("is-open", open);
        const card = wrap.closest("section.card");
        if (card) card.classList.toggle("has-open-select", open);
        trigger.setAttribute("aria-expanded", open ? "true" : "false");
      });

      select.addEventListener("change", syncFromSelect);

      const optionObserver = new MutationObserver(() => {
        rebuildMenu();
        syncFromSelect();
      });
      optionObserver.observe(select, { childList: true, subtree: true, attributes: true });

      rebuildMenu();
      syncFromSelect();
    }

    function scan() {
      $$(selector).forEach((select) => createGlassSelect(select));
    }

    scan();

    const rootObserver = new MutationObserver(() => scan());
    rootObserver.observe(document.body, { childList: true, subtree: true });

    document.addEventListener("click", (ev) => {
      if (!ev.target.closest(".glass-select")) closeAllGlass();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") closeAllGlass();
    });
    // Keep dropdown open while user scrolls inside it on mobile/HA WebView.
  }

  function init() {
    if (isHaUi) {
      document.documentElement.dataset.embed = "ha";
      document.body.classList.add("ha-embed");
    }

    if (!isHaMobile && isHaUi && panelAllowed[initialView] && panelAllowed[initialView].has(requestedPanel)) {
      panelState[initialView] = requestedPanel;
    }
    if (isHaMobile) {
      panelState.user = "all";
      panelState.admin = "all";
    }

    if (isHaUi && !isHaMobile) {
      const menuBtn = document.getElementById("panelMenuToggle");
      const drawer = document.getElementById("panelDrawer");
      const backdrop = document.getElementById("panelDrawerBackdrop");
      const setDrawer = (open) => {
        document.body.classList.toggle("panel-menu-open", open);
        if (menuBtn) menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
        if (drawer) drawer.setAttribute("aria-hidden", open ? "false" : "true");
        if (backdrop) backdrop.hidden = !open;
      };
      if (menuBtn) {
        menuBtn.addEventListener("click", () => {
          setDrawer(!document.body.classList.contains("panel-menu-open"));
        });
      }
      if (backdrop) {
        backdrop.addEventListener("click", () => setDrawer(false));
      }
      document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") setDrawer(false);
      });
    }

    if (isHaUi) {
      $$("button[data-panel-view][data-panel]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const view = btn.dataset.panelView;
          const panel = btn.dataset.panel;
          setPanel(view, panel);
          document.body.classList.remove("panel-menu-open");
          const menuBtn = document.getElementById("panelMenuToggle");
          if (menuBtn) menuBtn.setAttribute("aria-expanded", "false");
          const drawer = document.getElementById("panelDrawer");
          if (drawer) drawer.setAttribute("aria-hidden", "true");
          const backdrop = document.getElementById("panelDrawerBackdrop");
          if (backdrop) backdrop.hidden = true;
        });
      });
    }
    if (isHaUi) initGlassSelects();
    setView(initialView);

    $$(".seg__btn[data-view]").forEach(btn => {
      btn.addEventListener("click", () => {
        setView(btn.dataset.view);
        if (isHaUi) document.body.classList.remove("panel-menu-open");
        if (isHaUi) {
          const menuBtn = document.getElementById("panelMenuToggle");
          if (menuBtn) menuBtn.setAttribute("aria-expanded", "false");
          const drawer = document.getElementById("panelDrawer");
          if (drawer) drawer.setAttribute("aria-hidden", "true");
          const backdrop = document.getElementById("panelDrawerBackdrop");
          if (backdrop) backdrop.hidden = true;
        }
      });
    });
  }

  window.addEventListener("DOMContentLoaded", init);
})();
