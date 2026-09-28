# Ozon Vinted Bot

Monitora nuovi annunci Vinted e invia notifiche su Telegram. Ogni esecuzione
esegue una sola scansione; GitHub Actions la avvia ogni 5 minuti. Lo stato degli
annunci processati è conservato su Upstash Redis.

La prima scansione registra gli annunci presenti **senza inviare notifiche**.
Dalle scansioni successive vengono notificati solo quelli nuovi che superano i filtri.
Gli errori dello script vengono segnalati sulla stessa chat Telegram, quando raggiungibile.

## Requisiti

- Python 3.12 o superiore e [uv](https://docs.astral.sh/uv/).
- Un bot Telegram e l'ID della chat destinataria. Per un gruppo, aggiungi il bot
  e consentigli di inviare messaggi e foto.
- Un database Redis su [Upstash](https://console.upstash.com/): copia l'URL REST
  HTTPS e il token con permessi di lettura e scrittura. Non impostare scadenze
  manuali sulle chiavi o eviction del database.

## Configurazione

| Variabile obbligatoria | Descrizione |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token del bot ottenuto da BotFather |
| `TELEGRAM_CHAT_ID` | ID della chat destinataria, incluso l'eventuale segno meno |
| `UPSTASH_REDIS_REST_URL` | Endpoint REST HTTPS del database |
| `UPSTASH_REDIS_REST_TOKEN` | Token REST con accesso in scrittura |

| Opzione | Default | Descrizione |
| --- | --- | --- |
| `VINTED_SEARCH_TEXT` | `ps5` | Testo della ricerca |
| `VINTED_PRICE_FROM` | `300` | Prezzo minimo in EUR |
| `VINTED_PRICE_TO` | `450` | Prezzo massimo in EUR |
| `VINTED_PER_PAGE` | `20` | Numero di risultati richiesti |
| `VINTED_REQUIRE_PHOTO` | `1` | Imposta `0` per accettare annunci senza foto |
| `VINTED_EXCLUDED_TERMS` | Elenco in `vinted_watch.py` | Termini separati da virgole; vuoto usa i default, `,` disabilita il filtro |
| `VINTED_REDIS_PREFIX` | `vinted:ps5` | Namespace dello stato; cambiarlo avvia una nuova inizializzazione |
| `VINTED_SEEN_RETENTION_DAYS` | `30` | Giorni di conservazione degli ID non più visibili |

## Avvio locale

```bash
cp .env.example .env
```

Compila `.env` con le tue credenziali e opzioni, poi esegui:

```bash
uv sync --locked
uv run --locked --env-file .env python vinted_watch.py
```

Non pubblicare `.env`: è escluso da Git. Se le variabili sono già esportate nel
terminale, puoi usare `bash run.sh`.

## GitHub Actions

1. Assicurati che `.github/workflows/vinted-watcher.yml` sia sul branch predefinito.
2. In **Settings → Secrets and variables → Actions**, aggiungi le quattro
   variabili obbligatorie come **Secrets** e le eventuali opzioni come **Variables**.
3. Apri **Actions → Vinted watcher → Run workflow** per una scansione manuale.
4. Controlla i log del job `scan`: il primo avvio deve registrare gli annunci senza
   notificarli. Ripetendo l'avvio, gli stessi annunci non devono essere inviati.

Il workflow pianificato usa `*/5 * * * *` e impedisce job concorrenti nello stesso
repository. Non avviare contemporaneamente copie locali con lo stesso namespace Redis.

## Test

I test non richiedono credenziali né contattano i servizi esterni.

```bash
uv run --locked python -m unittest -v
```

## Limiti operativi

- Si legge una pagina: più di `VINTED_PER_PAGE` nuovi risultati tra scansioni
  possono far perdere annunci. Vinted può bloccare le richieste dai runner GitHub.
- Gli ID vengono salvati dopo la conferma Telegram. Un'interruzione tra invio e
  salvataggio può causare un duplicato al tentativo successivo. Gli ID rimossi
  dopo il periodo di conservazione possono essere notificati se ricompaiono.
- Gli errori producono un exit code non zero e un tentativo di avviso Telegram per
  esecuzione. Se Telegram non risponde, consulta i log Actions. Errori di avvio
  del job o interruzioni del runner non possono essere notificati dallo script.
- La pianificazione GitHub può subire ritardi; nei repository pubblici viene
  disabilitata dopo 60 giorni senza attività. Consulta la
  [documentazione schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
- I runner standard sono gratuiti per i repository pubblici; per quelli privati
  valgono le quote del piano. Verifica i limiti di
  [GitHub Actions](https://docs.github.com/en/actions/concepts/billing-and-usage)
  e [Upstash](https://upstash.com/pricing/redis).
