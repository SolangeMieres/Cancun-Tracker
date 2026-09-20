#!/usr/bin/env python3
"""
Rastreador de precios de vuelos a Cancún (CUN) - version Travelpayouts.

Usa la Aviasales Data API (Travelpayouts) para traer los tickets ida y vuelta
mas baratos de la ruta, para los meses de tu ventana de viaje. Encuentra el
mas barato que caiga dentro de tu rango de fechas/noches y avisa por Telegram
cuando aparece un nuevo minimo.

IMPORTANTE sobre los datos:
  Travelpayouts devuelve precios REALES pero desde una CACHE (basada en las
  busquedas de usuarios en Aviasales, guardadas ~7 dias). No es una busqueda
  en vivo. Sirve muy bien para trackear tendencia y detectar bajones; puede
  no tener datos si la ruta/fecha se busco poco. Los links de resultado ya
  llevan tu marker de afiliado.

Variables de entorno requeridas:
  TP_TOKEN            -> token de Travelpayouts (X-Access-Token)
  TP_MARKER           -> tu marker de afiliado (para los links). Opcional.
  TELEGRAM_BOT_TOKEN  -> token del bot de Telegram
  TELEGRAM_CHAT_ID    -> chat id donde recibir el aviso
"""

import os
import sys
import json
import datetime as dt
from pathlib import Path

import requests

# ----------------------------------------------------------------------
# CONFIGURACION - edita esto a gusto
# ----------------------------------------------------------------------
ORIGIN = "BUE"                    # Buenos Aires (agrupa EZE + AEP)
DESTINATION = "CUN"              # Cancun

# Ventana de fechas de referencia y flexibilidad
DEPART_REF = dt.date(2027, 1, 30)
RETURN_REF = dt.date(2027, 2, 13)
FLEX_DAYS = 3                    # +- dias alrededor de cada fecha de referencia

MIN_NIGHTS = 10                 # duracion minima de estadia aceptada
MAX_NIGHTS = 17                 # duracion maxima

CURRENCY = "usd"                # usd | ars | eur ...
DIRECT_ONLY = False             # True = solo vuelos directos

# La Data API consulta por MES. Derivamos los meses a pedir de la ventana.
# Endpoint: v3 prices_for_dates (cheapest tickets con filtros de fecha).
API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

HISTORY_FILE = Path(__file__).parent / "price_history.json"
AVIASALES_BASE = "https://www.aviasales.com"


# ----------------------------------------------------------------------
# UTILIDADES
# ----------------------------------------------------------------------

def env(name, required=True):
    val = os.environ.get(name)
    if required and not val:
        print(f"ERROR: falta la variable de entorno {name}", file=sys.stderr)
        sys.exit(1)
    return val


def months_in_window():
    """Meses (yyyy-mm-01) que toca la ventana de ida."""
    first = (DEPART_REF - dt.timedelta(days=FLEX_DAYS)).replace(day=1)
    last = (DEPART_REF + dt.timedelta(days=FLEX_DAYS)).replace(day=1)
    months = [first]
    if last != first:
        months.append(last)
    return months


