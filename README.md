# Statuspage Monitor

Ein kleines, abhängigkeitsfreies Monitoring für Atlassian Statuspage.
GitHub Actions prüft alle 5 Minuten einen oder mehrere HTTP-Health-Endpoints und
spiegelt das Ergebnis in die zugehörigen Statuspage-Components.

Standard-Endpoint: `https://api.core-fit.app/up`

* Kein Server, keine Datenbank, kein externer Dienst
* Nur Python-Standardbibliothek (kein `pip install`)
* Alle Zugangsdaten kommen aus GitHub Secrets
* Beliebig viele Endpoints über eine einzige Konfigurationsdatei

---

## Inhalt

| Datei | Zweck |
| --- | --- |
| `.github/workflows/monitor.yml` | Zeitplan (alle 5 Min.) + manueller Start, führt den Check aus |
| `.github/workflows/validate.yml` | Prüft Konfiguration und Code bei jedem Push/PR |
| `monitors.json` | Zentrale Konfiguration: URLs, Timeouts, Schwellwerte, Component-IDs |
| `monitor.py` | Healthcheck + Statuspage-API-Client (~450 Zeilen, stdlib-only) |
| `selftest.py` | Offline-Tests der Schwellwert-Logik und Konfigurationsprüfung |

---

## Funktionsweise

Pro Lauf und pro Monitor:

1. **Aktuellen Component-Status lesen**
   `GET https://api.statuspage.io/v1/pages/{page_id}/components/{component_id}`
2. **Healthcheck ausführen** – HTTP-Request auf die konfigurierte URL mit Timeout
   (Standard 10 Sekunden). Erfolgreich ist nur ein erwarteter Statuscode
   (Standard: genau `200`).
