# sma-trading-bot (API Deriv)

Bot de trading d'exemple pour l'API WebSocket de Deriv, en Python.
Stratégie pédagogique : croisement de deux moyennes mobiles sur les ticks
d'un indice synthétique, contrats Rise/Fall à mise fixe.

> ⚠️ Exemple éducatif, pas une stratégie rentable. À utiliser sur compte **démo**.

## Fichiers

| Fichier | Rôle |
|---|---|
| `deriv_bot.py` | le bot (connexion, stratégie, gestion du risque, journal CSV) |
| `fetch_history.py` | téléchargement de données historiques (bougies ou ticks) en CSV |
| `Dockerfile` | image pour Dokploy / Docker |
| `.env.example` | modèle de configuration |

## Lancer en local

```bash
pip install -r requirements.txt
cp .env.example .env          # renseigner DERIV_TOKEN
python deriv_bot.py           # DRY_RUN=1 par défaut : aucun achat
```

## Données historiques

```bash
python fetch_history.py --symbol R_100 --granularity 60 --days 7   # bougies 1 min, 7 jours
python fetch_history.py --symbol R_100 --ticks --count 5000         # 5000 derniers ticks
```

## Déploiement Dokploy

1. **Create Service → Application**, source **GitHub**, ce dépôt, branche `main`.
2. **Build Type : Dockerfile** (chemin `./Dockerfile`).
3. **Environment** : coller les variables de `.env.example` avec les vraies valeurs
   (au minimum `DERIV_APP_ID`, `DERIV_TOKEN`, `DRY_RUN`). Ne jamais committer `.env`.
4. **Advanced → Volumes** : monter un volume sur `/app/data` pour garder
   `trades.csv` (et donc le P&L du jour) entre deux déploiements.
5. Pas de domaine ni de port : c'est un worker, il n'expose rien.
6. **Deploy**, puis suivre l'onglet **Logs**.

Dans le conteneur, `STOP_ON_LIMIT=0` : à la limite journalière, le bot se met
en pause jusqu'au lendemain (UTC) au lieu de s'arrêter, et il se reconnecte
seul si la connexion tombe.

## Variables principales

| Variable | Défaut | Description |
|---|---|---|
| `DERIV_APP_ID` | — | App ID (developers.deriv.com) |
| `DERIV_TOKEN` | — | token API (permission Trade uniquement) |
| `DERIV_ACCOUNT_ID` | auto | forcer un compte précis |
| `SYMBOL` | `R_100` | marché |
| `STAKE` | `1` | mise fixe |
| `SMA_FAST` / `SMA_SLOW` | `5` / `20` | moyennes mobiles |
| `MAX_DAILY_LOSS` | `10` | perte max par jour |
| `MAX_TRADES` | `20` | trades max par jour |
| `DRY_RUN` | `1` | 1 = simulation, 0 = achats réels (sur le compte choisi) |
| `ALLOW_REAL` | `0` | refuse un compte réel tant que ≠ 1 |
| `STOP_ON_LIMIT` | `1` (`0` en Docker) | arrêt ou pause à la limite |
