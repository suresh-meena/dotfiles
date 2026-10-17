/* Fleetmon — color theme. Loaded in <head> so a stored choice applies before
   the first paint. "auto" follows the OS setting; "light" and "dark" stamp
   data-theme on <html>, which the stylesheet lets win both ways. The choice
   is a per-browser convenience: storage may be unavailable, and the page then
   simply follows the OS. */
(() => {
  "use strict";

  const KEY = "fleetmon-theme";
  const MODES = ["auto", "light", "dark"];
  const root = document.documentElement;

  function stored() {
    try {
      const mode = localStorage.getItem(KEY);
      return MODES.includes(mode) ? mode : "auto";
    } catch (_error) {
      return "auto";
    }
  }

  function apply(mode) {
    if (mode === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", mode);
    const button = document.getElementById("theme");
    if (!button) return;
    button.dataset.mode = mode;
    button.setAttribute("aria-label", `Color theme: ${mode}`);
    button.title = `Theme: ${mode} (click to change)`;
    const label = button.querySelector(".theme-label");
    if (label) label.textContent = mode;
  }

  apply(stored());
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme");
    if (!button) return;
    apply(stored());
    button.addEventListener("click", () => {
      const next = MODES[(MODES.indexOf(button.dataset.mode) + 1) % MODES.length];
      try {
        localStorage.setItem(KEY, next);
      } catch (_error) {
        /* the choice lasts for this page only */
      }
      apply(next);
    });
  });
  // Another tab changed the theme.
  window.addEventListener("storage", (event) => {
    if (event.key === KEY) apply(stored());
  });
})();
