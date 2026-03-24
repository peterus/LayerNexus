# GitHub Copilot Instructions for LayerNexus

## Project Overview

LayerNexus is a Django 6.0+ web application for managing large-scale 3D printing projects. It integrates with OrcaSlicer API (slicing), Klipper/Moonraker (printer control), and Spoolman (filament tracking).

**Core Workflow:** STL Upload → OrcaSlicer API Slicing → G-code Upload to Klipper → Print Job Tracking

## Development Environment

The project runs entirely in **Docker** using **Docker Compose**. Always use Docker Compose for development tasks.

- **Start services:** `docker compose up -d`
- **Run Django management commands:** `docker compose exec web python manage.py <command>`
- **Create migrations:** `docker compose exec web python manage.py makemigrations`
- **Apply migrations:** `docker compose exec web python manage.py migrate`
- **Run checks:** `docker compose exec web python manage.py check`
- **View logs:** `docker compose logs web --tail=50`
- **Restart after config changes:** `docker compose restart web`

The `docker-compose.yml` mounts the project directory as a volume (`.:/app`), so code changes are immediately visible in the container. Gunicorn runs with `--reload` for automatic reloading during development.

**Never run `manage.py` commands directly on the host** — always use `docker compose exec web`.

## Architecture & Structure

```
core/                  # Main Django app
├── models.py          # 17 database models (Project, Part, PrintJob, ProjectDocument, HardwarePart, etc.)
├── views.py           # 73 class-based views (CBVs)
├── forms.py           # Model forms with Meta classes
├── mixins.py          # Role-based access control mixins
├── services/          # External API clients
│   ├── orcaslicer.py  # OrcaSlicer API integration
│   ├── moonraker.py   # Klipper/Moonraker API
│   ├── spoolman.py    # Filament management API
├── templates/         # Django templates (Bootstrap 5.3)
└── templatetags/      # Custom template tags

layernexus/            # Django project settings
static/                # CSS, JavaScript, favicon, images
```

### Key Models

| Model | Purpose |
|---|---|
| **Project** | Hierarchical projects with sub-projects, cover images, and quantity multipliers |
| **Part** | Printable part with STL file, filament requirements, and Spoolman linking |
| **PrintJob** | Print job tracking from creation through completion |
| **PrintQueue** | Priority-ordered queue linking jobs to printers |
| **PrinterProfile** | Printer config with Moonraker URL and API key |
| **PrinterCostProfile** | Cost parameters (electricity, depreciation, maintenance) |
| **OrcaSlicerProfile** | Slicer profile bundle (machine, filament, print preset files) |
| **ProjectDocument** | File attachments (PDF, images, CAD files, up to 75 MB) |
| **HardwarePart** | Reusable hardware catalog (screws, nuts, motors, etc.) with pricing |
| **ProjectHardware** | Links hardware to projects with quantities and notes |

## Code Style & Conventions

### Python Code

- **Django Version:** 6.0+ (leverage modern Django features)
- **Python Version:** 3.10+ (use modern syntax)
- **Style Guide:** Follow PEP 8
- **Line Length:** 120 characters (Ruff configured)

### Type Hints

**Always use type hints for:**
- Function/method signatures
- Class attributes
- Return types
- Complex data structures

```python
from typing import Optional, Dict, List, Any
from django.http import HttpRequest, HttpResponse

def calculate_filament_usage(
    parts: List[Part],
    material_density: float = 1.25
) -> Dict[str, float]:
    """Calculate total filament usage for a list of parts.
    
    Args:
        parts: List of Part instances to calculate
        material_density: Density in g/cm³ (default PLA)
        
    Returns:
        Dictionary with 'grams' and 'meters' keys
    """
    ...
```

### String Formatting & Quotes

- **F-Strings:** Always use f-strings for string interpolation (Python 3.6+)
- **Quote Character:** Use double quotes `"` exclusively (never single quotes `'`)
- **Consistency:** This applies to regular strings, docstrings, and error messages

```python
# ✅ Correct
name = "Alice"
message = f"Hello {name}, welcome to {project_name}!"
error_msg = "Invalid file format"
logger.error(f"Failed to upload {filename}: {exc}")

# ❌ Avoid
name = 'Alice'
message = 'Hello ' + name + ', welcome!'
message = "Hello " + name + ", welcome!"  # Concatenation instead of f-string
error_msg = 'Invalid file format'
```

### Docstrings

- **Format:** Google-style docstrings
- **Required for:** All public functions, classes, and modules
- **Language:** English
- **Include:** Description, Args, Returns, Raises (if applicable)

