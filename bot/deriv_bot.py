"""
Bot d'exemple pour l'API WebSocket Deriv.

Stratégie (exemple pédagogique, PAS une stratégie rentable garantie) :
  - écoute les ticks d'un indice synthétique (R_100 par défaut)
  - calcule une moyenne mobile courte et une longue
  - croisement à la hausse -> contrat CALL (Rise), à la baisse -> PUT (Fall)
  - un seul contrat ouvert à la fois, mise FIXE (pas de martingale)

Garde-fous :
  - DRY_RUN=1 par défaut : le bot demande un prix mais n'achète rien
  - refuse de trader sur un compte réel sauf ALLOW_REAL=1
  - stop journalier (perte max) et nombre max de trades par session
  - journal CSV de chaque trade

Lancement :
  pip install -r requirements.txt
  cp .env.example .env   # puis renseigner DERIV_APP_ID et DERIV_TOKEN
  python deriv_bot.py
"""

import asyncio
import csv
import json
import logging
import os
import signal
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import websockets
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# Configuration (via .env)
# --------------------------------------------------------------------------- #
APP_ID = os.getenv("DERIV_APP_ID", "1089")            # 1089 = app_id de test public
TOKEN = os.getenv("DERIV_TOKEN", "")
# Nouveau portail (developers.deriv.com) : App ID alphanumérique + token PAT.
#   -> le bot obtient une URL WebSocket à usage unique (OTP) via l'API REST.
# Ancien système : App ID numérique -> endpoint v3 + appel "authorize".
API_BASE = os.getenv("DERIV_API_BASE", "https://api.derivws.com")
ACCOUNT_ID = os.getenv("DERIV_ACCOUNT_ID", "")       # optionnel : forcer un compte
USE_NEW_API = not APP_ID.isdigit() or os.getenv("DERIV_AUTH") == "pat"
WS_URL = os.getenv("DERIV_WS_URL") or f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

SYMBOL = os.getenv("SYMBOL", "R_100")
STAKE = float(os.getenv("STAKE", "1"))
DURATION = int(os.getenv("DURATION", "5"))           # durée du contrat
DURATION_UNIT = os.getenv("DURATION_UNIT", "t")      # t = ticks
SMA_FAST = int(os.getenv("SMA_FAST", "5"))
SMA_SLOW = int(os.getenv("SMA_SLOW", "20"))

MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "10"))
MAX_TRADES = int(os.getenv("MAX_TRADES", "20"))
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
ALLOW_REAL = os.getenv("ALLOW_REAL", "0") == "1"
# 1 = le programme s'arrête à la limite ; 0 = il se met en pause jusqu'au
# lendemain (mode serveur / Dokploy)
STOP_ON_LIMIT = os.getenv("STOP_ON_LIMIT", "1") == "1"

