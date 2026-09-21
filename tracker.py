#!/usr/bin/env python3
"""
Rastreador de precios de vuelos a Cancun (CUN) - Travelpayouts, modo VIGILANCIA.

Como los precios para la ventana objetivo (ene/feb 2027) todavia no estan
cargados en la cache (el viaje es muy a futuro), este modo:

  1. Trae las ofertas mas baratas a CUN que existan HOY en la cache
     (sin exigir la fecha exacta), asi siempre ves precios reales y tendencia.
  2. Marca de forma destacada cualquier oferta que caiga dentro de tu
     VENTANA OBJETIVO (ene/feb 2027 +- flex). Cuando eso aparezca, es la senal
     de que tu viaje ya se puede reservar.
  3. Guarda el minimo historico (global y el de tu ventana por separado).
  4. Avisa por Telegram cuando baja el minimo, y SIEMPRE que aparezca por
     primera vez una oferta dentro de tu ventana objetivo.

Datos: Travelpayouts entrega precios reales desde una CACHE (~7 dias) basada
en busquedas de usuarios. Muchas ofertas son one-way (ida sola): el script lo
detecta e informa si el precio es solo ida o ida y vuelta.

Variables de entorno:
  TP_TOKEN            -> token de Travelpayouts (X-Access-Token)
  TP_MARKER           -> marker de afiliado (opcional, para los links)
  TELEGRAM_BOT_TOKEN  -> token del bot
  TELEGRAM_CHAT_ID    -> chat id
  ALWAYS_NOTIFY       -> "true" para recibir aviso en cada corrida
"""

import os
import sys
import json
import datetime as dt
from pathlib import Path

import requests

# ----------------------------------------------------------------------
# CONFIGURACION
# ----------------------------------------------------------------------
ORIGIN = "BUE"
DESTINATION = "CUN"

# Ventana OBJETIVO (tu viaje ideal). No filtra la busqueda: solo sirve para
# marcar/avisar cuando aparezcan ofertas dentro de este rango.
TARGET_DEPART = dt.date(2027, 1, 30)
TARGET_RETURN = dt.date(2027, 2, 13)
TARGET_FLEX_DAYS = 5          # margen alrededor de las fechas objetivo
TARGET_MIN_NIGHTS = 10
TARGET_MAX_NIGHTS = 17

CURRENCY = "usd"
DIRECT_ONLY = False
TOP_N = 6                     # cuantas ofertas mostrar en el aviso general

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
HISTORY_FILE = Path(__file__).parent / "price_history.json"
AVIASALES_BASE = "https://www.aviasales.com"


def env(name, required=True):
    val = os.environ.get(name)
    if required and not val:
        print(f"ERROR: falta la variable de entorno {name}", file=sys.stderr)
        sys.exit(1)
    return val


# ----------------------------------------------------------------------
# TRAVELPAYOUTS
# ----------------------------------------------------------------------

def query_cheapest(token):
    """Trae las ofertas mas baratas a CUN que haya en la cache (sin fecha fija)."""
    headers = {"X-Access-Token": token}
    params = {
        "origin": ORIGIN,
        "destination": DESTINATION,
        "currency": CURRENCY,
        "sorting": "price",
        "direct": str(DIRECT_ONLY).lower(),
        "limit": 100,
        "page": 1,
        "one_way": "false",
    }
    try:
        resp = requests.get(API_URL, headers=headers, params=params, timeout=30)
    except requests.RequestException as e:
        print(f"red error: {e}", file=sys.stderr)
        return []
    if resp.status_code >= 400:
        print(f"API {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return []
    body = resp.json()
    if not body.get("success", True):
        print(f"API sin exito: {body}", file=sys.stderr)
        return []
    return body.get("data", [])


def parse_date(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def normalize(o, marker):
    depart = parse_date(o.get("departure_at", ""))
    ret = parse_date(o.get("return_at", "")) if o.get("return_at") else None
    # one-way si no hay vuelta o la duracion de regreso es 0
    is_round = bool(ret) and o.get("duration_back", 0) not in (0, None)
    link = o.get("link", "")
    full_link = (AVIASALES_BASE + link) if link.startswith("/") else link
    if marker and full_link and "marker=" not in full_link:
        sep = "&" if "?" in full_link else "?"
        full_link = f"{full_link}{sep}marker={marker}"
    nights = (ret - depart).days if (ret and depart) else None
    return {
        "price": o.get("price"),
        "currency": CURRENCY.upper(),
        "origin": o.get("origin", ORIGIN),
        "origin_airport": o.get("origin_airport", ""),
        "destination": o.get("destination", DESTINATION),
        "depart": depart.isoformat() if depart else "?",
        "depart_date": depart,
        "return": ret.isoformat() if ret else None,
        "nights": nights,
        "airline": o.get("airline", "?"),
        "transfers": o.get("transfers", "?"),
        "round_trip": is_round,
        "link": full_link,
    }


def in_target(o):
    """True si la oferta cae dentro de la ventana objetivo (ida, y noches si RT)."""
    d = o.get("depart_date")
    if not d:
        return False
    if abs((d - TARGET_DEPART).days) > TARGET_FLEX_DAYS:
        return False
    if o["round_trip"] and o["nights"] is not None:
        if not (TARGET_MIN_NIGHTS <= o["nights"] <= TARGET_MAX_NIGHTS):
            return False
    return True


# ----------------------------------------------------------------------
# HISTORIAL Y NOTIFICACION
# ----------------------------------------------------------------------

def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"runs": [], "min_global": None, "min_target": None,
            "target_seen": False}


def save_history(hist):
    HISTORY_FILE.write_text(json.dumps(hist, indent=2, ensure_ascii=False,
                                       default=str))


def send_telegram(text):
    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat, "text": text,
            "parse_mode": "Markdown", "disable_web_page_preview": True,
        }, timeout=20)
        if r.status_code >= 400:
            print(f"Telegram error {r.status_code}: {r.text[:200]}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Telegram red error: {e}", file=sys.stderr)