```python
def slice_part(part: Part, profile: OrcaSlicerProfile) -> Path:
    """Slice a part's STL file using OrcaSlicer API.
    
    Args:
        part: Part instance with stl_file
        profile: OrcaSlicer profile to use for slicing
        
    Returns:
        Path to the generated G-code file
        
    Raises:
        OrcaSlicerError: If slicing fails or OrcaSlicer API is not configured
        FileNotFoundError: If STL file does not exist
    """
    ...
```

### Models

- Use explicit `related_name` for ForeignKey/ManyToMany fields
- Add docstrings to models and complex properties
- Use validators from `django.core.validators`
- Implement `__str__()` for all models
- Add `Meta` class with `ordering` where appropriate

```python
class Part(models.Model):
    """A printable part within a 3D printing project."""
    
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='parts',
    )
    name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    
    class Meta:
        ordering = ['name']
        
    def __str__(self) -> str:
        return f"{self.name} ({self.project.name})"
```

### Views

- **Prefer:** Class-based views (CreateView, UpdateView, ListView, etc.)
- **Authentication:** Use `LoginRequiredMixin` for read-only views
- **Permissions:** Use role mixins from `core/mixins.py` for any write/delete operation (see User Roles & Permissions)
- **Messages:** Use Django `messages` framework for user feedback
- **Error Handling:** Catch service exceptions and display user-friendly messages

```python
class PartCreateView(LoginRequiredMixin, CreateView):
    """Create a new part for a project."""
    
    model = Part
    form_class = PartForm
    template_name = 'core/part_form.html'
    
    def form_valid(self, form: PartForm) -> HttpResponse:
        """Save part and optionally trigger slicing."""
        part = form.save(commit=False)
        part.project = get_object_or_404(Project, pk=self.kwargs['project_id'])
        part.save()
        
        if form.cleaned_data.get('slice_on_create'):
            try:
                slice_part(part, form.cleaned_data['slicer_profile'])
                messages.success(self.request, f"Part '{part.name}' created and sliced.")
            except OrcaSlicerError as e:
                messages.warning(self.request, f"Part created but slicing failed: {e}")
        else:
            messages.success(self.request, f"Part '{part.name}' created.")
            
        return redirect('core:part_detail', pk=part.pk)
```

### Forms

- Use `ModelForm` with explicit `Meta.fields`
- Add custom validation in `clean()` or `clean_<fieldname>()`
- Use widgets to customize HTML rendering
- Add helpful `help_text` for complex fields

### Services (External API Clients)

- Create separate classes in `core/services/` for each integration
- Use custom exception classes (e.g., `MoonrakerError`)
- Log all API calls with `logging.getLogger(__name__)`
- Make API calls idempotent where possible
- Handle connection errors gracefully

```python
class MoonrakerClient:
    """Client for interacting with the Klipper/Moonraker API."""
    
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        self.logger = logging.getLogger(__name__)
        
    def upload_gcode(self, file_path: Path, filename: str) -> Dict[str, Any]:
        """Upload G-code file to Moonraker.
        
        Args:
            file_path: Local path to G-code file
            filename: Target filename on the printer
            
        Returns:
            Response data from Moonraker API
            
        Raises:
            MoonrakerError: If upload fails
        """
        ...
```

### Templates

- **Framework:** Bootstrap 5.3 with light/dark theme support
- **Base Template:** Extend `base.html`
- **Forms:** Use `{{ form.as_p }}` or manual rendering with Bootstrap classes
- **Messages:** Display Django messages using Bootstrap alerts
- **Icons:** Use Bootstrap Icons or Font Awesome
- **JavaScript:** Minimal JS, prefer HTMX or simple vanilla JS

### URL Patterns

- Use named URL patterns with `name=` parameter
- Group related URLs with `include()`
- Use `app_name` for namespacing
- Prefer descriptive names (e.g., `'part_detail'` not `'detail'`)

## Testing

### Unit Tests

- Write tests for all model methods and properties
- Test service classes with mocked external API calls
- Use `django.test.TestCase` for database-dependent tests
- Use `unittest.mock` for mocking external services

```python
from unittest.mock import patch, MagicMock
from django.test import TestCase
from core.models import Part, Project
from core.services.orcaslicer import OrcaSlicerClient

class PartModelTest(TestCase):
    """Test Part model methods and properties."""
    
    def setUp(self):
        self.project = Project.objects.create(name="Test Project")
        self.part = Part.objects.create(
            project=self.project,
            name="Test Part",
            quantity=5,
            filament_used_grams=50.0,
        )
        
    def test_total_filament_requirement(self):
        """Test that total filament calculates quantity * usage."""
        self.assertEqual(self.part.total_filament_grams, 250.0)
        
@patch('core.services.orcaslicer.requests.post')
class OrcaSlicerClientTest(TestCase):
    """Test OrcaSlicer API integration."""
    
    def test_slice_success(self, mock_post: MagicMock):
        """Test successful slicing operation."""
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {'success': True})
        client = OrcaSlicerClient(base_url='http://orcaslicer:5000')
        result = client.slice('test.stl', 'test.gcode')
        self.assertTrue(result.success)
```

