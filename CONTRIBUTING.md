# Come contribuire a Dev-Intelligence

Grazie per l'interesse nel progetto! Di seguito le istruzioni per contribuire.

---

## Segnalazione di bug

Apri una [GitHub Issue](https://github.com/RubberDuckOps/dev-intelligence/issues) includendo:

- Versione di Docker e Docker Compose utilizzata
- Passi per riprodurre il problema
- Comportamento atteso vs comportamento osservato
- Log rilevanti (`docker-compose logs`)

Per vulnerabilità di sicurezza, **non aprire issue pubbliche**: segui le istruzioni in [SECURITY.md](SECURITY.md).

---

## Proporre modifiche

1. **Fai il fork** del repository e crea un branch descrittivo:
   ```bash
   git checkout -b fix/descrizione-del-fix
   git checkout -b feat/nome-della-funzionalità
   ```

2. **Apporta le modifiche** rispettando le convenzioni esistenti:
   - Python: stile conforme a `main.py` (type hints, docstring per funzioni pubbliche)
   - Test: aggiungi test per ogni nuova funzionalità o bug fix in `tests/test_main.py`
   - Sicurezza: non introdurre nuove dipendenze senza aggiornare `requirements.txt` con hash SHA256

3. **Verifica che i test passino:**
   ```bash
   docker-compose up --build -d
   docker exec dev-intelligence python -m pytest tests/ -v
   ```

4. **Apri una Pull Request** verso il branch `main` con:
   - Titolo descrittivo (es. `fix: correzione SSRF per hostname con trailing dot`)
   - Descrizione delle modifiche e motivazione
   - Riferimento all'issue correlata, se presente (es. `Closes #42`)

---

## Standard del codice

- Il codice deve essere leggibile e commentato dove la logica non è ovvia
- Le dipendenze di produzione vanno pinnate con hash in `requirements.txt`
- Le dipendenze dev vanno in `requirements-dev.txt`
- Non includere dati sensibili, chiavi API, o credenziali nel codice o nei test

---

## Domande

Per domande generali sul progetto apri una [Discussion](https://github.com/RubberDuckOps/dev-intelligence/discussions) o una Issue con l'etichetta `question`.
