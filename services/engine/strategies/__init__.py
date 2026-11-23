"""
Registre des stratégies.

Une stratégie est une classe qui, à partir d'une série de bougies CLÔTURÉES,
renvoie les signaux {index de bougie: "CALL" | "PUT"}. Le même code sert au
moteur live (on regarde seulement la dernière bougie) et au backtest (on
regarde toute la série) : ce qu'on backteste est exactement ce qui tourne.

Ajouter une stratégie :
  1. créer un fichier dans ce dossier avec une classe héritant de Strategy ;
  2. l'ajouter à REGISTRY ci-dessous ;
  3. redéployer : elle apparaît automatiquement dans le Monitor.
"""

from .base import Bar, Param, Strategy
from .ma_cross import EmaCross, SmaCross
from .rsi_reversal import RsiReversal
from .bollinger_reversal import BollingerReversal

REGISTRY: dict[str, Strategy] = {s.kind: s for s in (SmaCross(), EmaCross(), RsiReversal(), BollingerReversal())}

__all__ = ["Bar", "Param", "Strategy", "REGISTRY"]
