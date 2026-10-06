// The dashboard's small script (plan 05, P1). Every text it shows comes from
// the page (Persian, from the catalog); nothing here needs translating.
//
// - A form with data-confirm="…" asks first, in an accessible dialog that
//   names the action and its consequence.
// - A submitted form's button shows it's working, and a second submit is
//   ignored until the page changes.
// - The menu drawer (phones and tablets), dismissible messages, copy buttons,
//   and the chosen file's name in Persian file fields.
// - HTMX: a lost connection or a stale page says so, instead of freezing.
// - Ctrl+K (Cmd+K on a Mac) goes to any page, campaign, segment or preset.
(() => {
  "use strict";

  // ---------- confirmations ----------

  function askToConfirm(form, submitter, message) {
    const dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      if (window.confirm(message)) {
        form.dataset.confirmed = "1";
        form.requestSubmit(submitter || undefined);
      }
      return;
    }
    const label = submitter ? submitter.textContent.trim() : "";
    dialog.querySelector("[data-confirm-title]").textContent = label;
    dialog.querySelector("[data-confirm-text]").textContent = message;
    const ok = dialog.querySelector("[data-confirm-ok]");
    ok.textContent = label;
    ok.classList.toggle("danger-solid", Boolean(submitter && submitter.classList.contains("danger")));
    dialog.returnValue = "";
    dialog.addEventListener("close", () => {
      if (dialog.returnValue === "confirm") {
        form.dataset.confirmed = "1";
        form.requestSubmit(submitter || undefined);
      }
    }, { once: true });
    dialog.showModal();
  }

  // ---------- submitting ----------

  function markBusy(form, submitter) {
    form.dataset.busy = "1";
    if (submitter) {
      submitter.classList.add("is-busy");
      submitter.setAttribute("aria-busy", "true");
    }
    // After this tick, so the button's own name and value still go along.
    setTimeout(() => {
      form.querySelectorAll("button[type=submit], button:not([type])").forEach((button) => {
        button.disabled = true;
      });
    }, 0);
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method === "dialog" || event.defaultPrevented) return;
    if (form.dataset.busy === "1") {
      event.preventDefault();
      return;
    }
    const message = form.dataset.confirm;
    if (message && form.dataset.confirmed !== "1") {
      event.preventDefault();
      askToConfirm(form, event.submitter, message);
      return;
    }
    delete form.dataset.confirmed;
    markBusy(form, event.submitter);
  });

  // Back to a page from the browser's cache: its forms work again.
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    document.querySelectorAll("form[data-busy]").forEach((form) => {
      delete form.dataset.busy;
      form.querySelectorAll("button").forEach((button) => {
        button.disabled = false;
        button.classList.remove("is-busy");
        button.removeAttribute("aria-busy");
      });
    });
  });

  // ---------- clicks: drawer, messages, copy ----------

  function copied(button) {
    const before = button.textContent;
    button.textContent = button.dataset.copied || "✓";
    setTimeout(() => { button.textContent = before; }, 1500);
  }

  function copy(button) {
    const value = button.dataset.copy;
    // The clipboard API needs HTTPS or localhost; over NetBird the dashboard
    // may be plain HTTP, so fall back to a hidden text area.
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(value).then(() => copied(button));
      return;
    }
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      if (document.execCommand("copy")) copied(button);
    } finally {
      area.remove();
    }
  }

  document.addEventListener("click", (event) => {
    const target = event.target;
    const opener = target.closest("[data-open-drawer]");
    if (opener) {
      const drawer = document.getElementById(opener.getAttribute("aria-controls"));
      drawer.showModal();
      opener.setAttribute("aria-expanded", "true");
      drawer.addEventListener("close", () => opener.setAttribute("aria-expanded", "false"), { once: true });
      return;
    }
    if (target.closest("[data-close-drawer]")) {
      target.closest("dialog").close();
      return;
    }
    // A click beside the open drawer (on its backdrop) closes it.
    if (target instanceof HTMLDialogElement && target.classList.contains("drawer")) {
      const box = target.getBoundingClientRect();
      const inside = event.clientX >= box.left && event.clientX <= box.right
        && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) target.close();
      return;
    }
    const dismiss = target.closest("[data-dismiss]");
    if (dismiss) {
      leave(dismiss.closest(".callout"));
      return;
    }
    if (target.closest("[data-reload]")) {
      window.location.reload();
      return;
    }
    const copyButton = target.closest("button[data-copy]");
    if (copyButton) copy(copyButton);
  });

  // ---------- file fields ----------

  document.addEventListener("change", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.classList.contains("file-input")) return;
    const name = input.closest(".file-field").querySelector(".file-name");
    name.textContent = input.files.length ? input.files[0].name : name.dataset.empty;
  });

  // ---------- SMS length and tokens (template editor, settings) ----------

  // The same rule as campaigns/message.py: GSM 7-bit text is 160 per SMS
  // (153 per part), anything else (Persian) 70 (67 per part).
  const GSM7 = new Set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡" +
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà");
  const GSM7_EXT = new Set("^{}\\[~]|€\f");
  function smsLength(text) {
    const chars = [...text];
    if (chars.every((c) => GSM7.has(c) || GSM7_EXT.has(c))) {
      const units = chars.reduce((n, c) => n + (GSM7_EXT.has(c) ? 2 : 1), 0);
      return { units, parts: units <= 160 ? 1 : Math.ceil(units / 153) };
    }
    const units = text.length;  // UTF-16 units, as the network counts them
    return { units, parts: units <= 70 ? 1 : Math.ceil(units / 67) };
  }
  const faNumber = (n) => n.toLocaleString("en-US").replace(/,/g, "\u066c")
    .replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[d]);

  document.addEventListener("input", (event) => {
    const area = event.target;
    if (!area.dataset || !area.dataset.smsLength) return;
    const out = document.getElementById(area.dataset.smsLength);
    const { units, parts } = smsLength(area.value);
    out.textContent = out.dataset.template.replace("{chars}", faNumber(units)).replace("{parts}", faNumber(parts));
  });

  // A token button puts its placeholder where the cursor is.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-insert]");
    if (!button) return;
    const area = document.getElementById(button.dataset.into);
    const start = area.selectionStart ?? area.value.length;
    const end = area.selectionEnd ?? start;
    area.value = area.value.slice(0, start) + button.dataset.insert + area.value.slice(end);
    area.focus();
    area.selectionStart = area.selectionEnd = start + button.dataset.insert.length;
    area.dispatchEvent(new Event("input", { bubbles: true }));
  });

  // ---------- the campaign settings form ----------

  // Show only what applies: each token's value or column by its source,
  // the short-link section when a token carries the link, the translations
  // when a token uses a column, the pattern when the format is one. Without
  // this script everything shows, and the server still checks it all.
  function syncSettings(form) {
    form.querySelectorAll("[data-token]").forEach((row) => {
      const checked = row.querySelector("input[type=radio]:checked");
      const source = checked ? checked.value : "";
      row.querySelectorAll("[data-when]").forEach((part) => { part.hidden = part.dataset.when !== source; });
    });
    const uses = (value) => Boolean(form.querySelector(`[data-token] input[type=radio][value=${value}]:checked`));
    form.querySelectorAll("[data-needs-link]").forEach((part) => { part.hidden = !uses("link"); });
    form.querySelectorAll("[data-needs-column]").forEach((part) => { part.hidden = !uses("column"); });
    const kind = form.querySelector("input[name=link_format_kind]:checked");
    form.querySelectorAll("[data-when-format]").forEach((part) => {
      part.hidden = !kind || part.dataset.whenFormat !== kind.value;
    });
    // The campaign's own segment isn't one of its "more segments".
    const first = form.querySelector("select[name=segment]");
    form.querySelectorAll("[data-more-segment]").forEach((item) => {
      const same = Boolean(first) && item.dataset.moreSegment === first.value;
      item.hidden = same;
      if (same) item.querySelector("input").checked = false;
    });
  }
  document.addEventListener("change", (event) => {
    const form = event.target.form;
    if (form && form.hasAttribute("data-settings")) syncSettings(form);
  });
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("form[data-settings]").forEach(syncSettings);
  });

  // Translation rows: add one from the page's <template>, or remove one.
  document.addEventListener("click", (event) => {
    const add = event.target.closest("[data-add-row]");
    if (add) {
      const body = document.getElementById(add.dataset.addRow);
      const row = document.getElementById("value-map-row").content.firstElementChild.cloneNode(true);
      body.appendChild(row);
      row.querySelector("select, input").focus();
      return;
    }
    const remove = event.target.closest("[data-remove-row]");
    if (remove) {
      const row = remove.closest("tr");
      const form = row.closest("form");
      if (row.parentElement.children.length > 1) {
        row.remove();
      } else {
        row.querySelectorAll("input, select").forEach((field) => { field.value = ""; });
      }
      if (form) form.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });

  // ---------- unsaved changes ----------

  // A form marked data-guard asks before its changes are left behind.
  let unsaved = null;
  document.addEventListener("input", (event) => {
    const form = event.target.form;
    if (form && form.hasAttribute("data-guard")) unsaved = form;
  });
  document.addEventListener("submit", (event) => {
    if (event.target === unsaved) unsaved = null;
  });
  window.addEventListener("beforeunload", (event) => {
    if (unsaved) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // ---------- tabs ----------

  // Without a script every panel shows, one after another, and the tabs are
  // links to them. With it, one panel at a time; an address ending in
  // #recipients, or in an anchor inside a panel, opens that panel.
  function setupTabs(nav) {
    const tabs = Array.from(nav.querySelectorAll("[role=tab]"));
    if (!tabs.length) return;
    const panelOf = (tab) => document.getElementById(tab.getAttribute("aria-controls"));
    const select = (tab, focus) => {
      tabs.forEach((each) => {
        const on = each === tab;
        each.setAttribute("aria-selected", String(on));
        each.tabIndex = on ? 0 : -1;
        const panel = panelOf(each);
        if (panel) panel.hidden = !on;
      });
      if (focus) tab.focus();
    };
    const fromHash = () => {
      const id = decodeURIComponent(window.location.hash.slice(1));
      const target = id ? document.getElementById(id) : null;
      const panel = target ? target.closest("[data-tab-panel]") : null;
      return tabs.find((tab) => panel && tab.getAttribute("aria-controls") === panel.id) || tabs[0];
    };
    const remember = (tab) => window.history.replaceState(null, "", "#" + tab.getAttribute("aria-controls"));
    select(fromHash(), false);
    nav.addEventListener("click", (event) => {
      const tab = event.target.closest("[role=tab]");
      if (!tab) return;
      event.preventDefault();
      select(tab, true);
      remember(tab);
    });
    nav.addEventListener("keydown", (event) => {
      const i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      // Right to left: the next tab is the one to the left.
      let next = null;
      if (event.key === "ArrowLeft") next = (i + 1) % tabs.length;
      else if (event.key === "ArrowRight") next = (i - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      if (next === null) return;
      event.preventDefault();
      select(tabs[next], true);
      remember(tabs[next]);
    });
    window.addEventListener("hashchange", () => select(fromHash(), false));
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-tabs]").forEach(setupTabs);
  });

  // ---------- toasts ----------

  // Success and information leave after a while, unless someone is reading
  // them (pointer over, or focus inside).
  function leave(box) {
    if (!box) return;
    box.classList.add("is-leaving");
    setTimeout(() => box.remove(), 300);
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-toast]").forEach((toast) => {
      let timer = setTimeout(() => leave(toast), 8000);
      const hold = () => { clearTimeout(timer); };
      const release = () => { timer = setTimeout(() => leave(toast), 4000); };
      toast.addEventListener("mouseenter", hold);
      toast.addEventListener("focusin", hold);
      toast.addEventListener("mouseleave", release);
      toast.addEventListener("focusout", release);
    });
  });

  // ---------- after a page loads ----------

  // A form that came back with errors says so first: focus its summary, so
  // keyboard and screen-reader users start there.
  document.addEventListener("DOMContentLoaded", () => {
    const summary = document.querySelector("[data-autofocus]");
    if (summary) summary.focus();
  });

  // ---------- live updates ----------

  // A live update morphs the page in place; a <details> someone opened stays
  // open (the server's copy never has `open`).
  document.addEventListener("DOMContentLoaded", () => {
    if (!window.Idiomorph) return;
    Idiomorph.defaults.callbacks.beforeAttributeUpdated = (name, node) => !(name === "open" && node.tagName === "DETAILS");
  });

  // ---------- HTMX: connection and stale pages ----------

  function banner(kind) {
    const box = document.getElementById("connection");
    if (!box) return;
    if (!kind) {
      box.hidden = true;
      return;
    }
    box.querySelectorAll("[data-when]").forEach((line) => { line.hidden = line.dataset.when !== kind; });
    box.hidden = false;
  }

  document.addEventListener("htmx:sendError", () => banner("offline"));
  document.addEventListener("htmx:responseError", () => banner("stale"));
  document.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.successful) banner(null);
  });

  // ---------- The composer: Ctrl+Enter (Cmd+Enter on a Mac) presses its main button ----------

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.defaultPrevented) return;
    const primary = document.querySelector("[data-primary]:not([disabled])");
    if (!primary) return;
    event.preventDefault();
    primary.click();
  });

  // ---------- Go to anything: Ctrl+K (Cmd+K on a Mac) ----------
  //
  // The menu's pages are on the page already (only those your role sees);
  // campaigns, segments and presets come from /palette/ the first time it
  // opens. Persian and Arabic letters and digits match each other. A phone
  // number offers the number lookup, posted: a number never goes in a URL.

  const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const GROUPS = ["campaigns", "presets", "segments", "pages"];

  function fold(text) {
    return String(text || "").toLowerCase()
      .replace(/[يى]/g, "ی").replace(/ك/g, "ک")
      .replace(/[۰-۹]/g, (d) => String(d.charCodeAt(0) - 0x6f0))
      .replace(/[٠-٩]/g, (d) => String(d.charCodeAt(0) - 0x660))
      .replace(/[‌‎‏]/g, "")
      .replace(/\s+/g, " ").trim();
  }

  function isPhone(text) {
    return /^(\+?98|0098|0)?9\d{9}$/.test(fold(text).replace(/[\s()-]/g, ""));
  }

  function setupPalette(dialog) {
    const input = dialog.querySelector("#palette-input");
    const list = dialog.querySelector("#palette-list");
    const empty = dialog.querySelector("[data-palette-empty]");
    const count = dialog.querySelector("[data-palette-count]");
    const numberForm = document.querySelector("[data-palette-number]");
    let items = null; // everything findable, once /palette/ has answered
    let loading = null;
    let shown = []; // the options on screen, in order
    let active = 0;

    function keyed(item) {
      item.key = fold(item.label + " " + item.hint);
      return item;
    }

    function pages() {
      return Array.from(document.querySelectorAll(".sidebar .nav-list a"), (a) => keyed({
        group: "pages", label: (a.querySelector("span") || a).textContent.trim(), hint: "", url: a.getAttribute("href"),
      }));
    }

    function load() {
      if (!loading) {
        loading = fetch(dialog.dataset.source, { headers: { Accept: "application/json" }, credentials: "same-origin" })
          .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
          .then((data) => {
            items = pages();
            ["campaigns", "segments", "presets"].forEach((group) => {
              (data[group] || []).forEach((it) => items.push(keyed({ group, label: it.name, hint: it.hint, url: it.url })));
            });
            if (dialog.open) render();
          })
          .catch(() => { loading = null; }); // the menu's pages still work; asked again next time
      }
      return loading;
    }

    function select(index) {
      if (!shown.length) {
        input.removeAttribute("aria-activedescendant");
        return;
      }
      active = (index + shown.length) % shown.length;
      shown.forEach((entry, i) => entry.option.setAttribute("aria-selected", String(i === active)));
      input.setAttribute("aria-activedescendant", shown[active].option.id);
      shown[active].option.scrollIntoView({ block: "nearest" });
    }

    function addGroup(title, entries) {
      if (!entries.length) return;
      const group = document.createElement("div");
      const head = document.createElement("div");
      group.setAttribute("role", "group");
      head.className = "palette-group";
      head.id = `palette-group-${list.children.length}`;
      head.textContent = title;
      group.setAttribute("aria-labelledby", head.id);
      group.append(head);
      entries.forEach((entry) => {
        const option = document.createElement("div");
        option.id = `palette-option-${shown.length}`;
        option.className = "palette-option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        const label = document.createElement(entry.number ? "bdi" : "span");
        label.className = "palette-label";
        label.textContent = entry.label;
        if (entry.number) label.dir = "ltr";
        option.append(label);
        if (entry.hint && entry.hint !== entry.label) {
          const hint = document.createElement("bdi");
          hint.className = "palette-hint";
          hint.dir = "ltr";
          hint.textContent = entry.hint;
          option.append(hint);
        }
        const index = shown.length;
        option.addEventListener("click", () => go(entry));
        option.addEventListener("mousemove", () => { if (active !== index) select(index); });
        entry.option = option;
        shown.push(entry);
        group.append(option);
      });
      list.append(group);
    }

    function render() {
      const words = fold(input.value).split(" ").filter(Boolean);
      const pool = items || pages();
      const limit = words.length ? 8 : 5;
      shown = [];
      list.replaceChildren();
      if (dialog.dataset.find && numberForm && isPhone(input.value)) {
        addGroup(dialog.dataset.find, [{ label: input.value.trim(), number: input.value.trim() }]);
      }
      GROUPS.forEach((group) => {
        const found = pool.filter((it) => it.group === group && words.every((w) => it.key.includes(w)));
        addGroup(dialog.dataset[group], found.slice(0, limit));
      });
      empty.hidden = shown.length > 0;
      input.setAttribute("aria-expanded", String(shown.length > 0));
      if (words.length) count.textContent = dialog.dataset.results.replace("{n}", shown.length.toLocaleString("fa-IR"));
      select(0);
    }

    function go(entry) {
      dialog.close();
      if (entry.number) {
        numberForm.elements.number.value = entry.number;
        numberForm.requestSubmit();
      } else {
        window.location.assign(entry.url);
      }
    }

    function open() {
      if (dialog.open) return;
      const drawer = document.getElementById("drawer");
      if (drawer && drawer.open) drawer.close();
      input.value = "";
      count.textContent = "";
      dialog.showModal();
      render();
      load();
      input.focus();
    }

    input.addEventListener("input", render);
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        select(active + (event.key === "ArrowDown" ? 1 : -1));
      } else if (event.key === "Enter") {
        event.preventDefault(); // and so the composer's Ctrl+Enter stays out of it
        if (shown[active]) go(shown[active]);
      }
    });
    // A click on the backdrop closes it.
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) {
        dialog.close();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
      // event.code: on a Persian layout the K key types «ن».
      if (event.code !== "KeyK" && event.key.toLowerCase() !== "k") return;
      event.preventDefault();
      if (dialog.open) dialog.close();
      else open();
    });
    document.querySelectorAll("[data-open-palette]").forEach((button) => {
      const key = button.querySelector("[data-palette-key]");
      if (key) key.textContent = IS_MAC ? "⌘K" : "Ctrl K";
      button.hidden = false;
      button.addEventListener("click", open);
    });
  }

  const palette = document.getElementById("palette");
  if (palette && typeof palette.showModal === "function") setupPalette(palette);
})();
