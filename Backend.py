"""
================================================
WATCHEARN — Backend Python (FastAPI) v3.0
MODIFICATIONS :
  - Suppression complète d'Adsgram
  - Monetag + AdsTerra uniquement
  - Taux TON/USDT en temps réel
  - Conversion TON → USDT avant crédit utilisateur
================================================

INSTALLATION :
    pip install fastapi uvicorn python-dotenv aiohttp

LANCER :
    uvicorn Backend:app --host 0.0.0.0 --port $PORT

FICHIER .env (variables Railway) :
    BOT_TOKEN=your_telegram_bot_token
    ADMIN_WALLET=EQxxxxxxxxxxxxxxxxxx
    TONCENTER_KEY=your_toncenter_api_key
    SECRET_KEY=mot_de_passe_secret
"""

import os, time, hmac, hashlib, json, logging, asyncio
from datetime import datetime, date
from typing import Optional
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiohttp

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("watchearn")

# ================================================
# CONFIG
# ================================================
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ADMIN_WALLET   = os.getenv("ADMIN_WALLET", "")
TONCENTER_KEY  = os.getenv("TONCENTER_KEY", "")
SECRET_KEY     = os.getenv("SECRET_KEY", "change_me")
TONCENTER_URL  = "https://toncenter.com/api/v2"

# Délai minimum entre 2 vues (secondes) — anti-fraude
MIN_VIEW_INTERVAL = 25

# ================================================
# Limites de vues par réseau/jour
# Adsgram supprimé — Monetag + AdsTerra uniquement
# ================================================
DAILY_VIEW_LIMITS = {
    "monetag":  999,
    "adsterra": 999
}

# ================================================
# Revenus pub réels par vue (en TON)
# ================================================
REVENUE_PER_VIEW_TON = {
    "monetag":  0.0007,  # ~0.00245$ par vue
    "adsterra": 0.0005   # ~0.00175$ par vue
}

# 50% pour toi (publisher), 50% pour l'utilisateur
PUBLISHER_SHARE = 0.50
USER_SHARE      = 0.50

# ================================================
# BASE DE DONNÉES (JSON simple)
# ================================================
DB_FILE = "watchearn_db.json"

def load_db() -> dict:
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"users": {}, "withdrawals": [], "ton_rate": 3.5}

def save_db(db: dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2, default=str)

def get_user(db: dict, user_id: str) -> dict:
    if user_id not in db["users"]:
        db["users"][user_id] = {
            "user_id":      user_id,
            "pending_usdt": 0.0,
            "balance_usdt": 0.0,
            "watched":      0,
            "last_view":    0,
            "wallet":       "",
            "daily": {
                "date":        "",
                "monetag":     0,
                "adsterra":    0,
                "earned_usdt": 0.0
            },
            "created": datetime.now().isoformat()
        }
    return db["users"][user_id]

def reset_daily_if_needed(user: dict) -> dict:
    today = date.today().isoformat()
    if user["daily"].get("date") != today:
        user["daily"] = {
            "date":        today,
            "monetag":     0,
            "adsterra":    0,
            "earned_usdt": 0.0
        }
    return user

# ================================================
# Taux TON/USDT en temps réel (cache 5 min)
# ================================================
_ton_rate_cache = {"rate": 3.5, "last_update": 0}

async def get_ton_to_usdt() -> float:
    global _ton_rate_cache
    now = time.time()
    if now - _ton_rate_cache["last_update"] < 300:
        return _ton_rate_cache["rate"]
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {"ids": "the-open-network", "vs_currencies": "usd"}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                rate = data["the-open-network"]["usd"]
                _ton_rate_cache = {"rate": rate, "last_update": now}
                log.info(f"💱 Taux TON/USDT mis à jour: 1 TON = {rate}$")
                return rate
    except Exception as e:
        log.warning(f"⚠️ Erreur CoinGecko: {e} → cache {_ton_rate_cache['rate']}")
        return _ton_rate_cache["rate"]

async def convert_ton_to_usdt(ton_amount: float) -> tuple[float, float]:
    rate = await get_ton_to_usdt()
    usdt = ton_amount * rate
    return round(usdt, 6), rate

