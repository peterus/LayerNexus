# uv + Lockfile Implementation Plan

> **For agentic workers:** Execution via `kontor-recipes:kontor-manager-cycle` — ein Kind, ein PR, Koordinator merged über `gh`. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Abhängigkeiten via uv mit committetem `uv.lock` verwalten und Dependabot/Auto-Merge auf semver-basierte Regeln vereinfachen.

**Architecture:** `pyproject.toml` `[project]` + `[dependency-groups]` als Quelle, `uv.lock` als Lock; Docker/CI installieren `--frozen` aus dem Lock; Dependabot-Ökosystem `uv` mit Gruppe minor/patch (ohne Django) + Cooldown.

**Tech Stack:** uv, Python 3.14 (CI/Docker) / ≥3.12, Django 6, GitHub Actions (`astral-sh/setup-uv`), Dependabot v2.

**Spec:** `docs/superpowers/specs/2026-10-22-uv-lockfile-design.md`

## Global Constraints

- Lock auf den heute aufgelösten Stand; keine bewussten Upgrades.
- Ruff bleibt `0.15.20` (jetzt via Lock). Coverage-Gate 55 bleibt.
- Major-Tag-Pins für Actions/Images wie im restlichen Repo, keine SHA-Pins.
- Auto-Merge: nur `python-deps` (minor+patch, ohne django/ruff), `django-patch` (Nachtrag, User-Entscheidung) und `actions-all`; nie Majors, nie Django-Minor.
- Cooldown: uv default 3 / semver-major 7 Tage; actions 3 Tage.
- Merge nur über `gh`, squash; Koordinator arbeitet nicht im Haupt-Checkout.

## Review Focus

1. `uv.lock` passt nicht zu `pyproject.toml` → CI muss rot werden (`--locked`/`--frozen`-Check).
2. Docker-Release-Image enthält keine Dev-/Debug-Gruppen, gunicorn ist drin, `entrypoint.sh` läuft.
3. Debug-Stage hat debugpy + django-debug-toolbar weiterhin.
4. Auto-Merge-Workflow armt keinen Major- und keinen Django-Minor-PR (Gedankenprobe/Test mit fetch-metadata-Outputs dokumentieren).
5. pip-audit prüft tatsächlich die gelockten Versionen (nicht leer durchlaufen).

---

### Task 1: pyproject + uv.lock

**Files:** Modify `pyproject.toml`; Create `uv.lock`; Delete `requirements.txt`.

- [ ] `[project]` (name `layernexus`, version, `requires-python = ">=3.12"`, dependencies = heutige requirements.txt-Zeilen + `gunicorn`), `[dependency-groups]` dev/debug/(docs), `[tool.uv]` falls nötig (z. B. `package = false`).
- [ ] `uv lock`; prüfen, dass aufgelöste Versionen = heutiger Stand (`pip freeze` aus aktuellem Image/venv vergleichen).
- [ ] `uv sync` lokal, `uv run python manage.py test core` grün (vorher `collectstatic` falls Worktree frisch), `uv run ruff check . && uv run ruff format --check .`.
- [ ] Commit.

### Task 2: Docker

**Files:** Modify `Dockerfile`, `.dockerignore`.

- [ ] uv-Binary aus `ghcr.io/astral-sh/uv` kopieren; Base-Stage: `pyproject.toml`+`uv.lock` kopieren → `uv sync --frozen --no-dev --no-install-project` → Code kopieren; venv auf PATH (`/app/.venv/bin`) bzw. `UV_PROJECT_ENVIRONMENT` passend.
- [ ] Debug-Stage: `uv sync --frozen --group debug` statt pip.
- [ ] `docker build --target release` + Container starten, `curl /health/` = 200 (falls Docker lokal verfügbar; sonst CI-Smoke-Test als Nachweis).
- [ ] Commit.

### Task 3: CI + Docs-Workflow

**Files:** Modify `.github/workflows/ci.yml`, `.github/workflows/docs.yml`.

- [ ] `astral-sh/setup-uv` (Major-Tag, Cache an), Python 3.14; lint: `uv run ruff …` (Version aus Lock); test: `uv sync --frozen` + `uv run coverage …`; security: pip-audit gegen exportierten Lock + `check --deploy`; Lock-Konsistenz-Check (`uv lock --check` o. ä.).
- [ ] docs.yml analog (Gruppe docs).
- [ ] Commit, Push, CI grün.

### Task 4: Dependabot + Auto-Merge

**Files:** Modify `.github/dependabot.yml`, `.github/workflows/dependabot-automerge.yml`.

- [ ] Ökosystem `uv`; Gruppe `python-deps` (patterns `*`, exclude `django`, update-types minor+patch); cooldown (default-days 3, semver-major-days 7); actions-all + cooldown 3; Range-Root-Cause-Kommentare durch kurze Policy-Erklärung ersetzen.
- [ ] Auto-Merge-Workflow: `<`-Guard + compare-API-Logik entfernen; Bedingung: Gruppe ∈ {python-deps, actions-all} UND (Gruppe == actions-all ODER update-type != version-update:semver-major). Disarm-Job + `--match-head-commit` + concurrency behalten.
- [ ] actionlint + YAML-Parse; Gedankenprobe im PR-Body: (a) pillow patch → Gruppe → auto; (b) django 6.0→6.1 → Einzel-PR → manuell; (c) websockets 17→18 → Einzel-PR → manuell; (d) actions major → auto; (e) Nicht-Dependabot-PR → skip.
- [ ] Commit.

### Task 5: Doku

**Files:** Modify `CLAUDE.md`, `README.md`, `docs/development/*.md` (betroffene), `.claude/settings.local.json` (nur falls `.venv/bin`-Allow-Regeln existieren), `docs/configuration.md` falls Install-Schritte.

- [ ] Commands-Block auf uv (`uv sync`, `uv run python manage.py test core`, `uv add <pkg>`, `uv lock --upgrade-package <pkg>`); Ruff-Gotcha anpassen (Version kommt aus Lock); Hinweis Lockfile nie von Hand editieren.
- [ ] `ruff format --check` (Markdown!) grün; Commit; PR → CI grün → review-loop bis 0 → done.

### Task 6: Abschluss (Koordinator)

- [ ] PR gaten + mergen. Nach Merge: Dependabot-Lauf beobachten (Insights → Dependabot bzw. erste PRs), Ergebnis in Memory; Hinweis an User: lokale `.venv` einmal mit `uv sync` neu aufbauen; nächster Prod-Deploy baut mit Lock.
