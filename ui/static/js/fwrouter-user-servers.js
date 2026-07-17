// User view server label/flag rendering helpers.
(function () {
  const {
    escapeHtml,
    countryCodeToFlagEmoji,
    flagEmojiToCountryCode,
    stripLeadingFlagEmoji,
  } = window.FwrouterUI;

  function parseCurrentServerName(name) {
    const raw = String(name || "DIRECT").trim();

    if (!raw || raw === "DIRECT") {
      return {
        code: "",
        title: "Direct",
        protocol: "DIRECT",
        full: "DIRECT",
      };
    }

    const main = raw.split("|")[0].trim();
    const parts = raw.split("|").map((item) => item.trim()).filter(Boolean);
    const emojiMatch = main.match(/^([\u{1F1E6}-\u{1F1FF}]{2})\s*(.*)$/u);
    const codeMatch = main.match(/^([a-z]{2})\s+(.+)$/i);

    const code = emojiMatch
      ? flagEmojiToCountryCode(emojiMatch[1])
      : (codeMatch ? String(codeMatch[1] || "").toLowerCase() : "");
    const cleanTitle = emojiMatch
      ? (String(emojiMatch[2] || "").trim() || stripLeadingFlagEmoji(main) || main)
      : (codeMatch ? String(codeMatch[2] || main).trim() : main);

    if (parts.length <= 1) {
      return {
        code,
        title: cleanTitle,
        protocol: "VPN",
        full: raw,
      };
    }

    return {
      code,
      title: cleanTitle,
      protocol: parts.slice(1).join(" · "),
      full: raw,
    };
  }

  function getServerDisplayName(server) {
    if (typeof server === "string") return server.trim();

    return String(
      server?.label ||
      server?.name ||
      server?.title ||
      server?.server ||
      ""
    ).trim();
  }

  function getServerCountryCode(server) {
    if (server && typeof server !== "string" && (server.countryCode || server.country_code)) {
      return String(server.countryCode || server.country_code).trim().toLowerCase().slice(0, 2);
    }

    const name = getServerDisplayName(server);
    const emojiMatch = name.match(/^([\u{1F1E6}-\u{1F1FF}]{2})/u);
    if (emojiMatch) {
      const rendered = window.FwrouterPingSelect?.renderFlaggedName
        ? window.FwrouterPingSelect.renderFlaggedName(`${emojiMatch[1]}`)
        : "";
      const codeMatch = rendered.match(/\/static\/flags\/([a-z]{2})\.svg/i);
      if (codeMatch) return codeMatch[1].toLowerCase();
    }
    const match = name.match(/^([a-z]{2})\s+/i);
    return match ? match[1].toLowerCase() : "";
  }

  function getServerCleanLabel(server) {
    const name = getServerDisplayName(server);
    const emojiMatch = name.match(/^([\u{1F1E6}-\u{1F1FF}]{2})\s*(.*)$/u);
    if (emojiMatch) return String(emojiMatch[2] || "").trim() || name;
    return name.replace(/^([a-z]{2})\s+/i, "").trim() || name;
  }

  function renderServerFlag(server, className) {
    const code = getServerCountryCode(server);
    if (!/^[a-z]{2}$/.test(code)) return "";

    const safeCode = escapeHtml(code);
    const upper = escapeHtml(code.toUpperCase());
    const flagClass = className || "picklist__flag";
    const fallbackFlag = escapeHtml(countryCodeToFlagEmoji(code) || code.toUpperCase());

    return `
      <span class="picklist__flag-wrap" aria-label="${upper}">
        <img
          class="${flagClass}"
          src="/static/flags/${safeCode}.svg"
          alt="${upper}"
          loading="lazy"
          decoding="async"
          onerror="this.style.display='none';if(this.nextElementSibling){this.nextElementSibling.style.display='inline-flex';}"
        />
        <span class="picklist__flag picklist__flag--fallback" style="display:none">${fallbackFlag}</span>
      </span>
    `;
  }

  function renderServerListName(server) {
    const fullName = getServerDisplayName(server);
    const label = getServerCleanLabel(server);
    const flag = renderServerFlag(server, "picklist__flag");

    if (!fullName) return "—";

    if (!flag) {
      if (window.FwrouterPingSelect?.renderFlaggedName) {
        return window.FwrouterPingSelect.renderFlaggedName(fullName);
      }
      return `
        <span class="picklist__label">
          <span class="picklist__label-text">${escapeHtml(fullName)}</span>
        </span>
      `;
    }

    return `
      <span class="picklist__label picklist__label--with-flag">
        ${flag}
        <span class="picklist__label-text">${escapeHtml(label)}</span>
      </span>
    `;
  }

  function renderCurrentServerTitle(parsed) {
    const data = parsed || {};
    const title = String(data.title || "").trim();
    const code = String(data.code || "").trim().toLowerCase();

    if (!title || title === "Direct" || title === "DIRECT") {
      return escapeHtml(title || "Direct");
    }

    if (!/^[a-z]{2}$/.test(code)) {
      if (/^proxy(?:\s|$|\d)/i.test(title)) {
        return `<span class="picklist__label current-server-label current-server-label--proxy"><span class="picklist__flag picklist__flag--proxy" aria-hidden="true">🔌</span><span class="picklist__label-text">${escapeHtml(title)}</span></span>`;
      }
      return escapeHtml(title);
    }

    const upper = code.toUpperCase();
    const fallbackFlag = escapeHtml(countryCodeToFlagEmoji(code) || upper);

    return `
      <span class="picklist__label current-server-label">
        <span class="current-server-flag" aria-label="${escapeHtml(upper)}">
          <span class="current-server-flag__fallback">${fallbackFlag}</span>
          <img
            class="current-server-flag__img"
            src="/static/flags/${escapeHtml(code)}.svg"
            alt="${escapeHtml(upper)}"
            loading="eager"
            decoding="async"
            onload="this.classList.add('is-loaded')"
            onerror="this.remove()"
          />
        </span>
        <span class="picklist__label-text">${escapeHtml(title)}</span>
      </span>
    `;
  }

  function preloadCurrentServerFlag(parsed) {
    const code = String((parsed && parsed.code) || "").trim().toLowerCase();
    if (!/^[a-z]{2}$/.test(code)) return;

    const img = new Image();
    img.decoding = "async";
    img.src = `/static/flags/${code}.svg`;
  }

  window.FwrouterUserServers = {
    parseCurrentServerName,
    renderServerListName,
    renderCurrentServerTitle,
    preloadCurrentServerFlag,
  };
})();
