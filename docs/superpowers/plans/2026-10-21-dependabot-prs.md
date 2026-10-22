# Dependabot-PRs #57–#62 Implementation Plan

> **For agentic workers:** Execution via `kontor-recipes:kontor-manager-cycle` — Koordinator merged über `gh`, Kind-Sessions arbeiten in eigenen Worktrees (TDD → PR → grüne CI → review-loop bis 0 → done-Push). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Die sechs offenen Dependabot-PRs sauber abarbeiten (inkl. kontrolliertem websockets-17-Upgrade mit Live-Test) und Dependabot auf einen wartungsarmen Modus (Gruppierung + Auto-Merge) umstellen.

**Architecture:** Drei unabhängige Arbeitspakete: (1) Koordinator merged #62, #58–#61 sequenziell; (2) Kind A liefert websockets 17 als eigenen PR mit Analyse, neuen Client-Tests und Live-Nachweis gegen `klipper2`, ersetzt #57; (3) Kind B korrigiert `dependabot.yml`, fügt den Auto-Merge-Workflow hinzu und committet Spec + Plan.

**Tech Stack:** Python 3.13 (venv) / 3.14 (CI), Django 6, websockets 17.x, GitHub Actions, Dependabot v2, `gh`.

**Spec:** `docs/superpowers/specs/2026-10-21-dependabot-prs-design.md`

## Global Constraints

- Merge nur über `gh`/GitHub-API, immer `--squash`. Koordinator arbeitet nie im Haupt-Checkout.
- `main` ist strict geschützt (Required: `Lint & Format`, `Tests`, `Security Checks`, `Docker Build & Smoke Test`); `--admin` nur ausnahmsweise bei BEHIND + allen Required-Checks grün, im Merge-Commit begründet.
- `>=`-Untergrenzen-Strategie bleibt; keine Lockfiles, kein pip-tools, keine Upgrades außerhalb der Dependabot-PRs.
- Tests nutzen `unittest.mock`, nie echtes Netz. Django-Testrunner (`.venv/bin/python manage.py test core`), Ruff `0.15.20`.
- Code-Style laut `CLAUDE.md`: doppelte Quotes, f-Strings, Type Hints, Google-Docstrings.
- Live-Test-Freigabe (User): auf `klipper2` (192.168.88.189) nur `M117 <Text>` oder Solltemperatur setzen und sofort wieder auf 0; keine Bewegung, kein Druckstart, nicht während `print_stats.state == "printing"`.
- Auto-Merge: nur `dependabot[bot]`; pip-Gruppe und `actions-all` ja (Actions auch Majors); Anheben einer Obergrenze (django, websockets, djangorestframework, drf-spectacular) nie; keine handgepflegten Versions-Ignore-Listen; `GITHUB_TOKEN`, kein PAT (fehlende Push-Workflows auf `main` nach Auto-Merge sind akzeptiert und im Workflow kommentiert).

## Review Focus

1. websockets 17 schließt bei Server-Close/Keepalive-Timeout mit `ConnectionClosedError`/`ConnectionClosedOK` → `run()` muss weiter reconnecten, nicht sterben (Test in Task 2).
2. Subscribe-Antwort (JSON-RPC-Response ohne `"method"`) darf nicht an `on_event` gehen, `notify_status_update` schon (Test in Task 2).
3. Moonraker mit API-Key → Token-Query-String wird an die URL gehängt, `connect` behält `ping_interval=20, ping_timeout=20` (Test in Task 2).
4. Ein Obergrenzen-Bump (z. B. django `<7` → `<8`) darf weder gruppiert noch auto-gemergt werden (Verifikation in Task 3, Step 4).
5. Zwei gleichzeitig offene Dependabot-PRs unter strict-Protection: der zweite bleibt BEHIND → muss per `@dependabot rebase` weiterlaufen, nicht hängen (dokumentiert in Task 3).

---

### Task 1: #62, #58, #59, #60, #61 mergen (Koordinator, kein Kind)

**Files:** keine eigenen Änderungen (Dependabot-Branches, nicht von Hand editieren).

