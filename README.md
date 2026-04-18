# heli-tracker

Servizio che traccia una lista di elicotteri (pensato per elisoccorso FVG e Veneto) via **OpenSky Network** e invia una notifica **Telegram** quando decollano o atterrano. Pronto per il deploy su **Fly.io** nel free tier.

## Cosa fa

- Poll a intervalli regolari dello stato ADS-B degli elicotteri configurati.
- Rileva transizioni `on_ground: true → false` (DECOLLO) e viceversa (ATTERRAGGIO).
- Invia un messaggio Telegram con callsign, posizione, link a Google Maps, ADS-B Exchange e Flightradar24 per il tracking live.
- Resolve automaticamente gli ICAO24 hex a partire dalle marche (es. `I-GOOO`) via OpenSky.

## Prerequisiti

1. **Account Telegram** e un bot creato da [@BotFather](https://t.me/BotFather). Ottieni il `TELEGRAM_BOT_TOKEN`.
2. **chat_id**: scrivi un messaggio al bot, poi apri `https://api.telegram.org/bot<TOKEN>/getUpdates` e copia il campo `chat.id`. (Per un canale: aggiungi il bot come admin.)
3. **Account OpenSky** (gratuito) su [opensky-network.org](https://opensky-network.org/) per aumentare il rate limit. Senza credenziali l'API anonima è troppo limitata.
4. **Account Fly.io** + `flyctl` installato ([guida](https://fly.io/docs/flyctl/install/)).

## Test locale rapido

```bash
cd heli-tracker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# modifica .env con i tuoi token
python main.py
```

All'avvio ti arriverà un messaggio Telegram "heli-tracker avviato" con la lista dei mezzi monitorati.

## Deploy su Fly.io

```bash
# 1. Login
fly auth login

# 2. Crea l'app (senza deployare subito). Può chiederti un nome diverso
#    se "heli-tracker" è preso; in quel caso aggiorna `app = ...` in fly.toml.
fly launch --no-deploy --copy-config --name heli-tracker

# 3. Imposta i segreti (MAI committare .env)
fly secrets set \
  TELEGRAM_BOT_TOKEN="123456789:AA..." \
  TELEGRAM_CHAT_ID="123456789" \
  OPENSKY_USERNAME="tuo_user" \
  OPENSKY_PASSWORD="tua_password"

# 4. Deploy
fly deploy

# 5. Log in tempo reale
fly logs
```

### Free tier notes

Fly.io dà 3 macchine `shared-cpu-1x` 256MB sempre accese nel Free Allowance personale. Questo worker ne usa una. Restando in Europa (`primary_region = "cdg"`) la latenza verso OpenSky (infrastruttura in Germania) è minima.

> **Nota**: Fly al momento richiede l'aggiunta di una carta di credito in fase di signup per verifica antifrode, ma non addebita nulla se resti nel Free Allowance.

## Configurare gli elicotteri

Modifica `helicopters.yaml`. Ogni voce accetta:

```yaml
- registration: I-GOOO           # marche ENAC
  nickname: "Falco 1"            # nome visibile nelle notifiche
  base: "Campoformido (UD)"
  operator: "Elifriulia"
  icao24: ""                     # opzionale: se vuoto, risolto all'avvio
```

Se la risoluzione automatica fallisce (il DB di OpenSky non sempre è aggiornato per gli elicotteri):
- cerca il codice su [OpenSky Aircraft DB](https://opensky-network.org/aircraft-database-search)
- oppure su [hexdb.io](https://hexdb.io)
- oppure su Flightradar24 nell'URL di un volo passato
- e incollalo nel campo `icao24`.

Dopo modifiche a `helicopters.yaml`, serve un `fly deploy` per aggiornare il contenitore (il file viene copiato al build-time). In alternativa, monta il file come volume.

## Configurazione runtime

Tutte le variabili d'ambiente sono descritte in `.env.example`. Le principali:

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | Token del bot |
| `TELEGRAM_CHAT_ID` | — | Destinatario delle notifiche |
| `OPENSKY_USERNAME` | — | User OpenSky (consigliato) |
| `OPENSKY_PASSWORD` | — | Password OpenSky |
| `POLL_INTERVAL` | `45` | Secondi tra poll. Non scendere sotto 10s. |
| `HELICOPTERS_FILE` | `helicopters.yaml` | Percorso del file config |
| `LOG_LEVEL` | `INFO` | `DEBUG` per vedere tutti i poll |

## Limiti noti

- **Copertura ADS-B in montagna**: in Cadore, Carnia e Alpi Giulie a bassa quota spesso non c'è ricezione. Il volo può "sparire" per diversi minuti e poi ricomparire. Lo script usa `OFFLINE_CYCLES_FOR_LANDED = 4` per evitare falsi atterraggi: solo dopo 4 poll consecutivi senza contatto notifica l'atterraggio.
- **Trasponder spento / Mode C**: alcuni elicotteri di Stato o militari non appaiono su reti pubbliche. Gli HEMS regionali sono normalmente ADS-B attivi.
- **Latenza**: con `POLL_INTERVAL=45` la notifica arriva entro ~45-90 secondi dal decollo effettivo. Per latenza minore, abbassa il valore ma attento alla quota OpenSky.
- **Rate limit**: con account OpenSky base hai ~4000 richieste/giorno. A 45s di intervallo sono ~1920/giorno, ampiamente sufficienti.

## Struttura del progetto

```
heli-tracker/
├── main.py              # poller + logica decollo/atterraggio + Telegram
├── helicopters.yaml     # lista elicotteri da tracciare
├── requirements.txt
├── Dockerfile
├── fly.toml             # config Fly.io
├── .dockerignore
├── .env.example         # template variabili d'ambiente
└── README.md
```

## Possibili miglioramenti

Lista di estensioni valutate e rimandate, raggruppate per area. Ognuna è
indipendente dalle altre; ordina per rapporto valore/sforzo in base al tuo
contesto.

### Qualità dei messaggi

- **Traccia del volo come immagine**. Accumulare i waypoint osservati durante
  il volo e allegare al messaggio di atterraggio un PNG generato via
  `py-staticmaps` (o simile) con la rotta sovrapposta su basemap. Effetto visivo
  forte, nessuna persistenza richiesta (solo RAM).
- **Classificazione tipo missione**. Euristica su callsign, orario e pattern di
  volo (hover prolungato = recupero, cruise rettilineo punto-punto =
  trasferimento, volo notturno = HEMS notturno abilitato). Richiede aggregazione
  di più poll.

### Affidabilità dei dati

- **Fallback adsb.lol / airplanes.live**. Integrare un secondo provider ADS-B
  no-filter oltre a OpenSky (REST, no-auth, zero costi). Migliora la copertura
  nelle zone montuose dove i ricevitori OpenSky scarseggiano (Carnia, Cadore,
  Dolomiti). Implementazione: `OpenSkyClient` diventa `AdsbClient` che chiama
  OpenSky e poi adsb.lol per gli ICAO non visti, merge con priorità al
  `last_contact` più fresco.
- **Backoff + alert su fallimenti OpenSky**. Oggi un'outage prolungata del
  provider (o della rete Fly) passa silenziosa. Aggiungere exponential backoff
  su errori consecutivi e una notifica Telegram dopo N cicli falliti.
- **Health check esterno**. Worker muto da >2h = possibile deadlock silenzioso
  non rilevabile dall'interno. Un cron su un servizio esterno
  (UptimeRobot, healthchecks.io) che pinga un endpoint HTTP minimale esposto
  dal container fornirebbe allerta indipendente.

### Storico e statistiche

- **Persistenza SQLite su volume Fly**. Salvare ogni volo
  `(takeoff_ts, landing_ts, callsign, distanza, sito, durata)` per abilitare
  statistiche (voli/giorno, tempo totale di volo per mezzo, destinazioni più
  frequenti). Richiede `fly volumes create` e un `[[mounts]]` in `fly.toml`.
  Abilita anche un futuro comando `/stats` e una dashboard.
- **Dashboard web**. Flask/FastAPI + Leaflet per vedere mezzi su mappa live,
  storico voli e filtri. Richiede esporre un `[http_service]` sul container
  (oggi è worker puro).

### Distribuzione e multi-utente

- **Canale pubblico** in aggiunta a quello privato. Secondo target Telegram,
  eventualmente filtrato sui mezzi "pubblici". Solo config, niente codice.
- **Bot self-service con preferenze per utente**. Refactor a multi-tenant:
  comando `/subscribe` con inline keyboard per selezionare quali elicotteri
  seguire, persistenza in SQLite (`subscribers` + `subscriptions`), secondo
  thread per `getUpdates` sui messaggi in ingresso. Consente ad esempio di
  iscriversi solo ai mezzi di una provincia, o solo con destinazione un
  ospedale specifico. ~250 righe di codice nuove, raddoppia la superficie
  di test (gestione `/stop`, blocco del bot → 403 in broadcast, concorrenza
  su SQLite in WAL).

### Canali di notifica alternativi

- **Discord / ntfy.sh / Signal / email** come destinazioni affiancabili o
  alternative a Telegram. Astraibile dietro un'interfaccia `Notifier` con
  più implementazioni.

### Filtri e geofencing

- **Fascia oraria** per silenziare le notifiche fuori orario (es. mute fra
  01:00 e 06:00 se non vuoi essere svegliato).
- **Geofencing**. Notifica quando un mezzo entra/esce da un'area definita
  (es. "Falco 2 è appena entrato in Carnia"). Riusa la logica haversine già
  presente per il lookup dei siti di atterraggio.
