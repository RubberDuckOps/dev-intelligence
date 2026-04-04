# Dev-Intelligence — SDK generator per le API della Pubblica Amministrazione

[![CI](https://github.com/RubberDuckOps/dev-intelligence/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/RubberDuckOps/dev-intelligence/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.135-009688.svg)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](https://www.docker.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: Stable](https://img.shields.io/badge/status-stable-brightgreen.svg)](publiccode.yml)

![Screenshot dell'interfaccia principale di Dev-Intelligence con campo di ricerca software PA e schede risultato con stato OpenAPI.](/.github/assets/dev-intelligence-home.png)

Esplora il catalogo Developers Italia, valida le specifiche OpenAPI e genera client SDK in Python, Go, JavaScript, Rust e Java tramite un LLM completamente locale.

> Questo progetto non è un prodotto ufficiale di Developers Italia, AgID o di alcuna Pubblica Amministrazione. È uno strumento indipendente della community basato sulle [API pubbliche di Developers Italia](https://developers.italia.it/).

---

## Quick start

**Prerequisiti:** Docker e Docker Compose.

```bash
git clone https://github.com/RubberDuckOps/dev-intelligence.git
cd dev-intelligence
cp .env.example .env   # opzionale: personalizza le variabili
docker-compose up --build
```

L'app è disponibile su **http://localhost:8000**.

Al primo avvio il modello GGUF (~491 MB) viene scaricato automaticamente da HuggingFace Hub in `./models/`. I successivi avvii usano la copia locale.

---

## Cosa fa

Dev-Intelligence automatizza l'integrazione con le API della PA italiana in tre passi:

1. **Cerca** — digita un termine (`SPID`, `PagoPA`, `fattura`) e ottieni tutti i software PA corrispondenti dal [catalogo Developers Italia](https://developers.italia.it/), con nome, descrizione, linguaggio e link al repository.
2. **Valida** — per ogni software trovato, Dev-Intelligence verifica automaticamente se la specifica OpenAPI è raggiungibile: verde = disponibile, rosso = non raggiungibile, grigio = assente.
3. **Genera** — scegli il linguaggio target e un LLM locale genera un client SDK con gli header di interoperabilità AgID (`X-Request-Id`, `X-Correlation-Id`) già inclusi. Nessuna chiave API, nessun dato inviato fuori dalla tua macchina.

> Il codice generato è un punto di partenza, non un prodotto finito. Revisionalo e confrontalo con gli schemi ufficiali su [schema.gov.it](https://www.schema.gov.it/) prima di usarlo in produzione.

---

## Demo

<video src=".github/assets/dev-intelligence-usage.mp4" controls muted playsinline width="100%"></video>

---

## Caratteristiche

| Feature | Dettaglio |
|---------|-----------|
| **Ricerca catalogo PA** | Interroga `api.developers.italia.it/v1/software` con filtro testuale locale; cache 24h |
| **Validazione OAS** | HEAD asincrona con protezione SSRF e DNS-rebinding per ogni software trovato |
| **Generazione SDK via LLM** | Qwen2.5-Coder-0.5B-Instruct (GGUF Q4_K_M, ~491 MB) — Python, JS, Go, Rust, Java |
| **Inferenza locale** | CPU-only via `llama-cpp-python`; nessuna API esterna, nessun dato trasmesso |
| **Cache persistente** | `diskcache` su volume Docker: catalogo 24h, codice generato 24h |
| **Font self-hosted** | Inter + Fira Code serviti localmente — no Google Fonts, GDPR-compliant |
| **Security hardening** | CSP nonce, HSTS, container read-only, `cap_drop: ALL`, hash supply chain |

---

## Architettura

```
Browser
  │
  ├─ GET /          → index.html (SPA, nonce CSP per-request)
  │
  ├─ GET /search?q= → _get_catalog() → api.developers.italia.it/v1/software
  │                                     (paginato, cache 24h, filtro locale)
  │
  ├─ GET /validate-spec?url= → HEAD asincrona
  │                             → _check_ssrf_safe() (DNS resolution + IP check)
  │                             → httpx (follow_redirects=False)
  │
  └─ POST /generate-sdk/ → cache lookup → _generate_sdk_code()
                                           → asyncio.to_thread
                                           → Llama (GGUF, llama-cpp-python)
                                           → fallback mock se LLM non disponibile
```

**Endpoint:**

| Metodo | Path | Rate limit | Auth |
|--------|------|-----------|------|
| `GET` | `/` | — | Pubblica |
| `GET` | `/search` | 20/min | Pubblica |
| `POST` | `/generate-sdk/` | 5/min | Pubblica |
| `GET` | `/validate-spec` | 10/min | Pubblica |
| `GET` | `/health` | — | Pubblica |

---

## Configurazione

Tutte le variabili sono opzionali — i default permettono un avvio immediato.

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `CACHE_EXPIRE` | `86400` | TTL cache catalogo in secondi (24h) |
| `MAX_CATALOG_PAGES` | `10` | Numero massimo di pagine API da scaricare |
| `DEV_ITALIA_BASE_URL` | `https://api.developers.italia.it/v1` | URL base API Developers Italia |
| `SKIP_LLM` | `false` | `true` per saltare il caricamento del modello (utile in CI) |
| `MODEL_DIR` | `/app/models` | Directory per il modello GGUF |
| `MODEL_REPO` | `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF` | Repository HuggingFace del modello |
| `MODEL_FILE` | `qwen2.5-coder-0.5b-instruct-q4_k_m.gguf` | Nome del file GGUF |
| `LLM_N_CTX` | `2048` | Context window del modello |
| `LLM_N_THREADS` | `4` | Thread CPU per l'inferenza |
| `LLM_MAX_TOKENS` | `1024` | Token massimi generati per risposta |
| `LLM_TIMEOUT` | `120` | Timeout inferenza in secondi |
| `TRUSTED_PROXIES` | `` | IP del reverse proxy per `X-Forwarded-For` |

---

## Struttura del progetto

```
dev-intelligence/
├── main.py                    # App FastAPI (~700 righe, single-file)
├── templates/
│   └── index.html             # SPA: Tailwind CDN + Prism.js + JS puro
├── static/fonts/              # Inter e Fira Code self-hosted
├── tests/
│   ├── test_main.py           # 130+ test pytest
│   └── conftest.py            # Fixture: cache isolata, rate limiter reset
├── .github/workflows/ci.yml   # CI: build Docker + test con SKIP_LLM=true
├── Dockerfile                 # Multi-stage: builder + runtime (Python 3.12-slim)
├── docker-compose.yml         # Volume cache + models + hardening completo
├── requirements.txt           # Dipendenze pinnate con SHA256 hash
├── requirements-dev.txt       # Dipendenze sviluppo (pytest)
└── publiccode.yml             # Descrittore per il catalogo Developers Italia
```

---

## Test

**130+ test** coprono parsing YAML, protezione SSRF, sanitizzazione prompt LLM, tutti gli endpoint e gli header di sicurezza.

```bash
# Dentro il container (metodo principale)
docker exec dev-intelligence python -m pytest tests/ -v

# In locale
pip install --require-hashes -r requirements.txt
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

La CI GitHub Actions esegue build Docker e `pytest` con `SKIP_LLM=true` ad ogni push su `main`.

---

## Sicurezza

Per la documentazione completa su SSRF, CSP, hardening container e supply chain vedi [SECURITY.md](SECURITY.md).

**Privacy:** nessun dato viene inviato a servizi esterni. Font self-hosted, inferenza LLM locale, nessuna telemetria.

---

## Comandi utili

```bash
docker-compose logs -f       # Log in tempo reale
docker-compose down          # Ferma il container
docker-compose up --build    # Rebuild dopo modifiche al codice
rm -rf ./cache/*             # Reset cache (forza ricaricamento catalogo da API)
```

---

## Contribuire

Leggi [CONTRIBUTING.md](CONTRIBUTING.md) per le linee guida. Bug e proposte su [GitHub Issues](https://github.com/RubberDuckOps/dev-intelligence/issues).

---

## Manutenzione

| | |
|---|---|
| **Tipo** | Community open source |
| **Maintainer** | [RubberDuckOps](https://github.com/RubberDuckOps) |
| **Segnalazione bug** | [GitHub Issues](https://github.com/RubberDuckOps/dev-intelligence/issues) |
| **Vulnerabilità** | [SECURITY.md](SECURITY.md) — segnalazione privata via GitHub Security Advisories |
| **Catalogo PA** | [publiccode.yml](publiccode.yml) |

---

## Licenza

Distribuito sotto licenza **MIT** — vedi [LICENSE](LICENSE).

I dati del catalogo sono forniti da [Developers Italia](https://developers.italia.it/) sotto [Licenza IoDL 2.0](https://www.dati.gov.it/content/italian-open-data-license-v20). Le specifiche OpenAPI sono di proprietà dei rispettivi enti titolari.
