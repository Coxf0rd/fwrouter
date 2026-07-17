(function () {
  function wrapButtonContent(btn) {
    if (!btn || btn.dataset.liquidBuilt === "1") return;

    btn.dataset.liquidBuilt = "1";

    const glass = document.createElement("span");
    glass.className = "liquid-btn__glass";
    glass.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "liquid-btn__label";

    while (btn.firstChild) {
      label.appendChild(btn.firstChild);
    }

    btn.appendChild(glass);
    btn.appendChild(label);
  }

  function getDirectSegmentButtons(nav) {
    return Array.from(nav.children).filter(function (node) {
      return node.classList && node.classList.contains("seg__btn");
    });
  }

  function initLiquidSegment(nav) {
    if (!nav || nav.dataset.liquidGlassReady === "1") return;

    const buttons = getDirectSegmentButtons(nav);
    if (!buttons.length) return;

    nav.dataset.liquidGlassReady = "1";
    nav.classList.add("seg--liquid");

    buttons.forEach(wrapButtonContent);

    const lens = document.createElement("span");
    lens.className = "liquid-seg__lens";
    lens.setAttribute("aria-hidden", "true");
    nav.insertBefore(lens, nav.firstChild);

    function getActiveButton() {
      return buttons.find(function (btn) {
        return btn.classList.contains("is-active") && !btn.hidden;
      }) || buttons.find(function (btn) {
        return !btn.hidden;
      }) || null;
    }

    function setGlowVars(target, clientX, clientY) {
      if (!target) return;

      const rect = target.getBoundingClientRect();
      if (!rect.width || !rect.height) return;

      const x = ((clientX - rect.left) / rect.width) * 100;
      const y = ((clientY - rect.top) / rect.height) * 100;

      target.style.setProperty("--lx", `${Math.max(10, Math.min(90, x))}%`);
      target.style.setProperty("--ly", `${Math.max(8, Math.min(70, y))}%`);
    }

    function resetGlowVars(target) {
      if (!target) return;
      target.style.setProperty("--lx", "50%");
      target.style.setProperty("--ly", "22%");
    }

    function moveLens(immediate) {
      const active = getActiveButton();

      if (!active) {
        lens.style.opacity = "0";
        return;
      }

      const navRect = nav.getBoundingClientRect();
      const activeRect = active.getBoundingClientRect();

      if (!navRect.width || !activeRect.width) {
        lens.style.opacity = "0";
        return;
      }

      const x = activeRect.left - navRect.left;
      const y = activeRect.top - navRect.top;

      const apply = function () {
        lens.style.transform = `translate3d(${x}px, ${y}px, 0)`;
        lens.style.width = `${activeRect.width}px`;
        lens.style.height = `${activeRect.height}px`;
        lens.style.borderRadius = getComputedStyle(active).borderRadius;
        lens.style.opacity = "1";
      };

      if (immediate) {
        const previousTransition = lens.style.transition;
        lens.style.transition = "none";
        apply();
        void lens.offsetHeight;
        lens.style.transition = previousTransition;
        return;
      }

      apply();
    }

    function scheduleMove(immediate) {
      window.requestAnimationFrame(function () {
        moveLens(Boolean(immediate));
      });
    }

    buttons.forEach(function (btn) {
      resetGlowVars(btn);

      btn.addEventListener("pointermove", function (event) {
        setGlowVars(btn, event.clientX, event.clientY);
      });

      btn.addEventListener("pointerleave", function () {
        resetGlowVars(btn);
      });

      btn.addEventListener("focus", function () {
        scheduleMove(false);
      });
    });

    resetGlowVars(lens);

    nav.addEventListener("pointermove", function (event) {
      setGlowVars(lens, event.clientX, event.clientY);
    });

    nav.addEventListener("pointerleave", function () {
      resetGlowVars(lens);
    });

    nav.addEventListener("click", function (event) {
      const btn = event.target.closest(".seg__btn");
      if (!btn || !nav.contains(btn) || !buttons.includes(btn)) return;

      window.requestAnimationFrame(function () {
        scheduleMove(false);

        window.setTimeout(function () {
          scheduleMove(false);
        }, 120);
      });
    });

    const observer = new MutationObserver(function () {
      scheduleMove(false);
    });

    observer.observe(nav, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["class", "style", "hidden", "aria-hidden"]
    });

    const resizeObserver = new ResizeObserver(function () {
      scheduleMove(true);
    });

    resizeObserver.observe(nav);

    buttons.forEach(function (btn) {
      resizeObserver.observe(btn);
    });

    window.addEventListener("resize", function () {
      scheduleMove(true);
    });

    document.addEventListener("fwrouter:view", function () {
      scheduleMove(true);
    });

    scheduleMove(true);
  }

  function initAllLiquidSegments() {
    document.querySelectorAll("[data-liquid-seg]").forEach(initLiquidSegment);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAllLiquidSegments);
  } else {
    initAllLiquidSegments();
  }
})();