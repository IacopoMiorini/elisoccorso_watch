FROM python:3.12-slim

# Imposta un utente non-root per maggiore sicurezza
RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

# Installa deps prima del codice per sfruttare la cache
COPY --chmod=0644 requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chmod=0644 main.py helicopters.yaml ./

# File di config personalizzati si possono montare sovrascrivendo helicopters.yaml
# oppure puntare HELICOPTERS_FILE a un volume.

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO

# Nessun web server: è un worker persistente.
CMD ["python", "main.py"]
