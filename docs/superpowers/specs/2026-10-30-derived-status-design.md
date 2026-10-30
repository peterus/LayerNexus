# Status ableiten statt speichern — Design

Stand: 2026-10-30 · PM-Topics 32 (Architektur), 31 (Platte eingereiht), 30 (gedruckte Teile zählen)

## Absicht und Warum

Die gespeicherten Status in LayerNexus waren bisher **korrekt**, aber das System ist unnötig
kompliziert: rund 100 Stellen in 18 Dateien setzen Status von Hand, auch direkt in Views, obwohl
sich die meisten Zustände aus der Datenbank berechnen lassen. Ziel ist **Einfachheit**:

- Nur **Fakten** speichern (Ereignisse, Urteile, Ergebnisse, Zeitstempel).
- Sammelstatus **ableiten** (Modell-Properties / Querysets / Annotationen) — Vorbild ist
  `Project.aggregated_status` (`core/models/projects.py`), das bereits berechnet wird.
- Übergänge in **Modell-Methoden** bündeln (CLAUDE.md: „Business-Logik in Models“), Views und
  API rufen nur diese Methoden.

Nebenbei werden zwei Nutzerprobleme gelöst, weil sie aus dem neuen Modell direkt folgen:

- **Topic 31:** Man sieht, welche Platten schon eingereiht sind.
- **Topic 30:** Gedruckte Stückzahlen stimmen auch bei teilweise fertigen Aufträgen und für
  Drucke außerhalb von LayerNexus.

## Ziele

1. Druckhistorie bleibt erhalten: jeder Queue-Eintrag ist ein **Druckversuch** und wird nach dem
   Druck nicht mehr gelöscht.
2. Platten-Status, Druck-Status des Auftrags und Queue-Status werden **berechnet**, nicht gespeichert.
3. Eine Platte **kennt ihre Teile**; jede bestandene Platte zählt sofort.
4. Gedruckte Teile lassen sich **manuell buchen** (Fremddrucke, Korrekturen, Altfälle).
5. Weniger Status-Schreibcode; alle Übergänge an einer Stelle.

## Nicht-Ziele

- Keine neuen Funktionen außer den hier beschriebenen (Status, Zählung, Bearbeitungsregeln).
- Moonraker-WS-Re-Subscribe (Topic 33) — bereits erledigt in #93.
- Lösch-Sperre für einzelne Teile / im Admin (Topic 28), Gunicorn-Timeout (Topic 29).
- Kein Big-Bang: mehrere einzeln mergebare PRs (siehe Reihenfolge).

## Anti-Ziele (wäre ein Fehlschlag, auch wenn es technisch läuft)

- Anzeigen, die heute stimmen, sind danach falsch.
- Filter / Sortierungen (Druckauftragsliste mit Mehrfachfiltern aus #79, Farm-Dashboard,
  Queue-Liste) funktionieren nicht mehr oder werden spürbar langsamer (N+1).
- Gedruckte Stückzahlen oder Projektfortschritt ändern sich durch die Migration für bestehende
  Daten unerwartet (Ausnahme: sie werden *korrekter*, z. B. teilfertige Aufträge zählen jetzt).
- Druckhistorie geht verloren.

## Ist-Zustand (Recon 2026-10-29)

