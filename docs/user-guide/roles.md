# Roles & Permissions

LayerNexus has three built-in roles that control what each user can do. Every user gets exactly one role.

## Roles Overview

| Role | Who It's For |
|---|---|
| **Admin** | The person who runs LayerNexus — full access to everything, including managing other users |
| **Operator** | People who manage the printers — can add printers, upload G-code, start prints, and manage slicer profiles |
| **Designer** | People who design the parts — can create projects, upload STL files, and add jobs to the print queue |

All roles can **view** everything (projects, parts, printers, queue). The differences are in what each role can **change**.

---

## How Roles Are Assigned

- The **first user** who registers automatically becomes an **Admin**.
- Everyone who registers after that gets the **Designer** role.
- Admins can change any user's role through the **User Management** page.

!!! tip
    You can disable self-registration by setting `ALLOW_REGISTRATION=false` in your [configuration](../configuration.md). This is useful if you want to control exactly who has access.

---

## What Each Role Can Do

| Action | Admin | Operator | Designer |
|---|---|---|---|
| View projects, parts, printers | ✅ | ✅ | ✅ |
| Create/edit/delete projects & parts | ✅ | | ✅ |
| Manage project documents & hardware | ✅ | | ✅ |
| Add/remove printers | ✅ | ✅ | |
| Upload G-code & start/cancel prints | ✅ | ✅ | |
| Import OrcaSlicer profiles | ✅ | ✅ | |
| Manage filament mappings | ✅ | ✅ | |
| Add/remove jobs from the print queue | ✅ | ✅ | ✅ |
| Manage other users | ✅ | | |

---

## How It Works in Practice

- **Can't see a button?** Your role probably doesn't allow that action. Ask an Admin to change your role if needed.
- **Getting a "403 Forbidden" error?** You're logged in but your role doesn't have permission for that action.
- **Not logged in?** You'll be redirected to the login page.

---

## Next Steps

- [Manage projects and sub-projects](projects.md)
- [Print jobs and queue management](printing.md)
