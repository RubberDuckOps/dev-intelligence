# Sicurezza — Dev-Intelligence

Questo documento descrive le misure di sicurezza implementate in Dev-Intelligence e le istruzioni per la segnalazione responsabile di vulnerabilità.

---

## Segnalazione di vulnerabilità

Se hai individuato una vulnerabilità di sicurezza, **non aprire una issue pubblica**. Invia una segnalazione privata tramite [GitHub Security Advisories](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) o contatta il maintainer direttamente.

Includi nella segnalazione:
- Descrizione del problema e impatto stimato
- Passi per riprodurre la vulnerabilità
- Eventuali prove (screenshot, payload, log)

Risponderemo entro 7 giorni lavorativi con una valutazione e, se confermata, pubblicheremo una patch prima di divulgare pubblicamente i dettagli.

---

## 1. Sicurezza del container

### 1.1 Multi-stage Docker build

Il `Dockerfile` usa due stage separati:

- **`builder`**: installa `build-essential` e `cmake` (necessari per compilare `llama-cpp-python`), genera i wheel Python.
- **`runtime`**: copia solo i wheel compilati e il codice applicativo, senza compilatore né strumenti di build.

Il risultato è un'immagine di produzione priva di toolchain C/C++ (~200 MB in meno, superficie d'attacco ridotta).

```dockerfile
FROM python:3.12-slim AS builder
# ... installazione dipendenze e compilazione ...

FROM python:3.12-slim AS runtime
# ... solo i wheel compilati e l'app ...
```

### 1.2 Utente non-root

Il container gira con un utente dedicato senza privilegi:

```dockerfile
RUN groupadd -r appgroup && useradd -r -g appgroup appuser
USER appuser
```

### 1.3 Filesystem in sola lettura

Il filesystem radice del container è montato in sola lettura (`read_only: true`). L'unica directory scrivibile è `/tmp`, montata come `tmpfs` in memoria. Le directory dati (`/app/cache`, `/app/models`) sono bind-mount dall'host con permessi espliciti.

```yaml
read_only: true
tmpfs:
  - /tmp
```

### 1.4 Capabilities e privilegi

Tutte le Linux capabilities vengono rimosse e l'escalation dei privilegi è bloccata:

```yaml
security_opt:
  - no-new-privileges:true
cap_drop:
  - ALL
```

### 1.5 Limiti di risorse

Il container è soggetto a limiti espliciti per prevenire saturazione dell'host durante l'inferenza LLM:

```yaml
deploy:
  resources:
    limits:
      memory: 3g
      cpus: "2.0"
```

### 1.6 Porta esposta solo su localhost

Il mapping della porta limita l'accesso esclusivamente all'interfaccia loopback dell'host, prevenendo esposizione accidentale sulla rete:

```yaml
ports:
  - "127.0.0.1:8000:8000"
```

---

## 2. Supply chain

### 2.1 Hash delle dipendenze Python

Tutte le dipendenze in `requirements.txt` includono hash SHA-256 verificati. L'installazione usa il flag `--require-hashes`, che blocca qualsiasi pacchetto privo di hash corrispondente — anche in caso di compromissione di un mirror PyPI:

```bash
pip install --require-hashes -r requirements.txt
```

Esempio:
```
fastapi==0.115.12 \
    --hash=sha256:55b580f5f0...
```

### 2.2 SRI sulle risorse CDN

Tutte le risorse JavaScript caricate da CDN (Prism.js e sue estensioni) includono l'attributo `integrity` con hash SHA-384, secondo lo standard [Subresource Integrity](https://developer.mozilla.org/en-US/docs/Web/Security/Subresource_Integrity). Un CDN compromesso che servisse contenuto alterato verrebbe rifiutato dal browser:

```html
<script
  src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js"
  integrity="sha384-06z5D//U/xpvxZHuUz92xBvq3DqBBFi7Up53HRrbV7Jlv7Yvh/MZ7oenfUe9iCEt"
  crossorigin="anonymous"
  nonce="{{ csp_nonce }}">
</script>
```

> **Nota Tailwind CDN**: Tailwind v4 genera il CSS a runtime tramite un engine JavaScript, rendendo l'hash non prevedibile. Viene invece controllato tramite la `Content-Security-Policy` con `nonce` per-request.

### 2.3 Font self-hosted (GDPR)

