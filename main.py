# Standard library
import asyncio
import hashlib
import ipaddress
import json as _json
import logging
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, TypedDict
from urllib.parse import parse_qs, urlparse

# Third-party
import httpx
import requests
import yaml
from diskcache import Cache, Disk
from diskcache.core import MODE_BINARY, UNKNOWN
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

# Dipendenze LLM opzionali
try:
    from llama_cpp import Llama
    LLAMA_AVAILABLE = True
except ImportError:
    LLAMA_AVAILABLE = False

try:
    from huggingface_hub import hf_hub_download
    HF_HUB_AVAILABLE = True
except ImportError:
    HF_HUB_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# ---------------------------------------------------------------------------
# Costanti di configurazione
# ---------------------------------------------------------------------------
DEV_ITALIA_BASE_URL = os.getenv("DEV_ITALIA_BASE_URL", "https://api.developers.italia.it/v1")
USER_AGENT          = "Dev-Intelligence/1.0 (+https://github.com/RubberDuckOps/dev-intelligence)"
CATALOG_CACHE_KEY   = "catalog_v1"
CACHE_EXPIRE        = int(os.getenv("CACHE_EXPIRE", "86400"))
MAX_CATALOG_PAGES   = int(os.getenv("MAX_CATALOG_PAGES", "10"))

MODEL_DIR   = Path(os.getenv("MODEL_DIR", "/app/models"))
MODEL_REPO  = os.getenv("MODEL_REPO", "Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF")
MODEL_FILE  = os.getenv("MODEL_FILE", "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf")
LLM_N_CTX   = int(os.getenv("LLM_N_CTX", "2048"))
LLM_THREADS = int(os.getenv("LLM_N_THREADS", "4"))
LLM_MAX_TOK = int(os.getenv("LLM_MAX_TOKENS", "1024"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))   # secondi
SKIP_LLM        = os.getenv("SKIP_LLM", "false").lower() == "true"
TRUSTED_PROXIES = set(filter(None, os.getenv("TRUSTED_PROXIES", "").split(",")))

START_TIME  = time.time()

# Prefisso commento per linguaggio (usato da _clean_llm_output)
COMMENT_PREFIX: dict[str, str] = {
    "python":     "#",
    "go":         "//",
    "rust":       "//",
    "javascript": "//",
    "java":       "//",
}

# Token speciali LLM da rimuovere per prevenire prompt injection
_LLM_TOKEN_PATTERN = re.compile(r"<\|im_(?:start|end)\|>|<\|endoftext\|>")

# Frasi di prompt injection generica (blocklist)
_PROMPT_INJECTION_PATTERNS = re.compile(
    r"(?i)"
    r"(?:ignore|forget|disregard)\s+(?:all\s+)?(?:previous|above|prior)\s+(?:instructions?|rules?|prompts?)"
    r"|(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|new\s+instructions?)"
    r"|(?:system\s*:\s*|assistant\s*:\s*|user\s*:\s*)"
)

# Limiti di lunghezza per gli input al prompt
_MAX_NAME_LENGTH = 500
_MAX_OAS_LENGTH  = 2000

# Allowlist caratteri per il nome software (rimuove tutto ciò che non corrisponde)
_SAFE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9\u00C0-\u00FA\s.\-_/()'\",:;]")

# Pattern per la sanitizzazione della query di ricerca
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_search_query(q: str) -> str:
    """Rimuove caratteri di controllo e normalizza whitespace per la query di ricerca."""
    cleaned = _CONTROL_CHARS_PATTERN.sub("", q)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

# Reti private/riservate per la protezione SSRF
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata (RFC 3927)
    ipaddress.ip_network("0.0.0.0/8"),        # this-network (RFC 1122)
    ipaddress.ip_network("100.64.0.0/10"),    # Carrier-Grade NAT / shared space (RFC 6598)
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local (RFC 4291)
    ipaddress.ip_network("::ffff:0:0/96"),    # IPv4-mapped IPv6 (es. ::ffff:127.0.0.1)
]

# Disk backend JSON: sostituisce pickle con json.dumps/json.loads per prevenire
# deserializzazione insicura (RCE via payload pickle su bind-mount host → CVE-class H1).
class JSONDisk(Disk):
    """Serializza dict/list/tuple come JSON invece di pickle."""

    def store(self, value, read, key=UNKNOWN):
        # Tipi primitivi (str, int, float) → MODE_RAW via parent, nessun pickle coinvolto
        if not read and not isinstance(value, (dict, list, tuple)):
            return super().store(value, read, key=key)
        # Strutture complesse → JSON bytes salvati come MODE_BINARY inline (filename=None)
        if not read:
            json_bytes = _json.dumps(value, separators=(",", ":")).encode("utf-8")
            return len(json_bytes), MODE_BINARY, None, sqlite3.Binary(json_bytes)
        return super().store(value, read, key=key)

    def fetch(self, mode, filename, value, read):
        # filename=None + MODE_BINARY → valore JSON inline scritto da store() sopra
        if mode == MODE_BINARY and filename is None and value is not None:
            return _json.loads(bytes(value).decode("utf-8"))
        return super().fetch(mode, filename, value, read)


# Cache su volume Docker (./cache/ sull'host)
cache = Cache("/app/cache", disk=JSONDisk)

_llm: Optional["Llama"] = None
_llm_lock     = threading.Lock()
_catalog_lock = threading.Lock()

def _get_client_ip(request: Request) -> str:
    """key_func per slowapi: usa X-Forwarded-For solo se la richiesta proviene da un proxy fidato."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    if TRUSTED_PROXIES and client_ip in TRUSTED_PROXIES:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return client_ip


# Rate limiter (slowapi)
limiter = Limiter(key_func=_get_client_ip)


# ---------------------------------------------------------------------------
# TypedDict per i record del catalogo
# ---------------------------------------------------------------------------
class SoftwareItem(TypedDict):
    id:              str
    url:             str
    name:            str
    description:     str
    public_code_url: str
    license:         str


# ---------------------------------------------------------------------------
# Modelli Pydantic
# ---------------------------------------------------------------------------
class CodeRequest(BaseModel):
    software_id:     str
    target_language: str

    @field_validator("software_id")
    @classmethod
    def validate_software_id(cls, v: str) -> str:
        if len(v) > 128:
            raise ValueError("ID Software non valido: lunghezza massima 128 caratteri.")
        if not re.match(r"^[a-zA-Z0-9\-_]+$", v):
            raise ValueError(
                "ID Software non valido: sono consentiti solo lettere, cifre, trattini e underscore."
            )
        return v

    @field_validator("target_language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        allowed = ["python", "javascript", "rust", "go", "java"]
        if v.lower() not in allowed:
            raise ValueError(f"Linguaggio non supportato. Scegli tra: {allowed}")
        return v.lower()


# ---------------------------------------------------------------------------
# Middleware: security headers
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; "
            "style-src 'self' https://cdnjs.cloudflare.com 'unsafe-inline'; "
            "font-src 'self'; "
            "connect-src 'self';"
        )
        return response


# ---------------------------------------------------------------------------
# Helper: protezione SSRF (controllo IP literali e hostname noti)
# ---------------------------------------------------------------------------
def _is_private_host(hostname: str) -> bool:
    """Restituisce True se l'hostname è localhost o un IP privato/riservato."""
    if not hostname:
        return True
    if hostname.lower() in ("localhost", "localhost.localdomain", "0.0.0.0"):
        return True
    try:
        addr = ipaddress.ip_address(hostname)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        # È un nome di dominio — verrà controllato con risoluzione DNS
        return False


async def _check_ssrf_safe(hostname: str) -> Optional[str]:
    """
    Verifica che l'hostname sia sicuro per una richiesta esterna e restituisce
    l'IP validato come stringa, oppure None se l'host è privato/bloccato.

    Restituire l'IP (invece di un bool) elimina la race condition TOCTOU:
    la richiesta httpx successiva usa direttamente questo IP senza un secondo
    lookup DNS che un attaccante potrebbe influenzare (DNS rebinding).
    """
    # Blocca subito IP privati e hostname noti
    if _is_private_host(hostname):
        return None
    # Se è già un IP letterale che ha passato _is_private_host, è sicuro
    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        pass
    # Hostname: risolvi DNS, verifica ogni IP e restituisci il primo valido
    try:
        loop = asyncio.get_running_loop()
        results = await loop.getaddrinfo(hostname, None)
        safe_ip: Optional[str] = None
        for _, _, _, _, sockaddr in results:
            try:
                resolved = ipaddress.ip_address(sockaddr[0])
                if any(resolved in net for net in _PRIVATE_NETWORKS):
                    return None  # anche un solo IP privato → blocca
                if safe_ip is None:
                    safe_ip = str(resolved)
            except ValueError:
                pass
        return safe_ip  # None se nessun IP valido trovato
    except OSError:
        return None  # fail-safe: blocca in caso di errore DNS


# ---------------------------------------------------------------------------
# Helper: sanificazione input per il prompt LLM
# ---------------------------------------------------------------------------
def _sanitize_for_prompt(value: str, *, max_length: int = 0) -> str:
    """Rimuove token speciali LLM, frasi di injection comuni, e tronca a max_length."""
    cleaned = _LLM_TOKEN_PATTERN.sub("", value)
    cleaned = _PROMPT_INJECTION_PATTERNS.sub("[FILTERED]", cleaned)
    cleaned = cleaned.strip()
    if max_length > 0 and len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


# ---------------------------------------------------------------------------
# Helper: pulizia output LLM
# ---------------------------------------------------------------------------
def _clean_llm_output(text: str, language: str = "python") -> str:
    """
    Pulisce l'output del modello LLM:
    - estrae il codice dai blocchi ```...``` se presenti
    - sposta il testo narrativo come commenti in fondo al codice
    """
    if not text:
        return ""
    text   = text.strip()
    prefix = COMMENT_PREFIX.get(language, "#")
    fence_pattern = re.compile(r"```[a-zA-Z]*\n?(.*?)```", re.DOTALL)
    fences = fence_pattern.findall(text)
    if fences:
        code  = "\n\n".join(block.strip() for block in fences)
        prose = fence_pattern.sub("", text).strip()
    else:
        code  = text
        prose = ""
    if prose:
        comment_lines = "\n".join(
            f"{prefix} {line}" if line.strip() else prefix
            for line in prose.splitlines()
        )
        code = f"{code}\n\n\n{prefix} --- Note ---\n{comment_lines}"
    return code


