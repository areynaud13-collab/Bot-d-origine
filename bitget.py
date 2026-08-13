# ═══════════════════════════════════════════════════════
# BITGET FUTURES API CLIENT — XAU/USDT Perpetual (USDT-M)
# API v2 — https://www.bitget.com/api-doc/contract/intro
#
# Implémente l'interface définie dans EXCHANGE_INTERFACE.py
# pour remplacer mexc.py.
# ═══════════════════════════════════════════════════════

import time
import hmac
import hashlib
import base64
import json
import requests
from config import API_KEY, API_SECRET, PASSPHRASE, SYMBOL, LEVERAGE, OPEN_TYPE

BASE_URL     = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"   # Contrats perpétuels USDT-M
MARGIN_COIN  = "USDT"


# ── Authentification ────────────────────────────────────

def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    """
    Signature Bitget v2 :
    base64( HMAC_SHA256(secret, timestamp + METHOD + path + body) )
    """
    message = timestamp + method.upper() + path + body
    mac = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()


def _headers(method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY":        API_KEY,
        "ACCESS-SIGN":       _sign(ts, method, path, body),
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type":      "application/json",
        "locale":            "en-US",
    }


def _check(data: dict, label: str = "API") -> dict:
    """Lève une exception si la réponse Bitget signale une erreur."""
    if data.get("code") != "00000":
        raise Exception(f"{label} erreur [{data.get('code')}]: {data.get('msg')}")
    return data


# ── DONNÉES DE MARCHÉ (Public) ──────────────────────────

def get_candles(symbol: str = SYMBOL, interval: str = "1m", limit: int = 100) -> list:
    """
    Récupère les bougies OHLCV.

    interval Bitget : '1m' '3m' '5m' '15m' '30m' '1H' '4H' '1D'
    Retourne une liste ordonnée du plus ancien au plus récent.
    """
    path   = "/api/v2/mix/market/candles"
    params = {
        "symbol":      symbol,
        "productType": PRODUCT_TYPE,
        "granularity": interval,
        "limit":       str(limit),
    }
    r = requests.get(BASE_URL + path, params=params, timeout=10)
    r.raise_for_status()
    data = _check(r.json(), "get_candles")

    result = []
    for k in data["data"]:
        # Format Bitget v2 : [timestamp_ms, open, high, low, close, vol, volCcyQuote]
        result.append({
            "timestamp": int(k[0]) // 1000,
            "open":      float(k[1]),
            "high":      float(k[2]),
            "low":       float(k[3]),
            "close":     float(k[4]),
            "volume":    float(k[5]),
        })

    # Bitget renvoie du plus récent au plus ancien → on inverse
    result.sort(key=lambda x: x["timestamp"])
    return result[-limit:]


def get_price(symbol: str = SYMBOL) -> float:
    """Prix mark actuel."""
    path   = "/api/v2/mix/market/ticker"
    params = {"symbol": symbol, "productType": PRODUCT_TYPE}
    r = requests.get(BASE_URL + path, params=params, timeout=5)
    r.raise_for_status()
    data = _check(r.json(), "get_price")
    return float(data["data"]["lastPr"])


# ── COMPTE (Privé) ──────────────────────────────────────

def get_balance() -> float:
    """
    Solde USDT disponible sur le compte futures USDT-M.
    """
    path   = "/api/v2/mix/account/accounts"
    params = {"productType": PRODUCT_TYPE}
    h = _headers("GET", path + "?" + "&".join(f"{k}={v}" for k, v in params.items()))
    r = requests.get(BASE_URL + path, params=params, headers=h, timeout=10)
    r.raise_for_status()
    data = _check(r.json(), "get_balance")

    for account in data["data"]:
        if account.get("marginCoin", "").upper() == "USDT":
            return float(account.get("available", 0))
    return 0.0


