# Dependabot-PRs #57–#62 abarbeiten + Dependabot-Pipeline wartungsarm machen

Stand: 2026-10-21 · PM-Topic 12

## Intent / Warum

Seit #56 (2026-09-23) liegen sechs Dependabot-PRs offen und veralten. Die unkritischen
Updates sollen sauber rein, das riskante websockets-Major-Update (#57) kontrolliert
geprüft werden — `core/services/moonraker_ws.py` + `moonraker_worker` sind das
Herzstück (Projekt bleibt bei Moonraker). Danach soll Dependabot so laufen, dass er
künftig kaum Pflege braucht: ein gruppierter PR statt fünf, kleine Updates mergen sich
selbst, Major-Sprünge bleiben bewusste Handarbeit.

## Ziele

1. #58 (requests), #59 (pillow), #60 (markdown), #61 (whitenoise), #62 (actions-all,
   11 Actions) gemergt — squash, jeweils mit grüner CI.
2. #57 (websockets `>=14,<16` → `>=17.1,<18`) nach Analyse + Tests + Live-Test gegen
   echten Moonraker gemergt.
3. `.github/dependabot.yml` korrigiert, sodass Gruppierung und Major-Ausschluss für pip
   tatsächlich greifen.
4. Auto-Merge für Minor/Patch-Dependabot-PRs (pip + Actions), sobald CI grün.

## Nicht-Ziele

- Keine Upgrades über die Dependabot-PRs hinaus (kein Django-Major, keine Python-Version).
- Keine Umstellung des Dependency-Managements (keine Lockfiles, kein pip-tools). Die
  `>=`-Untergrenzen-Strategie bleibt (User-Entscheidung).
- Kein Docker-Base-Image-Update in diesem Scope (aktuell kein offener Docker-PR).

## Anti-Ziele (wäre Fehlschlag, auch wenn „technisch fertig“)

- Ein websockets-Upgrade, das in Tests grün ist, aber im Worker gegen echten Moonraker
  Status-Updates verliert oder Reconnect-Verhalten verschlechtert.
- Auto-Merge, das auch Major-Sprünge oder Nicht-Dependabot-PRs erfasst.
- Branch-Protection-Umgehung (`--admin`) als Normalweg — nur falls ein Dependabot-PR
  wegen strict/BEHIND hängt und die Required-Checks grün sind, und dann dokumentiert.

## Kontext (geprüft 2026-10-21)

- Alle sechs PRs: CLEAN, alle Checks SUCCESS.
- Branch-Protection `main`: strict (require up-to-date), Required Checks
  `Lint & Format`, `Tests`, `Security Checks`, `Docker Build & Smoke Test`, 0 Approvals
  nötig. Repo: `allow_auto_merge=true`, `allow_update_branch=true`,
  `delete_branch_on_merge=true`.
- websockets-Nutzung im Code: nur `core/services/moonraker_ws.py`
  (`import websockets`, `ConnectionClosed`, `WebSocketException`,
  `websockets.connect(url, ping_interval=20, ping_timeout=20)` als async-Context-Manager).
  Das ist bereits die neue asyncio-API → Erwartung: kompatibel, muss aber belegt werden.
- `dependabot.yml` hat bereits eine pip-Gruppe `python-minor-patch` (update-types
  minor/patch) und `ignore` semver-major für `*` — trotzdem kamen 4 Einzel-PRs **und**
  der websockets-Major-PR. Arbeitshypothese (vom Builder zu verifizieren): bei
  Range-Requirements (`>=X`, `>=X,<Y`) kann Dependabot keinen konkreten Ist-Stand
  bestimmen, die semver-update-type-Klassifikation greift nicht → weder die
  update-types-Gruppe noch die major-Ignore-Regel matchen.

## Entscheidungen

