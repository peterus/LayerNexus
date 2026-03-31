/* Printer filter for print queue form */

/**
 * Dynamically filter printer options based on the selected plate's machine profile.
 * Expects:
 *   - A select element with id "id_plate"
 *   - A select element with id "id_printer"
 *   - A JSON script tag with id "job-printer-map" containing
 *     a mapping of plate PK -> array of compatible printer PKs.
 */
function initPrinterFilter() {
  var plateSelect = document.getElementById('id_plate');
  var printerSelect = document.getElementById('id_printer');
  if (!plateSelect || !printerSelect) return;

  var mapEl = document.getElementById('job-printer-map');
  if (!mapEl) return;

  var jobPrinterMap = JSON.parse(mapEl.textContent);
  var allPrinterOptions = Array.from(printerSelect.options).map(function (opt) {
    return { value: opt.value, text: opt.textContent, selected: opt.selected };
  });

  function filterPrinters() {
    var platePk = plateSelect.value;
    var compatiblePks = jobPrinterMap[platePk] || null;
    var currentVal = printerSelect.value;
    printerSelect.innerHTML = '';

    allPrinterOptions.forEach(function (opt) {
      if (!opt.value) {
        // empty/placeholder option
        printerSelect.appendChild(new Option(opt.text, opt.value));
      } else if (!compatiblePks || compatiblePks.indexOf(parseInt(opt.value)) !== -1) {
        printerSelect.appendChild(new Option(opt.text, opt.value));
      }
    });

    // Try to restore previous selection
    if (currentVal) {
      printerSelect.value = currentVal;
    }
  }

  plateSelect.addEventListener('change', filterPrinters);
  // Apply filter on page load
  filterPrinters();
}