I font tipografici (Inter e Fira Code) sono ospitati localmente in `static/fonts/`, eliminando qualsiasi richiesta verso server Google Fonts. Questo evita la trasmissione degli indirizzi IP dei visitatori a terze parti, rilevante ai fini del GDPR nel contesto della Pubblica Amministrazione italiana.

### 2.4 Separazione dipendenze dev/prod

`pytest` e le librerie di test si trovano esclusivamente in `requirements-dev.txt` e non vengono installate nell'immagine Docker di produzione.

---

## 3. Protezione SSRF (Server-Side Request Forgery)

L'endpoint `/validate-spec` e l'endpoint `/generate-sdk/` accettano URL forniti dall'utente. Per prevenire che vengano usati come proxy verso risorse interne, è stata implementata una protezione SSRF a più livelli.

### 3.1 Blocco reti private e riservate

La lista `_PRIVATE_NETWORKS` copre 11 blocchi CIDR che non devono mai essere raggiungibili:

| Rete | RFC | Descrizione |
|------|-----|-------------|
| `127.0.0.0/8` | RFC 5735 | Loopback |
| `10.0.0.0/8` | RFC 1918 | Rete privata |
| `172.16.0.0/12` | RFC 1918 | Rete privata |
| `192.168.0.0/16` | RFC 1918 | Rete privata |
| `169.254.0.0/16` | RFC 3927 | Link-local / AWS metadata endpoint |
| `0.0.0.0/8` | RFC 1122 | This-network |
| `100.64.0.0/10` | RFC 6598 | Carrier-Grade NAT |
| `::1/128` | RFC 4291 | Loopback IPv6 |
| `fc00::/7` | RFC 4193 | Unique local IPv6 |
| `fe80::/10` | RFC 4291 | Link-local IPv6 |
| `::ffff:0:0/96` | RFC 4291 | IPv4-mapped IPv6 (bypass vector) |

### 3.2 Anti DNS rebinding

La vulnerabilità TOCTOU nel DNS rebinding viene eliminata con un approccio a singola risoluzione:

1. `_check_ssrf_safe(hostname)` risolve il DNS **una sola volta** e verifica ogni IP risolto contro `_PRIVATE_NETWORKS`.
2. Se la validazione ha successo, restituisce l'**IP già validato** (non l'hostname).
3. Il transport HTTP personalizzato usa quell'IP per la connessione effettiva, impostando l'header `Host` con l'hostname originale (per TLS SNI). httpx non esegue una seconda risoluzione DNS.

In caso di errore DNS, la funzione ritorna `None` (fail-safe: blocca la richiesta).

### 3.3 No redirect HTTP

`httpx.AsyncClient` è configurato con `follow_redirects=False`. Un URL HTTPS valido non può eseguire un redirect verso `http://169.254.169.254/` (AWS metadata endpoint) aggirando il controllo SSRF.

### 3.4 Validazione URL base al startup

L'hostname di `DEV_ITALIA_BASE_URL` (configurabile via variabile d'ambiente) viene verificato con `_is_private_host()` durante il lifespan dell'applicazione. Se l'URL punta a una rete privata, l'app rifiuta di avviarsi:

```python
if _is_private_host(parsed.hostname):
    raise RuntimeError("DEV_ITALIA_BASE_URL punta a una rete privata")
```

### 3.5 Blocco credenziali embedded negli URL

Gli URL accettati dagli endpoint vengono analizzati con `urlparse`. Se contengono username o password (es. `https://user:pass@127.0.0.1/`), la richiesta viene rifiutata con HTTP 400 prima di qualsiasi risoluzione DNS.

### 3.6 HTTPS obbligatorio

Entrambi gli endpoint che accettano URL (`/validate-spec` e `/generate-sdk/`) rifiutano qualsiasi URL che non inizi con `https://`.

---

## 4. Rate limiting

Ogni endpoint API è protetto da rate limiting per prevenire abusi e DoS:

| Endpoint | Limite |
|----------|--------|
| `GET /search` | 20 richieste/minuto |
| `POST /generate-sdk/` | 5 richieste/minuto |
| `GET /validate-spec` | 10 richieste/minuto |

### IP reale dietro reverse proxy

