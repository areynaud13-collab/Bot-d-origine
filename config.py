# ═══════════════════════════════════════════════════════
# CONFIG — Volume Profile Scalper · XAU/USDT · BITGET
# BOT D'ORIGINE — Stratégie de l'amie adaptée Bitget
# 3 Setups Long uniquement : VAL->POC · POC->VAH · HVN->POC
# ═══════════════════════════════════════════════════════

import os

# ── Clés API Bitget ─────────────────────────────────────
API_KEY    = os.environ.get("API_KEY",    "")
API_SECRET = os.environ.get("API_SECRET", "")
PASSPHRASE = os.environ.get("PASSPHRASE", "")

# ── Telegram ────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Futures ─────────────────────────────────────────────
SYMBOL    = "XAUUSDT"
LEVERAGE  = 20
OPEN_TYPE = 1   # 1 = Isolated

# ── Timeframe ───────────────────────────────────────────
INTERVAL  = "1m"

# ── Capital & Risk ──────────────────────────────────────
CAPITAL        = 500
RISK_PER_TRADE = 0.02
MAX_POSITIONS  = 2
MAX_MARGIN_PCT = 0.40
MIN_RISK_PCT   = 0.005   # Risque minimum 0.5% — garde-fou

# ── Volume Profile ──────────────────────────────────────
VP_LOOKBACK = 60
VP_BINS     = 24
VALUE_PCT   = 0.70

# ── Entrée ──────────────────────────────────────────────
TOL_MULT    = 0.8
MIN_SCORE   = 4.0
MIN_RR      = 1.0
MIN_RANGE   = 0.5
VOL_MULT    = 1.2

# ── CDV ─────────────────────────────────────────────────
CDV_PERIOD  = 30

# ── SL / TP ─────────────────────────────────────────────
ATR_PERIOD  = 14
ATR_SL      = 1.2
ATR_TP1     = 1.8        # TP1 = 1.8x ATR — RR 1:1.5
MIN_SL_DIST = 0.30       # Distance SL minimale absolue ($)
MIN_ATR     = 0.40       # ATR minimum ($) — filtre session morte (asiatique 01h-06h UTC)

# ── Runner — même logique que bots existants ────────────
RUNNER_PCT       = 0.50   # 50% des contrats = Lot 2 (runner)
RUNNER_TRAIL_ATR = 1.5    # Chandelier Exit = 1.5x ATR depuis highest close
RUNNER_MAX_STALL = 4      # Time exit : 4 bougies sans nouveau plus haut

# ── Sécurité ────────────────────────────────────────────
COOLDOWN_AFTER_SL = 5 * 60

# ── Drawdown protection ──────────────────────────────────
DD_LEVEL1       = 0.05
DD_LEVEL2       = 0.10
DD_LEVEL3       = 0.15
DD_PAUSE        = 3600
REDUCED_RISK_HOURS = [6, 13, 15, 17]
REDUCED_RISK_PCT   = 0.005

# ── Loop ────────────────────────────────────────────────
LOOP_SECONDS   = 60
CANDLES_NEEDED = 120

# ── Mode ────────────────────────────────────────────────
PAPER_MODE = True

# ── Journal N8N ─────────────────────────────────────────
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")