| Feld | Schreibstellen | Bewertung |
|---|---|---|
| `PrintJob.status` | 13 in 6 Dateien | Slicing-Zustände = Fakt; `printing`/`completed` ableitbar aus Platten. `uploading`, `uploaded`, `cancelled` werden nie gesetzt. Job mit verworfener Platte bleibt ewig `printing`. `failed` mischt Slicing- und Druckfehler. |
| `PrintJobPlate.status` | 6 in 3 Dateien | `completed`/`failed` = Operator-Urteil, heute nur hier gespeichert, weil Queue-Eintrag gelöscht wird (`services/queue.py:227,238`). `printing` ableitbar. `views/printers.py:160` setzt `printing` direkt bei G-Code-Upload ohne Queue. |
| `PrintQueue.status` | 4 + Create in 3 Dateien | Redundant zu `started_at`/`completed_at`. `views/queue.py:577` setzt `awaiting_review` direkt im View. Manuelles Löschen eines druckenden/prüfenden Eintrags lässt die Platte auf `printing` hängen. |
| `Part.estimation_status` | ~17 in 7 Dateien | Echte Arbeitswarteschlange (bleibt). „Reset auf none + Trigger“ 5× dupliziert in Views/API. |
| `printed_quantity` | berechnet | Zählt nur Aufträge, bei denen **alle** Platten fertig sind (`models/parts.py:255-336`). Platte kennt ihre Teile nicht. |
| `PrintJob.printer` | nie geschrieben | Wird aber in der Auftragsliste gefiltert (`views/print_jobs.py:185`). |
| `ScheduledQueueStart.status`, `HardwareShoppingItem.status`, `OrcaProfile.state` | wenige | Bleiben unverändert (Log / Nutzerfakt / Import-Zustand). |

## Zielmodell

### 1. Druckversuch (bisher `PrintQueue`)

Ein Queue-Eintrag ist ein Druckversuch einer Platte auf einem Drucker. Er bleibt nach dem Druck
bestehen. Gespeicherte Fakten:

- `added_at`, `started_at` (Drucker hat Start akzeptiert), `completed_at` (Drucker meldet Ende,
  Moonraker-Ereignis oder Abbruch-Erkennung), Live-Cache (`progress`, `status_updated_at`,
  `last_error`), Start-Lock `start_claimed_at`, Planungsfelder `priority`, `position`.
- **Neu:** `result` (leer / `passed` / `failed` / `cancelled`) und `resolved_at` (wann das Urteil
  gefallen ist).

Abgeleiteter Zustand (Property + Queryset-Filter/Annotation, kein Feld mehr):

| Zustand | Bedingung |
|---|---|
| wartet | `result` leer, `started_at` leer |
| startet | wie „wartet“ + aktiver `start_claimed_at` (existiert heute schon als Pseudo-Status) |
| druckt | `result` leer, `started_at` gesetzt, `completed_at` leer |
| wartet auf Prüfung | `result` leer, `completed_at` gesetzt |
| erledigt | `result` gesetzt |

- **Neu:** `is_retry` (bool) — der Versuch wurde vom System nach einem Fehlversuch angelegt, nicht
  vom Nutzer eingereiht. Gespeicherter Fakt, weil daran der Fehlversuch-Zähler hängt (siehe 2.).
- `printer` wird **nullable** (nur für nachgetragene Altversuche ohne bekannten Drucker) und
  `on_delete=PROTECT` (siehe „Löschschutz“).

`status`, `retry_count` fallen weg. `max_retries` bleibt als Einstellung (Builder entscheidet, ob
pro Versuch oder global — Verhalten muss gleich bleiben: Standard 3).

**Offener Versuch** = `result` leer. Pro Platte höchstens **ein** offener Versuch (ersetzt die
heutige Unique-Constraint „ein wartender Eintrag pro Platte“).

Queue-Ansicht, Drucker-Worker, Farm-Dashboard, Operator-Dialoge (KlipperScreen) sehen nur offene
Versuche. Erledigte Versuche erscheinen als Verlauf pro Platte in der Auftrags-Detailansicht.

**Übergänge** (Modell-Methoden, aufgerufen von Services/Views/API; keine direkten Feld-Writes mehr
außerhalb):

- *Einreihen* → neuer offener Versuch.
- *Start* (`mark_queue_entry_started`) → `started_at`.
- *Drucker meldet Ende* (`printer_status_sync`) → `completed_at`.
- *Prüfung bestanden* → `result=passed`, `resolved_at`.
- *Prüfung fehlgeschlagen* → `result=failed`, `resolved_at`; sind die maximalen Fehlversuche der
  Platte noch nicht erreicht, wird **ein neuer offener Versuch** ganz vorne in seiner
  Prioritätsgruppe angelegt (heute: `position=0`). Sonst nicht (Platte gilt als fehlgeschlagen).
- *Abbrechen* (`CancelQueueEntryView`, heute → `awaiting_review`): Verhalten für den Operator
  bleibt gleich (Druck wird gestoppt, Eintrag landet in der Prüfung).