- [ ] **Step 1:** `gh pr checks 62 --repo peterus/LayerNexus` → alle grün → `gh pr merge 62 --repo peterus/LayerNexus --squash`.
- [ ] **Step 2:** Für #58, #59, #60, #61 nacheinander: Kommentar `@dependabot rebase` (oder `gh pr update-branch <n>`), auf grüne Checks warten (ScheduleWakeup, kein Sleep-Poll), dann `gh pr merge <n> --squash`.
- [ ] **Step 3:** Bei `requirements.txt`-Konflikt: nur `@dependabot rebase`, nie Handedit. Bei roter CI: stoppen, Ursache prüfen, ggf. Kind-Zyklus (Update erfordert Code-Änderung).
- [ ] **Step 4:** `pm_report in_progress` mit PR-Links #58–#62.

### Task 2: websockets 17 (Kind A, ersetzt #57)

**Files:**
- Modify: `requirements.txt` (`websockets>=17.1,<18`)
- Modify (nur falls Analyse es verlangt): `core/services/moonraker_ws.py`
- Test: `core/tests/test_services_moonraker_ws.py` (neue Klasse `MoonrakerWebSocketClientTests`, `IsolatedAsyncioTestCase` bzw. `async_to_sync`, mock `websockets.connect` und `fetch_oneshot_token`)

**Interfaces:**
- Consumes: `MoonrakerWebSocketClient(printer, on_event)`, `.run()`, `._connect_and_listen()`, `build_ws_url()`, `fetch_oneshot_token()`, Konstanten `RECONNECT_INITIAL_DELAY`, `RECONNECT_MAX_DELAY` (unverändert).
- Produces: eigener PR `chore(deps): websockets 17` auf Branch `chore/websockets-17`; #57 wird mit Verweis geschlossen.

- [ ] **Step 1:** Branch von aktuellem `main`; Tests schreiben (noch mit websockets 15 im venv grün erwartet — sie sichern Verhalten, dann Upgrade):
  - `test_connect_uses_keepalive_params`: `websockets.connect` aufgerufen mit `ws_url` und `ping_interval=20, ping_timeout=20`.
  - `test_connect_appends_token_when_api_key`: Token `"abc"` → URL endet auf `?token=abc`.
  - `test_subscribe_request_sent`: erste `send` ist JSON mit `method == "printer.objects.subscribe"`, `id == 1`.
  - `test_status_update_forwarded_response_ignored`: Frames `{"jsonrpc":"2.0","id":1,"result":{...}}`, `"not json"`, `{"method":"notify_status_update","params":[...]}` → `on_event` genau einmal mit dem notify-Event.
  - `test_run_reconnects_after_connection_closed`: erster `_connect_and_listen` wirft `ConnectionClosedError(None, None)` (Konstruktor wie in websockets 17), zweiter wirft `asyncio.CancelledError` → `asyncio.sleep` einmal mit `RECONNECT_INITIAL_DELAY` aufgerufen, `CancelledError` propagiert.
  - `test_backoff_reset_after_subscribe`: nach erfolgreichem Subscribe ist `_reconnect_delay == RECONNECT_INITIAL_DELAY`.
- [ ] **Step 2:** `.venv/bin/python manage.py test core.tests.test_services_moonraker_ws` mit aktuellem websockets → PASS (Charakterisierung).
- [ ] **Step 3:** `requirements.txt` auf `websockets>=17.1,<18`, `.venv/bin/pip install "websockets>=17.1,<18"`, Tests erneut → PASS; falls FAIL: Code minimal anpassen.
- [ ] **Step 4:** Changelogs 15.0–17.x lesen, Tabelle „Breaking Change → betrifft uns? → Begründung“ (connect-Defaults/`open_timeout`, `max_queue`, Legacy-Entfernung, Exceptions, Python-Mindestversion vs. 3.13/3.14 und Docker-Base-Image) für den PR-Body.
- [ ] **Step 5:** Live-Test gegen `klipper2` (Details Memory `feature/moonraker-debug/access`): kleines Skript, das `MoonrakerWebSocketClient` mit einem In-Memory-`PrinterProfile(moonraker_url="http://192.168.88.189")` und einem loggenden `on_event` startet. Vorher `GET /printer/objects/query?print_stats` → nicht `printing`. Dann `POST /printer/gcode/script?script=M117 LayerNexus ws17 test` (alternativ `SET_HEATER_TEMPERATURE HEATER=extruder TARGET=40` → danach `TARGET=0`). Nachweis: ≥1 `notify_status_update` vor und ≥1 nach der Aktion empfangen (z. B. `display_status.message` bzw. `extruder.target`). Log-Auszug in den PR. Skript nicht committen.
- [ ] **Step 6:** `ruff check . && ruff format --check .`, `manage.py test core`, Commit, PR öffnen, CI grün, review-loop bis 0, `done` an Koordinator. Fallback (inkompatibel/Live-Test unmöglich): `blocked` mit Empfehlung „Pin `<16` behalten, #57 schließen“.