La funzione `_get_client_ip()` usa l'IP di connessione diretta per default. Per deployment dietro reverse proxy (es. nginx), è possibile configurare `TRUSTED_PROXIES` con gli IP dei proxy fidati: solo in quel caso viene letto `X-Forwarded-For`.

```
TRUSTED_PROXIES=10.0.0.1,10.0.0.2
```

Questo evita che un utente malevolo bypasassi il rate limit inviando un header `X-Forwarded-For` falsificato.

---

## 5. Validazione input

### 5.1 `software_id`

Il campo `software_id` nel payload di `/generate-sdk/` è validato da Pydantic con:
- **Regex**: `^[a-zA-Z0-9\-_]+$` — solo caratteri alfanumerici, trattini e underscore
- **Lunghezza massima**: 128 caratteri

### 5.2 Query di ricerca

Il parametro `q` di `/search` è validato e sanitizzato a più livelli:
- **Lunghezza minima**: 3 caratteri
- **Lunghezza massima**: 200 caratteri
- **Pattern regex**: richiede almeno un carattere alfanumerico (`\w`), rifiutando query composte solo da simboli
- **Rimozione caratteri di controllo**: `_sanitize_search_query()` rimuove i code point U+0000–U+001F e U+007F–U+009F (null bytes, newline, tab, ecc.) per prevenire log injection e cache pollution
- **Normalizzazione whitespace**: spazi multipli vengono collassati in uno singolo, spazi iniziali/finali rimossi
- **Rivalidazione lunghezza post-sanitizzazione**: se dopo la pulizia la query scende sotto i 3 caratteri, viene restituito un errore 422

### 5.3 Lingua target

Il campo `target_language` accetta solo valori da una lista esplicita (`python`, `javascript`, `rust`, `go`, `java`) e viene normalizzato in lowercase. Qualsiasi altro valore produce un errore 422.

### 5.4 Handler 422 sanitizzato

I dettagli interni di Pydantic non vengono mai esposti al client. Il gestore personalizzato restituisce sempre un messaggio generico:

```json
{"detail": "Input non valido. Controlla i parametri inviati."}
```

---

## 6. Sicurezza LLM e prompt injection

### 6.1 Rimozione token speciali del modello

I token del formato ChatML di Qwen (`<|im_start|>`, `<|im_end|>`, `<|endoftext|>`) vengono rimossi da ogni input utente prima dell'interpolazione nel prompt.

### 6.2 Blocklist frasi di injection

La regex `_PROMPT_INJECTION_PATTERNS` (case-insensitive) intercetta le frasi di prompt injection più comuni e le sostituisce con `[FILTERED]`:

- Varianti di "ignore/forget/disregard previous instructions"
- "you are now", "act as", "pretend to be", "new instructions"
- Tag di ruolo in testo libero: `system:`, `assistant:`, `user:`

### 6.3 Allowlist caratteri per il nome software

Il nome del software viene filtrato con `_SAFE_NAME_PATTERN`, che rimuove tutti i caratteri non presenti nell'allowlist (alfanumerici, lettere accentate latine, punteggiatura comune).

### 6.4 Troncamento input

- Nome software: massimo 500 caratteri
- URL OAS: massimo 2000 caratteri

### 6.5 Serializzazione JSON della cache (no pickle)

`diskcache` usa pickle per default. Un attaccante con accesso in scrittura alla directory `./cache/` bind-montata dall'host potrebbe iniettare un payload pickle ed eseguire codice arbitrario nel processo Python (RCE).

La classe `JSONDisk` sostituisce pickle con `json.dumps`/`json.loads` per tutti i valori strutturati (dict, list):

- **`store()`**: serializza dict/list come JSON bytes con `MODE_BINARY`; tipi primitivi delegati al parent (MODE_RAW, senza pickle)
- **`fetch()`**: deserializza JSON da entri MODE_BINARY inline; altri tipi delegati al parent

In questo modo nessun `MODE_PICKLE` viene mai scritto nel database SQLite.

> **Nota migrazione**: la cache esistente serializzata con pickle non è compatibile con il nuovo backend JSON. Prima del primo avvio dopo l'aggiornamento eseguire `rm -rf ./cache/*`.

---

## 7. Sicurezza frontend

### 7.1 Content Security Policy con nonce per-request

La CSP è configurata a livello di middleware. Ogni risposta include un nonce crittograficamente casuale (`secrets.token_urlsafe(16)`) diverso per ogni request. Solo i tag `<script>` con il nonce corrispondente vengono eseguiti, bloccando XSS via injection di script:

```
Content-Security-Policy:
  default-src 'self';
  script-src 'self' 'nonce-<RANDOM>' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com;
  style-src 'self' https://cdnjs.cloudflare.com 'unsafe-inline';
  font-src 'self';
  connect-src 'self'
```

### 7.2 Escaping XSS nel JavaScript

Ogni valore proveniente dall'API che viene inserito nel DOM passa attraverso funzioni di escaping dedicate:

- `escHtml(str)`: escaping per contenuto testuale tramite `textContent` del DOM
- `escAttr(str)`: escaping per valori negli attributi HTML (`&`, `"`, `'`, `<`, `>`)

### 7.3 Nessun `innerHTML` per dati dinamici

Il codice frontend usa sempre `appendChild` con nodi DOM costruiti esplicitamente invece di `innerHTML +=`, evitando re-parsing del DOM e injection via concatenazione di stringhe.

### 7.4 Nessun event handler inline

Tutti gli event listener (`onclick`, `onkeypress`, `onchange`) sono registrati via `addEventListener` nel codice JavaScript, compatibilmente con la CSP che non include `'unsafe-inline'` per gli script.

### 7.5 `textContent` invece di `innerText`

Il codice generato viene inserito nel pre-code con `element.textContent` (non `innerText`) per preservare correttamente i newline prima dell'highlighting con Prism.js e per evitare XSS.

### 7.6 `encodeURIComponent` sugli URL delle fetch

I parametri di query inviati all'API dal frontend vengono sempre codificati con `encodeURIComponent` prima di essere interpolati nell'URL della fetch.

### 7.7 Autoescape Jinja2

Il template engine è configurato con `autoescape=True` esplicito (non dipende dal default di Starlette):

```python
templates = Jinja2Templates(
    env=Environment(loader=FileSystemLoader("templates"), autoescape=True)
)
```

---

## 8. Header di sicurezza HTTP

Ogni risposta include i seguenti header, impostati da `SecurityHeadersMiddleware`:

| Header | Valore | Protezione |
|--------|--------|------------|
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` | Forza HTTPS per 2 anni, preload list |
| `X-Frame-Options` | `DENY` | Blocca clickjacking via iframe |
| `X-Content-Type-Options` | `nosniff` | Blocca MIME sniffing |
| `Referrer-Policy` | `no-referrer` | Nessun URL nel Referer |
| `Permissions-Policy` | `geolocation=(), microphone=()` | Disabilita API browser non usate |
| `Content-Security-Policy` | (vedi §7.1) | XSS, injection, data exfiltration |

---

## 9. Gestione sicura degli errori

### 9.1 Health endpoint minimale

`GET /health` espone solo `{"status": "ok"}` o `{"status": "degraded"}`. Non sono esposti:
- `uptime_s` (rivelerebbe l'orario di riavvio)
- Stato dettagliato del modello LLM
- Versioni delle dipendenze o altri metadati infrastrutturali

### 9.2 CORS restrittivo

Il middleware CORS è configurato per accettare richieste cross-origin solo da `http://localhost:8000`:

```python
allow_origins=["http://localhost:8000"]
allow_methods=["GET", "POST"]
allow_headers=["Content-Type"]
```

---

## 10. Limitazioni note

Le seguenti limitazioni sono documentate e non ancora risolte:

| ID | Descrizione |
|----|-------------|
| **L2** | Logging strutturato per eventi di sicurezza non ancora implementato. Attualmente non vengono loggati: tentativi SSRF bloccati, superamento rate limit, rimozione token injection dal prompt. |
| **CSP style** | `style-src 'unsafe-inline'` è presente perché Tailwind CDN inietta stili inline a runtime. La rimozione richiederebbe un bundle Tailwind locale o un nonce per gli stili. |

---

## 11. Audit di sicurezza

Questo progetto ha ricevuto 3 scansioni di sicurezza indipendenti a marzo 2026. I risultati dell'audit sono tracciati nel file `TODO.md` (sezione 6), organizzati per severità: **Critical**, **High**, **Medium**, **Low**.

Tutte le vulnerabilità di severità Critical, High e Medium sono state risolte. Le voci Low residue sono documentate nella sezione precedente.
