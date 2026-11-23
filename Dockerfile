FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRADE_LOG=/app/data/trades.csv \
    STOP_ON_LIMIT=0

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY deriv_bot.py fetch_history.py ./

# Utilisateur non-root + dossier persistant pour le journal des trades
RUN useradd --create-home bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot
VOLUME ["/app/data"]

CMD ["python", "deriv_bot.py"]