LOG_FILE = Path(os.getenv("TRADE_LOG", "trades.csv"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("deriv-bot")


# --------------------------------------------------------------------------- #
# Client WebSocket minimal : requêtes avec req_id + flux d'abonnements
# --------------------------------------------------------------------------- #
class DerivClient:
    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.streams: dict[str, asyncio.Queue] = {
            "tick": asyncio.Queue(),
            "proposal_open_contract": asyncio.Queue(),
        }
        self._reader_task = None
        self._ping_task = None

    async def connect(self):
        self.ws = await websockets.connect(self.url, ping_interval=None)
        self._reader_task = asyncio.create_task(self._reader())
        self._ping_task = asyncio.create_task(self._keepalive())
        log.info("Connecté à %s", self.url.split("?")[0])

    async def close(self):
        for t in (self._ping_task, self._reader_task):
            if t:
                t.cancel()
        if self.ws:
            await self.ws.close()

    async def send(self, payload: dict, timeout: float = 15) -> dict:
        """Envoie une requête et attend LA réponse correspondante (req_id)."""
        self._req_id += 1
        rid = self._req_id
        payload = {**payload, "req_id": rid}
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self.ws.send(json.dumps(payload))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        if "error" in resp:
            raise DerivError(resp["error"].get("code"), resp["error"].get("message"))
        return resp

    async def _reader(self):
        try:
            await self._read_loop()
        finally:
            # signale la fermeture à la boucle principale
            await self.streams["tick"].put({"_closed": True})

    async def _read_loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            mtype = msg.get("msg_type")
            # Les messages d'abonnement (tick, contrat ouvert) vont dans une file
            if mtype in self.streams and "subscription" in msg and msg.get("req_id") not in self._pending:
                await self.streams[mtype].put(msg)
                continue
            rid = msg.get("req_id")
            fut = self._pending.get(rid)
            if fut and not fut.done():
                fut.set_result(msg)
            elif mtype in self.streams:
                await self.streams[mtype].put(msg)

    async def _keepalive(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.send({"ping": 1})
            except Exception as e:  # noqa: BLE001
                log.warning("Ping échoué : %s", e)


def _rest(method: str, path: str) -> dict:
    """Appel REST vers api.derivws.com avec token PAT + App ID."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        API_BASE + path, method=method, data=b"{}" if method == "POST" else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Deriv-App-ID": APP_ID,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"API REST {method} {path} -> {e.code}: {e.read().decode(errors='ignore')}")


def _is_demo_account(acc: dict) -> bool:
    text = json.dumps(acc).lower()
    return '"demo"' in text or '"virtual"' in text or str(acc.get("is_virtual")) in ("1", "True", "true")


def _account_id(acc: dict) -> str:
    for k in ("account_id", "accountId", "id", "loginid", "login_id"):
        if acc.get(k):
            return str(acc[k])
    raise SystemExit(f"Impossible de lire l'identifiant du compte : {acc}")


def get_otp_ws_url() -> str:
    """Choisit le compte (démo par défaut) et récupère l'URL WebSocket OTP."""
    account_id = ACCOUNT_ID
    if not account_id:
        resp = _rest("GET", "/trading/v1/options/accounts")
        accounts = resp.get("data", resp)
        if isinstance(accounts, dict):
            accounts = accounts.get("accounts", [accounts])
        log.info("Comptes trouvés : %s", [(_account_id(a), "démo" if _is_demo_account(a) else "?") for a in accounts])
        demos = [a for a in accounts if _is_demo_account(a)]
        if not demos:
            raise SystemExit("Aucun compte démo identifié. Renseignez DERIV_ACCOUNT_ID dans .env.")
        account_id = _account_id(demos[0])
    otp = _rest("POST", f"/trading/v1/options/accounts/{account_id}/otp")
    url = (otp.get("data") or {}).get("url")
    if not url:
        raise SystemExit(f"Réponse OTP inattendue : {otp}")
    log.info("Compte %s -> WebSocket %s", account_id, url.split("?")[0])
    return url


class DerivError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


# --------------------------------------------------------------------------- #
# Stratégie
# --------------------------------------------------------------------------- #
def sma(values, n):
    if len(values) < n:
        return None
    return sum(list(values)[-n:]) / n


class SmaCrossStrategy:
    """Renvoie 'CALL', 'PUT' ou None à chaque nouveau tick."""

    def __init__(self, fast: int, slow: int):
        if fast >= slow:
            raise ValueError("SMA_FAST doit être < SMA_SLOW")
        self.fast, self.slow = fast, slow
        self.prices = deque(maxlen=slow + 1)
        self.prev_diff = None

    def on_tick(self, price: float):
        self.prices.append(price)
        f, s = sma(self.prices, self.fast), sma(self.prices, self.slow)
        if f is None or s is None:
            return None
        diff = f - s
        signal_ = None
        if self.prev_diff is not None:
            if self.prev_diff <= 0 < diff:
                signal_ = "CALL"
            elif self.prev_diff >= 0 > diff:
                signal_ = "PUT"
        self.prev_diff = diff
        return signal_


# --------------------------------------------------------------------------- #
# Gestion du risque + journal
# --------------------------------------------------------------------------- #
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    """Limites PAR JOUR (UTC). Le P&L du jour est relu depuis le CSV au
    démarrage, pour qu'un redémarrage du conteneur ne remette pas les
    compteurs à zéro."""

    def __init__(self, max_loss: float, max_trades: int):
        self.max_loss = max_loss
        self.max_trades = max_trades
        self.day = _today()
        self.pnl = 0.0
        self.trades = 0
        self._load_today()

    def _load_today(self):
        if not LOG_FILE.exists():
            return
        with LOG_FILE.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("time_utc", "").startswith(self.day) and row.get("status") != "dry_run":
                    self.trades += 1
                    self.pnl += float(row.get("profit") or 0)
        if self.trades:
            log.info("Reprise du jour %s : %d trades, P&L %.2f", self.day, self.trades, self.pnl)

    def roll_day(self):
        if _today() != self.day:
            self.day, self.pnl, self.trades = _today(), 0.0, 0
            log.info("Nouveau jour %s : compteurs remis à zéro", self.day)

    def can_trade(self) -> tuple[bool, str]:
        if self.pnl <= -self.max_loss:
            return False, f"stop journalier atteint (P&L {self.pnl:.2f})"
        if self.trades >= self.max_trades:
            return False, f"nombre max de trades atteint ({self.trades})"
        return True, ""

    def record(self, profit: float):
        self.trades += 1
        self.pnl += profit


def log_trade(row: dict):
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


# --------------------------------------------------------------------------- #
# Boucle principale
# --------------------------------------------------------------------------- #
class Bot:
    def __init__(self):
        self.client = DerivClient(WS_URL)
        self.strategy = SmaCrossStrategy(SMA_FAST, SMA_SLOW)
        self.risk = RiskManager(MAX_DAILY_LOSS, MAX_TRADES)
        self.currency = "USD"
        self.in_trade = False
        self.paused = False
        self.stop = asyncio.Event()

    async def start(self):
        if not TOKEN:
            raise SystemExit("DERIV_TOKEN manquant dans .env")
        if USE_NEW_API and not os.getenv("DERIV_WS_URL"):
            self.client.url = get_otp_ws_url()
        await self.client.connect()

        if USE_NEW_API:
            # L'URL OTP est déjà authentifiée : pas d'appel "authorize"
            bal = (await self.client.send({"balance": 1}))["balance"]
            self.currency = bal.get("currency") or "USD"
            is_demo = "/demo" in self.client.url
            log.info("Compte %s | solde %s %s | %s", bal.get("loginid", "?"),
                     bal.get("balance"), self.currency, "DÉMO" if is_demo else "RÉEL")
        else:
            auth = (await self.client.send({"authorize": TOKEN}))["authorize"]
            self.currency = auth.get("currency") or "USD"
            is_demo = bool(auth.get("is_virtual"))
            log.info("Compte %s | solde %s %s | %s",
                     auth.get("loginid"), auth.get("balance"), self.currency,
                     "DÉMO" if is_demo else "RÉEL")
        if not is_demo and not ALLOW_REAL:
            raise SystemExit("Compte réel détecté : arrêt. (ALLOW_REAL=1 pour forcer, à vos risques.)")
        if DRY_RUN:
            log.info("Mode DRY_RUN : aucun achat ne sera effectué.")

        await self.client.send({"ticks": SYMBOL, "subscribe": 1})
        log.info("Abonné aux ticks de %s | SMA %d/%d | mise %.2f",
                 SYMBOL, SMA_FAST, SMA_SLOW, STAKE)

        tick_q = self.client.streams["tick"]
        while not self.stop.is_set():
            try:
                msg = await asyncio.wait_for(tick_q.get(), 60)
            except asyncio.TimeoutError:
                raise ConnectionError("aucun tick depuis 60 s")
            if msg.get("_closed"):
                if self.stop.is_set():
                    break
                raise ConnectionError("WebSocket fermé par le serveur")
            price = float(msg["tick"]["quote"])
            signal_ = self.strategy.on_tick(price)
            if signal_ and not self.in_trade:
                self.risk.roll_day()
                ok, reason = self.risk.can_trade()
                if not ok:
                    if STOP_ON_LIMIT:
                        log.warning("Arrêt : %s", reason)
                        break
                    if not self.paused:
                        log.warning("Pause jusqu'à demain (UTC) : %s", reason)
                        self.paused = True
                    continue
                self.paused = False
                # on lance le trade sans bloquer la lecture des ticks
                self.in_trade = True
                asyncio.create_task(self.trade(signal_, price))

        await self.shutdown()

    async def trade(self, contract_type: str, spot: float):
        self.in_trade = True
        try:
            prop = (await self.client.send({
                "proposal": 1,
                "amount": STAKE,
                "basis": "stake",
                "contract_type": contract_type,
                "currency": self.currency,
                "duration": DURATION,
                "duration_unit": DURATION_UNIT,
                "symbol": SYMBOL,
            }))["proposal"]
            log.info("Signal %s @ %.4f | prix %.2f | gain potentiel %.2f",
                     contract_type, spot, prop["ask_price"], prop["payout"])

            if DRY_RUN:
                log_trade(self._row(contract_type, spot, prop, None, 0.0, "dry_run"))
                return

            buy = (await self.client.send({"buy": prop["id"], "price": prop["ask_price"]}))["buy"]
            cid = buy["contract_id"]
            log.info("Acheté contrat %s pour %.2f", cid, buy["buy_price"])

            profit, status = await self.wait_result(cid)
            self.risk.record(profit)
            log.info("Contrat %s %s | profit %.2f | P&L session %.2f (%d trades)",
                     cid, status, profit, self.risk.pnl, self.risk.trades)
            log_trade(self._row(contract_type, spot, prop, cid, profit, status))
        except DerivError as e:
            log.error("Erreur API : %s", e)
        except asyncio.TimeoutError:
            log.error("Timeout sur la requête de trade")
        finally:
            self.in_trade = False

    async def wait_result(self, contract_id: int) -> tuple[float, str]:
        first = await self.client.send({
            "proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1,
        })
        poc = first.get("proposal_open_contract", {})
        if poc.get("is_sold"):
            return float(poc.get("profit", 0)), poc.get("status", "sold")
        q = self.client.streams["proposal_open_contract"]
        while True:
            poc = (await q.get()).get("proposal_open_contract", {})
            if poc.get("contract_id") == contract_id and poc.get("is_sold"):
                return float(poc.get("profit", 0)), poc.get("status", "sold")

    def _row(self, ctype, spot, prop, cid, profit, status):
        return {
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": SYMBOL, "type": ctype, "spot": spot,
            "stake": prop["ask_price"], "payout": prop["payout"],
            "contract_id": cid or "", "profit": round(profit, 2), "status": status,
        }

    async def shutdown(self):
        log.info("Fermeture… P&L session : %.2f sur %d trades", self.risk.pnl, self.risk.trades)
        try:
            await self.client.send({"forget_all": ["ticks", "proposal_open_contract"]}, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        await self.client.close()


async def main():
    """Boucle de vie : reconnexion automatique (nouvelle URL OTP à chaque fois)
    si la connexion tombe."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    delay = 5
    while not stop.is_set():
        bot = Bot()
        bot.stop = stop
        started = loop.time()
        try:
            await bot.start()
            return  # arrêt normal (limite atteinte avec STOP_ON_LIMIT=1, ou signal)
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            if loop.time() - started > 120:
                delay = 5  # la session avait bien tourné : on repart d'un délai court
            log.warning("Connexion perdue (%s). Reconnexion dans %ds…", e, delay)
            await bot.client.close()
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 300)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await bot.shutdown()
            return


if __name__ == "__main__":
    asyncio.run(main())
