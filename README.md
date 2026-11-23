# Plateforme de trading automatisé — Deriv

```
Deriv API ──ws──▶ collector (Python) ──▶ PostgreSQL / TimescaleDB
                        │                        ▲
                        └── NOTIFY "market" ──▶ api (FastAPI)
                                                 │ REST + WebSocket
                                                 ▼
                                   monitor (React + Lightweight Charts)
```

| Dossier | Rôle |
|---|---|
| `services/collector` | **Service 1** — collecte les ticks des actifs de la watchlist, charge l'historique manquant, construit les bougies (1m → 1D), se reconnecte seul |
| `services/api` | API REST + WebSocket entre la base et le Monitor (`/api/docs` pour la doc interactive) |
| `apps/monitor` | **Service 2** — interface façon TradingView : bougies, outils de dessin, stratégies superposées, gestion de la watchlist |
| `db/schema.sql` | schéma (appliqué automatiquement au démarrage) |
| `bot/` | premier bot autonome (stratégie SMA) — sera branché sur la base dans une prochaine version |

## Fonctionnement

- **Watchlist pilotée depuis le Monitor** : ajouter / mettre en pause / retirer un actif. Le collecteur
  réagit immédiatement (LISTEN/NOTIFY PostgreSQL), charge l'historique (7 jours par défaut, au moins
  300 bougies par unité de temps), puis passe en temps réel.
- **Reprise incrémentale** : après un redémarrage ou une coupure, seul l'historique manquant est rechargé.
- **Marché fermé** (forex le week-end…) : l'actif est signalé « indisponible » et réessayé toutes les 5 min.
- **Ticks bruts conservés 30 jours** (avec TimescaleDB), bougies conservées sans limite.
- **Dessins** sauvegardés en base, par actif.
- **Stratégies** : calculées dans le Monitor (`apps/monitor/src/strategies.ts`), affichées en courbes +
  signaux CALL/PUT. V1 : croisement SMA (logique du bot) et croisement EMA.
- **Accès protégé** par le token `API_TOKEN`, demandé à l'ouverture du Monitor.

## Déploiement Dokploy

1. **Create Service → Compose**, source GitHub, ce dépôt, branche `main`, Compose Path `./docker-compose.yml`.
2. **Environment** : coller `.env.example` en remplaçant les valeurs (`POSTGRES_PASSWORD`, `API_TOKEN`,
   `DERIV_APP_ID`, `DERIV_TOKEN`).
3. **Domains** : ajouter un domaine sur le service **monitor**, port **80** (HTTPS Let's Encrypt possible).
   Aucun autre service n'est exposé.
4. **Deploy**. Suivre les logs du service `collector` : « Catalogue : N actifs », puis
   « Collecte en direct : R_100 ».
5. Ouvrir le domaine, entrer `API_TOKEN`.

Les données sont dans le volume `db-data` : elles survivent aux redéploiements.

## Développement local

```bash
cp .env.example .env            # remplir les valeurs
docker compose up --build       # monitor sur le port 80 du conteneur
# ou, sans Docker :
#   API       : cd services/api && DATABASE_URL=... uvicorn main:app --reload
#   collector : cd services/collector && DATABASE_URL=... python collector.py
#   monitor   : cd apps/monitor && npm install && npm run dev
```

## API (extrait)

| Méthode | Route | |
|---|---|---|
| GET | `/api/watchlist` | actifs suivis + dernier prix |
| POST | `/api/watchlist` | `{symbol, backfill_days}` |
| PATCH / DELETE | `/api/watchlist/{symbol}` | pause / retrait |
| GET | `/api/candles?symbol=R_100&tf=60&limit=1000&before=` | bougies |
| GET | `/api/symbols?q=` | catalogue Deriv |
| WS | `/api/ws?token=` | `{"action":"subscribe","symbol":"R_100","tf":60}` → ticks + bougies en direct |
