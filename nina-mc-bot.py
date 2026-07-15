#!/usr/bin/env python3
"""
NINA -> MeshCore Warnbot

Pollt die NINA-Dashboard-Endpunkte des BBK und sendet neue/aktualisierte
Warnmeldungen in MeshCore-Hashtag-Kanäle:

  DWD-Wetterwarnungen  -> Wetterkanal
  alles andere         -> Regionalkanal   (MoWaS, KATWARN, BIWAPP, LHP, ...)

pip install meshcore httpx
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from meshcore import MeshCore, EventType

# ---------------------------------------------------------------- Konfiguration

NINA_BASE = "https://warnung.bund.de/api31"

# ARS (12-stellig, rechts mit Nullen aufgefuellt) -> Anzeigename fuer die Region.
# Muss vor dem ersten Start mit den eigenen Regionen befuellt werden, siehe README.
# Beispiel: "094620000000": "Bayreuth",
REGIONS = {}

# Verbindung zum Companion-Node: "serial" | "tcp" | "ble"
CONN_TYPE = os.getenv("MC_CONN", "serial")
CONN_TARGET = os.getenv("MC_TARGET", "/dev/ttyACM0")
CONN_PORT = int(os.getenv("MC_PORT", "5000"))

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# --- Routing: Provider -> Kanal ------------------------------------------------
# "provider" aus dem NINA-Payload. DWD kommt separat, alles Uebrige in den
# Ortskanal. Der Schluessel "*" ist der Default.
#
# severity_min pro Route: DWD-"Minor" (Windboeen, leichter Frost) wuerde im
# Sommer mehrere Meldungen pro Tag erzeugen -> gefiltert. MoWaS & Co. sind
# selten, da geht alles durch.
ROUTES = {
    "DWD": {
        "channel": os.getenv("MC_CHANNEL_DWD", "#wetter"),
        "idx": os.getenv("MC_CHANNEL_IDX_DWD"),
        "severity_min": os.getenv("SEVERITY_MIN_DWD", "Moderate"),
    },
    "*": {
        "channel": os.getenv("MC_CHANNEL_DEFAULT", "#warnung"),
        "idx": os.getenv("MC_CHANNEL_IDX_DEFAULT"),
        "severity_min": os.getenv("SEVERITY_MIN_DEFAULT", "Unknown"),
    },
}

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "180"))
STATE_FILE = Path(os.getenv("STATE_FILE", "nina_state.json"))

# Startmeldung: einmalig nach erfolgreicher Verbindung in jeden konfigurierten
# Kanal senden, damit sichtbar ist, dass der Bot laeuft (z. B. nach Neustart
# des Diensts).
ANNOUNCE_STARTUP = os.getenv("ANNOUNCE_STARTUP", "1") == "1"
STARTUP_MESSAGE = os.getenv("STARTUP_MESSAGE", "NINA Warnbot aktiv")

SEVERITY_ORDER = ["Unknown", "Minor", "Moderate", "Severe", "Extreme"]

MAX_CHARS = 130          # pro MeshCore-Paket, konservativ
MAX_PARTS = 2            # laengere Meldungen werden abgeschnitten
SEND_GAP = 12            # Sekunden zwischen Paketen (Duty Cycle!)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("ninabot")

# ---------------------------------------------------------------- State


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State-Datei defekt, starte leer")
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- NINA


async def fetch_dashboard(client: httpx.AsyncClient, ars: str) -> list:
    r = await client.get(f"{NINA_BASE}/dashboard/{ars}.json", timeout=20)
    r.raise_for_status()
    return r.json()


async def fetch_details(client: httpx.AsyncClient, warn_id: str) -> dict | None:
    try:
        r = await client.get(f"{NINA_BASE}/warnings/{warn_id}.json", timeout=20)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        log.warning("Details fuer %s nicht abrufbar: %s", warn_id, e)
        return None


def route_for(provider: str) -> dict:
    return ROUTES.get(provider, ROUTES["*"])


def severity_ok(sev: str, minimum: str) -> bool:
    try:
        return SEVERITY_ORDER.index(sev) >= SEVERITY_ORDER.index(minimum)
    except ValueError:
        return True  # unbekannte Stufe -> lieber senden


def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def shorten_headline(h: str) -> str:
    """'Amtliche WARNUNG vor STARKEM GEWITTER' -> 'STARKEM GEWITTER'"""
    h = re.sub(r"^Amtliche\s+(Warnung|WARNUNG)\s+vor\s+", "", h)
    h = re.sub(r"^(Vorabinformation|Amtliche Gefahrenmeldung)\s*:?\s*", "", h)
    return h.strip()


def fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        now = datetime.now(dt.tzinfo or timezone.utc)
        if dt.date() == now.date():
            return dt.strftime("%H:%M")
        return dt.strftime("%d.%m. %H:%M")
    except ValueError:
        return ""


def build_message(entry: dict, region: str, details: dict | None) -> str:
    data = entry.get("payload", {}).get("data", {})

    # info[].event ist der Nominativ ("STARKES GEWITTER"), die Headline waere
    # "Amtliche WARNUNG vor STARKEM GEWITTER" - Dativ, liest sich abgeschnitten
    # bloed. Fallback auf die Headline, wenn keine Details da sind.
    de_info = None
    if details:
        infos = details.get("info") or []
        de_info = next(
            (i for i in infos if i.get("language", "").startswith("de")), None
        )

    headline = (de_info or {}).get("event") or shorten_headline(
        entry.get("i18nTitle", {}).get("de") or data.get("headline", "Warnung")
    )
    provider = data.get("provider", "?")
    severity = data.get("severity", "Unknown")
    msg_type = data.get("msgType", "Alert")

    prefix = "WARNUNG"
    if msg_type == "Cancel":
        prefix = "ENTWARNUNG"
    elif msg_type == "Update":
        prefix = "UPDATE"

    bis = fmt_time(entry.get("expires"))
    msg = f"[{prefix}] {region}: {headline} ({provider}/{severity}"
    msg += f", bis {bis})" if bis else ")"

    # Handlungshinweis anhaengen, falls Platz da ist
    if de_info and msg_type != "Cancel":
        instr = strip_html(
            de_info.get("instruction") or de_info.get("description") or ""
        )
        if instr:
            msg += " " + instr

    return msg


def chunk(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    parts, rest = [], text
    while rest and len(parts) < MAX_PARTS:
        if len(rest) <= MAX_CHARS:
            parts.append(rest)
            break
        cut = rest.rfind(" ", 0, MAX_CHARS - 6)
        cut = cut if cut > MAX_CHARS // 2 else MAX_CHARS - 6
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest and len(parts) == MAX_PARTS:
        parts[-1] = parts[-1][: MAX_CHARS - 4].rstrip() + " ..."
    n = len(parts)
    return [f"{p} ({i+1}/{n})" if n > 1 else p for i, p in enumerate(parts)]


# ---------------------------------------------------------------- MeshCore


async def connect() -> MeshCore | None:
    if DRY_RUN:
        log.warning("DRY_RUN aktiv - keine Verbindung zum Node, es wird nur geloggt")
        return None
    if CONN_TYPE == "serial":
        return await MeshCore.create_serial(CONN_TARGET)
    if CONN_TYPE == "tcp":
        return await MeshCore.create_tcp(CONN_TARGET, CONN_PORT)
    if CONN_TYPE == "ble":
        return await MeshCore.create_ble(CONN_TARGET)
    raise ValueError(f"Unbekannter MC_CONN: {CONN_TYPE}")


MAX_CHANNEL_SLOTS = 8    # Companion-Protokoll: Kanal-Index 0-7


async def scan_channels(mc: MeshCore) -> dict[str, int]:
    """Belegte Kanal-Slots einlesen -> {name_lowercase_ohne_raute: idx}.

    Die meshcore-Lib hat kein get_channels() - das ist ein Komfort-Kommando
    von meshcore-cli, das intern ueber die Slots loopt. Also selbst loopen.
    """
    found = {}
    for idx in range(MAX_CHANNEL_SLOTS):
        res = await mc.commands.get_channel(idx)
        if res.type == EventType.ERROR:
            break  # Slot existiert nicht -> fertig
        name = (res.payload.get("channel_name") or "").strip()
        secret = res.payload.get("channel_secret") or b""
        if not name or not any(secret):
            continue  # leerer Slot: kein Name, Secret komplett 0x00
        found[name.lstrip("#").lower()] = idx
        log.debug("Slot %d: %s", idx, name)
    return found


async def resolve_routes(mc: MeshCore | None) -> None:
    """Fuer jede Route den Kanal-Index ermitteln und in ROUTES eintragen."""
    if DRY_RUN:
        for key, route in ROUTES.items():
            route["idx"] = int(route["idx"]) if route["idx"] else -1
            log.info("[dry] %-4s -> %s (idx %s)", key, route["channel"], route["idx"])
        return

    channels = None
    for key, route in ROUTES.items():
        if route["idx"] is not None:
            route["idx"] = int(route["idx"])
            log.info("%-4s -> %s (idx %d, gesetzt)", key, route["channel"], route["idx"])
            continue

        if channels is None:
            channels = await scan_channels(mc)
            log.info("Kanaele auf dem Node: %s", channels or "(keine)")

        want = route["channel"].lstrip("#").lower()
        if want not in channels:
            raise RuntimeError(
                f"Kanal {route['channel']} nicht auf dem Node. Anlegen mit:\n"
                f"  KEY=$(printf '%s' '{route['channel']}' | sha256sum | cut -c1-32)\n"
                f"  meshcli -s {CONN_TARGET} set_channel <slot> '{route['channel']}' \"$KEY\"\n"
                f"Gefunden wurden: {channels or '(keine)'}"
            )
        route["idx"] = channels[want]
        log.info("%-4s -> %s (idx %d)", key, route["channel"], route["idx"])


async def send_channel(mc: MeshCore | None, route: dict, text: str) -> bool:
    if DRY_RUN:
        log.info("[dry] TX %s: %s", route["channel"], text)
        return True
    res = await mc.commands.send_chan_msg(route["idx"], text)
    if res.type == EventType.ERROR:
        log.error("Senden an %s fehlgeschlagen: %s", route["channel"], res.payload)
        return False
    log.info("TX %s (%dB): %s", route["channel"], len(text.encode()), text)
    return True


async def announce_startup(mc: MeshCore | None) -> None:
    """Startmeldung einmalig in jeden konfigurierten Kanal senden."""
    sent = set()
    for route in ROUTES.values():
        key = route["channel"] if DRY_RUN else route["idx"]
        if key in sent:
            continue
        sent.add(key)
        await send_channel(mc, route, STARTUP_MESSAGE)
        if not DRY_RUN:
            await asyncio.sleep(SEND_GAP)


# ---------------------------------------------------------------- Hauptschleife


async def poll_once(client: httpx.AsyncClient, mc: MeshCore | None, state: dict):
    seen_now = set()

    for ars, region in REGIONS.items():
        try:
            entries = await fetch_dashboard(client, ars)
        except httpx.HTTPError as e:
            log.warning("NINA (%s) nicht erreichbar: %s", region, e)
            # Region nicht abrufbar -> deren IDs nicht als "abgelaufen" werten
            seen_now.update(k for k, v in state.items() if v.get("ars") == ars)
            continue

        for entry in entries:
            wid = entry["id"]
            payload = entry.get("payload", {})
            whash = payload.get("hash", "")
            data = payload.get("data", {})
            seen_now.add(wid)

            if not data.get("valid", True):
                continue

            provider = data.get("provider", "?")
            route = route_for(provider)

            if not severity_ok(data.get("severity", "Unknown"), route["severity_min"]):
                continue
            if state.get(wid, {}).get("hash") == whash:
                continue  # unveraendert bekannt

            details = await fetch_details(client, wid)
            msg = build_message(entry, region, details)

            ok = True
            for part in chunk(msg):
                ok &= await send_channel(mc, route, part)
                if not DRY_RUN:
                    await asyncio.sleep(SEND_GAP)

            if ok:
                state[wid] = {"hash": whash, "ars": ars, "provider": provider}
                save_state(state)

    # Abgelaufene Warnungen aus dem State werfen
    for wid in [k for k in state if k not in seen_now]:
        del state[wid]
    save_state(state)


async def main():
    if not REGIONS:
        raise RuntimeError(
            "REGIONS ist leer - bitte im Skript mit den eigenen ARS-Codes befuellen "
            "(siehe README, Abschnitt 'Regionen konfigurieren')"
        )

    state = load_state()
    mc = await connect()
    if mc:
        log.info("Mit Node verbunden (%s: %s)", CONN_TYPE, CONN_TARGET)

    await resolve_routes(mc)

    if ANNOUNCE_STARTUP:
        await announce_startup(mc)

    async with httpx.AsyncClient(
        headers={"User-Agent": "nina-meshcore-bot/1.1"}
    ) as client:
        if not state:
            # Erststart: aktuelle Lage nur einlesen, nicht rausposaunen
            for ars in REGIONS:
                try:
                    for e in await fetch_dashboard(client, ars):
                        p = e.get("payload", {})
                        state[e["id"]] = {
                            "hash": p.get("hash", ""),
                            "ars": ars,
                            "provider": p.get("data", {}).get("provider", "?"),
                        }
                except httpx.HTTPError:
                    pass
            save_state(state)
            log.info(
                "Erststart: %d bestehende Warnungen als bekannt markiert", len(state)
            )

        while True:
            try:
                await poll_once(client, mc, state)
            except Exception as e:  # noqa: BLE001
                log.exception("Poll-Fehler: %s", e)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbeendet")