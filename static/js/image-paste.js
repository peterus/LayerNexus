/* Clipboard image paste handler for file inputs */

/**
 * Initialize image paste support for a file input.
 * Expects the following DOM structure near the file input:
 *   #paste-zone        - clickable drop zone
 *   #paste-preview      - preview container (initially d-none)
 *   #paste-preview-img  - img element for preview
 *   #paste-clear        - button to clear pasted image
 *
 * @param {string} fileInputId - The ID of the file input element.
 */
function initImagePaste(fileInputId) {
  var fileInput = document.getElementById(fileInputId);
  var pasteZone = document.getElementById("paste-zone");
  var preview = document.getElementById("paste-preview");
  var previewImg = document.getElementById("paste-preview-img");
  var clearBtn = document.getElementById("paste-clear");

  if (!fileInput || !pasteZone) return;

  var currentObjectUrl = null;

  function revokeCurrentUrl() {
    if (currentObjectUrl) {
      URL.revokeObjectURL(currentObjectUrl);
      currentObjectUrl = null;
    }
  }

  function setFileFromBlob(blob, filename) {
    var dt = new DataTransfer();
    dt.items.add(new File([blob], filename || "pasted-image.png", { type: blob.type }));
    fileInput.files = dt.files;
    revokeCurrentUrl();
    currentObjectUrl = URL.createObjectURL(blob);
    previewImg.src = currentObjectUrl;
    preview.classList.remove("d-none");
    pasteZone.classList.add("border-success", "text-success");
    pasteZone.innerHTML = '<i class="bi bi-check-circle me-1"></i>Image ready';
  }

  function resetPaste() {
    var dt = new DataTransfer();
    fileInput.files = dt.files;
    revokeCurrentUrl();
    preview.classList.add("d-none");
    previewImg.src = "";
    pasteZone.classList.remove("border-success", "text-success");
    pasteZone.innerHTML = '<i class="bi bi-clipboard me-1"></i>Paste image from clipboard (Ctrl+V) or click to select';
  }

  document.addEventListener("paste", function (e) {
    var items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (var i = 0; i < items.length; i++) {
      if (items[i].type.startsWith("image/")) {
        e.preventDefault();
        setFileFromBlob(items[i].getAsFile(), "pasted-image.png");
        return;
      }
    }
  });

  pasteZone.addEventListener("click", function () {
    fileInput.click();
  });

  fileInput.addEventListener("change", function () {
    if (fileInput.files && fileInput.files[0]) {
      setFileFromBlob(fileInput.files[0], fileInput.files[0].name);
    }
  });

  if (clearBtn) {
    clearBtn.addEventListener("click", resetPaste);
  }
}