- *Manuell entfernen* (`PrintQueueDeleteView`):
  - nie gestartet → Versuch wird wirklich gelöscht.
  - gestartet → `result=cancelled`. Zählt **nicht** als Fehlversuch; Platte ist danach wieder frei.

### 2. Platten-Status (berechnet, Feld `PrintJobPlate.status` entfällt)

In dieser Priorität:

1. Es gibt einen bestandenen Versuch → **fertig** (Zeitpunkt = dessen `resolved_at`;
   `PrintJobPlate.completed_at`/`started_at` entfallen bzw. werden daraus abgeleitet).
2. Es gibt einen offenen Versuch → **eingereiht / startet / druckt / wartet auf Prüfung**
   (inkl. Drucker und Queue-Position für die Anzeige).
3. Anzahl `failed`-Versuche ≥ max. Fehlversuche → **fehlgeschlagen**.
4. Sonst → **nicht eingereiht**.

Fehlgeschlagene Platten lassen sich wie heute wieder einreihen (`_queueable_plates` erlaubt heute
waiting/failed). „Max. Fehlversuche“ zählt nur die `failed`-Versuche **seit dem letzten vom Nutzer
eingereihten Versuch** (`is_retry=False`), d. h. manuelles Neu-Einreihen setzt den Zähler zurück.
Ein entfernter (`cancelled`) Versuch zählt nie als Fehlversuch.

`UploadToPrinterView` (direkter G-Code-Upload ohne Queue, `views/printers.py:160`) setzt **keinen**
Plattenstatus mehr.

**Topic 31 — Anzeige:** Der abgeleitete Platten-Status ist überall sichtbar, wo heute der
Plattenstatus steht (Auftrags-Detail, Plattenliste, Dashboard), mit Text wie
„Eingereiht · Drucker X, Position N“ bzw. „Druckt · Drucker X“. Der Knopf „In Queue stellen“ ist
für Platten mit offenem Versuch oder bestandenem Versuch nicht verfügbar (das ist heute schon die
Logik von `_queueable_plates`, sie wird nur auf die Ableitung umgestellt).

### 3. Druckauftrag

`PrintJob.status` behält **nur die Slicing-Pipeline** als gespeicherten Fakt:
`draft` → `pending` → `slicing` → `sliced` | `failed` (Slicing-Fehler). Die Builder darf das Feld
umbenennen (z. B. `slicing_status`), wenn das klarer ist; API/Filter müssen dann mitziehen.

Entfallen: `uploading`, `uploaded`, `cancelled` (nie gesetzt), `printing`, `completed`
(abgeleitet), `PrintJob.started_at`/`completed_at` (abgeleitet aus Versuchen).

Abgeleiteter **Anzeige-Status** des Auftrags (Property + Annotation für Filter/Sortierung):

| Anzeige | Bedingung |
|---|---|
| Entwurf / Wartet / Slicet / Slicing-Fehler | Slicing-Status (solange noch kein Versuch existiert) |
| Bereit | gesliced, noch kein Versuch |
| Druckt | mindestens eine Platte druckt gerade (offener, gestarteter Versuch) |
| In Arbeit | mindestens ein Versuch existiert, aber keine Platte druckt gerade und es sind noch nicht alle Platten fertig/fehlgeschlagen (z. B. Platten noch nicht eingereiht oder nur wartend) |
| Fertig | alle Platten fertig |
| Teilweise fehlgeschlagen | jede Platte ist fertig oder fehlgeschlagen, mindestens eine fehlgeschlagen |

Es gibt **keinen** „Abschließen“-Knopf; der Status ergibt sich nur aus den Platten. „Fertig“ und
„Teilweise fehlgeschlagen“ gelten als abgeschlossen (für „Fertige ausblenden“ und Dashboard).

