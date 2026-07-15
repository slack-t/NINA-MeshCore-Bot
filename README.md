# NINA-MeshCore-Bot

Ein kleiner Python-Bot, der amtliche Warnmeldungen des BBK (NINA-Warn-App) abruft und automatisch als Nachricht in [MeshCore](https://meshcore.co.uk/)-Funkkanäle sendet. Praktisch für lokale Mesh-Funknetze, die ihre Nutzer auch ohne Internet/Handynetz vor Unwettern, Katastrophen oder anderen Gefahren warnen wollen.

Der Bot ist ohne feste Region ausgeliefert und muss vor dem ersten Start mit der/den eigenen Region(en) konfiguriert werden (siehe [Regionen konfigurieren](#regionen-anpassen)) – lässt sich dann aber für jede Region in Deutschland nutzen.

## Was macht der Bot?

1. Fragt in regelmäßigen Abständen die [NINA-Dashboard-API](https://warnung.bund.de/) des BBK für die konfigurierten Regionen ab.
2. Erkennt neue oder veränderte Warnmeldungen (Vergleich per Hash, gespeichert in einer lokalen State-Datei).
3. Baut daraus eine kurze, lesbare Textnachricht (inkl. Handlungshinweis, wenn Platz ist).
4. Sendet die Nachricht über einen angeschlossenen MeshCore-Companion-Node in den passenden Funkkanal:
   - **DWD-Wetterwarnungen** (Unwetter, Sturm, Frost, ...) → Wetterkanal
   - **alles andere** (MoWaS, KATWARN, BIWAPP, Hochwasser/LHP, ...) → Regionalkanal
5. Merkt sich den Zustand, damit dieselbe Warnung nicht mehrfach verschickt wird, und entfernt abgelaufene Warnungen wieder aus dem Zustand.

Beim allerersten Start werden bereits bestehende Warnungen nur "stillschweigend" als bekannt markiert, statt sie sofort alle rauszuschicken.

## Voraussetzungen

- Python 3.10 oder neuer (wegen der `str | None`-Typannotationen)
- Ein MeshCore-fähiger Companion-Node (z. B. per USB/Seriell, TCP oder BLE erreichbar)
- Die konfigurierten Kanäle (siehe [Empfehlung für die Kanalwahl](#empfehlung-für-die-kanalwahl)) müssen bereits auf dem Node angelegt sein

## Installation

Es wird empfohlen, den Bot in einer eigenen virtuellen Umgebung (venv) laufen zu lassen, statt die Abhängigkeiten global zu installieren.

```bash
git clone <repo-url>
cd NINA-MeshCore-Bot

# Virtuelle Umgebung anlegen (z. B. unter .venv/meshcore)
python -m venv .venv/meshcore

# Aktivieren
source .venv/meshcore/bin/activate      # Linux/macOS
.venv\meshcore\Scripts\activate         # Windows (PowerShell/CMD)

# Abhängigkeiten installieren
pip install meshcore httpx
```

Der Ordnername `.venv/meshcore` ist nur ein Vorschlag – jeder andere Pfad (z. B. einfach `.venv`) funktioniert genauso. Wichtig ist nur, dass die venv vor jedem Start aktiviert ist (`source .venv/meshcore/bin/activate`), bzw. dass der Python-Interpreter aus der venv verwendet wird.

## Konfiguration

Der Bot wird komplett über Umgebungsvariablen konfiguriert. Alles ist optional und hat sinnvolle Standardwerte.

| Variable | Standard | Beschreibung |
|---|---|---|
| `MC_CONN` | `serial` | Verbindungsart zum Node: `serial`, `tcp` oder `ble` |
| `MC_TARGET` | `/dev/ttyACM0` | Serieller Port, TCP-Host oder BLE-Adresse/Name des Nodes |
| `MC_PORT` | `5000` | TCP-Port (nur bei `MC_CONN=tcp`) |
| `DRY_RUN` | `0` | Bei `1` wird nicht gesendet, sondern nur geloggt (zum Testen ohne Node) |
| `MC_CHANNEL_DWD` | `#wetter` | Kanalname für DWD-Wetterwarnungen |
| `MC_CHANNEL_IDX_DWD` | *(auto)* | Kanal-Index für DWD, falls automatische Auflösung übersprungen werden soll |
| `SEVERITY_MIN_DWD` | `Moderate` | Mindest-Schweregrad für DWD-Meldungen (siehe unten) |
| `MC_CHANNEL_DEFAULT` | `#warnung` | Kanalname für alle übrigen Warnquellen |
| `MC_CHANNEL_IDX_DEFAULT` | *(auto)* | Kanal-Index für den Default-Kanal, falls automatische Auflösung übersprungen werden soll |
| `SEVERITY_MIN_DEFAULT` | `Unknown` | Mindest-Schweregrad für alle übrigen Meldungen |
| `POLL_INTERVAL` | `180` | Abstand zwischen zwei Abfragen der NINA-API in Sekunden |
| `STATE_FILE` | `nina_state.json` | Pfad zur Datei, in der bekannte Warnungen gespeichert werden |

**Schweregrade** (`SEVERITY_MIN_*`), von niedrig nach hoch: `Unknown` < `Minor` < `Moderate` < `Severe` < `Extreme`. Meldungen unterhalb des eingestellten Minimums werden verworfen. Der Standardwert für DWD (`Moderate`) filtert z. B. harmlose Windböen oder leichten Frost heraus, die sonst mehrmals täglich Meldungen erzeugen würden.

### Regionen anpassen

Der Bot wird **ohne** vorkonfigurierte Region ausgeliefert. Die überwachten Regionen müssen vor dem ersten Start direkt im Skript in `nina-mc-bot.py` eingetragen werden:

```python
REGIONS = {
    "094620000000": "Bayreuth",
    # weitere Regionen nach Bedarf ergänzen
}
```

Der Schlüssel ist der zwölfstellige [amtliche Regionalschlüssel (ARS)](https://de.wikipedia.org/wiki/Amtlicher_Regionalschl%C3%BCssel), rechts mit Nullen aufgefüllt. Den ARS für die eigene Region findet man z. B. über die [NINA-Warnkarte](https://warnung.bund.de/) oder das Statistische Bundesamt. Der Wert dahinter ist nur ein Kurzname für die Ausgabe in den Nachrichten. Bleibt `REGIONS` leer, bricht der Bot beim Start mit einer entsprechenden Fehlermeldung ab.

### Provider-Routing anpassen

Welche Warnquelle (`provider` im NINA-Payload, z. B. `DWD`, `MOWAS`, `KATWARN`, `BIWAPP`, `LHP`) in welchen Kanal geht, steht im `ROUTES`-Dict im Skript. `"*"` ist der Fallback für alle nicht explizit gelisteten Provider. Bei Bedarf lassen sich hier weitere Einträge ergänzen, z. B. um MoWaS in einen eigenen Kanal zu leiten.

### Empfehlung für die Kanalwahl

MeshCore-Hashtag-Kanäle sind öffentlich und werden meist von vielen Knoten gemeinsam genutzt – deshalb lohnt es sich, die Kanalnamen bewusst zu wählen:

- **Ein separater Wetterkanal** (Standard: `#wetter`) für DWD-Meldungen. Diese kommen relativ häufig vor (mehrmals pro Woche), sind aber selten akut lebensbedrohlich. Nutzer können diesen Kanal bei Bedarf stummschalten, ohne wichtige Katastrophenmeldungen zu verpassen.
- **Ein separater Regional-/Alarmkanal** (Standard: `#warnung`) für alles andere (MoWaS, KATWARN, BIWAPP, Hochwasser/LHP, ...). Diese Meldungen sind selten, dafür in der Regel deutlich dringlicher – ein eigener Kanal sorgt dafür, dass sie nicht im "Wetterrauschen" untergehen.
- Für mehrere Regionen im selben Mesh empfiehlt sich ein regionsspezifischer Name, z. B. `#wetter-<ort>` bzw. `#warnung-<ort>`, damit Nutzer gezielt nur ihre Region abonnieren können.
- Kanalnamen kurz und eindeutig halten (siehe MeshCore-Konventionen der eigenen Community), da sie über `MC_CHANNEL_DWD` bzw. `MC_CHANNEL_DEFAULT` frei konfigurierbar sind.

## Nutzung

Venv aktivieren (falls noch nicht geschehen) und Bot starten:

```bash
source .venv/meshcore/bin/activate   # Linux/macOS
python nina-mc-bot.py
```

Zum Testen ohne echten Node und ohne Nachrichten zu verschicken:

```bash
DRY_RUN=1 python nina-mc-bot.py
```

Über TCP verbinden (z. B. wenn der Node über WLAN/TCP erreichbar ist):

```bash
MC_CONN=tcp MC_TARGET=192.168.1.50 MC_PORT=5000 python nina-mc-bot.py
```

Der Bot läuft dauerhaft in einer Schleife (`POLL_INTERVAL` Sekunden zwischen den Abfragen) und beendet sich sauber mit `Strg+C`.

### Fehlt ein Kanal auf dem Node?

Falls ein konfigurierter Kanal (z. B. `#wetter`) noch nicht auf dem Node existiert, bricht der Bot beim Start mit einer Fehlermeldung ab und zeigt den passenden `meshcli`-Befehl zum Anlegen an, z. B.:

```bash
KEY=$(printf '%s' '#wetter' | sha256sum | cut -c1-32)
meshcli -s /dev/ttyACM0 set_channel <slot> '#wetter' "$KEY"
```

## Als Dauerdienst betreiben (systemd)

Beispiel für eine `systemd`-Unit, um den Bot dauerhaft im Hintergrund laufen zu lassen:

```ini
[Unit]
Description=NINA MeshCore Warnbot
After=network-online.target

[Service]
WorkingDirectory=/opt/nina-mc-bot
ExecStart=/opt/nina-mc-bot/.venv/meshcore/bin/python3 nina-mc-bot.py
Environment=MC_CONN=serial
Environment=MC_TARGET=/dev/ttyACM0
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
```

## Nachrichtenformat

Eine gesendete Nachricht sieht z. B. so aus:

```
[WARNUNG] Bayreuth: STARKEM GEWITTER (DWD/Severe, bis 18:30) Unwetterartige Gewitter mit Starkregen...
```

- `[WARNUNG]`, `[UPDATE]` oder `[ENTWARNUNG]` je nach Meldungstyp
- Region, Ereignis, Quelle und Schweregrad
- Ablaufzeit, falls vorhanden
- Handlungshinweis, falls Platz ist

Nachrichten werden bei Bedarf auf maximal 2 Teile (`MAX_PARTS`) à ca. 130 Zeichen (`MAX_CHARS`) aufgeteilt, mit 12 Sekunden Pause zwischen den Paketen (`SEND_GAP`), um den Duty Cycle des Funknetzes nicht zu überlasten.

## Lizenz

Dieses Projekt steht unter der [GNU General Public License v3.0](LICENSE).