# ---------------------------------------------------------------------------
# Helper: generazione SDK via LLM (sincrona — usare asyncio.to_thread)
# ---------------------------------------------------------------------------
def _generate_sdk_code(name: str, language: str, endpoint_oas: str) -> str:
    """Inferenza sincrona. Chiamare sempre via asyncio.to_thread() dall'endpoint."""
    if _llm is None:
        raise RuntimeError("Modello non disponibile")
    safe_name     = _SAFE_NAME_PATTERN.sub("", _sanitize_for_prompt(name, max_length=_MAX_NAME_LENGTH))
    safe_endpoint = _sanitize_for_prompt(endpoint_oas, max_length=_MAX_OAS_LENGTH)
    prompt = (
        "<|im_start|>system\n"
        "Sei un esperto sviluppatore software. Genera SOLO codice sorgente, "
        "senza spiegazioni, senza markdown, senza ```.\n<|im_end|>\n"
        f"<|im_start|>user\n"
        f"Genera un client SDK in {language} per questa API.\n"
        f"Nome software: {safe_name}\n"
        f"URL documentazione OpenAPI: {safe_endpoint}\n"
        "Requisiti: classe principale, almeno un metodo GET, gestione errori HTTP, "
        "docstring in italiano. "
        "Includi header di interoperabilità ModI in ogni richiesta: "
        "X-Request-Id (UUID v4 generato per ogni chiamata), "
        "X-Correlation-Id (UUID propagato per tracciare il flusso tra servizi). "
        "Aggiungi un commento TODO che menziona Agid-JWT-Signature per scenari PDND avanzati.\n<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    with _llm_lock:
        out = _llm(
            prompt,
            max_tokens=LLM_MAX_TOK,
            temperature=0.2,
            top_p=0.95,
            repeat_penalty=1.1,
            stop=["<|im_end|>", "<|im_start|>"],
        )
    return _clean_llm_output(out["choices"][0]["text"], language=language)


# ---------------------------------------------------------------------------
# Helper: download/verifica modello
# ---------------------------------------------------------------------------
def _ensure_model() -> bool:
    """Scarica il GGUF da HuggingFace se assente nel volume. Ritorna True se pronto."""
    if not LLAMA_AVAILABLE or not HF_HUB_AVAILABLE:
        return False
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_DIR / MODEL_FILE
    if model_path.exists():
        logger.info("Modello trovato: %s (%.1f MB)", model_path, model_path.stat().st_size / 1e6)
        return True
    logger.info("Modello non trovato. Download da HuggingFace: %s/%s", MODEL_REPO, MODEL_FILE)
    try:
        hf_hub_download(
            repo_id=MODEL_REPO,
            filename=MODEL_FILE,
            local_dir=str(MODEL_DIR),
            local_dir_use_symlinks=False,
        )
        logger.info("Download completato: %s", MODEL_DIR / MODEL_FILE)
        return True
    except Exception as e:
        logger.error("Download modello fallito: %s", e)
        return False


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _llm
    # Validazione SSRF su DEV_ITALIA_BASE_URL: blocca l'avvio se punta a rete privata
    parsed_base = urlparse(DEV_ITALIA_BASE_URL)
    if _is_private_host(parsed_base.hostname or ""):
        raise RuntimeError(
            f"DEV_ITALIA_BASE_URL punta a un host privato/riservato: {DEV_ITALIA_BASE_URL}"
        )
    if LLAMA_AVAILABLE and HF_HUB_AVAILABLE and not SKIP_LLM:
        ready = await asyncio.to_thread(_ensure_model)
        if ready:
            try:
                logger.info("Caricamento modello in memoria...")
                _llm = await asyncio.to_thread(
                    Llama,
                    model_path=str(MODEL_DIR / MODEL_FILE),
                    n_ctx=LLM_N_CTX,
                    n_threads=LLM_THREADS,
                    verbose=False,
                )
                logger.info("Modello Qwen2.5-Coder caricato.")
            except Exception as e:
                logger.error("Caricamento modello fallito: %s", e)
                _llm = None
    else:
        logger.warning("LLM non disponibile. Il backend usera il fallback mock.")
    print("=" * 50, flush=True)
    print("  App disponibile su: http://localhost:8000", flush=True)
    print("=" * 50, flush=True)
    yield
    if _llm is not None:
        del _llm
        _llm = None


# ---------------------------------------------------------------------------
# App + middleware
# ---------------------------------------------------------------------------
app = FastAPI(title="Dev-Intelligence Pro 2026", lifespan=lifespan)
templates = Jinja2Templates(env=Environment(loader=FileSystemLoader("templates"), autoescape=True))

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — solo origine locale
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# File statici (font self-hosted)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Exception handler: nasconde dettagli Pydantic negli errori 422
# ---------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": "Input non valido. Controlla i parametri inviati."},
    )


# ---------------------------------------------------------------------------
# Helper: parsing item catalogo
# ---------------------------------------------------------------------------
def _parse_software_item(item: dict) -> SoftwareItem | None:
    try:
        pc = yaml.safe_load(item.get("publiccodeYml", "")) or {}
    except yaml.YAMLError:
        return None

    desc = pc.get("description", {})
    # Priorità lingua: it → en → primo disponibile
    lang_desc = desc.get("it") or desc.get("en") or next(iter(desc.values()), {})
    if not isinstance(lang_desc, dict):
        lang_desc = {}

    return SoftwareItem(
        id=item.get("id", ""),
        url=item.get("url", ""),
        name=pc.get("name", ""),
        description=lang_desc.get("shortDescription", ""),
        public_code_url=lang_desc.get("apiDocumentation", ""),
        license=pc.get("legal", {}).get("license", ""),
    )


