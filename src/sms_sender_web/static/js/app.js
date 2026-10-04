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
      dismiss.closest(".callout").remove();
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

  // ---------- after a page loads ----------

  // A form that came back with errors says so first: focus its summary, so
  // keyboard and screen-reader users start there.
  document.addEventListener("DOMContentLoaded", () => {
    const summary = document.querySelector("[data-autofocus]");
    if (summary) summary.focus();
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
})();
