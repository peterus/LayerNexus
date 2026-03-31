/* Auto-refresh for Admin Dashboard */

function initAdminRefresh() {
  var STORAGE_KEY = 'admin_refresh_interval';
  var select = document.getElementById('refresh-interval');
  var btn = document.getElementById('refresh-btn');
  var status = document.getElementById('refresh-status');
  var timerId = null;

  if (!select || !btn) return;

  // Restore saved preference
  var saved = localStorage.getItem(STORAGE_KEY);
  if (saved !== null) {
    select.value = saved;
  }

  function updateStatus(msg) {
    status.textContent = msg;
    setTimeout(function () { status.textContent = ''; }, 3000);
  }

  function doRefresh() {
    var scrollY = window.scrollY;
    var icon = btn.querySelector('i');
    icon.classList.add('spin-animation');

    // Save collapse states before refresh
    var collapseStates = {};
    document.querySelectorAll('#admin-content .collapse').forEach(function (el) {
      collapseStates[el.id] = el.classList.contains('show');
    });

    fetch(window.location.href, {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
    .then(function (r) { return r.text(); })
    .then(function (html) {
      var parser = new DOMParser();
      var doc = parser.parseFromString(html, 'text/html');
      var newContent = doc.querySelector('#admin-content');
      var current = document.getElementById('admin-content');
      if (newContent && current) {
        current.innerHTML = newContent.innerHTML;
      }
      // Restore collapse states
      for (var id in collapseStates) {
        if (collapseStates.hasOwnProperty(id) && collapseStates[id]) {
          var el = document.getElementById(id);
          if (el) {
            el.classList.add('show');
          }
        }
      }
      window.scrollTo({ top: scrollY, behavior: 'instant' });
      updateStatus('Updated ' + new Date().toLocaleTimeString());
    })
    .catch(function () {
      updateStatus('Refresh failed');
    })
    .finally(function () {
      icon.classList.remove('spin-animation');
    });
  }

  function scheduleRefresh() {
    if (timerId) { clearInterval(timerId); timerId = null; }
    var secs = parseInt(select.value, 10);
    localStorage.setItem(STORAGE_KEY, secs);
    if (secs > 0) {
      timerId = setInterval(doRefresh, secs * 1000);
    }
  }

  select.addEventListener('change', scheduleRefresh);
  btn.addEventListener('click', doRefresh);
  scheduleRefresh();

  // Pause auto-refresh when tab is hidden
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      if (timerId) { clearInterval(timerId); timerId = null; }
    } else {
      scheduleRefresh();
    }
  });
}