3. **Wiederholen, bis ein Ergebnis feststeht** (siehe [Verhalten bei UP/DOWN](#verhalten-bei-updown)):
   * `failure_threshold` aufeinanderfolgende Fehlversuche → **DOWN**
   * die nötige Anzahl aufeinanderfolgender Erfolge → **UP**
4. **Statuspage aktualisieren** – nur wenn sich der Status tatsächlich ändert:
   ```
   PATCH https://api.statuspage.io/v1/pages/{page_id}/components/{component_id}
   Authorization: OAuth <STATUSPAGE_API_KEY>
   Content-Type: application/json

   {"component": {"status": "operational"}}
   ```

Weil der Ist-Zustand vor jedem Lauf von Statuspage gelesen wird, braucht das
Projekt **keinen eigenen Zustandsspeicher** – Statuspage *ist* der Zustand.

### Genutzte Atlassian-API (Stand: aktuelle Statuspage API v1)

| Zweck | Methode & Pfad |
| --- | --- |
| Component lesen | `GET https://api.statuspage.io/v1/pages/{page_id}/components/{component_id}` |
| Component-Status setzen | `PATCH https://api.statuspage.io/v1/pages/{page_id}/components/{component_id}` |
| Alle Components auflisten (zum Ermitteln der IDs) | `GET https://api.statuspage.io/v1/pages/{page_id}/components` |

* Authentifizierung: Header `Authorization: OAuth <API_KEY>` (das Wort `OAuth` gehört wörtlich dazu)
* Payload beim PATCH: `{"component": {"status": "<wert>"}}`
* Gültige Statuswerte: `operational`, `under_maintenance`, `degraded_performance`,
  `partial_outage`, `major_outage`
* Referenz: <https://developer.statuspage.io/> (Components → Update a component)

`monitor.py` sendet ausschliesslich Werte aus dieser Liste; ein unbekannter Wert in
der Konfiguration führt zu einem Fehler statt zu einem falschen API-Request.

---

## Benötigte GitHub Secrets

Anlegen unter **Repository → Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Beschreibung |
| --- | --- |
| `STATUSPAGE_API_KEY` | API-Key des Statuspage-Accounts |
| `STATUSPAGE_PAGE_ID` | ID der Statuspage |
| `STATUSPAGE_COMPONENT_ID` | ID des Components, der überwacht wird |

Für weitere Components kommt je ein zusätzliches Secret dazu, z. B.
`STATUSPAGE_COMPONENT_ID_BILLING` (siehe
[Weitere Endpoints hinzufügen](#weitere-endpoints-hinzufügen)).

Es liegen **keine** Schlüssel, IDs oder Tokens im Repository.

---

## Atlassian Statuspage einrichten

### 1. API-Key erzeugen

1. Auf <https://manage.statuspage.io> einloggen.
2. Oben rechts auf den Account-Namen → **API info** (bzw. **Your account → API info**).
3. Unter **API keys** einen Key erzeugen und kopieren.
   Der Key wird nur einmal vollständig angezeigt.
4. Als GitHub Secret `STATUSPAGE_API_KEY` speichern.

> Der Key hat die Rechte des Benutzers, dem er gehört. Für reines Monitoring
> genügt ein Benutzer, der die betreffende Page bearbeiten darf.

### 2. `PAGE_ID` finden

Drei Wege, alle liefern denselben Wert:

* **Aus der URL:** In <https://manage.statuspage.io> die Page öffnen. Die Adresse
  lautet `https://manage.statuspage.io/pages/<PAGE_ID>/...` – der Teil hinter
  `/pages/` ist die Page-ID (z. B. `t7lm3xn8kz9q`).
* **Im Admin:** **Your account → API info** zeigt die Page-ID direkt an.
* **Per API:**
  ```bash
  curl -s https://api.statuspage.io/v1/pages \
    -H "Authorization: OAuth $STATUSPAGE_API_KEY"
  ```
  Im JSON steht `"id"` neben `"name"` der Page.

Als Secret `STATUSPAGE_PAGE_ID` speichern.

### 3. Component anlegen und `COMPONENT_ID` finden

1. Im Statuspage-Admin **Components → Add a component**, z. B. Name „CoreFit API“.
2. Die Component-ID ermitteln:

   * **Per API (empfohlen):**
     ```bash
     curl -s "https://api.statuspage.io/v1/pages/$STATUSPAGE_PAGE_ID/components" \
       -H "Authorization: OAuth $STATUSPAGE_API_KEY" \
       | python3 -c 'import json,sys; [print(c["id"], "-", c["name"]) for c in json.load(sys.stdin)]'
     ```
     Ausgabe z. B. `9k2f4b7c1d3e - CoreFit API`.
   * **Mit dem mitgelieferten Helfer:** `STATUSPAGE_API_KEY=... python3 monitor.py --list-components`
     listet alle Pages samt Component-IDs.
   * **Aus der Admin-URL:** Component öffnen; die URL enthält
     `.../components/<COMPONENT_ID>`.
   * **Öffentlich:** `https://<deine-statuspage-domain>/api/v2/components.json`
     listet Namen und IDs aller sichtbaren Components.

3. Als Secret `STATUSPAGE_COMPONENT_ID` speichern.

> Component-IDs sind keine Geheimnisse (sie stehen auf der öffentlichen Statuspage).
> Sie werden hier trotzdem als Secret geführt, damit die Konfiguration im Repository
> vollständig neutral bleibt. Wer möchte, kann die ID stattdessen direkt in
> `monitors.json` eintragen.

---

## GitHub Actions Workflow einrichten

1. Repository mit diesen Dateien anlegen bzw. klonen.
2. Die drei Secrets oben hinterlegen.
3. Sicherstellen, dass **Actions aktiviert** ist:
   **Settings → Actions → General → Allow all actions and reusable workflows**.
4. `monitor.yml` muss auf dem **Default-Branch** (`main`) liegen –
   GitHub führt `schedule`-Trigger nur von dort aus.
5. Fertig. Der erste geplante Lauf startet innerhalb weniger Minuten; sofort testen
   lässt sich der Workflow über **Actions → API Monitor → Run workflow**.

### Berechtigungen

| Ebene | Benötigt |
| --- | --- |
| GitHub-Workflow | `permissions: contents: read` – mehr braucht der Job nicht (bereits gesetzt) |
| GitHub-Benutzer | Rechte zum Anlegen von Repository-Secrets (Admin/Maintainer) |
| Statuspage-Benutzer | Darf die betreffende Page und ihre Components bearbeiten |
| Netzwerk | Ausgehend HTTPS auf die überwachte URL und auf `api.statuspage.io` |

Der Workflow benötigt **keinen** Schreibzugriff auf das Repository und pusht nichts.

---

## Konfiguration

Alles Einstellbare steht in `monitors.json` – nichts davon ist im Code verteilt.
`defaults` gilt für alle Monitore, jeder Monitor kann jeden Wert überschreiben.

```json
{
  "defaults": {
    "method": "GET",
    "timeout_seconds": 10,
    "expected_status": [200],
    "failure_threshold": 3,
    "success_threshold": 2,
    "retry_delay_seconds": 15,
    "up_status": "operational",
    "down_status": "major_outage",
    "flapping_status": null,
    "page_id": "${STATUSPAGE_PAGE_ID}"
  },
  "monitors": [
    {
      "name": "CoreFit API",
      "url": "https://api.core-fit.app/up",
      "component_id": "${STATUSPAGE_COMPONENT_ID}"
    }
  ]
}
```

| Feld | Standard | Bedeutung |
| --- | --- | --- |
| `name` | – | Eindeutiger Anzeigename im Log (Pflichtfeld in der Praxis) |
| `url` | – | Zu prüfende URL. Muss `https://` sein |
| `enabled` | `true` | Auf `false` setzen, um einen Monitor vorübergehend zu pausieren |
| `method` | `GET` | `GET`, `HEAD` oder `OPTIONS` |
| `timeout_seconds` | `10` | Timeout pro Versuch |
| `expected_status` | `[200]` | Erlaubte HTTP-Statuscodes. Alles andere gilt als Fehler |
| `expected_body_contains` | `null` | Optional: Zeichenkette, die im Response-Body vorkommen muss |
| `headers` | `{}` | Optionale Request-Header, z. B. für geschützte Health-Endpoints |
| `failure_threshold` | `3` | Aufeinanderfolgende Fehlversuche bis `down_status` |
| `success_threshold` | `2` | Aufeinanderfolgende Erfolge für die Rückkehr zu `up_status` |
| `retry_delay_seconds` | `15` | Pause zwischen den Versuchen |
| `up_status` | `operational` | Statuspage-Status bei UP |
| `down_status` | `major_outage` | Statuspage-Status bei DOWN |
| `flapping_status` | `null` | Status bei unentschiedenem Ergebnis; `null` = Component unverändert lassen |
| `page_id` | `${STATUSPAGE_PAGE_ID}` | Ziel-Page |
| `component_id` | `${STATUSPAGE_COMPONENT_ID}` | Ziel-Component |
| `allow_http` | `false` | Nur setzen, wenn bewusst unverschlüsseltes `http://` geprüft werden soll |

**`${NAME}`-Platzhalter** werden zur Laufzeit aus Umgebungsvariablen ersetzt, die
im Workflow aus GitHub Secrets kommen. Sie funktionieren in `url`, `headers`,
`page_id` und `component_id`. Im Log erscheint immer die Schreibweise mit
Platzhalter, nie der aufgelöste Wert.

Zusätzliche Umgebungsvariablen im Workflow:

| Variable | Standard | Wirkung |
| --- | --- | --- |
| `FAIL_JOB_ON_DOWN` | `false` | Auf `true` setzen, damit der Actions-Lauf bei DOWN rot wird (löst GitHubs Fehler-Benachrichtigung aus) |
| `DRY_RUN` | `false` | Checks ausführen, Statuspage nicht anfassen |
| `MONITOR_ONLY` | leer | Kommaliste von Monitor-Namen, die allein laufen sollen |

---

## Weitere Endpoints hinzufügen

Ein Monitor = ein Statuspage-Component. Beliebig viele davon, **ein einziger
Eintrag pro Endpoint** – Code und Workflow müssen nie angefasst werden.

**Schritt 1:** Component in Statuspage anlegen (**Components → Add a component**).

**Schritt 2:** Component-ID nachschlagen:

```bash
export STATUSPAGE_API_KEY=...
python3 monitor.py --list-components
```

```
page_id      t7lm3xn8kz9q   CoreFit Status
  component  9k2f4b7c1d3e   CoreFit API   [operational]
  component  aa11bb22cc33   Billing API   [major_outage]
  group      gg99hh88ii77   Backend       [operational]
```

**Schritt 3:** Monitor in `monitors.json` ergänzen – die ID direkt eintragen:

```json
{
  "monitors": [
    {
      "name": "CoreFit API",
      "url": "https://api.core-fit.app/up",
      "component_id": "${STATUSPAGE_COMPONENT_ID}"
    },
    {
      "name": "Billing API",
      "url": "https://billing.core-fit.app/health",
      "component_id": "aa11bb22cc33",
      "timeout_seconds": 5,
      "expected_status": [200, 204]
    },
    {
      "name": "Marketing-Site",
      "url": "https://core-fit.app/",
      "component_id": "d4e5f6a7b8c9",
      "method": "HEAD",
      "down_status": "partial_outage"
    }
  ]
}
```

Committen, fertig. Der nächste geplante Lauf prüft alle Monitore und aktualisiert
jeden Component einzeln.

> **Warum steht beim ersten Monitor ein Platzhalter?**
> Nur aus Gewohnheit der Erstkonfiguration – `${STATUSPAGE_COMPONENT_ID}` liest die
> ID aus dem gleichnamigen GitHub Secret. Component-IDs sind **keine Geheimnisse**
> (sie stehen auf der öffentlichen Statuspage unter `/api/v2/components.json`),
> deshalb dürfen alle weiteren IDs unverschlüsselt in `monitors.json` stehen.
> Wer lieber alles über Secrets führt, legt pro Component ein Secret an und reicht
> es im `env:`-Block von `.github/workflows/monitor.yml` durch – dort stehen
> auskommentierte Beispielzeilen.

### Geschützte Health-Endpoints

Braucht ein Endpoint einen Token, gehört **dieser** in ein GitHub Secret:

```yaml
# .github/workflows/monitor.yml, im env:-Block
          HEALTHCHECK_TOKEN: ${{ secrets.HEALTHCHECK_TOKEN }}
```

```json
{
  "name": "Admin API",
  "url": "https://admin.core-fit.app/health",
  "component_id": "b3c4d5e6f7a8",
  "headers": { "Authorization": "Bearer ${HEALTHCHECK_TOKEN}" },
  "expected_body_contains": "\"status\":\"ok\""
}
```

### Weitere Möglichkeiten

| Ziel | Konfiguration |
| --- | --- |
| Monitor pausieren (z. B. Wartungsfenster) | `"enabled": false` |
| Zweite Statuspage bedienen | `"page_id": "${STATUSPAGE_PAGE_ID_INTERNAL}"` pro Monitor |
| Weniger empfindlich reagieren | `"failure_threshold": 5` |
| Teilausfall statt Totalausfall melden | `"down_status": "partial_outage"` |
| Bei wechselhaftem Verhalten gelb schalten | `"flapping_status": "degraded_performance"` |
| Nur einen Monitor testen | Workflow-Input `only` = Monitor-Name |

Die Monitore laufen nacheinander im selben Job; Requests an Statuspage werden auf
ca. 1 Request/Sekunde gedrosselt, weil Atlassian den API-Zugriff in dieser
Grössenordnung begrenzt. Bei sehr vielen Endpoints (Faustregel: > ~20) die
Timeouts und Retry-Pausen senken oder mit `MONITOR_ONLY` auf zwei Workflows
aufteilen, damit ein Lauf deutlich unter 5 Minuten bleibt.

---

## Test des Workflows

**Ohne Statuspage-Änderung (empfohlen für den ersten Test):**
Actions → **API Monitor** → **Run workflow** → `dry_run` auf `true`.
Der Healthcheck läuft, es wird kein `PATCH` gesendet.

**Echter manueller Lauf:** dasselbe mit `dry_run = false`.
Optional nur einen Monitor prüfen: Feld `only` = `CoreFit API`.

**Lokal (ohne Secrets):**

```bash
python3 monitor.py --list-components    # alle Page- und Component-IDs auflisten
python3 monitor.py --validate           # Konfiguration prüfen
python3 monitor.py --dry-run            # Checks ausführen, Statuspage nicht anfassen
python3 selftest.py                     # Logik-Tests (kein Netzwerk nötig)
```

**Lokal mit echten Zugangsdaten:**

```bash
export STATUSPAGE_API_KEY=...
export STATUSPAGE_PAGE_ID=...
export STATUSPAGE_COMPONENT_ID=...
python3 monitor.py --only "CoreFit API"
```

**Ausfall simulieren:** in `monitors.json` temporär
`"url": "https://api.core-fit.app/gibt-es-nicht"` setzen oder
`"expected_status": [999]` – der Lauf muss nach `failure_threshold` Versuchen
`major_outage` setzen. Danach zurückändern; der nächste Lauf stellt nach zwei
erfolgreichen Checks wieder `operational` her.

Jeder Lauf schreibt zusätzlich eine Ergebnistabelle in die **Job Summary** der
Actions-Oberfläche.

---

## Verhalten bei UP/DOWN

| Situation | Nötige Checks | Ergebnis | Statuspage-Aktion |
| --- | --- | --- | --- |
| Component `operational`, Check OK | 1 Erfolg | UP | keine (Status schon korrekt) |
| Component `operational`, einzelner Fehler, danach OK | – | UP | keine |
| Component `operational`, 3 Fehler in Folge | 3 Fehler | DOWN | `PATCH` → `major_outage` |
| Component `major_outage`, 2 Erfolge in Folge | 2 Erfolge | UP | `PATCH` → `operational` |
| Component `major_outage`, Ergebnis wechselhaft | keine Schwelle erreicht | FLAPPING | keine (bzw. `flapping_status`, falls gesetzt) |
| Statuspage-API nicht erreichbar | – | ERROR | 3 Versuche mit Backoff, danach roter Job |

Als Fehlversuch zählt jeweils: Timeout, Netzwerk-/TLS-Fehler oder ein Statuscode,
der nicht in `expected_status` steht. **Ein unbekannter Statuscode gilt nie als
`operational`.**

Bewusste Eigenschaften:

* **Ein kurzer Aussetzer erzeugt keinen Ausfall.** Standard sind 3 Fehlversuche im
  Abstand von 15 Sekunden, also rund 45 Sekunden durchgehende Störung, bevor
  `major_outage` gesetzt wird.
* **Rückkehr nur bei stabiler Erreichbarkeit.** Aus einem gestörten Zustand braucht
  es 2 aufeinanderfolgende Erfolge. Ist der Component bereits `operational`,
  genügt ein Erfolg – der Normalfall bleibt schnell und sparsam.
* **Idempotent.** Es wird nur geschrieben, wenn sich der Status wirklich ändert.
  Ein manuell in Statuspage gesetzter Wartungsstatus (`under_maintenance`) wird
  allerdings beim nächsten UP-Lauf auf `operational` zurückgesetzt – für geplante
  Wartungsfenster den Monitor vorher auf `"enabled": false` setzen.

### Beispiel-Log

```
[2026-08-24T10:15:02Z] starting checks for 1 monitor(s) from monitors.json
[2026-08-24T10:15:02Z] --- CoreFit API ---
[2026-08-24T10:15:02Z]   url=https://api.core-fit.app/up method=GET timeout=10s expected_status=[200]
[2026-08-24T10:15:02Z]   thresholds: 3 consecutive failures -> major_outage, 2 consecutive successes -> operational (retry delay 15s)
[2026-08-24T10:15:02Z]   component reference: page_id=${STATUSPAGE_PAGE_ID} component_id=${STATUSPAGE_COMPONENT_ID}
[2026-08-24T10:15:03Z]   current component status: operational
[2026-08-24T10:15:03Z]   attempt 1/3: HTTP 200 in 142 ms - status OK -> OK
[2026-08-24T10:15:03Z]   result: UP (last check: HTTP 200 in 142 ms - status OK)
[2026-08-24T10:15:03Z]   statuspage: already 'operational' - no update needed
[2026-08-24T10:15:03Z] summary: 1 UP, 0 DOWN, 0 error(s)
```

Das Log enthält Zeitpunkt, URL, HTTP-Status, Antwortzeit, UP/DOWN und die
Statuspage-Aktion. Secrets erscheinen nie: IDs werden als Platzhalter ausgegeben,
und alle Ausgaben laufen zusätzlich durch einen Filter, der secret-artige
Umgebungswerte durch `***` ersetzt (zusätzlich zur Maskierung durch GitHub selbst).

---

## Hinweise zu GitHub Actions Limits

* **Kürzestes Intervall: 5 Minuten.** Kürzere `cron`-Angaben werden von GitHub
  nicht schneller ausgeführt.
* **Zeitplan ist „best effort“.** Bei hoher Last können Läufe verzögert werden
  oder ganz ausfallen – besonders zur vollen Stunde. GitHub Actions ersetzt daher
  kein SLA-Monitoring mit Sekundengenauigkeit.
* **`schedule` läuft nur auf dem Default-Branch.**
* **Inaktive Repositories:** In öffentlichen Repositories deaktiviert GitHub
  geplante Workflows nach 60 Tagen ohne Repository-Aktivität. Es genügt ein Commit
  oder das erneute Aktivieren in der Actions-Oberfläche.
* **Minuten-Kontingent:** In öffentlichen Repositories sind Actions-Minuten
  kostenlos. In **privaten** Repositories zählt jeder Lauf gegen das Kontingent –
  alle 5 Minuten ≈ 8 900 Läufe/Monat. Selbst bei einer abgerechneten Minute pro
  Lauf ist das Freikontingent (2 000 Minuten/Monat) deutlich überschritten. Für
  private Repositories deshalb entweder das Intervall vergrössern (z. B.
  `*/15 * * * *`) oder die Kosten einplanen.
* **Nebenläufigkeit:** Der Workflow nutzt eine `concurrency`-Gruppe, damit sich
  zwei Läufe nie gegenseitig überholen. Ein Lauf sollte deutlich unter 5 Minuten
  bleiben (Standardkonfiguration: wenige Sekunden bis ca. 1 Minute).
* **Statuspage-API:** Atlassian begrenzt den API-Zugriff auf etwa 1 Request pro
  Sekunde je Key; `monitor.py` drosselt entsprechend und wiederholt `429`/`5xx`
  mit exponentiellem Backoff.

---

## Sicherheit

* Keine Secrets, Keys oder Tokens im Repository – ausschliesslich GitHub Secrets.
* Keine Secrets im Log: IDs werden als `${PLATZHALTER}` protokolliert, jede
  Ausgabe durchläuft zusätzlich eine Redaction-Funktion.
* Alle Requests laufen über HTTPS mit Zertifikatsprüfung; `http://` muss pro
  Monitor ausdrücklich erlaubt werden, die Statuspage-API akzeptiert nur HTTPS.
* Response-Bodies werden auf 64 KB begrenzt gelesen und nie ins Log geschrieben.
* Konfigurationswerte werden validiert, bevor ein Request entsteht; an Statuspage
  gehen nur Statuswerte aus der offiziellen Liste.
* Der Workflow läuft mit `contents: read` und schreibt nicht ins Repository.
