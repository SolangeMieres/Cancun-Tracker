# Rastreador de precios ✈️ Cancún

Busca la tarifa ida y vuelta más barata a **Cancún (CUN)** dentro de una ventana
de fechas flexible, usando la API de **Duffel**. Corre solo en **GitHub Actions**
cada 12 h, guarda el historial de precios en el repo y te avisa por **Telegram**
cuando aparece un nuevo mínimo.

---

## Qué hace en cada corrida

1. Genera las combinaciones ida/vuelta dentro de tu ventana (± días) que cumplan
   el rango de noches.
2. Consulta Duffel para cada combinación, filtrando por cabina y escalas.
3. Encuentra la más barata de todas.
4. La compara con el mínimo histórico guardado en `price_history.json`.
5. Si bajó → te manda un Telegram. Si no → solo lo registra (sin molestar).

Todo lo configurable (orígenes, fechas, noches, cabina, escalas, frecuencia)
está arriba de `tracker.py` y en el `cron` de `.github/workflows/tracker.yml`.

---

## Setup — una sola vez (~15 min)

### 1. Token de Duffel
1. Creá cuenta en https://app.duffel.com/join
2. Menú **Developers → Access tokens → New token**.
3. Empezá con un token **Test** (gratis, datos de prueba). Para precios reales
   de venta necesitás activar el modo **Live** (Duffel pide unos datos de la
   empresa; para uso personal el test alcanza para validar que todo funciona).
4. Copiá el token (empieza con `duffel_test_` o `duffel_live_`).

> ⚠️ En modo **test** los precios son de un entorno de pruebas, no reales.
> Sirven para confirmar que el rastreador anda. Pasá a **live** cuando quieras
> precios de verdad.

### 2. Bot de Telegram (2 min)
1. En Telegram, hablá con **@BotFather** → `/newbot` → seguí los pasos.
   Te da un **BOT_TOKEN** (algo tipo `123456:ABC-DEF...`).
2. Conseguí tu **CHAT_ID**:
   - Mandale cualquier mensaje a tu bot nuevo.
   - Abrí en el navegador:
     `https://api.telegram.org/bot<TU_BOT_TOKEN>/getUpdates`
   - Buscá `"chat":{"id": XXXXXXX` → ese número es tu CHAT_ID.

### 3. Subir a GitHub
```bash
cd cancun-tracker
git init
git add .
git commit -m "Rastreador Cancún inicial"
gh repo create cancun-tracker --private --source=. --push
# (o creá el repo a mano en github.com y hacé git push)
```

### 4. Cargar los secrets en el repo
En GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
Creá estos tres:

| Nombre | Valor |
|--------|-------|
| `DUFFEL_TOKEN` | tu token de Duffel |
| `TELEGRAM_BOT_TOKEN` | el BOT_TOKEN de BotFather |
| `TELEGRAM_CHAT_ID` | tu CHAT_ID |

### 5. Probar
- Pestaña **Actions → Rastreador Cancún → Run workflow** (botón manual).
- Mirá el log. Si todo está bien, te llega el Telegram con la mejor tarifa.
- A partir de ahí corre solo cada 12 h.

---

## Ajustes rápidos

- **Cambiar fechas / noches / orígenes** → variables arriba de `tracker.py`.
- **Cambiar frecuencia** → línea `cron` en `.github/workflows/tracker.yml`
  (usá https://crontab.guru). Recordá que el cron va en **UTC**.
- **Aviso en cada corrida** (no solo cuando baja) → poné `ALWAYS_NOTIFY: "true"`
  en el workflow.
- **Cuidar la cuota de la API** → bajá `MAX_QUERIES` o achicá `FLEX_DAYS`.
  Cada combinación de fecha × origen es una llamada.

---

## Notas honestas

- **Fechas 2027**: las puse en `tracker.py` como 30/01/2027 y 13/02/2027 porque
  dijiste "fechas de referencia". Si eran de **2026**, cambiá `DEPART_REF` y
  `RETURN_REF`. (Duffel no vende fechas ya pasadas).
- Duffel devuelve ofertas de las aerolíneas/GDS que tiene conectadas; puede no
  incluir low-costs que venden solo por canal propio. Es muy bueno para tracking,
  pero no es "todo lo que existe en el mundo".
- El WhatsApp queda para una v2: requiere WhatsApp Business API (alta en Meta y
  plantillas aprobadas). Telegram cubre la necesidad hoy sin fricción.
