# GameReleasesBot

[English](#english) · [Italiano](#italiano)

---

## English

A Discord bot that tracks upcoming video game releases using the
[IGDB API v4](https://api-docs.igdb.com/), keeps a few low-spam panels up to
date in a channel, announces games on release day and lets members subscribe to
personal reminders. Bot replies and embeds are in Italian.

### Table of contents

- [Features](#features)
- [Slash commands](#slash-commands)
- [Setup](#setup)
- [Local development (Windows)](#local-development-windows)
- [Deployment on Oracle Cloud Always Free](#deployment-on-oracle-cloud-always-free)

### Features

- **IGDB v4 panels** — an overview message with the top 10 upcoming games plus
  one rich card per game (cover, date, platforms, genres, store links). The
  panels refresh every 2 hours and are edited in place, and only when their
  content actually changed, so the channel never fills with new messages.
  Deleted messages are recreated and surplus ones are removed.
- **Interactive `/nextreleases`** — one card at a time with `◀️ Indietro` /
  `Avanti ▶️` pagination and a `X/Y` page counter.
- **`/calendar`** — a compact list of upcoming releases grouped by day.
- **`/game` with autocomplete** — type a name and pick a suggestion to get a
  detailed card with countdown, score, stores and trailer. Numeric titles such
  as "1942" are always searched by name.
- **Release-day alerts** — each server gets a single "🎮 Esce oggi!" message
  per game, in a dedicated alert channel and optionally mentioning a role.
- **Personal reminders** — the `🔔 Avvisami` button on every card toggles a
  reminder. On release day the bot sends a DM and, if DMs are closed, falls
  back to a mention in the channel where the button was pressed.
- **Per-server settings** — `/config` sets platform presets, the number of
  detailed cards, the alert channel and role, and the time zone used to decide
  what "today" is.
- **Store links** — Steam, Epic, GOG, PlayStation, Xbox, Nintendo, the official
  site or IGDB, in that order of preference.

### Slash commands

| Command | Who can use it | Description |
| --- | --- | --- |
| `/nextreleases [giorni]` | Everyone | Paginated cards of the most anticipated releases (1–90 days, default 30). |
| `/calendar [giorni]` | Everyone | Releases grouped by day in a single embed (1–90 days, default 30). |
| `/game <nome>` | Everyone | Detailed card for a game, with name autocomplete. |
| `/setchannel <canale>` | Manage Server | Sets the panel channel, moves existing panels and publishes them. |
| `/config [...]` | Manage Server | Shows or changes the server settings (see below). |
| `/checknow [ripristina]` | Manage Server | Bypasses the cache and refreshes panels and alerts now; `ripristina` re-posts the panels from scratch. |
| `/status` | Manage Server | Uptime, Discord latency, database counters and sync schedule. |

`/config` options, all optional (without any, it shows the current settings):

| Option | Meaning |
| --- | --- |
| `piattaforme` | `Tutte`, `PC + Console`, `Solo PC`, `PlayStation`, `Xbox`, `Nintendo`. |
| `numero_pannelli` | Number of detailed game cards, 1–10. |
| `canale_avvisi` | Channel for release-day alerts (defaults to the panel channel). |
| `ruolo_avvisi` | Role mentioned in release-day alerts. |
| `azzera_avvisi` | `True` removes the alert channel and role first, then applies any other option passed. |
| `fuso_orario` | IANA time zone, e.g. `Europe/Rome` (default). |

The management commands are hidden from members without **Manage Server** and
check the permission again when they run.

### Setup

#### 1. Twitch credentials (for IGDB)

1. Sign in to the [Twitch Developer Console](https://dev.twitch.tv/console)
   and enable two-factor authentication on your Twitch account (required to
   register applications).
2. Open **Applications → Register Your Application**.
3. Choose any name, set the OAuth redirect URL to `http://localhost`, pick a
   category (e.g. *Application Integration*) and the **Confidential** client
   type.
4. Open the application, copy the **Client ID** and generate a **Client
   Secret**.

#### 2. Discord application

1. Create an application in the
   [Discord Developer Portal](https://discord.com/developers/applications) and
   add a bot under **Bot**. Copy its token.
2. No privileged intents are needed.
3. Invite the bot with the `bot` and `applications.commands` scopes and these
   permissions in the channels it uses:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History

   `/setchannel` and `/config` tell you which of them are missing.

#### 3. Environment variables

Copy `.env.example` to `.env` and fill it in. The file is ignored by Git, and
secrets are never logged.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | — | Discord bot token. |
| `TWITCH_CLIENT_ID` | Yes | — | Twitch application Client ID. |
| `TWITCH_CLIENT_SECRET` | Yes | — | Twitch application Client Secret. |
| `DB_PATH` | No | `data/game_releases.db` | SQLite database file. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `TIMEZONE` | No | `Europe/Rome` | Default time zone for new servers. |

### Local development (Windows)

Requires Python 3.12 – 3.14.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env   # then fill in the credentials
```

Run the bot with `start_bot.bat` (it activates `.venv`, runs
`python -m gamereleases` and keeps the window open if the bot exits with an
error) or manually:

```powershell
.venv\Scripts\activate
python -m gamereleases
```

Run all four checks before committing:

```powershell
ruff check .
ruff format --check .
pyright
pytest
```

### Deployment on Oracle Cloud Always Free

The bot is light (it is capped at 160 MB in `docker-compose.yml`), so several
bots can share one Always Free VM. The container runs as UID/GID `1000`, which
is the default user on Ubuntu images, so no permission fixes are needed on the
bind-mounted data folder.

1. **Install Docker** on the VM (Ubuntu) following the
   [official instructions](https://docs.docker.com/engine/install/ubuntu/),
   then allow your user to run it:

   ```bash
   sudo usermod -aG docker $USER   # log out and back in afterwards
   ```

2. **Clone the repository** in its own folder and create the data directory:

   ```bash
   git clone <repository-url> ~/gamereleases
   cd ~/gamereleases
   mkdir -p data
   cp .env.example .env
   nano .env   # fill in DISCORD_TOKEN, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
   ```

3. **Build and start**:

   ```bash
   docker compose up -d --build
   ```

4. **Check the logs**:

   ```bash
   docker compose logs -f
   ```

5. **Update** to a new version:

   ```bash
   git pull
   docker compose up -d --build
   ```

   The database lives in `./data`, so it survives rebuilds.

**Running several bots on one VM:** give each bot its own folder, `.env` and
`data/` directory. Compose names the project after the folder, but
`container_name` and `image` are fixed in `docker-compose.yml`, so change them
in every additional copy (for instance `gamereleases-second`) to avoid clashes.
Each container is memory-capped, so the VM total stays predictable.

---

## Italiano

Un bot Discord che segue le prossime uscite dei videogiochi tramite le
[API IGDB v4](https://api-docs.igdb.com/), mantiene pochi pannelli sempre
aggiornati in un canale senza fare spam, annuncia i giochi nel giorno di uscita
e permette a ciascuno di ricevere promemoria personali. Risposte ed embed del
bot sono in italiano.

### Indice

- [Funzionalità](#funzionalità)
- [Comandi slash](#comandi-slash)
- [Configurazione](#configurazione)
- [Sviluppo locale (Windows)](#sviluppo-locale-windows)
- [Deploy su Oracle Cloud Always Free](#deploy-su-oracle-cloud-always-free)

### Funzionalità

- **Pannelli IGDB v4** — un messaggio riepilogativo con le 10 uscite più
  attese più una scheda dettagliata per gioco (copertina, data, piattaforme,
  generi, link agli store). I pannelli si aggiornano ogni 2 ore e vengono
  modificati sul posto, e solo se il contenuto è cambiato: il canale non si
  riempie di nuovi messaggi. I messaggi cancellati vengono ricreati e quelli in
  eccesso rimossi.
- **`/nextreleases` interattivo** — una scheda alla volta con paginazione
  `◀️ Indietro` / `Avanti ▶️` e contatore di pagina `X/Y`.
- **`/calendar`** — un elenco compatto delle prossime uscite raggruppate per
  giorno.
- **`/game` con autocompletamento** — scrivi un nome e scegli un suggerimento
  per ottenere una scheda con conto alla rovescia, voto, store e trailer. I
  titoli numerici come "1942" vengono sempre cercati per nome.
- **Avvisi nel giorno di uscita** — ogni server riceve un solo messaggio
  "🎮 Esce oggi!" per gioco, in un canale dedicato e con la menzione
  facoltativa di un ruolo.
- **Promemoria personali** — il pulsante `🔔 Avvisami` su ogni scheda attiva o
  disattiva un promemoria. Il giorno dell'uscita il bot invia un messaggio
  privato e, se i DM sono chiusi, ripiega su una menzione nel canale in cui è
  stato premuto il pulsante.
- **Impostazioni per server** — `/config` imposta le piattaforme, il numero di
  schede dettagliate, il canale e il ruolo degli avvisi e il fuso orario usato
  per stabilire quale sia "oggi".
- **Link agli store** — Steam, Epic, GOG, PlayStation, Xbox, Nintendo, sito
  ufficiale o IGDB, in quest'ordine di preferenza.

### Comandi slash

| Comando | Chi può usarlo | Descrizione |
| --- | --- | --- |
| `/nextreleases [giorni]` | Tutti | Schede paginate delle uscite più attese (1–90 giorni, predefinito 30). |
| `/calendar [giorni]` | Tutti | Uscite raggruppate per giorno in un unico embed (1–90 giorni, predefinito 30). |
| `/game <nome>` | Tutti | Scheda dettagliata di un gioco, con autocompletamento del nome. |
| `/setchannel <canale>` | Gestisci server | Imposta il canale dei pannelli, sposta quelli esistenti e li pubblica. |
| `/config [...]` | Gestisci server | Mostra o modifica le impostazioni del server (vedi sotto). |
| `/checknow [ripristina]` | Gestisci server | Ignora la cache e aggiorna subito pannelli e avvisi; `ripristina` ripubblica i pannelli da zero. |
| `/status` | Gestisci server | Uptime, latenza Discord, contatori del database e pianificazione dei controlli. |

Opzioni di `/config`, tutte facoltative (senza opzioni mostra le impostazioni
attuali):

| Opzione | Significato |
| --- | --- |
| `piattaforme` | `Tutte`, `PC + Console`, `Solo PC`, `PlayStation`, `Xbox`, `Nintendo`. |
| `numero_pannelli` | Numero di schede dettagliate, da 1 a 10. |
| `canale_avvisi` | Canale per gli avvisi di uscita (predefinito: il canale dei pannelli). |
| `ruolo_avvisi` | Ruolo menzionato negli avvisi di uscita. |
| `azzera_avvisi` | `True` rimuove prima canale e ruolo degli avvisi, poi applica le altre opzioni indicate. |
| `fuso_orario` | Fuso orario IANA, ad esempio `Europe/Rome` (predefinito). |

I comandi di gestione sono nascosti a chi non ha il permesso **Gestisci
server** e ne verificano il possesso a ogni esecuzione.

### Configurazione

#### 1. Credenziali Twitch (per IGDB)

1. Accedi alla [Twitch Developer Console](https://dev.twitch.tv/console) e
   attiva l'autenticazione a due fattori sull'account Twitch (necessaria per
   registrare applicazioni).
2. Apri **Applications → Register Your Application**.
3. Scegli un nome qualsiasi, imposta l'URL di reindirizzamento OAuth su
   `http://localhost`, seleziona una categoria (ad es. *Application
   Integration*) e il tipo di client **Confidential**.
4. Apri l'applicazione, copia il **Client ID** e genera un **Client Secret**.

#### 2. Applicazione Discord

1. Crea un'applicazione nel
   [Discord Developer Portal](https://discord.com/developers/applications) e
   aggiungi un bot nella sezione **Bot**. Copia il token.
2. Non servono intent privilegiati.
3. Invita il bot con gli scope `bot` e `applications.commands` e questi
   permessi nei canali che usa:
   - Visualizzare i canali (View Channels)
   - Inviare messaggi (Send Messages)
   - Incorporare link (Embed Links)
   - Leggere la cronologia dei messaggi (Read Message History)

   `/setchannel` e `/config` indicano quali permessi mancano.

#### 3. Variabili d'ambiente

Copia `.env.example` in `.env` e compilalo. Il file è ignorato da Git e i
segreti non vengono mai scritti nei log.

| Variabile | Obbligatoria | Predefinito | Descrizione |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Sì | — | Token del bot Discord. |
| `TWITCH_CLIENT_ID` | Sì | — | Client ID dell'applicazione Twitch. |
| `TWITCH_CLIENT_SECRET` | Sì | — | Client Secret dell'applicazione Twitch. |
| `DB_PATH` | No | `data/game_releases.db` | File del database SQLite. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` o `CRITICAL`. |
| `TIMEZONE` | No | `Europe/Rome` | Fuso orario predefinito per i nuovi server. |

### Sviluppo locale (Windows)

Richiede Python 3.12 – 3.14.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env   # poi inserisci le credenziali
```

Avvia il bot con `start_bot.bat` (attiva `.venv`, esegue
`python -m gamereleases` e lascia la finestra aperta se il bot termina con un
errore) oppure a mano:

```powershell
.venv\Scripts\activate
python -m gamereleases
```

Prima di ogni commit esegui tutti e quattro i controlli:

```powershell
ruff check .
ruff format --check .
pyright
pytest
```

### Deploy su Oracle Cloud Always Free

Il bot è leggero (in `docker-compose.yml` è limitato a 160 MB), quindi più bot
possono convivere sulla stessa VM Always Free. Il container gira con UID/GID
`1000`, l'utente predefinito delle immagini Ubuntu, quindi la cartella dei dati
montata dall'host non richiede correzioni di permessi.

1. **Installa Docker** sulla VM (Ubuntu) seguendo le
   [istruzioni ufficiali](https://docs.docker.com/engine/install/ubuntu/), poi
   consenti al tuo utente di usarlo:

   ```bash
   sudo usermod -aG docker $USER   # esci e rientra dopo il comando
   ```

2. **Clona il repository** in una cartella dedicata e crea la cartella dei dati:

   ```bash
   git clone <url-del-repository> ~/gamereleases
   cd ~/gamereleases
   mkdir -p data
   cp .env.example .env
   nano .env   # inserisci DISCORD_TOKEN, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
   ```

3. **Compila e avvia**:

   ```bash
   docker compose up -d --build
   ```

4. **Consulta i log**:

   ```bash
   docker compose logs -f
   ```

5. **Aggiorna** a una nuova versione:

   ```bash
   git pull
   docker compose up -d --build
   ```

   Il database vive in `./data`, quindi sopravvive alle ricostruzioni.

**Più bot sulla stessa VM:** usa una cartella, un `.env` e una cartella `data/`
distinti per ogni bot. Compose assegna il nome del progetto in base alla
cartella, ma `container_name` e `image` sono fissi in `docker-compose.yml`:
cambiali in ogni copia aggiuntiva (ad esempio `gamereleases-secondo`) per
evitare conflitti. Ogni container ha un limite di memoria, così il totale sulla
VM resta prevedibile.
