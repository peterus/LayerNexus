/* GCode Preview using gcode-preview library */
function initGCodePreview(containerOrId, gcodeUrl) {
  "use strict";

  var container = typeof containerOrId === 'string' ? document.getElementById(containerOrId) : containerOrId;
  if (!container || !gcodeUrl) return;

  var loadingEl = container.querySelector(".gcode-loading");
  var canvas = container.querySelector(".gcode-preview-canvas");
  if (!canvas) return;

  var rect = container.getBoundingClientRect();
  canvas.width = rect.width * window.devicePixelRatio;
  canvas.height = rect.height * window.devicePixelRatio;

  var dark = document.documentElement.getAttribute("data-bs-theme") === "dark"
    || window.matchMedia("(prefers-color-scheme: dark)").matches;

  var preview = GCodePreview.init({
    canvas: canvas,
    extrusionColor: dark ? "#44bbff" : "#007bff",
    backgroundColor: dark ? "#212529" : "#f8f9fa",
    buildVolume: { x: 256, y: 256, z: 256 },
    initialCameraPosition: [0, 400, 450],
    lineWidth: 2,
    renderExtrusion: true,
    renderTravel: false,
  });

  fetch(gcodeUrl)
    .then(function (r) { return r.text(); })
    .then(function (gcode) {
      preview.processGCode(gcode);
      if (loadingEl) loadingEl.style.display = "none";
    })
    .catch(function (err) {
      console.error("G-code preview error:", err);
      if (loadingEl) {
        loadingEl.innerHTML = '<i class="bi bi-exclamation-triangle"></i> <small>Preview failed</small>';
      }
    });
}

/* Auto-init all gcode preview canvases with IntersectionObserver for lazy loading */
function initAllGCodePreviews() {
  "use strict";

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        observer.unobserve(entry.target);
        var canvas = entry.target.querySelector(".gcode-preview-canvas");
        if (canvas) {
          var url = canvas.getAttribute("data-gcode-url");
          if (url) {
            initGCodePreview(entry.target.id || entry.target, url);
          }
        }
      }
    });
  }, { rootMargin: "200px" });

  document.querySelectorAll(".gcode-preview-container").forEach(function (container) {
    observer.observe(container);
  });
}
