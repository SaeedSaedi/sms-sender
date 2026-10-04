// A form with data-confirm asks before it submits (Persian text from the
// template, so nothing here needs translating).
document.addEventListener("submit", (event) => {
  const message = event.target.dataset && event.target.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// A copy button copies its exact value. The clipboard API needs HTTPS or
// localhost; over NetBird the dashboard is plain HTTP, so fall back to a
// hidden text area.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) return;
  const value = button.dataset.copy;
  const done = () => {
    const before = button.textContent;
    button.textContent = button.dataset.copied || "✓";
    setTimeout(() => { button.textContent = before; }, 1500);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(value).then(done);
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
    if (document.execCommand("copy")) done();
  } finally {
    area.remove();
  }
});
