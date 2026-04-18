# heli-tracker

Servizio Python che traccia una lista di elicotteri (pensato per elisoccorso
FVG e Veneto) via **OpenSky Network** con fallback **adsb.lol** e notifica su
**Telegram** i decolli e gli atterraggi. Le notifiche arrivano sia in un
**canale broadcast** (firehose) sia via **bot in DM** con filtri per mezzo
scelti dall'utente. Deploy su **Fly.io** nel free tier, stato persistente
su volume.

---

## Cosa fa

- Poll ogni ~45 s dello stato ADS-B dei mezzi configurati (OpenSky primario,
  adsb.lol come fallback per gli ICAO non visti — utile nelle zone alpine
  dove OpenSky ha copertura scarsa).
- Rileva transizioni `on_ground: true → false` (DECOLLO) e viceversa
  (ATTERRAGGIO) e notifica su Telegram.
- Il messaggio di atterraggio include **durata del volo, distanza coperta,
  quota e velocità massime**, e il **nome dell'ospedale/base** se il punto di
  landing cade entro il raggio configurato in `landing_sites.yaml` (altrimenti
  coordinate raw + link Google Maps).
- Al landing, se ha raccolto almeno 2 waypoint, **allega un PNG della rotta**
  disegnata su mappa OpenStreetMap.
- Se il mezzo esce dalla copertura ADS-B per 8 cicli consecutivi (~6 min),
  dichiara un **landing inferito** con wording esplicito ("segnale perso") per
  non spacciare un buco di segnale come un atterraggio confermato.
- Tutti i voli completati vengono registrati in **SQLite** su volume Fly per
  abilitare statistiche (`/stats`, `/last` dal bot).
- Un **alert** automatico avvisa l'admin se OpenSky è irresponsive per ≥ 10
  poll (~7.5 min); intanto il fallback adsb.lol garantisce la continuità.

---

## Architettura

```
main.py              orchestratore, spawn dei thread
detector.py          Helicopter, LandingSite, AdsbClient (OpenSky + adsb.lol),
                     geo helpers, formattazione messaggi, render_track_png,
                     process_update
storage.py           SQLite (WAL): subscribers, subscriptions, flights, bot_state
telegram_bot.py      TelegramClient, TelegramNotifier (broadcast),
                     TelegramPoller (getUpdates), CommandHandler
```

Due thread:

- **main**: poll loop OpenSky/adsb.lol → `process_update` → `broadcast_event`.
- **telegram-poller**: long-polling `getUpdates`, dispaccia i comandi bot.

---

## Prerequisiti

