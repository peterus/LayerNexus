/* Theme toggle for LayerNexus */
(function () {
  // Initial theme detection (run early, before DOMContentLoaded)
  var saved = localStorage.getItem('ln-theme');
  if (saved) {
    document.documentElement.setAttribute('data-bs-theme', saved);
  } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
    document.documentElement.setAttribute('data-bs-theme', 'dark');
  }

  function updateIcon(theme) {
    var icon = document.getElementById('theme-icon');
    if (icon) {
      icon.className = theme === 'dark' ? 'bi bi-moon-stars-fill' : 'bi bi-sun-fill';
    }
  }

  // Attach toggle handler once DOM is ready
  document.addEventListener('DOMContentLoaded', function () {
    var toggle = document.getElementById('theme-toggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        var html = document.documentElement;
        var current = html.getAttribute('data-bs-theme');
        var next = current === 'dark' ? 'light' : 'dark';
        html.setAttribute('data-bs-theme', next);
        localStorage.setItem('ln-theme', next);
        updateIcon(next);
      });
    }
    updateIcon(document.documentElement.getAttribute('data-bs-theme'));
  });
})();
