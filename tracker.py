#!/usr/bin/env python3
"""
Rastreador de precios de vuelos a Cancún (CUN).
Consulta la API de Duffel para varias combinaciones de fechas dentro de una
ventana flexible, encuentra la tarifa más baja y avisa por Telegram cuando
baja respecto al mínimo histórico guardado.

Variables de entorno requeridas:
  DUFFEL_TOKEN        -> token de acceso de Duffel (test o live)
  TELEGRAM_BOT_TOKEN  -> token del bot de Telegram
  TELEGRAM_CHAT_ID    -> chat id donde recibir el aviso

Config editable abajo (ORIGENS, ventana de fechas, noches, etc.).
"""

import os
import sys
import json
import time
import datetime as dt
from pathlib import Path
from itertools import product

import requests

# ----------------------------------------------------------------------
# CONFIGURACIÓN — editá esto a gusto
# ----------------------------------------------------------------------
ORIGINS = ["EZE", "AEP"]          # aeropuertos de salida a probar
DESTINATION = "CUN"               # Cancún

# Fechas de referencia y flexibilidad (± días alrededor de cada una)
DEPART_REF = dt.date(2027, 1, 30)  # ida de referencia
RETURN_REF = dt.date(2027, 2, 13)  # vuelta de referencia
FLEX_DAYS = 3                      # ± días alrededor de cada fecha

# Duración de estadía a considerar (en noches). Se filtran las combinaciones
# ida/vuelta cuya duración caiga en este rango.
MIN_NIGHTS = 10
MAX_NIGHTS = 17

CABIN = "economy"                 # economy | premium_economy | business | first
ADULTS = 1                        # cantidad de pasajeros adultos
MAX_CONNECTIONS = 1               # 0 = solo directos; 1 = hasta 1 escala

CURRENCY_HINT = "USD"             # solo informativo para el mensaje

# Límite de combinaciones de fechas a consultar por corrida (protege tu cuota).
MAX_QUERIES = 40
SLEEP_BETWEEN = 0.6               # segundos entre llamadas para no golpear rate limit

HISTORY_FILE = Path(__file__).parent / "price_history.json"

DUFFEL_URL = "https://api.duffel.com/air/offer_requests"
DUFFEL_VERSION = "v2"

# ----------------------------------------------------------------------
# UTILIDADES
# ----------------------------------------------------------------------

def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: falta la variable de entorno {name}", file=sys.stderr)
        sys.exit(1)
    return val


def date_window(ref: dt.date, flex: int):
    return [ref + dt.timedelta(days=d) for d in range(-flex, flex + 1)]


def build_date_pairs():
    """Genera pares (ida, vuelta) válidos según ventana y noches."""
    departs = date_window(DEPART_REF, FLEX_DAYS)
    returns = date_window(RETURN_REF, FLEX_DAYS)
    pairs = []
    for d, r in product(departs, returns):
        nights = (r - d).days
        if MIN_NIGHTS <= nights <= MAX_NIGHTS:
            pairs.append((d, r, nights))
    # ordena por duración cercana a la referencia para priorizar lo relevante
    ref_nights = (RETURN_REF - DEPART_REF).days
    pairs.sort(key=lambda p: abs(p[2] - ref_nights))
    return pairs[:MAX_QUERIES]


# ----------------------------------------------------------------------
# DUFFEL
# ----------------------------------------------------------------------