# ================================================
# FASTAPI APP
# ================================================
app = FastAPI(title="WatchEarn API v3", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# ✅ ICI — après app = FastAPI(...)
@app.get("/")
async def serve_index():
    return FileResponse("index.html")
# ================================================
# MODÈLES
# ================================================
class AdWatchedRequest(BaseModel):
    user_id:   str
    network:   str    # monetag / adsterra
    timestamp: int

class WithdrawRequest(BaseModel):
    user_id: str
    wallet:  str
    amount:  float
    network: str      # "trc20" ou "erc20"

# ================================================
# VÉRIFICATION TELEGRAM
# ================================================
def verify_telegram(init_data: str) -> bool:
    if not init_data or not BOT_TOKEN:
        return True
    try:
        params = dict(x.split("=", 1) for x in init_data.split("&") if "=" in x)
        received_hash = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, received_hash)
    except:
        return True

# ================================================
# ROUTE RACINE — évite l'erreur "Not Found"
# ================================================
@app.get("/")
async def root():
    return {"message": "WatchEarn API v3 — OK", "status": "running"}

# ================================================
# ROUTE : VUE PUB
# ================================================
@app.post("/api/ad_watched")
async def ad_watched(
    req: AdWatchedRequest,
    x_telegram_init_data: str = Header(default="")
):
    log.info(f"👁️ Vue pub: user={req.user_id} network={req.network}")

    if not verify_telegram(x_telegram_init_data):
        raise HTTPException(403, "Accès non autorisé")

    db   = load_db()
    user = get_user(db, req.user_id)
    user = reset_daily_if_needed(user)

    network     = req.network.lower()
    daily_count = user["daily"].get(network, 0)
    daily_limit = DAILY_VIEW_LIMITS.get(network, 999)

    if daily_count >= daily_limit:
        return {
            "success":    False,
            "error":      f"Limite journalière atteinte pour {network}",
            "daily_left": 0
        }

    elapsed = time.time() - user.get("last_view", 0)
    if elapsed < MIN_VIEW_INTERVAL:
        wait = int(MIN_VIEW_INTERVAL - elapsed)
        return {"success": False, "error": f"Trop rapide ! Attends {wait}s", "daily_left": daily_limit - daily_count}

    ton_revenue     = REVENUE_PER_VIEW_TON.get(network, 0.0005)
    usdt_total, rate = await convert_ton_to_usdt(ton_revenue)
    usdt_user       = round(usdt_total * USER_SHARE, 6)

    log.info(f"💱 {ton_revenue} TON × {rate}$ = {usdt_total}$ → user: {usdt_user}$")

    user["pending_usdt"]         += usdt_user
    user["watched"]              += 1
    user["last_view"]             = time.time()
    user["daily"][network]        = daily_count + 1
    user["daily"]["earned_usdt"] += usdt_user

    db["users"][req.user_id] = user
    db["ton_rate"] = rate
    save_db(db)

    return {
        "success":       True,
        "usdt_earned":   usdt_user,
        "ton_rate":      rate,
        "pending_usdt":  round(user["pending_usdt"], 6),
        "total_watched": user["watched"],
        "daily_count":   daily_count + 1,
        "daily_limit":   daily_limit,
        "daily_left":    daily_limit - (daily_count + 1)
    }

# ================================================
# ROUTE : STATUT QUOTIDIEN
# ================================================
@app.get("/api/daily_status/{user_id}")
async def daily_status(user_id: str):
    db   = load_db()
    user = get_user(db, user_id)
    user = reset_daily_if_needed(user)

    result = {}
    for network, limit in DAILY_VIEW_LIMITS.items():
        done = user["daily"].get(network, 0)
        result[network] = {
            "watched": done,
            "limit":   limit,
            "left":    max(0, limit - done),
            "done":    done >= limit
        }

    rate = await get_ton_to_usdt()

    return {
        "user_id":       user_id,
        "networks":      result,
        "pending_usdt":  round(user["pending_usdt"], 6),
        "balance_usdt":  round(user["balance_usdt"], 6),
        "total_watched": user["watched"],
        "ton_rate":      rate,
        "daily_earned":  round(user["daily"].get("earned_usdt", 0), 6)
    }

