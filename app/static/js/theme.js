/* Applies the saved (or system) colour theme before the first paint, so the
   interface never flashes light before switching to dark.  Kept in its own
   tiny file and loaded synchronously in <head>; CSP allows self-hosted
   scripts only. */
(function () {
  "use strict";

  var STORAGE_KEY = "bmshop-theme";
  var root = document.documentElement;
  var theme = null;

  try {
    var saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "dark" || saved === "light") {
      theme = saved;
    }
  } catch (error) {
    /* Private mode or storage disabled: fall back to the system preference. */
  }

  if (!theme) {
    var prefersDark =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    theme = prefersDark ? "dark" : "light";
  }

  root.setAttribute("data-theme", theme);
})();