### A. #58–#62 (vom User/PM entschieden)
- Nacheinander squash-mergen über `gh`/GitHub-API; CI-grün ist das Gate, kein eigener
  Review-Loop (Ausnahme: ein Update erzwingt Code-Änderungen → dann normaler Zyklus).
- Wegen strict-Protection zwischen den Merges `gh pr update-branch` bzw.
  `@dependabot rebase` und erneut auf grün warten.
- Reihenfolge: #62 (Actions) zuerst, dann #58–#61. *(Builder-Call: Reihenfolge ist
  egal, Diffs sind disjunkt bis auf `requirements.txt`-Nachbarzeilen.)*
- Entsteht beim Rebase ein `requirements.txt`-Konflikt, löst Dependabot ihn per
  `@dependabot rebase`; kein Handeditieren der Dependabot-Branches.

### B. #57 websockets (vom User/PM entschieden)
1. Changelogs 15.x, 16.x, 17.x durchgehen (Breaking Changes: Entfernung der
   Legacy-Implementierung, `connect`-Signatur/Defaults, Exceptions, Keepalive-/
   Ping-Verhalten, Python-Mindestversion vs. CI 3.14 / venv 3.13 / Docker-Image).
2. Eigenen Code dagegen prüfen; anpassen falls nötig. Ergebnis der Analyse kurz im PR
   dokumentieren (Tabelle Breaking Change → betroffen ja/nein → Begründung).
3. Tests: bestehende WS-Tests müssen mit 17.x grün laufen; fehlende Abdeckung für die
   genutzten API-Punkte (connect-Aufruf/Parameter, Verhalten bei `ConnectionClosed`
   und `WebSocketException`) ergänzen — mit `unittest.mock`, nie echtes Netz in Tests.
4. **Live-Test gegen `klipper2`** (192.168.88.189, Moonraker v0.10.0, API ohne Auth im
   Trusted-LAN; Details Memory `feature/moonraker-debug/access`). Lokal mit
   websockets 17.x den Moonraker-WS-Client/Worker-Pfad gegen den echten Drucker laufen
   lassen; Nachweis: (a) initialer Druckerstatus empfangen, (b) eine Statusänderung
   empfangen. **User-Freigabe:** die Statusänderung darf selbst ausgelöst werden, mit
   harmlosen Aktionen — `M117 <Text>` (Display-Message) oder kurz eine Solltemperatur
   setzen und **sofort wieder auf 0**. Keine Bewegungen, kein Druckstart, nicht
   während eines laufenden Drucks (vorher `print_stats.state` prüfen; läuft ein Druck →
   nur passiv beobachten bzw. warten). Log-Auszug des Tests in den PR.
5. Code-Änderungen (falls nötig) kommen als Commit auf einen eigenen Branch/PR oder
   auf #57 — *Builder-Call*. Merge erst nach Live-Test.
6. Fallback, falls Live-Test nicht machbar oder 17.x inkompatibel ohne vertretbaren
   Fix: Pin `<16` beibehalten, #57 mit Begründung schließen, Empfehlung an User.

### C. Dependabot-Konfiguration (User-Entscheidung: Gruppierung + Auto-Merge ja)
- Ursache für „Gruppe/Ignore greift nicht“ verifizieren (Dependabot-Doku/Issues;
  Dependabot-Job-Logs unter Insights → Dependency graph → Dependabot, falls nötig).
- `dependabot.yml` so ändern, dass pip-Updates wöchentlich als **ein** gruppierter PR
  kommen und Major-Sprünge nicht automatisch reinkommen. Wenn update-types bei Ranges
  nicht funktioniert, ist eine pattern-basierte Gruppe plus explizite Major-Sperren
  (z. B. per `ignore`-`versions` für die bekannten Obergrenzen wie django `<7`,
  websockets `<18`, djangorestframework `<4`, drf-spectacular `<1`) akzeptabel —
  *Builder-Call*, welche Variante; Hauptsache Verhalten wie gewollt und im YAML-Kommentar
  begründet.
