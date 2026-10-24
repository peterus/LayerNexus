# Hardware-Einkaufsliste je Projekt (Bestellhilfe)

Stand: 2026-10-24 · PM-Topic 18

## Intent / Warum

Beim Bestellen von Hardware (Schrauben, Muttern, Lager, Magnete …) verliert der User den
Überblick. Er sammelt erst alles in Shop-Warenkörben und bestellt dann gesammelt. Gewünscht ist
eine **reine Bestellhilfe**: auf einen Blick sehen, wie viele Stück je Teil gebraucht werden, wo
man es kauft und was schon im Warenkorb liegt bzw. bestellt ist — ohne Pflegeaufwand, keine
Warenwirtschaft.

## Ziele

1. Eigene Seite **„Shopping List“** je Projekt (jedes Projekt, auch Unterprojekte). Der Bedarf
   wird rekursiv über alle Unterprojekte × Kantenmenge summiert — eine Zeile pro `HardwarePart`.
   Grundlage ist das bestehende `Project.hardware_requirements()` (nutzt
   `_collect_hardware_with_multiplier()`); keine neue Aggregationslogik.
2. Je Zeile ein **Status mit drei Stufen: offen → im Warenkorb → bestellt**, gespeichert je
   (angesehenes Projekt × HardwarePart). Jede Liste hat ihren eigenen Zustand; Häkchen in der
   Liste von Projekt A beeinflussen die Liste eines Unterprojekts nicht und umgekehrt.
3. **Automatischer Reset pro Zeile:** Ändert sich der aggregierte Bedarf eines Teils, gilt nur
   diese Zeile wieder als „offen“; alle anderen bleiben.
4. Bulk-Aktionen: **„Warenkorb als bestellt markieren“** und **„Alle zurücksetzen“**, beide mit
   Bestätigung.
5. Projektansicht: die bisherige Tabelle „Aggregated Hardware (incl. Sub-Projects)“ wird durch
   eine kompakte Zusammenfassungszeile mit Link zur Einkaufsliste ersetzt.

## Nicht-Ziele (ausdrücklich vom User)

- **kein Preis** / keine Kostensumme auf der Einkaufsliste
- **kein CSV-Export**
- **kein Lagerbestand**
- **kein Bestellmanagement** (keine Bestellungen, Lieferstatus, Lieferanten, Teillieferungen)
- **kein API-Endpoint** (bewusst weggelassen, schlank halten)

Anti-Ziel: Irgendetwas davon „nebenbei“ einbauen. Die Liste soll Bestellhilfe bleiben.

## Entscheidungen (mit dem User abgestimmt)

| Thema | Entscheidung |
|---|---|
| Status-Modell | Ein Status, drei Stufen (offen / im Warenkorb / bestellt) — User sammelt erst, bestellt dann gesammelt. |
| Ort | Eigene Seite, verlinkt aus Projektansicht und Kostenseite. |
| Gültigkeit | Jedes Projekt hat eine Liste; Zustand je angesehenem Projekt, unabhängig von Unter-/Oberprojekten. |
| Reset | Nur die Zeile, deren aggregierte Stückzahl sich geändert hat, fällt auf „offen“ zurück. |
| Alter Aggregat-Block | „Aggregated Hardware (incl. Sub-Projects)“ in der Projektansicht entfällt, ersetzt durch Zusammenfassung + Link. Tabelle „Hardware“ (eigene Zuweisungen, Bearbeiten/Löschen) bleibt unverändert. Kosten bleiben auf der Kostenseite; die Herkunft („Used in“) steht jetzt sichtbar auf der Einkaufsliste. |
| RBAC | Ansehen: `LoginRequiredMixin`. Status setzen / Bulk-Aktionen: `ProjectManageMixin` (403 sonst); Bedienelemente nur bei `perms.core.can_manage_projects` sichtbar. |

## Verhalten im Detail

### Seite „Shopping List“ (`projects/<pk>/shopping-list/`)

- **Kopf:** Projektname, Zurück-Link zum Projekt, Zusammenfassung
  „N open · M in cart · K ordered“.
- **Bulk-Knöpfe** (nur mit Recht): „Mark cart as ordered“ (nur aktiv, wenn M > 0) und
  „Reset all“. Bestätigung jeweils über ein Bootstrap-Modal (`data-bs-toggle`, kein eigenes JS).
- **Tabelle gruppiert nach Kategorie** in der Reihenfolge von `HardwarePart.CATEGORY_CHOICES`
  (Screws, Nuts, Bolts, …), innerhalb alphabetisch nach Name. *(Builder-Call des Koordinators;
  `hardware_requirements()` sortiert nach dem Roh-Key — das darf für Kostenseite/API so bleiben.)*
