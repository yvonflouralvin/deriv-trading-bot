# Contexte de build : racine du dépôt (voir docker-compose.yml)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY services/engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/engine/*.py ./
COPY services/engine/strategies ./strategies
COPY services/engine/analysis ./analysis
COPY services/engine/portfolio ./portfolio
COPY shared/deriv.py ./
COPY db/schema.sql ./schema.sql
RUN useradd --create-home app
USER app
CMD ["python", "engine.py"]
