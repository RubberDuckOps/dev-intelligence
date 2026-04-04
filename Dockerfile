# =============================================================================
# Stage 1: builder — compila le dipendenze Python (include build tools)
# Questo stage viene scartato nell'immagine finale: nessun compilatore in prod.
# =============================================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Build tools necessari per compilare llama-cpp-python (C++ extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Installa le dipendenze Python in un prefix isolato
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.txt


# =============================================================================
# Stage 2: runtime — immagine di produzione minimale, senza compilatore
# =============================================================================
FROM python:3.12-slim AS runtime

# Variabili d'ambiente Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:$PATH"

WORKDIR /app

# curl per l'healthcheck + libgomp1 richiesta da llama-cpp-python (OpenMP runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copia i pacchetti Python compilati dallo stage builder
COPY --from=builder /install /usr/local

# Utente non-privilegiato + directory scrivibili (cache e modelli)
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && mkdir -p /app/cache /app/models \
    && chown -R appuser:appgroup /app

# Codice applicativo (layer volatile — viene per ultimo)
COPY --chown=appuser:appgroup . .

USER appuser

EXPOSE 8000

# Health check: l'app deve rispondere entro 10s; 60s di grace per il download del modello
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]


# =============================================================================
# Stage 3: test — estende runtime aggiungendo pytest (non va in produzione)
# =============================================================================
FROM runtime AS test

USER root
COPY requirements-dev.txt pytest.ini ./
RUN pip install --no-cache-dir -r requirements-dev.txt
USER appuser

CMD ["python", "-m", "pytest", "tests/", "-v", "--tb=short"]