- **Spalten:** Teil (Name), benötigte Stückzahl, Shop-Link, Status.
  - **Shop-Link:** Knopf/Icon, `target="_blank" rel="noopener noreferrer"`. Ohne `url`: gelbes
    Badge „no link“ (Hinweis, dass im Katalog eine URL fehlt).
  - **Herkunft:** sichtbare, gedämpfte Zeile unter dem Teilnamen
    (`<div class="small text-body-secondary">Used in: A, B</div>`), die die beitragenden Projekte
    sortiert auflistet (`hardware_requirements()[…]["projects"]`). Kein Tooltip (auf Touch-Geräten
    nicht erreichbar) — die Herkunft ersetzt die „Used in“-Badges des entfallenen Aggregat-Blocks.
  - **Status:** Badge (offen / im Warenkorb / bestellt) plus — nur mit Recht — ein Knopf, der
    eine Stufe weiterschaltet: offen → im Warenkorb → bestellt → offen.
  - **Bestellte Zeilen** werden ausgegraut (z. B. `text-body-secondary`/`opacity`-Utility-Klasse),
    bleiben aber an ihrer Position. Keine Umsortierung nach Status.
  - **Bedarf geändert:** Ist eine Zeile wegen geänderter Stückzahl auf „offen“ zurückgefallen,
    zeigt sie den Hinweis „Quantity changed (was 8)“. Der Hinweis verschwindet, sobald die Zeile
    neu gesetzt oder zurückgesetzt wird.
- **Leerzustand:** Hat das Projekt (inkl. Unterprojekte) keine Hardware: Hinweis + Link zum
  Projekt.
- Kein Preis, keine Kosten, kein Export auf dieser Seite.
- **Technik:** klassische POST-Formulare (CSRF), nach jeder Aktion Redirect (PRG) zurück auf die
  Liste mit Anker auf die Zeile (`#hw-<hardware_part_pk>`), damit man an derselben Stelle bleibt.
  Kein AJAX, kein Inline-CSS/JS.

### Persistenz und Reset-Logik

Neues Modell in `core/models/hardware.py`, Migration via `makemigrations`, re-export in
`core/models/__init__.py`, Admin-Registrierung:

```
HardwareShoppingItem
  project        FK Project, on_delete=CASCADE, related_name="hardware_shopping_items"
  hardware_part  FK HardwarePart, on_delete=CASCADE, related_name="shopping_items"
  status         CharField choices: "in_cart" | "ordered"
  quantity       PositiveIntegerField  # aggregierte Stückzahl zum Zeitpunkt des Setzens
  updated_at     DateTimeField(auto_now=True)
  Meta: unique (project, hardware_part); ordering; __str__
```

- **„Offen“ wird nicht gespeichert:** kein Datensatz = offen. Weiterschalten auf „offen“ löscht
  den Datensatz.
- **Effektiver Status** beim Anzeigen: Datensatz vorhanden **und** `quantity` == aktuelle
  aggregierte Stückzahl → gespeicherter Status. Datensatz vorhanden, Stückzahl weicht ab →
  effektiv **offen**, mit Hinweis „was `quantity`“. Kein Datensatz → offen.
  Begründung: robuster als Signals auf allen Änderungswegen (Menge, Kantenmenge, Teil/Unterprojekt
  hinzufügen oder entfernen).
- **Weiterschalten** setzt den Ziel-Status explizit (Formular sendet `status=<ziel>`, nicht
  „advance“) — Doppelklick bzw. erneutes Absenden ist damit idempotent. Beim Setzen wird
  `quantity` auf die **aktuelle** aggregierte Stückzahl geschrieben. Das Weiterschalten geht
  immer vom effektiven Status aus (eine „geänderte“ Zeile ist offen → nächster Schritt
  „im Warenkorb“).
- Ein Teil, das nicht (mehr) im aggregierten Bedarf des Projekts vorkommt, kann nicht gesetzt
  werden (404).
- **Veraltete Seite:** Das Formular sendet zusätzlich die angezeigte Stückzahl (`quantity`). Weicht
  sie von der aktuellen aggregierten Stückzahl ab, wird nichts gespeichert; Redirect auf die Zeile
  mit Warnhinweis (verhindert, dass eine nie gesehene Menge als „bestellt“ markiert wird).
- **Verwaiste Datensätze** (Teil nicht mehr im Bedarf) werden ignoriert und bei jeder
  Schreibaktion auf dieser Liste entfernt. Bekannte Grenze (akzeptiert): Wird ein Teil entfernt
  und später mit *exakt derselben* Stückzahl wieder hinzugefügt, ohne dass dazwischen eine
  Schreibaktion auf der Liste stattfand, erscheint es wieder mit dem alten Status.
