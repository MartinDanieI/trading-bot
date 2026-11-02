# 🤖 Bot escáner de trading — BTC/USD y Oro (XAU/USD) con alertas por WhatsApp

Cada hora descarga velas de **1h** y **15m**, analiza las **últimas 24 horas** de Bitcoin y Oro,
puntúa setups de **SUBIDA (LONG)** y **BAJADA (SHORT)** de 0 a 100 y, si alguno supera el umbral,
te manda un WhatsApp con entrada, Stop Loss y dos Take Profit.

> ⚠️ Herramienta técnica e informativa. **No es asesoría financiera.** Las señales pueden fallar;
> prueba primero en cuenta demo y usa siempre gestión de riesgo.

---

## 📁 Estructura

```
trading-bot/
├── firebase.json / .firebaserc / firestore.rules   # despliegue en Firebase
├── run_standalone.py                               # local y GitHub Actions
├── requirements-standalone.txt                     # dependencias sin Firebase
├── .github/workflows/hourly_scanner.yml            # cron horario gratis en GitHub
├── .env.example
└── functions/
    ├── main.py                    # Cloud Functions: hourly_scanner + manual_scan (HTTP)
    ├── requirements.txt
    └── scanner/
        ├── config.py              # variables de entorno
        ├── data_fetcher.py        # Kraken / Yahoo con reintentos + horario del oro
        ├── strategy.py            # EMA, RSI, MACD, ATR, puntaje y niveles
        ├── state_manager.py       # anti-spam (Firestore o JSON)
        ├── whatsapp_notifier.py   # Meta WhatsApp Cloud API
        └── runner.py              # orquesta un escaneo
```

## 🧠 Cómo decide

| Fuente de datos | Orden de intento |
|---|---|
| BTC/USD | Kraken (ccxt, público) → Yahoo `BTC-USD` |
| XAU/USD | Yahoo `GC=F` → Yahoo `XAUUSD=X` → Kraken `PAXG/USD` (proxy 1:1) |

Binance no se usa porque bloquea con HTTP 451 las IPs de Google Cloud y GitHub.
El oro se **omite** sábados, viernes desde las 17:00 y domingos antes de las 18:00 (hora de Nueva York).

**Puntaje LONG** = el mejor entre:
- **A · Rebote en soporte 24h** — precio en el 25 % inferior del rango (30) · RSI 28–42 subiendo o divergencia alcista (25) · cierre 1h sobre EMA9 (15) · cierre 15m sobre EMA9 (15) · histograma MACD mejorando (10) · RSI 15m subiendo (5).
- **B · Momentum / Breakout** — EMA20 > EMA50 (25) · cerca del máximo 24h (20) · volumen > 1.2× media 24h (20) · RSI 52–68 (15) · histograma MACD creciente (10) · 15m alineado (10).

**SHORT** es el espejo (rechazo en resistencia con RSI 60–78 cayendo, o breakdown con EMA20 < EMA50 y RSI 32–48).
Hay señal si el mejor puntaje ≥ `SCORE_THRESHOLD` (70) y supera al de la dirección contraria.

**Niveles:** Entrada = cierre de la última vela 1h cerrada · SL = 1.5 × ATR(14) · TP1 = 1.5 × riesgo · TP2 = 2.5 × riesgo.

**Mensaje diario "sigo vivo":** una vez al día, desde las `HEARTBEAT_HOUR` (8:00 Bogotá), llega
`✅ Bot activo` con las señales de las últimas 24 h y los precios. Si un día no llega, revisa GitHub → Actions.

**Reporte semanal:** cada domingo desde las 18:00 (`REPORT_WEEKDAY`, `REPORT_HOUR`) llega
`📊 REPORTE SEMANAL` con las señales de los últimos 7 días evaluadas contra el precio real:
ganadas, perdidas, % de acierto y resultado en R.

**Estrategia principal: Tendencia diaria** (`functions/scanner/daily_trend.py`). Reglas "Turtle" en velas
diarias: entrada al romper el máximo (mínimo) de N días, stop inicial de 2 ATR y salida cuando un día cierra
debajo del mínimo (encima del máximo) de M días. BTC: 20/10 con filtro SMA 200, compras y ventas. Oro: 55/20,
solo compras. Validada en `research/trend_research.py` con 12 años de BTC y 26 de Oro, comisiones incluidas.
El bot avisa la entrada, manda un 📌 SEGUIMIENTO diario mientras la posición está abierta y un 🔔 SALIDA al
cerrarla. La estrategia horaria anterior (Confluencia) perdió en el backtest con costos y quedó apagada; se
puede reactivar con `ACTIVE_STRATEGIES=tendencia_diaria,confluencia`.

