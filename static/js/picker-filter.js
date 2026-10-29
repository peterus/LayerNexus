/* Client-side text filter for <select> pickers */

/**
 * Hide <option>s whose text does not contain the typed query.
 * Expects a text input with a "data-picker-filter" attribute holding the id of the
 * <select> to filter, e.g. <input data-picker-filter="add-existing-part-select">.
 */
function initPickerFilters() {
  document.querySelectorAll('[data-picker-filter]').forEach(function (input) {
    var select = document.getElementById(input.dataset.pickerFilter);
    if (!select) return;

    input.addEventListener('input', function () {
      var query = input.value.trim().toLowerCase();
      Array.from(select.options).forEach(function (opt) {
        opt.hidden = query !== '' && opt.textContent.toLowerCase().indexOf(query) === -1;
        // Never submit an option the user can no longer see.
        if (opt.hidden && opt.selected) opt.selected = false;
      });
    });
  });
}

document.addEventListener('DOMContentLoaded', initPickerFilters);