def get_contract_info(symbol: str = SYMBOL) -> dict:
    """
    Informations sur le contrat XAU/USDT.

    Retourne :
    {
        "contractSize":  float,  # Taille d'1 contrat (en oz d'or)
        "minVol":        float,  # Nombre de contrats minimum
        "volDecimalNum": int,    # Décimales pour le volume
    }
    """
    path   = "/api/v2/mix/market/contracts"
    params = {"productType": PRODUCT_TYPE, "symbol": symbol}
    r = requests.get(BASE_URL + path, params=params, timeout=10)
    r.raise_for_status()
    data = _check(r.json(), "get_contract_info")

    contracts = data.get("data", [])
    info = contracts[0] if contracts else {}

    # sizeMultiplier = taille d'un lot en unité de base (oz pour XAU)
    contract_size  = float(info.get("sizeMultiplier", 0.01))
    min_vol        = float(info.get("minTradeNum",    1))
    vol_decimal    = int(info.get("volumePlace",      0))

    return {
        "contractSize":  contract_size,
        "minVol":        min_vol,
        "volDecimalNum": vol_decimal,
    }


def get_open_positions(symbol: str = SYMBOL) -> list:
    """
    Positions ouvertes sur le symbole.
    Retourne une liste de dicts compatibles avec le bot.
    """
    path   = "/api/v2/mix/position/single-position"
    params = {
        "symbol":      symbol,
        "productType": PRODUCT_TYPE,
        "marginCoin":  MARGIN_COIN,
    }
    query  = "&".join(f"{k}={v}" for k, v in params.items())
    h = _headers("GET", path + "?" + query)
    r = requests.get(BASE_URL + path, params=params, headers=h, timeout=10)
    r.raise_for_status()
    data = _check(r.json(), "get_open_positions")

    positions = []
    for p in data.get("data", []):
        total = float(p.get("total", 0))
        if total <= 0:
            continue
        positions.append({
            "positionId":   p.get("positionId", ""),
            "holdSide":     p.get("holdSide", ""),      # "long" ou "short"
            "openPriceAvg": float(p.get("openPriceAvg", 0)),
            "total":        total,
            "unrealizedPL": float(p.get("unrealizedPL", 0)),
            # Champs supplémentaires utiles pour le journal
            "leverage":     p.get("leverage", str(LEVERAGE)),
            "marginMode":   p.get("marginMode", ""),
        })
    return positions


# ── LEVIER ─────────────────────────────────────────────

def set_leverage(symbol: str = SYMBOL, leverage: int = LEVERAGE) -> dict:
    """
    Définit le levier pour les deux sens (long + short).
    Bitget v2 exige un appel séparé par holdSide.
    """
    path = "/api/v2/mix/account/set-leverage"
    results = {}

    for hold_side in ("long", "short"):
        body = json.dumps({
            "symbol":      symbol,
            "productType": PRODUCT_TYPE,
            "marginCoin":  MARGIN_COIN,
            "leverage":    str(leverage),
            "holdSide":    hold_side,
        }, separators=(",", ":"))
        h = _headers("POST", path, body)
        r = requests.post(BASE_URL + path, data=body, headers=h, timeout=10)
        r.raise_for_status()
        results[hold_side] = _check(r.json(), f"set_leverage_{hold_side}")

    return results


# ── ORDRES (Privé) ──────────────────────────────────────
#
# Mapping MEXC → Bitget :
#   1 = Open Long   → side="buy",  tradeSide="open"
#   2 = Close Long  → side="sell", tradeSide="close"
#   3 = Open Short  → side="sell", tradeSide="open"
#   4 = Close Short → side="buy",  tradeSide="close"
#
# orderType : "market" (recommandé) ou "limit"
# OPEN_TYPE  : 1=isolated (recommandé), 2=cross
#   → marginMode : "isolated" ou "crossed"

_SIDE_MAP = {
    1: ("buy",  "open"),    # Open Long
    2: ("sell", "close"),   # Close Long
    3: ("sell", "open"),    # Open Short
    4: ("buy",  "close"),   # Close Short
}

_MARGIN_MODE = {1: "isolated", 2: "crossed"}