**Zona de entrada:** justo antes de enviar una señal el bot consulta el precio en vivo. El mensaje trae
`📍 Zona de entrada` (entrada ± `ENTRY_ZONE_R` × riesgo, 0.3 por defecto) y el precio al enviar. Si el precio
ya salió de la zona, la señal no se envía (`LATE_SIGNALS=skip`) o llega como `⏰ SEÑAL TARDE · NO ENTRAR` (`mark`).

**Tipos de mensaje:** 🚨 SEÑAL DE ENTRADA · 💚 ESTADO DEL BOT (diario) · 📊 REPORTE SEMANAL · 🕐 RESUMEN HORARIO.

**Backtest:** `python backtest.py` repite la estrategia hora por hora sobre los últimos 58 días
(BTC y Oro) y muestra acierto y resultado por setup y por semana (`--threshold 80`, `--days 30`).
El detalle queda en `out/backtest_*.csv`.

**Anti-spam:** la misma dirección de un activo no se repite durante `COOLDOWN_HOURS` (4 h). Un cambio LONG ↔ SHORT sí se avisa.

**Modos (`ALERT_MODE`):** `SIGNALS_ONLY` (por defecto, solo entradas) o `HOURLY_SUMMARY` (resumen cada hora aunque no haya entrada).

---

## 1️⃣ Pruébalo en tu PC en 2 minutos

Necesitas Python 3.11 o superior.

```bash
cd C:\Users\marti\trading-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-standalone.txt
copy .env.example .env
python run_standalone.py --dry-run --mode HOURLY_SUMMARY
```

`--dry-run` descarga datos reales y muestra en la consola el mensaje que llegaría al WhatsApp, sin enviar nada.
Otras opciones:

```bash
python run_standalone.py --dry-run          # solo señales
python run_standalone.py --dry-run --json   # todo el análisis en JSON
python run_standalone.py --test-whatsapp    # envía un mensaje de prueba (requiere .env completo)
python run_standalone.py                    # escaneo real
```

---

## 2️⃣ Conecta tu canal de alertas

Hay tres canales. El bot elige solo (`ALERT_PROVIDER=auto`) según las credenciales que encuentre:
**Telegram** → **CallMeBot** (WhatsApp) → **Meta** (WhatsApp oficial).

### Telegram (gratis, oficial, 1 minuto)

1. En Telegram busca **@BotFather** (con la palomita azul) y envíale `/newbot`.
2. Ponle un nombre (ej. `Escáner Trading`) y un usuario que termine en `bot` (ej. `martin_escaner_bot`).
3. BotFather te da un token tipo `123456789:AAH...` → ponlo en `.env` como `TELEGRAM_BOT_TOKEN=`.
4. Abre tu bot nuevo (el enlace `t.me/...` que te da BotFather), pulsa **Iniciar** y escríbele "hola".
5. Ejecuta `python run_standalone.py --telegram-chat-id` y copia la línea `TELEGRAM_CHAT_ID=...` al `.env`.
   **Varios destinatarios:** cada persona abre el bot, pulsa **Iniciar** y escribe "hola"; repite el comando y pon
   todos los IDs separados por comas: `TELEGRAM_CHAT_ID=111111111,222222222`.
6. Prueba: `python run_standalone.py --test-whatsapp`

