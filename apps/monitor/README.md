# Monitor

Interface web façon TradingView : graphiques en bougies (Lightweight Charts v5),
outils de dessin (lightweight-charts-drawing), stratégies superposées,
gestion de la watchlist du collecteur.

```bash
npm install
npm run dev      # http://localhost:5173 — /api relayé vers http://localhost:8000
npm run build
```

Ajouter une stratégie : `src/strategies.ts` (objet `Strategy` ajouté à `STRATEGIES`).
