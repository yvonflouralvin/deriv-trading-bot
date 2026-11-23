"""Client WebSocket minimal pour l'API Deriv.

- requêtes/réponses appariées par req_id
- messages d'abonnement (ticks…) routés vers un callback
- deux modes d'authentification :
    * nouveau portail (App ID alphanumérique + token PAT) : URL WebSocket OTP
      obtenue via l'API REST, déjà authentifiée ;
    * ancien système (App ID numérique) : endpoint v3 (+ authorize si token).
"""

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request

import websockets

log = logging.getLogger("deriv")

APP_ID = os.getenv("DERIV_APP_ID", "1089")
TOKEN = os.getenv("DERIV_TOKEN", "")
ACCOUNT_ID = os.getenv("DERIV_ACCOUNT_ID", "")
API_BASE = os.getenv("DERIV_API_BASE", "https://api.derivws.com")
WS_URL_OVERRIDE = os.getenv("DERIV_WS_URL", "")
USE_NEW_API = not APP_ID.isdigit() or os.getenv("DERIV_AUTH") == "pat"


class DerivError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


# --------------------------------------------------------------------------- #
# Authentification "nouveau portail" (REST -> URL OTP)
# --------------------------------------------------------------------------- #
def _rest(method: str, path: str) -> dict:
    req = urllib.request.Request(
        API_BASE + path, method=method, data=b"{}" if method == "POST" else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Deriv-App-ID": APP_ID,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        raise DerivError(e.code, f"REST {method} {path}: {body}") from None


def _is_demo(acc: dict) -> bool:
    text = json.dumps(acc).lower()
    return '"demo"' in text or '"virtual"' in text or str(acc.get("is_virtual")) in ("1", "True", "true")


def _account_id(acc: dict) -> str:
    for k in ("account_id", "accountId", "id", "loginid", "login_id"):
        if acc.get(k):
            return str(acc[k])
    raise DerivError("account", f"identifiant de compte introuvable : {acc}")


def _otp_url() -> str:
    account_id = ACCOUNT_ID
    if not account_id:
        resp = _rest("GET", "/trading/v1/options/accounts")
        accounts = resp.get("data", resp)
        if isinstance(accounts, dict):
            accounts = accounts.get("accounts", [accounts])
        demos = [a for a in accounts if _is_demo(a)]
        if not (demos or accounts):
            raise DerivError("account", "aucun compte renvoyé par l'API")
        account_id = _account_id((demos or accounts)[0])
    otp = _rest("POST", f"/trading/v1/options/accounts/{account_id}/otp")
    url = (otp.get("data") or {}).get("url")
    if not url:
        raise DerivError("otp", f"réponse OTP inattendue : {otp}")
    return url


async def resolve_ws_url() -> str:
    if WS_URL_OVERRIDE:
        return WS_URL_OVERRIDE
    if USE_NEW_API:
        return await asyncio.to_thread(_otp_url)
    return f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class DerivClient:
    def __init__(self, on_stream=None):
        self.url = ""
        self.ws = None
        self.on_stream = on_stream          # callback(msg) pour les ticks
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self.closed = asyncio.Event()

    async def connect(self):
        self.url = await resolve_ws_url()
        self.ws = await websockets.connect(self.url, ping_interval=None, max_size=2**24)
        self._tasks = [asyncio.create_task(self._reader()), asyncio.create_task(self._keepalive())]
        log.info("Connecté à Deriv (%s)", self.url.split("?")[0])
        if not USE_NEW_API and TOKEN and not WS_URL_OVERRIDE:
            await self.send({"authorize": TOKEN})

    async def close(self):
        for t in self._tasks:
            t.cancel()
        if self.ws:
            await self.ws.close()

    async def send(self, payload: dict, timeout: float = 30) -> dict:
        if self.closed.is_set():
            raise ConnectionError("WebSocket Deriv fermé")
        self._req_id += 1
        rid = self._req_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self.ws.send(json.dumps({**payload, "req_id": rid}))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        if "error" in resp:
            raise DerivError(resp["error"].get("code"), resp["error"].get("message"))
        return resp

    async def _reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                fut = self._pending.get(msg.get("req_id"))
                if fut and not fut.done():
                    fut.set_result(msg)
                    # la 1re réponse d'un abonnement est aussi une donnée
                    if "subscription" in msg and self.on_stream and "error" not in msg:
                        self.on_stream(msg)
                elif "subscription" in msg and self.on_stream:
                    self.on_stream(msg)
        except websockets.ConnectionClosed as e:
            log.warning("WebSocket Deriv fermé : %s", e)
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("WebSocket Deriv fermé"))

    async def _keepalive(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.send({"ping": 1}, timeout=15)
            except Exception as e:  # noqa: BLE001
                log.warning("Ping Deriv échoué : %s", e)
                await self.ws.close()
                return