# ================================================
# ROUTE : RETRAIT USDT
# ================================================
@app.post("/api/withdraw")
async def withdraw(
    req: WithdrawRequest,
    x_telegram_init_data: str = Header(default="")
):
    log.info(f"💎 Retrait: user={req.user_id} wallet={req.wallet} amount={req.amount} USDT")

    if not verify_telegram(x_telegram_init_data):
        raise HTTPException(403, "Accès non autorisé")

    wallet = req.wallet.strip()
    if not (wallet.startswith("T") or wallet.startswith("0x")):
        return {"success": False, "error": "Adresse USDT invalide (TRC20: T... ou ERC20: 0x...)"}

    if req.amount < 1.0:
        return {"success": False, "error": "Minimum de retrait : 1 USDT"}

    db   = load_db()
    user = get_user(db, req.user_id)

    if user["pending_usdt"] < req.amount:
        return {"success": False, "error": f"Solde insuffisant ({user['pending_usdt']:.4f} USDT disponible)"}

    user["pending_usdt"] -= req.amount
    user["balance_usdt"] += req.amount
    user["wallet"]        = wallet

    withdrawal = {
        "id":       f"wd_{int(time.time())}_{req.user_id}",
        "user_id":  req.user_id,
        "wallet":   wallet,
        "network":  req.network,
        "amount":   req.amount,
        "currency": "USDT",
        "status":   "pending",
        "created":  datetime.now().isoformat()
    }
    db["withdrawals"].append(withdrawal)
    db["users"][req.user_id] = user
    save_db(db)

    asyncio.create_task(process_usdt_payment(wallet, req.amount, req.network, withdrawal["id"]))

    return {
        "success":       True,
        "withdrawal_id": withdrawal["id"],
        "message":       "Retrait en cours de traitement (1-24h)"
    }

# ================================================
# ENVOI USDT
# ================================================
async def process_usdt_payment(wallet: str, amount: float, network: str, wid: str):
    log.info(f"📤 Envoi {amount} USDT ({network}) → {wallet}")
    await asyncio.sleep(2)
    db = load_db()
    for wd in db["withdrawals"]:
        if wd["id"] == wid:
            wd["status"]    = "completed"
            wd["completed"] = datetime.now().isoformat()
            break
    save_db(db)
    log.info(f"✅ Retrait {wid} complété")

# ================================================
# ROUTES UTILITAIRES
# ================================================
@app.get("/api/rate")
async def get_rate():
    rate = await get_ton_to_usdt()
    return {"ton_usdt": rate, "updated": datetime.now().isoformat()}

@app.get("/api/user/{user_id}")
async def get_user_info(user_id: str):
    db   = load_db()
    user = get_user(db, user_id)
    return {
        "user_id":      user_id,
        "pending_usdt": round(user["pending_usdt"], 6),
        "balance_usdt": round(user["balance_usdt"], 6),
        "watched":      user["watched"],
        "wallet":       user.get("wallet", "")
    }

@app.get("/api/stats")
async def global_stats():
    db    = load_db()
    users = db["users"]
    return {
        "total_users":         len(users),
        "total_watched":       sum(u.get("watched", 0) for u in users.values()),
        "total_pending_usdt":  round(sum(u.get("pending_usdt", 0) for u in users.values()), 4),
        "total_paid_usdt":     round(sum(u.get("balance_usdt", 0) for u in users.values()), 4),
        "ton_rate":            db.get("ton_rate", 3.5),
        "withdrawals_pending": len([w for w in db["withdrawals"] if w["status"] == "pending"])
    }

@app.get("/health")
async def health():
    rate = await get_ton_to_usdt()
    return {"status": "ok", "ton_usdt_rate": rate, "time": datetime.now().isoformat()}

# ================================================
# RESET QUOTIDIEN À MINUIT
# ================================================
async def midnight_reset():
    while True:
        now = datetime.now()
        seconds_until_midnight = (
            (23 - now.hour) * 3600 +
            (59 - now.minute) * 60 +
            (60 - now.second)
        )
        await asyncio.sleep(seconds_until_midnight)
        db = load_db()
        today = date.today().isoformat()
        for uid in db["users"]:
            db["users"][uid]["daily"] = {
                "date": today, "monetag": 0,
                "adsterra": 0, "earned_usdt": 0.0
            }
        save_db(db)
        log.info("🔄 Compteurs journaliers remis à zéro")

@app.on_event("startup")
async def startup():
    log.info("🚀 WatchEarn Backend v3.0 démarré!")
    await get_ton_to_usdt()
    asyncio.create_task(midnight_reset())

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("Backend:app", host="0.0.0.0", port=port)
