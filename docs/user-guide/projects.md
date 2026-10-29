# Projects & Sub-Projects

Projects are the core organizational unit in LayerNexus. They group parts, documents, hardware, and print jobs into a manageable hierarchy.

## Creating Projects

1. Go to **Projects** in the navigation bar.
2. Click **New Project**.
3. Fill in the details:

| Field | Description |
|---|---|
| **Name** | Project name (required) |
| **Description** | Optional description or notes |
| **Cover Image** | Optional cover image displayed in lists and detail views |
| **Default Print Preset** | **Required for top-level projects.** The OrcaSlicer print preset used for every part in this project (and its sub-projects) that has no preset of its own. |

4. Click **Save**.

### Print Presets

The print preset of a part is resolved **bottom-up**:

- A part with its **own** print preset override always uses that override.
- Otherwise it uses the default preset of the project it belongs to.
- If that project has no default preset, the nearest parent project with one is used
  (shown as *inherited from parent* in the forms).

Only **top-level projects** must have a default print preset — they anchor the
inheritance, so every part always resolves to exactly one preset. On sub-projects the
preset is optional; set it only where a module should be sliced differently from its
parent.

- Creating jobs from a project produces **one draft job per distinct preset + filament**
  combination, and each job records the preset it was created with.
- Changing a project's default preset, or moving a part or sub-project to a place with a
  different effective preset, automatically re-queues the estimates of the affected parts.

The REST API follows the same rules: `POST /projects/{id}/re-estimate/` and
`POST /parts/{id}/estimate/` resolve the preset the same way, and creating a top-level
project via the API requires `default_print_preset`. The OpenAPI schema (served by
drf-spectacular at the schema/Swagger endpoints) is generated automatically from the
serializers.

### Cover Images

Projects support cover images that are displayed in:

- Project list (card headers)
- Project detail view
- Sub-project tables

You can upload an image file or **paste from the clipboard** — the upload form includes JavaScript-powered clipboard paste support.

---

## Sub-Projects

Projects can contain sub-projects, creating a hierarchy with quantity multipliers.
Projects form a strict tree: every sub-project has exactly one parent, and every part
belongs to exactly one project. Nothing is shared between projects — reusing something
always means copying it (see below).

### Example

```
Quadcopter (×1)
├── Motor Mount (×4)
│   ├── mount_base.stl (×1) → effective: 4
│   └── mount_cap.stl (×1) → effective: 4
├── Frame Arm (×4)
│   └── arm.stl (×1) → effective: 4
└── Controller Case (×1)
    ├── case_top.stl (×1) → effective: 1
    └── case_bottom.stl (×1) → effective: 1
```

### How Quantity Multipliers Work

Each sub-project has a **quantity** field. The effective quantity of parts is calculated recursively:

- A part with quantity `1` in a sub-project with quantity `4` has an effective quantity of `4`.
- Multipliers compound across nesting levels.

This affects:

- **Filament calculations** — total filament usage accounts for effective quantities
- **Cost estimates** — costs multiply with effective quantities
- **Hardware requirements** — hardware counts multiply with effective quantities

### Creating Sub-Projects

1. Open a parent project.
2. Click **Add Sub-Project**.
3. Set the name, quantity, and other details.
4. The sub-project appears in the parent's detail view.

### Copying projects and parts

- **Copy a project:** open it and click **Copy**. Choose a new name and either *As new
  top-level project* or a project to copy into. The whole subtree is copied: sub-projects,
  parts, documents, hardware assignments and cover images. All files are duplicated, so
  the copy is fully independent. Print history and shopping-list status are not copied.
  A copy made as a new top-level project receives the original's effective preset.
- **Copy a part:** open it and click **Copy**, then pick the target project. The model file
  is duplicated; the existing filament/time estimate is kept unless the copy ends up with a
  different effective preset, in which case it is re-estimated.