## User Roles & Permissions

LayerNexus uses a **group-based Role-Based Access Control (RBAC)** system. Every view that modifies data **must** use the appropriate mixin from `core/mixins.py`. Never grant access based solely on `LoginRequiredMixin` for write operations.

### Roles

| Role | Django Group | Key Permissions |
|---|---|---|
| **Admin** | `Admin` | All permissions + `auth.change_user` (user management) |
| **Operator** | `Operator` | `can_manage_printers`, `can_control_printer`, `can_manage_print_queue`, `can_dequeue_job`, `can_manage_orca_profiles`, `can_manage_filament_mappings` |
| **Designer** | `Designer` | `can_manage_projects`, `can_manage_print_queue`, `can_dequeue_job` |

The first registered user becomes **Admin**. Subsequent self-registered users receive the **Designer** role.

### Permission Mixins (use instead of `LoginRequiredMixin` for write operations)

```python
from core.mixins import (
    AdminRequiredMixin,         # User management only
    ProjectManageMixin,         # Create/edit/delete projects and parts
    PrinterManageMixin,         # Create/edit/delete printer profiles & cost profiles
    PrinterControlMixin,        # Upload G-code, start/cancel prints
    OrcaProfileManageMixin,     # Import/delete OrcaSlicer profiles
    FilamentMappingManageMixin, # Manage Spoolman filament mappings
    QueueManageMixin,           # Add jobs to print queue
    QueueDequeueMixin,          # Remove jobs from queue
)
```

### Permission Checklist (verify on every Copilot-assisted implementation)

Before writing or reviewing any view, always check:

- [ ] **Read-only views** (list, detail): use `LoginRequiredMixin`
- [ ] **Project/Part create/update/delete**: use `ProjectManageMixin`
- [ ] **Printer profile create/update/delete**: use `PrinterManageMixin`
- [ ] **Print control (upload, start, cancel)**: use `PrinterControlMixin`
- [ ] **OrcaSlicer profile import/delete**: use `OrcaProfileManageMixin`
- [ ] **Filament mapping save**: use `FilamentMappingManageMixin`
- [ ] **Queue add**: use `QueueManageMixin`
- [ ] **Queue remove**: use `QueueDequeueMixin`
- [ ] **User management**: use `AdminRequiredMixin`
- [ ] **No view uses raw `created_by=request.user` filtering as a permission check** — use mixins instead

### Anti-Patterns for Permissions

- ❌ Using `LoginRequiredMixin` alone for any write/delete operation
- ❌ Checking `request.user.is_staff` directly in views (use `AdminRequiredMixin`)
- ❌ Filtering querysets by `created_by=request.user` as a substitute for permission checks
- ❌ Skipping `raise_exception = True` on `RoleRequiredMixin` subclasses (authenticated users without permission must get a 403, not a redirect)



### Error Handling in Views

```python
try:
    # Service call
    moonraker.upload_gcode(gcode_path, filename)
    messages.success(request, "G-code uploaded successfully")
except MoonrakerError as e:
    logger.error(f"Failed to upload G-code: {e}")
    messages.error(request, f"Upload failed: {e}")
```

### Logging

```python
import logging
logger = logging.getLogger(__name__)

# Use appropriate log levels
logger.debug("Debug details for development")
logger.info("Informational message")
logger.warning("Something unexpected but handled")
logger.error("Error that should be investigated")
logger.exception("Error with full traceback")
```

## External Integrations

### OrcaSlicer API
- External API running in a separate Docker container
- RESTful API for slicing operations
- Profiles sent as JSON to the API endpoint
- Returns G-code with metadata (filament usage, print time)

### Moonraker API
- RESTful API for Klipper control
- Upload G-code, start/stop prints, monitor status
- WebSocket connection for real-time updates

### Spoolman
- Filament inventory management
- Track spools by ID and material type
- Automatic color/material population in parts



## Environment Variables

When adding new configuration:
- Use `os.environ.get()` with sensible defaults
- Document in README.md
- Add to docker-compose.yml example

## Security Considerations

- Never commit secrets (use environment variables)
- Validate all user file uploads (STL, G-code)
- Sanitize filenames before filesystem operations
- Use Django's CSRF protection (enabled by default)
- Implement proper permission checks for shared projects