def fmt(o, short=False):
    tag = "ida y vuelta" if o["round_trip"] else "solo ida"
    orig = o["origin_airport"] or o["origin"]
    line = f"*{o['price']:.0f} {o['currency']}* ({tag}) - {orig}->{o['destination']}"
    if short:
        detail = f"  {o['depart']}"
        if o["return"]:
            detail += f" -> {o['return']} ({o['nights']}n)"
        detail += f" - {o['airline']} - {o['transfers']} esc."
        return line + "\n" + detail
    line += f"\nIda {o['depart']}"
    if o["return"]:
        line += f" - Vuelta {o['return']} ({o['nights']} noches)"
    line += f"\n{o['airline']} - {o['transfers']} escala(s)"
    if o.get("link"):
        line += f"\n[Ver en Aviasales]({o['link']})"
    return line


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    token = env("TP_TOKEN")
    marker = env("TP_MARKER", required=False)
    always = os.environ.get("ALWAYS_NOTIFY", "false").lower() == "true"

    print(f"Consultando ofertas mas baratas {ORIGIN}->{DESTINATION}...")
    raw = query_cheapest(token)
    print(f"Ofertas crudas: {len(raw)}")

    if not raw:
        print("Cache vacia esta corrida (puede pasar). No es un error.")
        if always:
            send_telegram("Rastreador Cancun: sin datos en la cache esta corrida.")
        return

    offers = [normalize(o, marker) for o in raw if o.get("price") is not None]
    offers.sort(key=lambda x: x["price"])

    cheapest = offers[0]
    target_offers = [o for o in offers if in_target(o)]
    best_target = target_offers[0] if target_offers else None

    print(f"Mas barata global: {cheapest['price']} {cheapest['currency']} "
          f"({cheapest['depart']})")
    if best_target:
        print(f"Mas barata EN VENTANA OBJETIVO: {best_target['price']} "
              f"{best_target['currency']} ({best_target['depart']})")
    else:
        print("Todavia no hay ofertas dentro de tu ventana objetivo (ene/feb 2027).")

    hist = load_history()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    hist["runs"].append({"ts": now, "cheapest": cheapest,
                         "best_target": best_target})
    hist["runs"] = hist["runs"][-200:]

    notify_parts = []

    # --- Novedad importante: primera aparicion en ventana objetivo ---
    first_target = best_target and not hist.get("target_seen")
    if first_target:
        hist["target_seen"] = True
        hist["min_target"] = best_target
        notify_parts.append(
            "*Ya hay vuelos en tus fechas objetivo (ene/feb 2027)!*\n"
            "Esto suele significar que tu viaje ya se puede reservar.\n\n"
            + fmt(best_target))
    elif best_target:
        prev = hist.get("min_target")
        if prev is None or best_target["price"] < prev["price"]:
            diff = (prev["price"] - best_target["price"]) if prev else 0
            hist["min_target"] = best_target
            msg = "*Nuevo minimo en tu ventana objetivo*\n\n" + fmt(best_target)
            if prev:
                msg += f"\n\nBajo {diff:.0f} {best_target['currency']} vs antes."
            notify_parts.append(msg)

    # --- Minimo global (tendencia de la ruta) ---
    prev_g = hist.get("min_global")
    new_global_min = prev_g is None or cheapest["price"] < prev_g["price"]
    if new_global_min:
        diff = (prev_g["price"] - cheapest["price"]) if prev_g else 0
        hist["min_global"] = cheapest
        msg = "*Nuevo minimo historico de la ruta*\n\n" + fmt(cheapest)
        if prev_g:
            msg += f"\n\nBajo {diff:.0f} {cheapest['currency']} vs antes."
        else:
            msg += "\n\nPrimer minimo registrado."
        notify_parts.append(msg)

    # --- Aviso ---
    header = "*Rastreador Cancun*\n\n"
    if notify_parts:
        body = "\n\n---\n\n".join(notify_parts)
        send_telegram(header + body)
        print("Aviso enviado.")
    elif always:
        top = "\n\n".join(fmt(o, short=True) for o in offers[:TOP_N])
        send_telegram(header + "Sin cambios. Top ofertas actuales:\n\n" + top)
        print("Aviso enviado (ALWAYS_NOTIFY).")
    else:
        print("Sin novedades para avisar.")

    save_history(hist)
    print("Listo.")


if __name__ == "__main__":
    main()
