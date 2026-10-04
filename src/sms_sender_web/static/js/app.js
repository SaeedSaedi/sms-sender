// A form with data-confirm asks before it submits (Persian text from the
// template, so nothing here needs translating).
document.addEventListener("submit", (event) => {
  const message = event.target.dataset && event.target.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});