En GitHub Actions agrega `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` como *secrets*.
En Firebase: `firebase functions:secrets:set TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `functions/.env`.

### Opción rápida: CallMeBot (gratis, 2 minutos, sin Meta)

1. Abre <https://www.callmebot.com/blog/free-api-whatsapp-messages/> y copia el **número actual de CallMeBot** (cambia de vez en cuando).
2. Guárdalo en los contactos de tu celular.
3. Desde tu WhatsApp envíale exactamente: `I allow callmebot to send me messages`
4. En unos segundos te responde con tu **API key**. Ponla en `.env`:
   ```
   CALLMEBOT_APIKEY=123456
   WHATSAPP_TO=573001234567
   ```
5. Prueba: `python run_standalone.py --test-whatsapp`

Es un servicio de terceros no oficial, sin ventana de 24 h ni plantillas, ideal para alertas personales.
En GitHub Actions agrega `CALLMEBOT_APIKEY` y `WHATSAPP_TO` como *secrets*.

### Opción oficial: Meta WhatsApp Cloud API — `WHATSAPP_TOKEN` y `PHONE_NUMBER_ID`

1. Entra a <https://developers.facebook.com/> con tu cuenta de Facebook y pulsa **My Apps → Create App**.
2. Tipo de app: **Business** (o "Other → Business"). Ponle un nombre, p. ej. `Escaner Trading`.
3. En el panel de la app busca **WhatsApp → Set up**. Meta crea una cuenta de WhatsApp Business de prueba y un **número de prueba gratuito**.
4. Ve a **WhatsApp → Paso 1. Pruébalo** y pulsa **Solicitar número de prueba**. Allí verás (también en el bloque `curl`):
   - **Temporary access token** → es tu `WHATSAPP_TOKEN` (dura 24 h; sirve para probar).
   - **Phone number ID** → es tu `WHATSAPP_PHONE_NUMBER_ID` (un número largo, *no* es el teléfono).
5. En **"To"** pulsa **Manage phone number list**, agrega **tu número personal** y confírmalo con el código que te llega por WhatsApp. Ese número es tu `WHATSAPP_TO` (formato `573001234567`, sin `+`).
6. Pulsa **Send message** en esa misma página: te llegará la plantilla `hello_world`. **Respóndele cualquier cosa** ("hola"): eso abre la ventana de 24 h para recibir los mensajes completos del bot.

### Token permanente (para que no caduque)
1. Ve a <https://business.facebook.com/settings> → **Users → System users → Add** (rol *Admin*).
2. **Add assets** → elige tu app con control total.
3. **Generate new token** → selecciona la app, caducidad **Never** y los permisos `whatsapp_business_messaging` y `whatsapp_business_management`.
4. Ese token reemplaza al temporal en `WHATSAPP_TOKEN`.

### La ventana de 24 horas (importante)
WhatsApp solo deja enviar texto libre si **tú escribiste al número del bot en las últimas 24 h**.
Si la ventana está cerrada, el bot cae automáticamente a la plantilla `hello_world` como "timbre":
respóndela y las siguientes alertas llegan completas. Para recibir la señal completa siempre:
crea en **WhatsApp Manager → Message templates** una plantilla de categoría *Utility* con cuerpo
`Alerta del escáner: {{1}}`, espera su aprobación y configura:

```
WHATSAPP_TEMPLATE_NAME=nombre_de_tu_plantilla
WHATSAPP_TEMPLATE_LANG=es
WHATSAPP_TEMPLATE_HAS_BODY_PARAM=true
WHATSAPP_ALWAYS_TEMPLATE=true
```

> Meta regala 1.000 conversaciones de servicio al mes; las plantillas *Utility* pueden tener un costo
> pequeño según el país. Con `SIGNALS_ONLY` el volumen es bajo.

---

## 3️⃣ Opción A (recomendada, sin tarjeta): GitHub Actions

1. Crea un repositorio **privado** en GitHub y sube esta carpeta:
   ```bash
   git init
   git add .
   git commit -m "Bot escáner BTC/XAU"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/trading-bot.git
   git push -u origin main
   ```
   (`.env` está en `.gitignore`: tus claves no se suben.)
2. En el repo: **Settings → Secrets and variables → Actions → New repository secret** y crea:
   `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (Telegram), `WHATSAPP_TO` y `CALLMEBOT_APIKEY` (CallMeBot) o `WHATSAPP_TOKEN` y `WHATSAPP_PHONE_NUMBER_ID` (Meta).
3. (Opcional) en la pestaña **Variables**: `ALERT_MODE`, `SCORE_THRESHOLD`, `COOLDOWN_HOURS`, `WHATSAPP_TEMPLATE_NAME`, etc.
4. Pestaña **Actions → Escáner horario BTC / XAU → Run workflow** para probarlo en un clic
   (marca *dry_run* si solo quieres ver el mensaje en el log). Desde ahí corre solo cada hora.

### Ejecución puntual cada hora con cron-job.org (recomendado)

El horario propio de GitHub (`schedule`) se retrasa o se salta ejecuciones. Por eso el workflow
solo lo usa como respaldo cada 2 h, y el disparo principal lo hace **cron-job.org** (gratis):

1. **Token de GitHub:** <https://github.com/settings/personal-access-tokens/new> (fine-grained) →
   nombre `cron-trading-bot`, expiración 1 año, *Repository access → Only select repositories →
   trading-bot*, *Permissions → Repository → Actions: Read and write* → **Generate token**.
