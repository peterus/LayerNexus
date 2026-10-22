# Umstellung auf uv + Lockfile, Dependabot vereinfachen

Stand: 2026-10-22 · Folgeprojekt zu `2026-10-21-dependabot-prs-design.md`

## Intent / Warum

Heute pinnt `requirements.txt` nur Bereiche (`>=X`, `>=X,<Y`). Folgen:
- Images sind nicht reproduzierbar — jeder Build zieht die gerade neuesten Versionen; CI testet
  ggf. andere Versionen als später ausgeliefert werden.
- Dependabot-PRs heben nur Untergrenzen an (kaum Wert), und weil Dependabot bei Ranges keine
  aktuelle Version kennt, braucht `dependabot.yml` Workarounds (Ausnahmeliste gecappter Pakete,
  `<`-Wächter im Auto-Merge-Workflow).

Ziel: ein echtes Lockfile mit uv. Dependabot schlägt dann konkrete, von der CI getestete
Versionssprünge vor; semver-Klassifikation funktioniert wieder, die Workarounds entfallen.

## Ziele

1. `pyproject.toml` bekommt `[project]` mit den Laufzeit-Abhängigkeiten (heutige Bereiche aus
   `requirements.txt` übernehmen, plus `gunicorn`, das heute nur im Dockerfile installiert wird),
   `requires-python = ">=3.12"`, `version` passend zu `VERSION` (statisch oder dynamisch —
   Builder-Call). Kein Build-Backend nötig (uv „virtual project“), sofern uv das sauber trägt.
2. `[dependency-groups]`: `dev` (ruff `==0.15.20`, coverage, pip-audit), `debug` (debugpy,
   django-debug-toolbar — heute im Debug-Stage des Dockerfiles), ggf. `docs` (was `docs.yml`
   heute per pip installiert).
3. `uv.lock` committet; `requirements.txt` entfernt.
4. Dockerfile: uv aus offiziellem Image (`COPY --from=ghcr.io/astral-sh/uv:<version-tag>`),
   Release-Stage `uv sync --frozen --no-dev` (+ nur Runtime), Debug-Stage zusätzlich Gruppe
   `debug`. Layer-Caching beibehalten (erst `pyproject.toml`+`uv.lock` kopieren, dann Code).
   Docker-Smoke-Test (`/health/`) muss grün bleiben.
5. CI (`ci.yml`, `docs.yml`): uv statt pip (`astral-sh/setup-uv`, `uv sync --frozen`),
   ruff-Version kommt aus dem Lock statt hartcodiert. pip-audit prüft den Lock-Inhalt
   (z. B. `uv export --frozen --no-hashes | pip-audit -r /dev/stdin` oder gleichwertig).
   CI muss scheitern, wenn `uv.lock` nicht zu `pyproject.toml` passt (`--frozen`/`--locked`).
6. Doku: `CLAUDE.md` (Commands-Block, Python-/venv-Hinweise, Ruff-Gotcha), `README.md`,
   `docs/development/*`, `.dockerignore`, `.claude/settings.local.json` (falls dort
   `.venv/bin/...`-Befehle erlaubt sind) auf uv-Befehle (`uv sync`, `uv run python manage.py …`).
   Alte Plan-/Spec-Dokumente unter `docs/superpowers/` NICHT anfassen (historisch).
7. Dependabot (`dependabot.yml` + `dependabot-automerge.yml`):
   - Ökosystem `pip` → `uv`.
   - Gruppe `python-deps`: alle Pakete, `update-types: [minor, patch]`, **ohne** django.
     Django minor + alle Majors → Einzel-PRs, kein Auto-Merge.
   - Ignore-/Exclude-Workarounds und der `<`-Wächter im Auto-Merge-Workflow entfallen;
     Auto-Merge nur für Gruppen `python-deps` und `actions-all` (actions-all unverändert inkl. Majors).
     Zusätzlich im Workflow `update-type != semver-major` als Gürtel+Hosenträger für `python-deps`.
   - **Cooldown** für uv: 3 Tage default/minor/patch, 7 Tage major. Für actions ebenfalls 3 Tage.
   - Docker-Ökosystem unverändert (trackt künftig auch das uv-Image-Tag).
   - YAML-Kommentare aktualisieren (Root-Cause-Text der Ranges entfernen/kürzen).
   - **Nachtrag (User-Entscheidung während PR #71):** Django-**Patches** werden ebenfalls
     auto-gemergt — eigene Gruppe `django-patch` (patterns `django`, `update-types: [patch]`,
     vor `python-deps`), im Workflow nur bei `update-type == semver-patch` scharf. Außerdem
     `ruff` aus `python-deps` ausgenommen (bewusst gepinnt) und `allow: dependency-type: all`
     (transitive Pakete). Nur Django-Minor + Majors + ruff bleiben manuell.

## User-Entscheidungen (2026-10-22)

- uv (nicht pip-tools).
- Cooldown ja; versetzte Liefertage nein; Actions-SHA-Pinning nein.
- Auto-Merge: patch+minor aller Pakete inkl. transitiver; Django-Minor manuell; Majors immer manuell.

## Nicht-Ziele

- Keine Versions-Upgrades als Teil der Umstellung: Lock zunächst auf den aktuell aufgelösten
  Stand (das, was heute ein frischer Build installiert). Keine Python-/Django-Major-Wechsel.
- Kein Umbau der App-Struktur zu einem installierbaren Paket.
- Keine Änderungen an Docker-Compose-Services außer was der Build zwingend braucht.

## Anti-Ziele

- Prod-Image bricht (Smoke-Test, `entrypoint.sh` migrate/collectstatic, gunicorn-Start).
- CI wird spürbar langsamer (uv sollte schneller sein; Cache nutzen).
- Auto-Merge erfasst Majors oder Django-Minor.

## Akzeptanzkriterien

- `uv.lock` committet, `requirements.txt` weg, `uv sync --frozen` lokal + CI + Docker grün.
- Alle CI-Gates grün inkl. Docker-Smoke-Test, Coverage ≥ 55, pip-audit auf dem Lock.
- `docker compose up -d --build` startet web+worker (Builder verifiziert lokal, sofern Docker verfügbar).
- `dependabot.yml` validiert (Dependabot meldet nach Merge keinen Config-Fehler im Repo-Insights-Tab
  bzw. erster Lauf erzeugt erwartete PRs) — Beobachtung nach Merge durch Koordinator.
- Doku/`CLAUDE.md` beschreiben den uv-Workflow inkl. „Paket hinzufügen: `uv add <pkg>`“.

## Dem Builder überlassen

- Statische vs. dynamische `version`, exakte uv-Image-/Action-Versionen (Major-Tag-Pin wie im Repo).
- Ob ein PR oder zwei (Umstellung + Dependabot); Empfehlung: ein PR, da eng gekoppelt.