## Common Tasks

### Adding a New Model
1. Define model in `core/models.py`
2. Create migration: `docker compose exec web python manage.py makemigrations`
3. Add to `core/admin.py` for admin interface
4. Create form in `core/forms.py`
5. Create views (Create, Update, Delete, List, Detail)
6. Add URL patterns in `core/urls.py`
7. Create templates in `core/templates/core/`
8. Write unit tests

### Adding a New External Service
1. Create client class in `core/services/`
2. Define custom exception class
3. Add environment variables for configuration
4. Mock external calls in tests
5. Handle errors gracefully in views
6. Document API requirements in README.md

### Sub-Project Aggregation Pattern

When adding data that should aggregate across sub-projects, follow the existing recursive pattern:

```python
# In Project model — see _collect_parts_with_multiplier() as reference
def _collect_items_with_multiplier(self, multiplier: int = 1) -> list:
    """Recursively collect items from this project and all sub-projects."""
    items = []
    for item in self.items.all():
        items.append((item, item.quantity * multiplier))
    for sub in self.subprojects.all():
        items.extend(sub._collect_items_with_multiplier(multiplier * sub.quantity))
    return items
```

Existing aggregation methods: `_collect_parts_with_multiplier()`, `_collect_hardware_with_multiplier()`, `_collect_documents()`.

### File Upload Pattern

For models with file uploads:
- Define allowed extensions and max size as constants in `forms.py`
- Validate in the form's `clean_<field>()` method
- Use `upload_to='<subfolder>/'` in FileField/ImageField
- Ensure form template uses `enctype="multipart/form-data"`
- See `ProjectDocumentForm` (75 MB limit, 9 file types) as reference

### Project Cover Images

The `Project.image` field supports cover images:
- Displayed in project list (card header), detail view, and sub-project tables
- Upload form includes clipboard paste support via JavaScript
- Template uses `{% if project.image %}` guards

## Questions to Ask Before Implementing

1. **Does this need authentication?** → Use `LoginRequiredMixin`
2. **Does this write or delete data?** → Use the appropriate role mixin (see User Roles & Permissions above)
3. **Should this be logged?** → Add appropriate logging
4. **Can this fail?** → Add try/except with user-friendly messages
5. **Is this user-specific?** → Filter by `request.user`
6. **Does this modify data?** → Add success message
7. **Does this involve sub-projects?** → Consider recursive aggregation

## Preferred Libraries

- **HTTP Requests:** `requests` library
- **ORM:** Django ORM (avoid raw SQL)
- **Linting/Formatting:** Ruff (line-length=120, excludes `core/migrations`)
- **Testing:** Django TestCase + unittest.mock
- **Task Queue:** (Future: Celery for long-running tasks)
- **Frontend:** Bootstrap 5.3, Three.js for 3D viewer
- **Static Files:** WhiteNoise with CompressedManifestStaticFilesStorage

## Anti-Patterns to Avoid

- ❌ Function-based views (use CBVs instead)
- ❌ Raw SQL queries (use ORM)
- ❌ Hardcoded configuration (use environment variables)
- ❌ Missing type hints on new code
- ❌ Missing docstrings on public functions
- ❌ Swallowing exceptions without logging
- ❌ Using `LoginRequiredMixin` alone for write/delete views (use role mixins)
- ❌ Checking `request.user.is_staff` directly instead of `AdminRequiredMixin`
- ❌ Inline CSS or JavaScript (use static files)
- ❌ Single quotes for strings (use double quotes)
- ❌ String concatenation for interpolation (use f-strings)
- ❌ Running `manage.py` on the host (use `docker compose exec web`)

## Django Best Practices

- Use Django's timezone-aware datetime (`timezone.now()`)
- Leverage Django signals sparingly (explicit > implicit)
- Use `select_related()` and `prefetch_related()` for query optimization
- Keep business logic in models, not views
- Use Django's caching framework for expensive operations
- Implement proper pagination for large querysets

## Code Review Checklist

When reviewing AI-generated code, verify:
- [ ] Type hints present on all function signatures
- [ ] Docstrings follow Google-style format
- [ ] Error handling with user-friendly messages
- [ ] Logging at appropriate levels
- [ ] **Correct permission mixin used** (see User Roles & Permissions section)
- [ ] **Read-only views use `LoginRequiredMixin`; write views use role mixins**
- [ ] Tests included for new functionality
- [ ] Django messages for user feedback
- [ ] No hardcoded paths or secrets
- [ ] Proper use of Django ORM (no raw SQL)
- [ ] Bootstrap classes for consistent styling