2. **Cuenta en <https://cron-job.org>** → **Create cronjob**:
   - URL: `https://api.github.com/repos/MartinDanieI/trading-bot/actions/workflows/hourly_scanner.yml/dispatches`
   - Schedule: *Every hour* en el minuto **2** (zona horaria America/Bogota).
   - Pestaña **Advanced**: método **POST**; cabeceras
     `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
     `X-GitHub-Api-Version: 2022-11-28`; body `{"ref":"main"}`.
3. **Test run**: debe responder **204** y aparecer una ejecución "Manually run" en Actions.

Notas:
- En repos privados GitHub da **2.000 minutos/mes gratis**; cada ejecución consume ~1 minuto → ~744 min/mes.
- El cron de GitHub puede retrasarse unos minutos en horas pico.
- El estado anti-spam se guarda en el caché de Actions entre ejecuciones.

## 3️⃣ Opción B: Firebase Cloud Functions

Firebase exige el plan **Blaze** (pago por uso, requiere tarjeta) para funciones programadas,
pero este bot queda dentro de la capa gratuita.

1. Instala Node.js y la CLI: `npm install -g firebase-tools`, luego `firebase login`.
2. Crea un proyecto en <https://console.firebase.google.com/>, actívale **Blaze** y crea una base **Firestore** (modo producción).
3. **Alerta de presupuesto en $0.01:** Google Cloud Console → **Billing → Budgets & alerts → Create budget** →
   alcance: tu proyecto, monto **0.01 USD**, alertas al 50 % / 90 % / 100 % por correo.
4. Pon el ID del proyecto en `.firebaserc` (reemplaza `TU-ID-DE-PROYECTO-FIREBASE`).
5. Prepara el entorno de la función (Firebase exige un `venv` dentro de `functions/`):
   ```bash
   cd functions
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   cd ..
   ```
   Edita `functions/.env` con tu `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_TO`, etc.
6. Guarda los secretos en Secret Manager:
   ```bash
   firebase functions:secrets:set WHATSAPP_TOKEN
   firebase functions:secrets:set TELEGRAM_BOT_TOKEN
   firebase functions:secrets:set MANUAL_TRIGGER_KEY
   ```
   Los tres deben existir para desplegar: al canal que no uses ponle cualquier valor (ej. `no`).
   (`MANUAL_TRIGGER_KEY` es una clave que inventas para proteger el endpoint de prueba.)
7. Despliega:
   ```bash
   firebase deploy --only functions,firestore:rules
   ```
8. Prueba manual (la URL aparece al final del deploy):
   ```bash
   curl -H "X-Trigger-Key: TU_CLAVE" "https://us-central1-TU-PROYECTO.cloudfunctions.net/manual_scan?dry_run=1&mode=HOURLY_SUMMARY"
   ```
9. Para no pagar almacenamiento de imágenes viejas:
   ```bash
   firebase functions:artifacts:setpolicy
   ```

---

## ⚙️ Variables de entorno

| Variable | Por defecto | Descripción |
|---|---|---|
| `ALERT_PROVIDER` | `auto` | `telegram`, `callmebot`, `meta` o `auto` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Bot de Telegram y tu chat |
| `CALLMEBOT_APIKEY` | — | Clave de CallMeBot |
| `WHATSAPP_TOKEN` | — | Token de la Cloud API |
| `WHATSAPP_PHONE_NUMBER_ID` | — | ID del número emisor |
| `WHATSAPP_TO` | — | Tu número (`57...`) |
| `ALERT_MODE` | `SIGNALS_ONLY` | o `HOURLY_SUMMARY` |
| `SCORE_THRESHOLD` | `70` | Puntaje mínimo para señal |
| `COOLDOWN_HOURS` | `4` | Horas sin repetir la misma señal |
| `HEARTBEAT_HOUR` | `8` | Hora local del mensaje diario (`-1` lo apaga) |
| `WHATSAPP_TEMPLATE_NAME` / `_LANG` | `hello_world` / `en_US` | Plantilla de respaldo |
| `WHATSAPP_TEMPLATE_HAS_BODY_PARAM` | `false` | La plantilla lleva `{{1}}` |
| `WHATSAPP_ALWAYS_TEMPLATE` | `false` | Enviar siempre por plantilla |
| `STATE_BACKEND` | `auto` | `firestore`, `json` o `auto` |
| `DRY_RUN` | `false` | No envía ni guarda estado |
| `REPORT_TIMEZONE` | `America/Bogota` | Zona horaria del mensaje |

Si faltan las credenciales de WhatsApp, el bot no falla: imprime el mensaje en la consola.
