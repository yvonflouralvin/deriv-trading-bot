# Contexte de build : racine du dépôt (voir docker-compose.yml)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY services/collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/collector/*.py ./
COPY shared/deriv.py ./
COPY db/schema.sql ./schema.sql
RUN useradd --create-home app
USER app
CMD ["python", "collector.py"]