- Actions-Gruppe `actions-all` bleibt.
- **Was „Major“ heißt (User-Entscheidung 2026-10-21):**
  - Pakete **ohne** Obergrenze (requests, pillow, markdown, bleach, whitenoise,
    defusedxml): Dependabot hebt nur die Untergrenze; der Docker-Build installiert
    ohnehin bereits die neueste Version → Untergrenzen-Bumps (auch über Majors) sind
    risikoarm, gehören in die wöchentliche pip-Gruppe und dürfen auto-mergen.
  - Pakete **mit** Obergrenze (django `<7`, websockets `<18` nach #57,
    djangorestframework `<4`, drf-spectacular `<1`): Anheben der Obergrenze = echtes
    Upgrade → kommt als Einzel-PR, **kein** Auto-Merge, manueller Zyklus wie #57.
    Untergrenzen-Bumps innerhalb der Obergrenze dürfen in die Gruppe.
  - Die Lösung darf keine von Hand nachzupflegenden Versions-Ignore-Listen erfordern
    (kein Wartungsaufwand bei jedem neuen Major).
  - GitHub Actions: Gruppe `actions-all` darf auch Majors auto-mergen (Actions sind auf
    Major-Tags gepinnt, sonst würde nie etwas automatisch gemergt). Gate = grüne CI.
- **Auto-Merge:** neuer Workflow (z. B. `.github/workflows/dependabot-automerge.yml`),
  nur für `github.actor == 'dependabot[bot]'`, nutzt `dependabot/fetch-metadata`, setzt
  `gh pr merge --auto --squash` nur für die pip-Gruppe und `actions-all` gemäß den Regeln
  oben. Obergrenzen-Upgrades und sonstige Einzel-PRs werden **nicht** auto-gemergt.
  Minimal-Permissions (`contents: write`, `pull-requests: write`). Token = `GITHUB_TOKEN`
  (kein PAT vorhanden). **Akzeptiert (User):** dadurch triggern Auto-Merges keine
  Push-Workflows auf `main` (Docker-Publish, CI-on-main); das Image wird beim nächsten
  normalen Merge gebaut. Im Workflow-Kommentar dokumentieren.
  Strict-Protection: Auto-Merge wartet auf up-to-date; für den seltenen Fall, dass zwei
  Dependabot-PRs gleichzeitig offen sind, reicht `@dependabot rebase` von Hand bzw.
  Dependabots eigenes Rebase — keine `--admin`-Automatik.
- Workflow-Datei muss Ruff/CI nicht berühren; Actions-Versionen gepinnt wie im Rest
  des Repos.

## Akzeptanzkriterien

- #58, #59, #60, #61, #62 gemergt (oder mit Begründung geschlossen), jeweils grüne CI.
- #57: Breaking-Change-Analyse im PR; Code angepasst oder als kompatibel belegt, mit
  Tests; Live-Test gegen `klipper2` zeigt Status + Statusänderung (Log im PR); gemergt.
  Sonst: klare Empfehlung an User (Pin `<16`, PR schließen).
- `dependabot.yml` korrigiert + Auto-Merge-Workflow gemergt (eigener PR, normaler
  Review-Loop, CI grün).
- Fortschritt/Abschluss per `pm_report` an Topic 12.

## Dem Builder überlassen

- Exakte Merge-Reihenfolge innerhalb #58–#62.
- Ob websockets-Code-Anpassungen auf #57 oder in einen Folge-PR kommen.
- Konkrete YAML-Form der Gruppierung/Major-Sperre und des Auto-Merge-Gates, solange das
  Verhalten oben erfüllt ist.
- Wie der Live-Test technisch läuft (Worker lokal im venv mit websockets 17.x gegen
  `klipper2` oder ein kleines Skript über `MoonrakerWebSocket`-Klasse), solange der
  echte Produktionscode-Pfad genutzt wird.