- **„Mark cart as ordered“:** alle Zeilen mit effektivem Status „im Warenkorb“ → „bestellt“
  (`quantity` bleibt die aktuelle). Geänderte Zeilen werden nicht angefasst.
- **„Reset all“:** löscht alle `HardwareShoppingItem` dieses Projekts.

Geschäftslogik (Liste mit effektivem Status, Zählwerte, Setzen/Bulk) gehört ins Modell bzw. auf
`Project`/Manager, nicht in die Views (CLAUDE.md: „business logic in models“). Die Views bleiben
dünn.

### Projektansicht

- Block „Aggregated Hardware (incl. Sub-Projects)“ entfernen.
- Stattdessen über/im Hardware-Abschnitt eine kompakte Zeile, sobald das Projekt (inkl.
  Unterprojekte) Hardware hat: Warenkorb-Icon, „Shopping list — N open · M in cart · K ordered“,
  Knopf zur Einkaufsliste. Sichtbar für alle eingeloggten Nutzer.
- Der Leerzustand „No hardware items yet.“ bleibt sinngemäß erhalten.

### Kostenseite

- Zusätzlicher Link/Knopf „Shopping list“ zum gleichen Ziel. Sonst unverändert.

## Constraints

- Django-Konventionen aus `CLAUDE.md`: Class-Based Views, Rollen-Mixin auf jeder Schreib-View,
  doppelte Anführungszeichen, f-Strings, Type Hints, Google-Docstrings, Bootstrap 5.3 (hell/dunkel),
  kein Inline-CSS/JS, `select_related`/`prefetch_related` gegen N+1.
- UI-Texte auf Englisch wie der Rest der App.
- Migration nur via `makemigrations`.
- CI-Gate: ruff, `makemigrations --check`, `check --fail-level WARNING`, Tests, Coverage ≥ 55.

## Akzeptanzkriterien (je per Test abgedeckt, wo angegeben)

1. Projekt mit Unterprojekten (inkl. Kantenmengen > 1 und einem Teil, das in mehreren
   Unterprojekten vorkommt) → korrekt summierte Stückzahlen. *(Test)*
2. Status setzen bleibt nach Reload erhalten; Weiterschalten offen → Warenkorb → bestellt →
   offen funktioniert; erneutes Absenden desselben Ziel-Status ist idempotent. *(Test)*
3. Bedarf eines Teils ändert sich (z. B. Kantenmenge oder `ProjectHardware.quantity`) → nur diese
   Zeile ist effektiv offen und zeigt „was N“, andere Zeilen behalten ihren Status. *(Test)*
4. „Mark cart as ordered“ setzt genau die effektiven Warenkorb-Zeilen auf bestellt. *(Test)*
5. „Reset all“ leert alle Datensätze des Projekts, aber nicht die anderer Projekte. *(Test)*
6. Zustand ist je Projekt unabhängig (Setzen in Oberprojekt ändert Unterprojekt-Liste nicht). *(Test)*
7. RBAC: Nutzer ohne `can_manage_projects` bekommen 403 auf alle Schreib-Endpunkte; Knöpfe
   werden für sie nicht gerendert; anonyme Nutzer werden zum Login umgeleitet. *(Test)*
8. Setzen eines Teils, das nicht im Bedarf ist → 404. *(Test)*
9. Projektansicht zeigt die Zusammenfassung + Link, der alte Aggregat-Block ist weg. *(Test)*
10. Keine Preise/Kosten auf der Einkaufsliste. *(Test: kein „€“/Preis im Response)*
11. CI grün, Review-Loop bis 0 Findings, Merge.

## Dem Builder überlassen

- Genaue URL-Namen und View-Klassennamen (Vorschlag: `project_shopping_list`,
  `project_shopping_item_set`, `project_shopping_cart_ordered`, `project_shopping_reset`).
- Ob die Logik als Methoden auf `Project` oder als Manager/Service-Funktion im Modellmodul lebt.
- Template-Details (Badge-Farben, Icons aus Bootstrap Icons) im Stil der bestehenden Seiten.
- N+1 in `hardware_requirements()` (`hw.project.name` ohne `select_related("project")`) darf
  mitbehoben werden, wenn es ohnehin angefasst wird — die Dict-Keys (inkl. `total_price`) müssen
  gleich bleiben, weil Kostenseite und API (`core/api/views.py`) sie nutzen.
- Ob die Bestätigungs-Modals einen gemeinsamen Partial nutzen.