# ---------------------------------------------------------------------------
# Helper: catalogo locale con cache 24h e protezione race condition
# ---------------------------------------------------------------------------
def _get_catalog() -> list[SoftwareItem]:
    catalog = cache.get(CATALOG_CACHE_KEY)
    if catalog is not None:
        return catalog
    with _catalog_lock:
        # Double-check: un altro thread potrebbe aver già popolato la cache
        catalog = cache.get(CATALOG_CACHE_KEY)
        if catalog is not None:
            return catalog
        all_items: list[SoftwareItem] = []
        cursor: Optional[str] = None
        for _ in range(MAX_CATALOG_PAGES):
            params: dict = {"page[size]": 100}
            if cursor:
                params["page[after]"] = cursor
            r = requests.get(
                f"{DEV_ITALIA_BASE_URL}/software",
                params=params,
                timeout=15,
                headers={"User-Agent": USER_AGENT},
            )
            r.raise_for_status()
            data = r.json()
            for raw in data.get("data", []):
                parsed = _parse_software_item(raw)
                if parsed and parsed["name"]:
                    all_items.append(parsed)
            next_link = data.get("links", {}).get("next")
            if not next_link:
                break
            qs = parse_qs(next_link.lstrip("?"))
            cursor = qs.get("page[after]", [None])[0]
            if not cursor:
                break
        cache.set(CATALOG_CACHE_KEY, all_items, expire=CACHE_EXPIRE)
        return all_items


# ---------------------------------------------------------------------------
# Endpoint: homepage
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def read_item(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "csp_nonce": request.state.csp_nonce,
    })


# ---------------------------------------------------------------------------
# Endpoint: health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Health check minimale — nessun dettaglio infrastrutturale esposto."""
    return {
        "status": "ok" if _llm is not None else "degraded",
    }


# ---------------------------------------------------------------------------
# Endpoint: ricerca
# ---------------------------------------------------------------------------
@app.get("/search")
@limiter.limit("20/minute")
def search_software(
    request: Request,
    q: str = Query(..., min_length=3, max_length=200, pattern=r".*\w+.*", description="Nome del software o ente"),
):
    """
    Ricerca nel catalogo locale costruito da /software di Developers Italia.
    La prima chiamata impiega ~15s per caricare il catalogo; le successive
    rispondono dalla cache in millisecondi.
    """
    q = _sanitize_search_query(q)
    if len(q) < 3:
        raise HTTPException(status_code=422, detail="Input non valido.")
    cache_key = f"search_{q.lower()}"
    if cache_key in cache:
        return {"source": "cache", "results": cache[cache_key]}
    try:
        catalog = _get_catalog()
        q_lower = q.lower()
        results = [
            item for item in catalog
            if q_lower in item["name"].lower() or q_lower in item["description"].lower()
        ]
        cache.set(cache_key, results, expire=3600)
        return {"source": "live", "results": results}
    except requests.RequestException as e:
        logger.error("Errore caricamento catalogo: %s", e)
        raise HTTPException(status_code=500, detail="Errore nel recupero del catalogo.")
    except yaml.YAMLError as e:
        logger.error("Errore parsing YAML catalogo: %s", e)
        raise HTTPException(status_code=500, detail="Errore nel parsing dei metadati.")


# ---------------------------------------------------------------------------
# Endpoint: generazione SDK
# ---------------------------------------------------------------------------
@app.post("/generate-sdk/")
@limiter.limit("5/minute")
async def generate_sdk(request: Request, payload: CodeRequest):
    """
    Valida l'input, recupera i metadati dal catalogo e restituisce
    le informazioni necessarie per generare l'SDK.
    """
    software_id = payload.software_id
    cache_key   = f"meta_{software_id}"
    software_data = cache.get(cache_key)

    if not software_data or not software_data.get("name"):
        catalog = await asyncio.to_thread(_get_catalog)
        software_data = next((s for s in catalog if s.get("id") == software_id), None)
        if not software_data:
            raise HTTPException(status_code=404, detail="Software non trovato nel catalogo.")
        cache.set(cache_key, software_data, expire=86400)

    api_url = software_data.get("public_code_url", "")
    if not api_url or not api_url.startswith("https://"):
        raise HTTPException(
            status_code=422,
            detail="Il software non fornisce una specifica API sicura (HTTPS)."
        )
    # Blocca URL con credenziali embedded (es. https://user:pass@host/)
    _parsed_api = urlparse(api_url)
    if _parsed_api.username is not None or _parsed_api.password is not None:
        raise HTTPException(
            status_code=422,
            detail="Il software non fornisce una specifica API sicura (HTTPS)."
        )

    sdk_key        = f"sdk_{software_id}_{payload.target_language}"
    generated_code = cache.get(sdk_key)
    model_used     = "cache"

    if generated_code is None:
        if _llm is not None:
            try:
                generated_code = await asyncio.wait_for(
                    asyncio.to_thread(
                        _generate_sdk_code,
                        software_data.get("name", ""),
                        payload.target_language,
                        api_url,
                    ),
                    timeout=LLM_TIMEOUT,
                )
                cache.set(sdk_key, generated_code, expire=86400)
                model_used = "qwen2.5-coder-0.5b"
            except asyncio.TimeoutError:
                logger.error("LLM timeout (%.0fs) per software_id=%s", LLM_TIMEOUT, software_id)
                generated_code = None
                model_used     = "fallback"
            except Exception as e:
                logger.error("Errore generazione LLM: %s", e)
                generated_code = None
                model_used     = "fallback"
        else:
            model_used = "fallback"

    return {
        "status":       "validated",
        "software":     software_data.get("name"),
        "language":     payload.target_language,
        "endpoint_oas": api_url,
        "cache_hit":    cache_key in cache,
        "code":         generated_code,
        "model":        model_used,
    }


