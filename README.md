<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/firntech-logo-white.png">
  <img src=".github/assets/firntech-logo.png" alt="FIRNTECH" width="420">
</picture>

# Statuspage Monitor

**Abhängigkeitsfreies Uptime-Monitoring für Atlassian Statuspage, betrieben von GitHub Actions.**<br>
Alle 5 Minuten ein HTTP-Healthcheck, das Ergebnis landet direkt in den Statuspage-Components.

<br>

[![Monitor](https://img.shields.io/github/actions/workflow/status/VALCODE-CH/Statuspage/monitor.yml?branch=main&style=for-the-badge&label=monitor&labelColor=0f172a&color=22c55e&logo=githubactions&logoColor=white)](https://github.com/VALCODE-CH/Statuspage/actions/workflows/monitor.yml)
[![Validate](https://img.shields.io/github/actions/workflow/status/VALCODE-CH/Statuspage/validate.yml?branch=main&style=for-the-badge&label=validate&labelColor=0f172a&color=22c55e&logo=githubactions&logoColor=white)](https://github.com/VALCODE-CH/Statuspage/actions/workflows/validate.yml)
[![Live Status](https://img.shields.io/badge/Live_Status-firntech.statuspage.io-38bdf8?style=for-the-badge&labelColor=0f172a&logo=statuspage&logoColor=white)](https://firntech.statuspage.io/)

</div>

---

## Funktionsweise

```mermaid
flowchart LR
    A["GitHub Actions<br/><code>*/5 * * * *</code>"] --> B["Ist-Status<br/>von Statuspage lesen"]
    B --> C["Healthcheck<br/>GET /up · Timeout 10s"]
    C -->|"3× Fehler in Folge"| D["PATCH<br/>major_outage"]
    C -->|"2× Erfolg in Folge"| E["PATCH<br/>operational"]
    C -->|"Status bereits korrekt"| F["kein API-Call"]
    D --> G["Statuspage"]
    E --> G
```

Pro Lauf und pro Monitor:

1. **Aktuellen Component-Status lesen**
   `GET https://api.statuspage.io/v1/pages/{page_id}/components/{component_id}`
2. **Healthcheck ausführen** – HTTP-Request auf die konfigurierte URL mit Timeout (Standard 10 Sekunden).
   Erfolgreich ist nur ein erwarteter Statuscode (Standard: genau `200`).
3. **Wiederholen, bis ein Ergebnis feststeht**:
   * `failure_threshold` aufeinanderfolgende Fehlversuche → **DOWN**
   * die nötige Anzahl aufeinanderfolgender Erfolge → **UP**
4. **Statuspage aktualisieren** – nur wenn sich der Status tatsächlich ändert:

   ```http
   PATCH https://api.statuspage.io/v1/pages/{page_id}/components/{component_id}
   Authorization: OAuth <STATUSPAGE_API_KEY>
   Content-Type: application/json

   {"component": {"status": "operational"}}
   ```

Weil der Ist-Zustand vor jedem Lauf von Statuspage gelesen wird, braucht das Projekt **keinen eigenen
Zustandsspeicher**.

---

## Weitere Endpoints hinzufügen

> Ein Monitor = ein Statuspage-Component. Beliebig viele davon, **ein einziger Eintrag pro Endpoint** –
> Code und Workflow müssen nie angefasst werden.

**Schritt 1:** Component in Statuspage anlegen (**Components → Add a component**).

**Schritt 2:** Component-ID nachschlagen:

```bash
export STATUSPAGE_API_KEY=...
python3 monitor.py --list-components
```

```text
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

Committen, fertig. Der nächste geplante Lauf prüft alle Monitore und aktualisiert jeden Component einzeln.