def in_window(depart_str, return_str):
    """True si las fechas de ida/vuelta caen en la ventana y rango de noches."""
    try:
        d = dt.datetime.fromisoformat(depart_str.replace("Z", "+00:00")).date()
        r = dt.datetime.fromisoformat(return_str.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return False
    if abs((d - DEPART_REF).days) > FLEX_DAYS:
        return False
    nights = (r - d).days
    if not (MIN_NIGHTS <= nights <= MAX_NIGHTS):
        return False
    return True


# ----------------------------------------------------------------------
# TRAVELPAYOUTS
# ----------------------------------------------------------------------

def query_travelpayouts(token):
    """Trae ofertas ida y vuelta de la ruta para los meses de la ventana."""
    headers = {"X-Access-Token": token}
    offers = []
    for month in months_in_window():
        params = {
            "origin": ORIGIN,
            "destination": DESTINATION,
            "departure_at": month.strftime("%Y-%m"),
            "return_at": RETURN_REF.strftime("%Y-%m"),
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
            print(f"  red error mes {month:%Y-%m}: {e}", file=sys.stderr)
            continue
        if resp.status_code >= 400:
            print(f"  API {resp.status_code} mes {month:%Y-%m}: {resp.text[:200]}",
                  file=sys.stderr)
            continue
        body = resp.json()
        if not body.get("success", True):
            print(f"  API sin exito mes {month:%Y-%m}: {body}", file=sys.stderr)
            continue
        data = body.get("data", [])
        print(f"  mes {month:%Y-%m}: {len(data)} ofertas crudas")
        offers.extend(data)
    return offers


def pick_best(offers, marker):
    """Filtra por ventana/noches y devuelve la oferta mas barata."""
    best = None
    for o in offers:
        depart = o.get("departure_at", "")
        ret = o.get("return_at", "")
        if not ret:
            continue
        if not in_window(depart, ret):
            continue
        price = o.get("price")
        if price is None:
            continue
        if best is None or price < best["price"]:
            d = dt.datetime.fromisoformat(depart.replace("Z", "+00:00")).date()
            r = dt.datetime.fromisoformat(ret.replace("Z", "+00:00")).date()
            link = o.get("link", "")
            full_link = (AVIASALES_BASE + link) if link.startswith("/") else link
            if marker and full_link and "marker=" not in full_link:
                sep = "&" if "?" in full_link else "?"
                full_link = f"{full_link}{sep}marker={marker}"
            best = {
                "price": price,
                "currency": CURRENCY.upper(),
                "origin": o.get("origin", ORIGIN),
                "destination": o.get("destination", DESTINATION),
                "depart": d.isoformat(),
                "return": r.isoformat(),
                "nights": (r - d).days,
                "airline": o.get("airline", "?"),
                "transfers": o.get("transfers", "?"),
                "link": full_link,
            }
    return best


# ----------------------------------------------------------------------
# HISTORIAL Y NOTIFICACION
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
    txt = (f"{o['price']:.0f} {o['currency']} - {o['origin']}->{o['destination']}\n"
           f"Ida {o['depart']} - Vuelta {o['return']} ({o['nights']} noches)\n"
           f"{o['airline']} - {o['transfers']} escala(s)")
    if o.get("link"):
        txt += f"\n[Ver en Aviasales]({o['link']})"
    return txt


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    token = env("TP_TOKEN")
    marker = env("TP_MARKER", required=False)

    print(f"Consultando Travelpayouts {ORIGIN}->{DESTINATION} "
          f"para meses {[m.strftime('%Y-%m') for m in months_in_window()]}...")
    offers = query_travelpayouts(token)
    print(f"Total ofertas crudas: {len(offers)}")

    best = pick_best(offers, marker)

    if not best:
        msg = ("No hay ofertas dentro de tu ventana de fechas. "
               "La cache de Travelpayouts puede no tener datos recientes de "
               "esta ruta/fecha. Proba ampliar FLEX_DAYS o el rango de noches.")
        print(msg, file=sys.stderr)
        if os.environ.get("ALWAYS_NOTIFY", "false").lower() == "true":
            send_telegram("Rastreador Cancun: sin datos en la ventana esta corrida.")
        save_run_empty()
        return

    hist = load_history()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    hist["runs"].append({"ts": now, "best": best})
    hist["runs"] = hist["runs"][-200:]

    prev_min = hist.get("min_ever")
    is_new_min = prev_min is None or best["price"] < prev_min["price"]

    header = "*Cancun - mejor tarifa encontrada*\n\n"
    body = fmt_offer(best)

    if is_new_min:
        if prev_min:
            diff = prev_min["price"] - best["price"]
            body += f"\n\n*Bajo* {diff:.0f} {best['currency']} " \
                    f"vs minimo previo ({prev_min['price']:.0f})."
        else:
            body += "\n\nPrimer minimo registrado."
        hist["min_ever"] = best
        send_telegram(header + body)
        print("Aviso enviado (nuevo minimo).")
    else:
        if os.environ.get("ALWAYS_NOTIFY", "false").lower() == "true":
            body += f"\n\n(Minimo historico sigue en " \
                    f"{prev_min['price']:.0f} {prev_min['currency']}.)"
            send_telegram(header + body)
        print(f"Sin nuevo minimo (historico: {prev_min['price']:.0f}).")

    save_history(hist)
    print("Listo.")


def save_run_empty():
    hist = load_history()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    hist["runs"].append({"ts": now, "best": None})
    hist["runs"] = hist["runs"][-200:]
    save_history(hist)


if __name__ == "__main__":
    main()