1. **Bot Telegram** creato da [@BotFather](https://t.me/BotFather) →
   ottieni il `TELEGRAM_BOT_TOKEN`.
2. **Canale Telegram** (privato o pubblico) in cui postare il firehose:
   aggiungi il bot come admin con permesso "Post Messages" e ricava il
   `chat_id` del canale (numero negativo che inizia con `-100…` per canali
   privati, oppure `@nomecanale` per canali pubblici).
3. **Account OpenSky** gratuito su [opensky-network.org](https://opensky-network.org/).
   Senza credenziali l'API anonima non basta.
4. **Account Fly.io** + `flyctl` installato ([guida](https://fly.io/docs/flyctl/install/)).
5. **Il tuo `user_id` Telegram** (serve per il comando admin `/mock`). Lo trovi
   scrivendo al tuo bot e guardando `chat.id` in `getUpdates`, oppure tramite
   [@userinfobot](https://t.me/userinfobot).

---

## Deploy su Fly.io

```bash
# 1. Login e creazione app
fly auth login
fly launch --no-deploy --copy-config --name <nome-app>
# (aggiorna `app = ...` in fly.toml se il nome viene rinominato)

# 2. Volume per lo storage (1 GB encrypted nel free allowance)
fly volumes create data --size 1 --region cdg

# 3. Segreti
fly secrets set \
  TELEGRAM_BOT_TOKEN="123456789:AA..." \
  TELEGRAM_CHAT_ID="-1001234567890" \
  TELEGRAM_ADMIN_USER_ID="<il_tuo_user_id>" \
  OPENSKY_USERNAME="tuo_user" \
  OPENSKY_PASSWORD="tua_password" \
  DB_PATH="/data/heli_tracker.db"

# 4. Deploy
fly deploy

# 5. Log in tempo reale
fly logs
```

Il primo boot inizializza il volume, crea lo schema SQLite e manda un
messaggio di benvenuto al canale.

### Note operative

- **Una sola machine**: `fly.toml` ha un `[[mounts]]` pinnato al volume `data`
  in region `cdg`, quindi non supporta HA multi-machine (il volume è
  region-locked e non replica). Dopo `fly deploy` la machine viene
  **ricreata** per attaccare il volume, ma il DB persiste.
- **Rigenerazione welcome**: ogni restart manda un nuovo messaggio di avvio al
  canale. È un comportamento voluto: se lo vedi, il worker è su.
- **Fly standby**: non usare `fly scale count N > 1` senza creare N volumi
  separati, il deploy fallirà.

---

## Configurazione runtime (env)

| Variabile                 | Default                  | Descrizione |
|---------------------------|--------------------------|-------------|
| `TELEGRAM_BOT_TOKEN`      | —                        | Token del bot (obbligatorio) |
| `TELEGRAM_CHAT_ID`        | —                        | Canale broadcast (opzionale: senza, solo il bot in DM) |
| `TELEGRAM_ADMIN_USER_ID`  | —                        | Tuo user_id Telegram; abilita `/mock` |
| `OPENSKY_USERNAME`        | —                        | User OpenSky (consigliato) |
| `OPENSKY_PASSWORD`        | —                        | Password OpenSky |
| `POLL_INTERVAL`           | `45`                     | Secondi tra poll. Non sotto 10. |
| `HELICOPTERS_FILE`        | `helicopters.yaml`       | Percorso config elicotteri |
| `LANDING_SITES_FILE`      | `landing_sites.yaml`     | Percorso lookup siti |
| `DB_PATH`                 | `heli_tracker.db`        | Path SQLite (in prod `/data/heli_tracker.db`) |
| `LOG_LEVEL`               | `INFO`                   | `DEBUG` per vedere ogni poll |

---

## Modello d'uso

### Canale (firehose)

Chi vuole tutte le notifiche, senza configurazione: iscrizione al canale via
invite link. Riceve ogni decollo/atterraggio di tutti i mezzi monitorati.
Read-only.

### Bot in DM (filtri per mezzo)

Chi vuole selezionare quali mezzi seguire: apre il bot, `/start`, poi
`/subscribe` per scegliere dalla inline keyboard.

**Comandi disponibili:**

| Comando            | Cosa fa |
|--------------------|---------|
| `/start`           | Attiva il bot, registra l'utente |
| `/subscribe`       | Menu interattivo per iscriversi/disiscriversi dai mezzi |
| `/list`            | Mostra le iscrizioni correnti |
| `/all`             | Iscrive a tutti i mezzi |
| `/none`            | Rimuove tutte le iscrizioni |
| `/stats [giorni]`  | Aggregato voli ultimi N giorni (default 7) |
| `/last`            | Ultimo volo per ciascun mezzo |
| `/stop`            | Cancella l'utente dal bot |
| `/help`            | Riepilogo comandi |
| `/mock` (admin)    | Simula decollo+atterraggio in DM — non spamma canale né altri iscritti |

Il canale e il bot sono indipendenti: puoi essere in entrambi senza
duplicazioni di ruolo (il canale è firehose, il bot è filtrato per mezzo).

---

## Configurare gli elicotteri

Modifica `helicopters.yaml`. Ogni voce:

```yaml
- registration: I-GOOO
  nickname: "Falco 1"
  base: "Campoformido (UD)"
  operator: "Elifriulia"
  icao24: "300816"    # opzionale; se vuoto viene risolto al boot via OpenSky
```

Se il lookup automatico fallisce cerca il codice ICAO24 su
[OpenSky Aircraft DB](https://opensky-network.org/aircraft-database-search),
[hexdb.io](https://hexdb.io), o in un URL Flightradar24 di volo passato, e
incollalo come `icao24`.

Dopo modifiche serve `fly deploy` per copiare il nuovo YAML nel container.

## Configurare i siti di atterraggio

`landing_sites.yaml` contiene una lista di ospedali HEMS / basi con
coordinate e raggio di match:

```yaml
sites:
  - name: "Ospedale Cattinara"
    city: "Trieste"
    lat: 45.6285
    lon: 13.7960
    radius_m: 500
```

Al landing lo script cerca il sito più vicino entro il suo `radius_m`. Le
coordinate iniziali sono stime: quando vedi il primo atterraggio reale in un
sito noto, raffina `lat`/`lon` con la media dei punti osservati e tieni
`radius_m` fra 300 e 600 m.

---

## Operazioni comuni

```bash
# Log live
fly logs

# Shell nel container
fly ssh console

# Stato macchine
fly status

# Riavvio
fly machine restart <id>

# Ispezionare il DB (sqlite3 CLI non è nell'image slim, ma c'è Python):
fly ssh console -C 'python -c "
import sqlite3
c = sqlite3.connect(\"/data/heli_tracker.db\")
print(c.execute(\"SELECT helicopter_key, takeoff_ts, duration_s, distance_km, landing_site, inferred FROM flights ORDER BY takeoff_ts DESC LIMIT 10\").fetchall())
"'
```

### Lanciare un mock da Telegram

Scrivi `/mock` al bot dal tuo account admin: arrivano 2 messaggi `[TEST]` in
DM (decollo + atterraggio a Cattinara) senza spammare canale o iscritti.

### Lanciare un mock da shell

```bash
ENV_DUMP=$(fly ssh console -C env 2>&1)
eval "$(printf '%s\n' "$ENV_DUMP" | grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' | sed 's/^/export /')"
.venv/bin/python scripts/mock_test.py
```

Manda i messaggi al `TELEGRAM_CHAT_ID` (di default il canale, con prefisso
`[TEST]` sul nickname per distinguerli dai decolli veri).

---

## Limiti noti

- **Copertura ADS-B in montagna**. In Cadore, Carnia e Alpi Giulie a bassa
  quota può mancare segnale per 10+ min. Con soglia di 8 cicli (~6 min)
  diamo un "landing inferito" esplicito che chiarisce l'incertezza; il
  fallback adsb.lol (rete di ricevitori diversa) recupera parte dei mezzi
  non visti da OpenSky.
- **Transponder in hover sul mare / piattaforme**. Il bit `on_ground` dei
  Mode-S sugli elicotteri è talora legato al collective / regime rotore e
  può flickerare durante hover: possibili false notifiche di atterraggio
  con posizione sopra il mare (tipicamente su missioni SAR marittime).
- **Latenza**: con `POLL_INTERVAL=45` la notifica di decollo arriva entro
  ~45-90 s dall'evento reale.
- **Rate limit OpenSky**: ~4000 richieste/giorno con account base → a 45 s
  di intervallo siamo a ~1920/giorno. Ampiamente entro i limiti.
- **Rate limit Telegram**: 30 msg/s globali del bot, 1 msg/s per chat.
  Rilevante solo sopra i 30 iscritti simultanei al broadcast.
- **Volume region-locked**: un guasto hardware sulla machine `cdg` richiede
  recovery manuale. Accettabile per il caso d'uso hobby.

---

## Struttura del progetto

```
heli-tracker/
├── main.py              # Config, orchestratore, spawn thread
├── detector.py          # Helicopter, AdsbClient, process_update, rendering
├── storage.py           # SQLite wrapper (WAL, thread-safe)
├── telegram_bot.py      # Client, Notifier, Poller, CommandHandler
├── helicopters.yaml     # Lista elicotteri monitorati
├── landing_sites.yaml   # Ospedali / basi per il lookup
├── requirements.txt
├── Dockerfile
├── fly.toml             # include [[mounts]] per /data
├── scripts/
│   └── mock_test.py     # smoke test locale via canale
├── .gitignore
└── README.md
```

---

## Possibili miglioramenti

Lista di estensioni valutate e rimandate. Le prime tre sono già state
implementate nelle ultime iterazioni e sono marcate con ✅.

### Qualità dei messaggi

- ✅ Durata / distanza / quota max / velocità max nell'atterraggio.
- ✅ Lookup ospedale/base via `landing_sites.yaml`.
- ✅ PNG della rotta sovrapposta a OpenStreetMap al landing.
- **Classificazione tipo missione**. Euristica su callsign, orario e pattern
  di volo (hover prolungato = recupero, cruise rettilineo = trasferimento,
  volo notturno = HEMS notturno abilitato).

### Affidabilità dei dati

- ✅ Fallback adsb.lol sui mezzi non visti da OpenSky.
- ✅ Alert all'admin dopo N cicli consecutivi di OpenSky down.
- **Health check esterno** (UptimeRobot / healthchecks.io) per allerta
  indipendente su worker muto.

### Storico e distribuzione

- ✅ Storico voli in SQLite (`flights`) + comandi `/stats`, `/last`.
- ✅ Bot multi-utente con filtri per mezzo (`/subscribe`).
- **Dashboard web**: Flask/FastAPI + Leaflet per vedere mezzi e storico voli
  su mappa. Richiede esporre un `[http_service]` sul container.

### Notifier alternativi

- Discord / ntfy.sh / Signal / email come destinazioni affiancabili o
  alternative a Telegram. Astraibile dietro un `Notifier` protocol con
  più implementazioni.

### Filtri

- **Fascia oraria** per silenziare le notifiche fuori orario (es. mute fra
  01:00 e 06:00 se non vuoi essere svegliato).