- **Add existing:** the **Add Existing Part** and **Add Existing Sub-Project** buttons on a
  project page add a **copy** of the chosen part or project. Later changes to the original
  do not affect the copy, and vice versa. A project cannot be added into its own tree.

### Moving parts and sub-projects

- **Move a sub-project:** edit it and change **Parent project** (leave it empty to make
  it a top-level project, which then needs a default preset). A project cannot be moved
  into itself or one of its own sub-projects.
- **Move a part:** edit it and change **Project**. Its model file and print history move
  with it.

In both cases the estimates of parts whose effective preset changed are re-queued.

### Deleting a project

Deleting a project deletes its **whole subtree**: all sub-projects, parts, documents and
hardware assignments. The confirmation page shows how many of each will be removed.

- Deletion is **blocked** while an unfinished print job (anything not completed, failed
  or cancelled) contains a part of the subtree. The page lists those jobs — finish,
  cancel or remove them first. The same rule applies when deleting a single part.
- **Finished print jobs are kept**; only their entries for the deleted parts are removed.
- The files of everything deleted (model files, documents, images) are removed from
  storage.

---

## Parts Management

Parts represent individual 3D-printable components within a project.

### Adding Parts

1. Open a project.
2. Click **Add Part**.
3. Fill in the details:

| Field | Description |
|---|---|
| **Name** | Part name |
| **STL File** | Upload the STL file |
| **Quantity** | Number of copies needed |
| **Color** | Part color (auto-populated from Spoolman) |
| **Material** | Filament material (auto-populated from Spoolman) |
| **Filament Usage** | Estimated filament in grams (updated after slicing) |

4. Click **Save**.

### 3D Preview

After uploading an STL file, you can view a 3D preview directly in the browser using the built-in Three.js viewer. Rotate, zoom, and pan to inspect the model.

---

## Hardware Parts Catalog

LayerNexus includes a reusable hardware catalog for non-printed components.

### Hardware Categories

Hardware parts are organized into categories:

- Screws, Nuts, Bolts
- Motors, Electronics
- Bearings, Linear rails
- And more

### Adding Hardware to Projects

1. Open a project.
2. Navigate to the **Hardware** section.
3. Click **Add Hardware**.
4. Select from the hardware catalog or create a new hardware part.
5. Set the quantity and any project-specific notes.

Hardware costs are included in the project's total cost calculation.

### Shopping list

Each project has a **Shopping list** (linked from the project and cost pages) that sums up all hardware needed, including sub-projects, so you can buy it in one go. Every item moves through three steps: **open**, **in cart** and **ordered**; **Mark cart as ordered** advances all in-cart items at once. If the required quantity of an item changes after you set its status, the item automatically falls back to **open** so nothing is under-ordered. Everyone with project access can view the list; changing statuses requires the Designer or Admin role.

---

## Project Documents

Attach files to projects for reference documentation.

### Supported File Types

| Type | Extensions |
|---|---|
| Documents | `.pdf`, `.txt` |
| Markup | `.md` (Markdown) |
| Images | `.png`, `.jpg`, `.svg` |
| CAD | `.step`, `.dxf` |

!!! info "File Size Limit"
    The maximum file size for project documents is **75 MB**.

### Uploading Documents

1. Open a project.
2. Navigate to the **Documents** section.
3. Click **Upload Document**.
4. Select the file and click **Upload**.

---

## Cost Estimation

LayerNexus calculates costs across the entire project hierarchy, including sub-projects.

### Cost Components

| Component | Source |
|---|---|
| **Filament cost** | Based on filament usage per part and spool pricing |
| **Electricity cost** | From printer cost profiles |
| **Depreciation** | From printer cost profiles |
| **Maintenance** | From printer cost profiles |
| **Hardware cost** | From hardware catalog pricing |

Costs are aggregated recursively across sub-projects, respecting quantity multipliers.

---

## Next Steps

- [Print jobs and queue management](printing.md)
- [User roles and permissions](roles.md)
- [OrcaSlicer integration](../integrations/orcaslicer.md)