def place_order(side: int, vol: float, price: float = 0,
                order_type: int = 5, symbol: str = SYMBOL) -> dict:
    """
    Place un ordre futures market.

    side       : 1=Open Long, 2=Close Long, 3=Open Short, 4=Close Short
    vol        : Nombre de contrats
    price      : Prix (0 pour market)
    order_type : 5=Market (compatibilité MEXC, converti en 'market')
    """
    if side not in _SIDE_MAP:
        raise ValueError(f"Side invalide: {side}. Valeurs: 1=OL, 2=CL, 3=OS, 4=CS")

    bitget_side, trade_side = _SIDE_MAP[side]
    order_type_str = "market" if order_type == 5 else "limit"
    margin_mode    = _MARGIN_MODE.get(OPEN_TYPE, "isolated")

    path = "/api/v2/mix/order/place-order"
    body_dict = {
        "symbol":      symbol,
        "productType": PRODUCT_TYPE,
        "marginMode":  margin_mode,
        "marginCoin":  MARGIN_COIN,
        "size":        str(int(vol)),        # Bitget attend une chaîne entière
        "side":        bitget_side,
        "tradeSide":   trade_side,
        "orderType":   order_type_str,
        "force":       "gtc",
    }
    if order_type_str == "limit" and price > 0:
        body_dict["price"] = str(price)

    body = json.dumps(body_dict, separators=(",", ":"))
    h = _headers("POST", path, body)
    r = requests.post(BASE_URL + path, data=body, headers=h, timeout=10)
    r.raise_for_status()
    return _check(r.json(), "place_order")


def cancel_order(order_id: str, symbol: str = SYMBOL) -> dict:
    """Annule un ordre ouvert."""
    path = "/api/v2/mix/order/cancel-order"
    body = json.dumps({
        "symbol":      symbol,
        "productType": PRODUCT_TYPE,
        "orderId":     order_id,
    }, separators=(",", ":"))
    h = _headers("POST", path, body)
    r = requests.post(BASE_URL + path, data=body, headers=h, timeout=10)
    r.raise_for_status()
    return _check(r.json(), "cancel_order")


# ── SL / TP ─────────────────────────────────────────────

def set_stop_loss_take_profit(position_id: str, sl: float,
                               tp: float, symbol: str = SYMBOL) -> dict:
    """
    Pose un SL et un TP sur une position ouverte via des plan orders.
    Deux appels : un pour le TP (pos_profit), un pour le SL (pos_loss).

    position_id : positionId retourné par get_open_positions
    sl          : prix du stop-loss
    tp          : prix du take-profit
    """
    path = "/api/v2/mix/order/place-tpsl-order"

    # Détecter le sens de la position depuis l'id n'est pas fiable ;
    # on l'extrait des positions ouvertes.
    open_pos   = get_open_positions(symbol)
    hold_sides = [p["holdSide"] for p in open_pos if p.get("positionId") == position_id]
    hold_side  = hold_sides[0] if hold_sides else "long"

    results = {}
    for plan_type, trigger_price in (("pos_profit", tp), ("pos_loss", sl)):
        body = json.dumps({
            "symbol":       symbol,
            "productType":  PRODUCT_TYPE,
            "marginCoin":   MARGIN_COIN,
            "planType":     plan_type,
            "triggerPrice": str(round(trigger_price, 2)),
            "triggerType":  "fill_price",
            "holdSide":     hold_side,
            "size":         "",   # Taille vide = position complète
        }, separators=(",", ":"))
        h = _headers("POST", path, body)
        r = requests.post(BASE_URL + path, data=body, headers=h, timeout=10)
        r.raise_for_status()
        results[plan_type] = _check(r.json(), f"set_{plan_type}")

    return results


# ── UTILITAIRES ─────────────────────────────────────────

def calc_contracts(usdt_amount: float, price: float,
                   contract_size: float, leverage: int = LEVERAGE) -> int:
    """
    Calcule le nombre de contrats depuis un montant USDT.
    contract_size : taille d'1 contrat en oz d'or
    """
    contract_value  = price * contract_size       # Valeur notionnelle d'un contrat
    n_contracts     = (usdt_amount * leverage) / contract_value
    return max(1, int(n_contracts))
