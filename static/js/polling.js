/* Generic polling utility for LayerNexus */

/**
 * Start polling a URL at a given interval.
 * @param {string} url - The URL to fetch.
 * @param {function} callback - Called with the parsed JSON response. Return false to stop polling.
 * @param {number} intervalMs - Polling interval in milliseconds.
 * @returns {object} An object with a stop() method to cancel polling.
 */
function startPolling(url, callback, intervalMs) {
  var timerId = null;

  function poll() {
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var result = callback(data);
        if (result === false) {
          stop();
        }
      })
      .catch(function () { /* silently ignore network errors */ });
  }

  function stop() {
    if (timerId) {
      clearInterval(timerId);
      timerId = null;
    }
  }

  // Initial poll immediately
  poll();
  timerId = setInterval(poll, intervalMs);

  return { stop: stop };
}
