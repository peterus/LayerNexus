# User Roles & Permissions

LayerNexus uses a group-based **Role-Based Access Control (RBAC)** system with three built-in roles. Every user is assigned exactly one role that determines what they can do.

## Roles Overview

| Role | Django Group | Description |
|---|---|---|
| **Admin** | `Admin` | Full access to all features, including user management |
| **Operator** | `Operator` | Printer control, queue management, and slicer profile management |
| **Designer** | `Designer` | Project and part management, print queue access |

---

## Role Assignment

### First User

The **first user** to register is automatically assigned the **Admin** role with full permissions.

### Subsequent Users

All subsequent self-registered users receive the **Designer** role by default.

### Changing Roles

Admins can change any user's role through the **User Management** section:

1. Go to **User Management** in the navigation bar (Admin only).
2. Select the user.
3. Change their role.
4. Click **Save**.

!!! tip
    You can disable self-registration by setting `ALLOW_REGISTRATION=false` in the environment variables. See [Configuration](../getting-started/configuration.md).

---

## Detailed Permissions

### Admin

Admins have **all permissions**, including:

| Permission | Description |
|---|---|
| `auth.change_user` | Manage user accounts and roles |
| `can_manage_projects` | Create, edit, and delete projects and parts |
| `can_manage_printers` | Create, edit, and delete printer profiles and cost profiles |
| `can_control_printer` | Upload G-code, start and cancel prints |
| `can_manage_print_queue` | Add jobs to the print queue |
| `can_dequeue_job` | Remove jobs from the print queue |
| `can_manage_orca_profiles` | Import and delete OrcaSlicer profiles |
| `can_manage_filament_mappings` | Manage Spoolman filament mappings |

### Operator

Operators focus on printer operations:

| Permission | Description |
|---|---|
| `can_manage_printers` | Create, edit, and delete printer profiles and cost profiles |
| `can_control_printer` | Upload G-code, start and cancel prints |
| `can_manage_print_queue` | Add jobs to the print queue |
| `can_dequeue_job` | Remove jobs from the print queue |
| `can_manage_orca_profiles` | Import and delete OrcaSlicer profiles |
| `can_manage_filament_mappings` | Manage Spoolman filament mappings |

### Designer

Designers focus on project design and management:

| Permission | Description |
|---|---|
| `can_manage_projects` | Create, edit, and delete projects and parts |
| `can_manage_print_queue` | Add jobs to the print queue |
| `can_dequeue_job` | Remove jobs from the print queue |

---

## Permission Matrix

| Action | Admin | Operator | Designer |
|---|---|---|---|
| View projects & parts | ✅ | ✅ | ✅ |
| Create/edit/delete projects | ✅ | ❌ | ✅ |
| Create/edit/delete parts | ✅ | ❌ | ✅ |
| Manage project documents | ✅ | ❌ | ✅ |
| Manage hardware catalog | ✅ | ❌ | ✅ |
| Create/edit/delete printers | ✅ | ✅ | ❌ |
| Upload G-code / start prints | ✅ | ✅ | ❌ |
| Cancel prints | ✅ | ✅ | ❌ |
| Manage print queue | ✅ | ✅ | ✅ |
| Import OrcaSlicer profiles | ✅ | ✅ | ❌ |
| Manage filament mappings | ✅ | ✅ | ❌ |
| Manage users | ✅ | ❌ | ❌ |

---

## Access Control Behavior

- **Unauthenticated users** are redirected to the login page.
- **Authenticated users without permission** receive a `403 Forbidden` error. They are not redirected — this prevents confusion about why an action failed.
- **All list and detail views** require authentication (`LoginRequiredMixin`) but no specific role.
- **All write/delete operations** require the appropriate role-based permission.

---

## Next Steps

- [Manage projects and sub-projects](projects.md)
- [Print jobs and queue management](printing.md)