def query_duffel(token, origin, depart, ret):
    """Una búsqueda ida y vuelta. Devuelve (precio_min, moneda, oferta) o None."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": DUFFEL_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "data": {
            "slices": [
                {"origin": origin, "destination": DESTINATION,
                 "departure_date": depart.isoformat()},
                {"origin": DESTINATION, "destination": origin,
                 "departure_date": ret.isoformat()},
            ],
            "passengers": [{"type": "adult"} for _ in range(ADULTS)],
            "cabin_class": CABIN,
        }
    }
    # return_offers=true hace que la respuesta ya incluya las ofertas
    params = {"return_offers": "true", "supplier_timeout": "15000"}
    try:
        resp = requests.post(DUFFEL_URL, headers=headers, params=params,
                             json=payload, timeout=40)
    except requests.RequestException as e:
        print(f"  red error {origin} {depart}->{ret}: {e}", file=sys.stderr)
        return None

    if resp.status_code >= 400:
        print(f"  API {resp.status_code} {origin} {depart}->{ret}: {resp.text[:200]}",
              file=sys.stderr)
        return None

    offers = resp.json().get("data", {}).get("offers", [])
    best = None
    for off in offers:
        # filtro de escalas
        max_stops = max(len(s.get("segments", [])) - 1 for s in off.get("slices", []))
        if max_stops > MAX_CONNECTIONS:
            continue
        amount = float(off["total_amount"])
        if best is None or amount < best["amount"]:
            best = {
                "amount": amount,
                "currency": off["total_currency"],
                "origin": origin,
                "depart": depart.isoformat(),
                "return": ret.isoformat(),
                "nights": (ret - depart).days,
                "airline": off.get("owner", {}).get("name", "?"),
                "stops": max_stops,
                "offer_id": off["id"],
            }
    return best


# ----------------------------------------------------------------------
# HISTORIAL Y NOTIFICACIÓN
# ----------------------------------------------------------------------

def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"runs": [], "min_ever": None}


def save_history(hist):
    HISTORY_FILE.write_text(json.dumps(hist, indent=2, ensure_ascii=False))


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


def fmt_offer(o):
    return (f"{o['amount']:.0f} {o['currency']} — {o['origin']}→{DESTINATION}\n"
            f"Ida {o['depart']} · Vuelta {o['return']} ({o['nights']} noches)\n"
            f"{o['airline']} · {o['stops']} escala(s)")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    token = env("DUFFEL_TOKEN")
    pairs = build_date_pairs()
    print(f"Consultando {len(pairs)} combinaciones de fechas x {len(ORIGINS)} orígenes...")

    best_overall = None
    checked = 0
    for origin in ORIGINS:
        for depart, ret, nights in pairs:
            checked += 1
            res = query_duffel(token, origin, depart, ret)
            if res:
                mark = ""
                if best_overall is None or res["amount"] < best_overall["amount"]:
                    best_overall = res
                    mark = "  <-- nuevo mínimo"
                print(f"  [{checked}] {origin} {depart}->{ret} "
                      f"{res['amount']:.0f} {res['currency']}{mark}")
            time.sleep(SLEEP_BETWEEN)

    if not best_overall:
        print("No se obtuvieron ofertas. Revisá token/config.", file=sys.stderr)
        # aviso opcional de fallo
        # send_telegram("⚠️ Rastreador Cancún: no obtuve ofertas en esta corrida.")
        sys.exit(1)

    # historial
    hist = load_history()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    hist["runs"].append({"ts": now, "best": best_overall})
    hist["runs"] = hist["runs"][-200:]  # recorta historial

    prev_min = hist.get("min_ever")
    is_new_min = prev_min is None or best_overall["amount"] < prev_min["amount"]

    header = "✈️ *Cancún — mejor tarifa encontrada*\n\n"
    body = fmt_offer(best_overall)

    if is_new_min:
        if prev_min:
            diff = prev_min["amount"] - best_overall["amount"]
            body += f"\n\n📉 *Bajó* {diff:.0f} {best_overall['currency']} " \
                    f"vs mínimo previo ({prev_min['amount']:.0f})."
        else:
            body += "\n\n🎯 Primer mínimo registrado."
        hist["min_ever"] = best_overall
        send_telegram(header + body)
        print("Aviso enviado (nuevo mínimo).")
    else:
        # no bajó: guardamos pero no molestamos (cambiá a True para avisar siempre)
        ALWAYS_NOTIFY = os.environ.get("ALWAYS_NOTIFY", "false").lower() == "true"
        if ALWAYS_NOTIFY:
            body += f"\n\n(Mínimo histórico sigue en " \
                    f"{prev_min['amount']:.0f} {prev_min['currency']}.)"
            send_telegram(header + body)
        print(f"Sin nuevo mínimo (histórico: {prev_min['amount']:.0f}).")

    save_history(hist)
    print("Listo.")


if __name__ == "__main__":
    main()
