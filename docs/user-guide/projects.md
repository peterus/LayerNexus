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
| **Default Slicer Profile** | Optional default OrcaSlicer profile for all parts |

4. Click **Save**.

### Cover Images

Projects support cover images that are displayed in:

- Project list (card headers)
- Project detail view
- Sub-project tables

You can upload an image file or **paste from the clipboard** — the upload form includes JavaScript-powered clipboard paste support.

---

## Sub-Projects

Projects can contain sub-projects, creating a hierarchy with quantity multipliers.

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
