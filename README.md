# Plateforme de trading automatisé — Deriv

```
Deriv API ──ws──▶ collector (Python) ──▶ PostgreSQL / TimescaleDB ◀── engine (Python)
                        │ NOTIFY "market"        ▲     │              │  ▲
                        ├────────────────────────┼─────┼──────────────┘  │ achats démo
                        │                        │     │ NOTIFY "engine" Deriv API (compte démo)
                        └──────────────────▶ api (FastAPI)
                                                 │ REST + WebSocket
                                                 ▼
                                   monitor (React + Lightweight Charts)
```

| Dossier | Rôle |
|---|---|
| `services/collector` | **Service 1** — collecte les ticks des actifs de la watchlist, charge l'historique manquant, construit les bougies (1m → 1D), se reconnecte seul |
| `services/engine` | **Service 3** — moteur de stratégie : évalue les stratégies à chaque clôture de bougie, exécute en paper ou sur le compte démo, lance les backtests |
| `services/engine/strategies/` | les stratégies, en Python (même code pour le live et le backtest) |
| `shared/deriv.py` | client WebSocket Deriv commun (collecteur + moteur) |
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
- **Indicateurs du graphique** (barre du haut) : SMA / EMA calculées dans le navigateur, pour l'analyse visuelle.

## Analyse de marché — étape 1 : tendance multi-timeframe

- **Détecteur** (`services/engine/analysis/trend.py`) : pour chaque bougie clôturée, état haussier / baissier /
  neutre + force 0–100, à partir de la structure de marché (sommets et creux confirmés par un ZigZag en ATR),
  des moyennes (EMA 50 / 200) et du directionnel (ADX, +DI / −DI). Calcul strictement causal.
- **Confirmation fractale** : tendance « forte » quand les trois unités de temps de `TREND_TFS`
  (défaut H4 / H1 / M15) sont d'accord.
- **Stockage continu** des états (table `trend_states`), recalculés toutes les 30 s pour chaque actif suivi.
- **Monitor** : onglet **Tendances** (tableau actifs × unités de temps, ajout rapide d'EUR/USD, GBP/USD, USD/JPY
  avec 1 an d'historique) et bande colorée sous le graphique (une ligne par unité de temps + alignement).
- **Évaluation** (bouton « Évaluer ») : quand les unités de temps sont alignées, le prix part-il ensuite dans ce
  sens plus souvent qu'à un instant quelconque ? Avantage vs référence, mouvement en ATR, MFE/MAE, nombre
  d'épisodes, comparaison avec chaque unité de temps seule, stabilité, et validation sur les 30 % de données les
  plus récentes.
- Historique : les unités de temps ≥ 15 min sont chargées sur la profondeur demandée (jusqu'à 365 jours) ;
  les plus petites sont plafonnées à `SHORT_TF_MAX_DAYS` (30 jours).

## Moteur de stratégie

- **Stratégies disponibles** : croisement SMA, croisement EMA, retournement RSI, retour dans les bandes de
  Bollinger. Toutes produisent des signaux CALL / PUT pour des contrats Rise/Fall.
- **Création et réglage depuis le Monitor** (onglet Stratégies) : actif, unité de temps, paramètres, durée du
  contrat (ticks / secondes / minutes), mise, limites (trades et perte max par jour), mode.
- **Deux modes d'exécution** :
  - **paper** : contrat simulé, résolu avec les vrais ticks enregistrés par le collecteur ; aucun ordre ;
  - **démo** : vrai contrat sur le compte **démo** Deriv. Le moteur vérifie le compte et **refuse tout compte
    réel**. `DEMO_TRADING_ENABLED=0` force toutes les stratégies en paper.
- **Garde-fous** : un seul contrat ouvert à la fois par stratégie, limites journalières (UTC), chaque signal est
  journalisé avec son action (`traded` / `skipped:<raison>`).
- **Backtest** depuis le Monitor : même code que le live ; prix exacts quand les ticks couvrent la période
  (≤ 3 jours récents), sinon bougies 1m (approximation ; les contrats trop courts pour être résolus sont exclus).
  Résultats : taux de réussite vs seuil de rentabilité, P&L, drawdown, profit factor, courbe de capital, trades
  affichables sur le graphique.
- **Ajouter une stratégie** : un fichier dans `services/engine/strategies/` (classe `Strategy` avec `params`,
  `warmup()` et `signals()`), l'ajouter à `REGISTRY`, redéployer : elle apparaît dans le Monitor.
- **Accès protégé** par le token `API_TOKEN`, demandé à l'ouverture du Monitor.

## Déploiement Dokploy

1. **Create Service → Compose**, source GitHub, ce dépôt, branche `main`, Compose Path `./docker-compose.yml`.
2. **Environment** : coller `.env.example` en remplaçant les valeurs (`POSTGRES_PASSWORD`, `API_TOKEN`,
   `DERIV_APP_ID`, `DERIV_TOKEN`, `DEMO_TRADING_ENABLED`).
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
| GET / POST | `/api/strategies` | stratégies (+ stats du jour et totales) |
| PATCH / DELETE | `/api/strategies/{id}` | modifier, activer/désactiver, supprimer |
| GET | `/api/strategy-kinds` | types de stratégie publiés par le moteur |
| GET | `/api/trades?symbol=&strategy_id=&since=` | trades paper et démo |
| POST / GET | `/api/backtests`, `/api/backtests/{id}` | lancer / lire un backtest |
| WS | `/api/ws?token=` | `{"action":"subscribe","symbol":"R_100","tf":60}` → ticks, bougies, signaux et trades en direct |
