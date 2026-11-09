// Runs before the stylesheet paints (a classic script, not a module): applies the theme the sun schedule chose last
// (see sun.js and applyAppearance in app.js) while that choice is still valid, so a page opened after sunset does not
// flash light first. Without a valid cached choice the page follows the system light/dark setting.
try {
  var cached = JSON.parse(localStorage.getItem("iris.theme") || "null");
  if (cached && (cached.theme === "dark" || cached.theme === "light") && typeof cached.until === "number"
      && Date.now() < cached.until) {
    document.documentElement.dataset.theme = cached.theme;
  }
} catch (e) { /* storage blocked or unreadable: follow the system setting */ }