(Exakte Bezeichnungen/Badges: Builder-Entscheidung, angelehnt an `_job_status_badge.html`.)
Der Filter „Status“ der Auftragsliste (#79, Mehrfachauswahl, „Fertige ausblenden“) filtert auf
diesen abgeleiteten Status per Annotation — ohne N+1. Dashboard-Zählungen
(`views/dashboard.py`) ebenso.

`PrintJob.printer` (nie geschrieben, aber gefiltert): Filter „Drucker“ in der Auftragsliste wird
auf „Aufträge mit einem Versuch auf Drucker X“ umgestellt; das Feld entfällt.

**Bearbeitungsregeln** (eine Methode am Auftrag, z. B. `print_started` = es existiert irgendein
Versuch für eine seiner Platten; Formulare, Buttons und API fragen nur diese):

- **Name, Notiz:** immer bearbeitbar.
- **Teile hinzufügen/entfernen, Preset wechseln:** erlaubt, solange `print_started` falsch ist.
  Heute nur im Entwurf.
- Wird so eine Änderung an einem bereits geslicten (oder fehlgeschlagenen) Auftrag gemacht:
  Platten (G-Code, Vorschaubilder) werden verworfen, der Auftrag fällt auf **Entwurf** zurück.
  Neu slicen löst der Nutzer selbst aus (oft macht man mehrere Änderungen hintereinander).
- **Neu slicen** (`PrintJobSliceView`, löscht heute alle Platten): nur solange `print_started`
  falsch ist. Danach würde es die Druckhistorie löschen. Eine fehlgeschlagene Platte wird einfach
  neu eingereiht, dafür braucht es kein neues Slicing.
- **Löschen:** nur solange `print_started` falsch ist. Ein Auftrag mit Druckversuchen ist
  **nicht löschbar** — die Historie soll nicht verloren gehen. Ein wartender, nie gestarteter
  Versuch muss vorher aus der Queue entfernt werden (dann wird er gelöscht und der Auftrag ist
  wieder löschbar).

### 3a. Löschschutz für Druckhistorie

Die Historie darf auch nicht indirekt verschwinden (heute `CASCADE` über Drucker, Teil, Projekt):

- **Drucker** mit Druckversuchen: nicht löschbar (`PrintQueue.printer` → `PROTECT`). Stattdessen
  neues Feld zum **Deaktivieren** (z. B. `PrinterProfile.is_active`): deaktivierte Drucker
  bekommen keine neuen Versuche, tauchen nicht im Farm-Dashboard / in Drucker-Auswahlen auf, ihre
  Historie bleibt sichtbar.
- **Teil** mit Druckhistorie (es liegt auf einer Platte eines Auftrags mit Versuchen, oder hat
  manuelle Buchungen): nicht löschbar.
- **Projekt**, in dessen Teilbaum ein solches Teil liegt: nicht löschbar.
- Die bestehenden Lösch-Dialoge / Guards (`Project.deletion_summary()`, `blocking_jobs()`,
  Delete-Guards aus #91, REST-API-Deletes) zeigen den Grund an („hat Druckhistorie“) und
  verweigern das Löschen mit klarer Meldung statt 500.
- Umsetzung (DB-`PROTECT` + Guard-Methoden am Modell, oder nur Guards) entscheidet der Builder;
  Hauptsache kein Weg (UI, API, Admin) löscht Historie still.
- Abgrenzung Topic 28: allgemeine Lösch-Sperre für Teile bleibt draußen; hier geht es nur um
  Objekte mit Druckhistorie.

### 4. Platte kennt ihre Teile (Topic 30, Variante b)

**Spike-Ergebnis (2026-10-29, Prod-G-Code gelesen):** OrcaSlicer schreibt pro Platte
`EXCLUDE_OBJECT_DEFINE NAME=Object_<N>_id_<i>_copy_0` und `; printing object Object_<N> …`.
`<N>` ist die 3MF-Objekt-ID, die `create_3mf_bundle` (`core/services/threemf.py`,
`_build_3dmodel_xml`) fortlaufend **pro Kopie** vergibt. Bei Job 13 (34 Kopien) und Job 45
(40 Kopien) liegt jede Kopie auf genau einer Platte. Bei **Job 41** (25 Kopien) liegen nur 23 auf
den Platten — Objekte 12 und 13 fehlen (sehr wahrscheinlich 2× R11, vom Slicer nicht platziert).

Daraus:

- Beim Bauen des Bundles wird die Zuordnung Objekt-ID → Teil **festgehalten** (die
  Teile-Reihenfolge hat heute kein `order_by` — muss deterministisch werden).
- Beim Anlegen der Platten wird der G-Code nach den Objektnamen geparst; pro Platte wird
  gespeichert „Teil X × Anzahl“ (neues Modell, z. B. `PrintJobPlatePart`: Platte, Teil, Anzahl).
- Liegen Kopien auf **keiner** Platte, zeigt der Auftrag eine **Warnung**
  („2× R11 liegen auf keiner Platte“). Kein Fehler, Slicing gilt als erfolgreich.
- **Teile mit mehreren Einzelteilen** (3MF-Datei mit M Meshes → M Objekte pro Kopie): eine Kopie
  zählt erst als gedruckt, wenn **alle** ihre M Objekte auf Platten mit bestandenem Versuch lagen.
  Die Zuordnung speichert daher pro Platte die Objekte (bzw. Mesh-Anteile) so, dass das
  berechenbar ist (Detail: Builder).
- Fallback, wenn keine Objektnamen im G-Code stehen (z. B. Einzelplatte ohne Labels, anderer
  Slicer-Stand): Builder-Entscheidung; vernünftig ist „bei genau einer Platte alle Teile dieser
  Platte zuordnen, sonst Warnung ‚Zuordnung unbekannt‘“.

### 5. Gedruckte Stückzahl (Topic 30)

`Part.printed_quantity` = Σ Anzahl auf Platten mit **bestandenem Versuch** + Σ **manuelle
Buchungen**. Keine gespeicherte Zahl. Der Projektfortschritt deckelt wie heute pro Teil beim Bedarf
(`Project.build_progress`). Mehr als der Bedarf darf gebucht werden (Ersatzteile).

**Manuelle Buchung (Variante a):** neues Modell (z. B. `PrintedPartRecord`): Teil, Anzahl (> 0),
Datum, Notiz, erstellt von. Liste pro Teil, einzelne Einträge löschbar (Korrektur durch Löschen +
neu Anlegen). Wer darf: `ProjectManageMixin` (Admin, Designer). UI am Teil (Detailseite) und
REST-API-Endpunkt analog zu den bestehenden Part-Aktionen.

Doppelzählung: Plattenzählung und manuelle Buchungen sind getrennte Quellen; doppelt zählt nur,
wer bewusst etwas nachbucht, das LayerNexus schon gezählt hat. Die Buchungsliste zeigt deshalb
neben den manuellen Einträgen auch die automatisch gezählten Platten (read-only), damit man sieht,
was schon drin ist.

### 6. Teile-Schätzung

`Part.estimation_status` bleibt gespeichert (Arbeitswarteschlange + Slicer-Ergebnis). Die fünf
duplizierten „zurücksetzen + neu schätzen“-Stellen (`views/parts.py:252,345`,
`views/projects.py:374`, `api/views.py:202,450`, plus Helfer in `views/helpers.py`) werden zu
**einer** Methode am Teil (z. B. `Part.request_estimate()`); die Worker-Übergänge
(`slicing_worker`) ebenfalls als Modell-Methoden. Sonst keine Verhaltensänderung.

## Migration bestehender Daten

Alles per `makemigrations` + Datenmigration (RunPython), nie handeditiert.

1. **Versuche nachtragen:** Für jede Platte mit Status `completed` einen Versuch
   `result=passed` (Zeitpunkte aus `plate.started_at`/`completed_at`, Drucker: aus
   `PrintJob.printer` falls gesetzt, sonst leer — dafür wird `printer` nullable). Für `failed` analog `result=failed` (so viele, dass die Platte als
   fehlgeschlagen gilt). Offene Queue-Einträge bleiben offene Versuche, ihr bisheriger `status`
   wird in Zeitstempel übersetzt (`printing` ohne `started_at` → `started_at` setzen usw.).
2. **Platten-Teile nachtragen:** Vorhandene G-Code-Dateien werden geparst (Objekt-ID → Teil unter
   der Annahme, dass die Bundle-Reihenfolge bisher der `PrintJobPart`-pk-Reihenfolge entsprach).
   Wo das nicht eindeutig klappt (Datei fehlt, Objektanzahl passt nicht zur Kopienzahl ×
   Meshes, Teildatei inzwischen ersetzt), gilt für diesen Auftrag die **bisherige Regel**
   (alle Platten fertig → alle Teile zählen) — **Stückzahlen dürfen nicht sinken** gegenüber heute.
3. Danach Felder entfernen (`PrintJobPlate.status`, `PrintQueue.status`, `retry_count`,
   Auftrags-Druckstatus, `PrintJob.printer`, …).
4. Altfälle (Job 33 → R02, R05–R09) korrigiert der Nutzer danach per manueller Buchung.

Prod-Deploy **nach jedem gemergten PR** (Freigabe User 2026-10-30), immer mit DB-Backup. Läuft
gerade ein Druck unter 95 % Fortschritt, wird sofort deployt; ab 95 % wird das Druckende
abgewartet. Nach dem Deploy werden Stückzahlen/Projektfortschritt gegen den Stand vorher geprüft.

## Reihenfolge (je ein PR, einzeln mergebar)

1. **Druckversuch-Historie:** Queue-Einträge bleiben, `result`/`resolved_at`/`is_retry`,
   Queue-Status abgeleitet, Retry = neuer Versuch, Abbrechen/Entfernen-Regeln, Datenmigration
   Versuche, Drucker deaktivieren + Löschschutz Drucker.
   Plattenstatus wird in diesem PR noch zentral über eine Methode mitgeschrieben.
2. **Platten- und Auftragsstatus ableiten:** `PrintJobPlate.status` + Auftrags-Druckstatus
   entfernen, Anzeige-Status + Annotationen (Filter/Dashboard), Topic-31-Anzeige,
   Bearbeitungs-, Neu-Slicen- und Löschregeln, Löschschutz Teil/Projekt, tote Status raus,
   `PrintJob.printer` raus.
3. **Teile-Schätzung bündeln** (unabhängig, kann parallel zu 1/2 laufen).
4. **Platte kennt ihre Teile:** deterministische Bundle-Reihenfolge, G-Code-Parsing,
   `PrintJobPlatePart`, Warnung „auf keiner Platte“, Backfill aus vorhandenen G-Code-Dateien.
5. **Gedruckte Stückzahl neu + manuelle Buchung:** `printed_quantity` aus bestandenen Platten +
   Buchungen, Buchungs-UI + API.

Je PR: TDD, grüne CI (inkl. `makemigrations --check`, Coverage ≥ 55), Review-Loop bis 0 Findings.

## Akzeptanzkriterien

- Kein Code außerhalb der Modell-Methoden/Services setzt mehr Status-Felder der Druckseite.
- Eingereihte Platten sind in Auftrags-Detail, Plattenliste und Dashboard erkennbar; die Anzeige
  stimmt nach Einreihen, Umsortieren, Entfernen, Start, Ende, Bestanden, Fehlgeschlagen,
  Abbrechen — mit Tests.
- Druckhistorie: nach Bestanden/Fehlgeschlagen/Abbrechen bleibt der Versuch erhalten und ist im
  Auftrag sichtbar.
- Teilfertige Aufträge zählen ihre bestandenen Platten; manuelle Buchungen zählen; Tests für
  beide Quellen und die Summe.
- Bestehende Filter/Sortierungen (#79, Dashboard, Queue-Liste) funktionieren; keine N+1
  (Query-Count-Tests wie bestehend).
- Nach der Migration sind keine Stückzahlen gesunken und kein Projektfortschritt gefallen.

## Den Builder überlassen

- Genaue Feld- und Modellnamen, ob `PrintQueue` umbenannt wird (Empfehlung: Name behalten, um
  Churn zu sparen), Badge-Texte.
- Umsetzung der Ableitungen (Properties vs. Manager-Methoden vs. `annotate`), solange Listen ohne
  N+1 bleiben.
- Aufteilung innerhalb der PRs, Testschnitt.
