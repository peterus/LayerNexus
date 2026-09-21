/* API token copy-to-clipboard button */

(function () {
  function initCopyButton() {
    var btn = document.getElementById("copy-token-btn");
    var tokenEl = document.getElementById("api-token-value");
    if (!btn || !tokenEl) return;

    btn.addEventListener("click", function () {
      var text = tokenEl.textContent.trim();
      navigator.clipboard.writeText(text).then(function () {
        var icon = btn.querySelector("i");
        var label = btn.querySelector(".btn-label");
        icon.className = "bi bi-check-lg me-1";
        if (label) label.textContent = "Copied";
        setTimeout(function () {
          icon.className = "bi bi-clipboard me-1";
          if (label) label.textContent = "Copy";
        }, 2000);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCopyButton);
  } else {
    initCopyButton();
  }
})();