# ---------------------------------------------------------------------------
# Endpoint: validazione spec OAS
# ---------------------------------------------------------------------------
@limiter.limit("10/minute")
@app.get("/validate-spec")
async def validate_spec(
    request: Request,
    url: str = Query(..., description="URL HTTPS della specifica OAS da validare"),
):
    """
    Verifica se l'URL della specifica API è attivo e sicuro (HTTPS, HEAD request).
    Protetto contro SSRF con verifica DNS per prevenire DNS rebinding.
    """
    if not url.startswith("https://"):
        return {"valid": False, "reason": "Protocollo non sicuro (richiesto HTTPS)"}

    parsed_url = urlparse(url)
    hostname   = parsed_url.hostname or ""

    # Blocca URL con credenziali embedded (es. https://user:pass@127.0.0.1/)
    if parsed_url.username is not None or parsed_url.password is not None:
        return {"valid": False, "reason": "URL non consentito"}

    # SSRF protection: IP letterali + DNS rebinding.
    # _check_ssrf_safe restituisce l'IP validato (non None) — lo usiamo direttamente
    # nella richiesta httpx per evitare un secondo lookup DNS (TOCTOU race condition).
    safe_ip = await _check_ssrf_safe(hostname)
    if safe_ip is None:
        return {"valid": False, "reason": "URL non consentito"}

    # Cache SHA-256 (TTL 60s) per evitare uso come proxy anonimo
    vc_key        = f"validate_{hashlib.sha256(url.encode()).hexdigest()}"
    cached_result = cache.get(vc_key)
    if cached_result is not None:
        return cached_result

    # Sostituisce l'hostname con l'IP pre-validato nell'URL della richiesta.
    # L'header Host preserva il nome originale per TLS SNI e virtual hosting.
    # verify=False: il certificato è emesso per il dominio, non per l'IP diretto;
    # la sicurezza SSRF è garantita dall'IP pre-validato, non dalla verifica TLS.
    port        = parsed_url.port
    netloc_ip   = f"[{safe_ip}]" if ":" in safe_ip else safe_ip  # IPv6 → brackets
    netloc_ip   = f"{netloc_ip}:{port}" if port else netloc_ip
    ip_url      = parsed_url._replace(netloc=netloc_ip).geturl()
    try:
        async with httpx.AsyncClient(
            timeout=3.0,
            follow_redirects=False,
            verify=False,          # TLS non valida il cert per IP — vedere commento sopra
        ) as client:
            response = await client.head(ip_url, headers={"Host": hostname, "User-Agent": USER_AGENT})
            if 200 <= response.status_code < 300:
                result: dict = {"valid": True, "status": response.status_code}
            else:
                result = {"valid": False, "reason": f"Errore HTTP: {response.status_code}"}
    except httpx.RequestError as e:
        # Loga solo scheme+host+path per evitare leak di token in query string
        parsed_for_log = urlparse(url)
        safe_log_url   = f"{parsed_for_log.scheme}://{parsed_for_log.netloc}{parsed_for_log.path}"
        logger.warning("validate-spec irraggiungibile [%s]: %s", safe_log_url, e)
        result = {"valid": False, "reason": "Server non raggiungibile"}

    cache.set(vc_key, result, expire=60)
    return result