### Task 3: Dependabot-Config + Auto-Merge-Workflow (Kind B)

**Files:**
- Modify: `.github/dependabot.yml`
- Create: `.github/workflows/dependabot-automerge.yml`
- Add: `docs/superpowers/specs/2026-10-21-dependabot-prs-design.md`, `docs/superpowers/plans/2026-10-21-dependabot-prs.md` (aus dem Haupt-Checkout, dort untracked, in den Worktree kopieren)

**Interfaces:**
- Produces: Gruppenname der pip-Gruppe (bisher `python-minor-patch`, ggf. umbenennen) und `actions-all` — beide im Auto-Merge-Workflow referenziert.

- [ ] **Step 1:** Ursache verifizieren, warum Gruppe + `ignore semver-major` bei den `>=`-Ranges nicht griffen (Dependabot-Doku/Issues zu `requirements.txt`-Ranges, `versioning-strategy`, update-type-Klassifikation). Ergebnis als YAML-Kommentar + im PR-Body.
- [ ] **Step 2:** `dependabot.yml` so ändern, dass: Untergrenzen-Bumps aller Pakete wöchentlich in **einem** pip-PR kommen; Obergrenzen-Anhebungen (django, websockets, djangorestframework, drf-spectacular) als Einzel-PRs außerhalb der Gruppe; keine handgepflegten Versionslisten. `actions-all` und Docker unverändert.
- [ ] **Step 3:** Auto-Merge-Workflow: Trigger `pull_request` (bzw. `pull_request_target` nur falls nötig, begründen), Bedingung `github.actor == 'dependabot[bot]'`, `dependabot/fetch-metadata` (auf Major-Tag gepinnt wie Rest des Repos), `gh pr merge --auto --squash "$PR_URL"` nur wenn `dependency-group` ∈ {pip-Gruppe, `actions-all`}; Permissions `contents: write`, `pull-requests: write`; Kommentar zu GITHUB_TOKEN-Folge (keine Push-Workflows auf `main`) und zu strict-Protection (`@dependabot rebase` bei BEHIND).
- [ ] **Step 4:** Verifikation: YAML-Lint (`python -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))"` für beide Dateien); `actionlint` falls verfügbar. Gedankenprobe im PR-Body dokumentieren: (a) pillow-Untergrenzen-Bump → Gruppe → Auto-Merge; (b) django `<7` → `<8` → Einzel-PR → kein Auto-Merge; (c) Actions-Major → `actions-all` → Auto-Merge; (d) Nicht-Dependabot-PR → Job übersprungen.
- [ ] **Step 5:** Commit, PR, CI grün, review-loop bis 0, `done` an Koordinator.

### Task 4: Abschluss (Koordinator)

- [ ] **Step 1:** PRs aus Task 2 und 3 gaten (`gh pr checks`, 0 unresolved threads) und squash-mergen; dazwischen update-branch.
- [ ] **Step 2:** Beim nächsten Dependabot-Lauf (wöchentlich) prüfen, dass pip als ein Gruppen-PR kommt und Auto-Merge greift — ggf. manuell `Check for updates` anstoßen; Ergebnis in Memory festhalten.
- [ ] **Step 3:** `pm_report done` mit allen PR-Links.
