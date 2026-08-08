# ═══════════════════════════════════════════════════════════════════
# CONFIG V4 — VP+VWAP+Delta+MTF Scalper · XAU/USDT · BITGET
# Architecture quant institutionnelle · Sans EMA · DXY 4h
# Capital $2000 · Risque 2% · Marge 30% max · DD 20% max
# ═══════════════════════════════════════════════════════════════════

import os

# ── API Bitget ───────────────────────────────────────────────────
API_KEY    = os.environ.get("API_KEY",    "")
API_SECRET = os.environ.get("API_SECRET", "")
PASSPHRASE = os.environ.get("PASSPHRASE", "")

# ── Telegram ─────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── N8N Journal ──────────────────────────────────────────────────
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")

# ── Futures ──────────────────────────────────────────────────────
SYMBOL    = "XAUUSDT"
LEVERAGE  = 20    # Levier paper Bitget
OPEN_TYPE = 1  # 1 = Isolated

# ── Timeframes ───────────────────────────────────────────────────
INTERVAL_SIGNAL   = "5m"    # Signal décisionnel
INTERVAL_CONFIRM  = "1m"    # Confirmation entrée
INTERVAL_HTF_1H   = "1H"    # Structure + Sweep (MAJUSCULE Bitget)
INTERVAL_HTF_4H   = "4H"    # Liquidité + OB + DXY (MAJUSCULE Bitget)
CANDLES_5M        = 650     # ~54h historique 5m _ source VP Daily actuel + précédent
CANDLES_1M        = 10      # Confirmation micro
CANDLES_1H        = 90      # Bitget limite 100 max (90+10=100)
CANDLES_4H        = 100     # Liquidité 4h

# ── Symbole DXY ──────────────────────────────────────────────────
# Bitget ne propose pas DXY directement
# Alternative : utiliser USDIndex ou reconstruire depuis paires FX
# En paper mode : désactiver si non disponible → DXY_ENABLED = False
DXY_SYMBOL  = "USDIDX"     # À ajuster selon disponibilité Bitget
DXY_ENABLED = False         # Activer quand symbole DXY confirmé sur Bitget

# ── Capital & Risk ───────────────────────────────────────────────
CAPITAL        = 2000.0     # Capital de référence fixe
RISK_PER_TRADE = 0.020      # 2% risque nominal
MARGIN_CAP     = 0.30       # Marge max 30% du capital par trade
LOT_MAX        = 0.08       # Lot maximum par trade
LOT_MIN        = 0.03       # Lot minimum viable — fermeture partielle 2/3+1/3
                             # lot_tp2 = 0.03 × 1/3 = 0.01 lot minimum cohérent

# ── Gestion de position — 1 position, fermeture partielle ────────
# 1 seule position ouverte à la fois
# Fermeture partielle au TP1 puis TP2 — PAS DE RUNNER
LOT_RATIO_TP1  = 2/3        # 2/3 du lot fermé au TP1
LOT_RATIO_TP2  = 1/3        # 1/3 du lot fermé au TP2
MAX_POSITIONS  = 1          # 1 seule position simultanée

# ── Volume Profile ───────────────────────────────────────────────
VP_LOOKBACK = 48            # 48 × 5m = 4h
VP_BINS     = 48
VALUE_PCT   = 0.68          # Value Area 68%
HVN_MULT    = 1.4
TOL_MULT    = 0.6           # Tolérance autour niveaux VP (× ATR)

# ── VWAP ─────────────────────────────────────────────────────────
VWAP_RESET_UTC = 0

# ── Setups continuation de tendance ──────────────────────────────
# TF1 : Pullback VWAP central en tendance (ADX > 22 requis)
# TF2 : Retest POC en support/résistance après cassure (ADX > 25 requis)
TF_ADX_MIN_TREND   = 22    # ADX minimum pour activer TF1
TF_ADX_MIN_RETEST  = 25    # ADX minimum pour activer TF2
TF_VWAP_TOL        = 0.8   # Tolérance autour VWAP central (× ATR)
TF_DELTA_EXHAUST   = 0.50  # Delta épuisé sur pullback (moins strict que ±2SD)

# ── Delta Volume ─────────────────────────────────────────────────
DELTA_PERIOD     = 20
DELTA_IMBALANCE  = 0.60
DELTA_EXHAUSTION = 0.75

# ── ATR ──────────────────────────────────────────────────────────
ATR_PERIOD  = 14
ATR_SL      = 1.2           # SL = ATR × 1.2
ATR_TP1     = 1.8           # TP1 = ATR × 1.8 (fixe)
MIN_ATR     = 0.50
MIN_SL_DIST = 0.25

# ── EMA — SUPPRIMÉE ──────────────────────────────────────────────
# La direction est gérée par Structure HH/HL 1h + DXY 4h
# EMA_FAST et EMA_SLOW supprimées volontairement

# ── Score ────────────────────────────────────────────────────────
MIN_SCORE  = 3.5
MIN_RR     = 1.3
MIN_RANGE  = 1.2
VOL_MULT   = 1.15

# ── RR Dynamique ─────────────────────────────────────────────────
RRD_CALM      = 2.2         # ATR ratio < 0.7 (marché calme)
RRD_NORMAL    = 1.9         # ATR ratio < 1.0
RRD_VOLATILE  = 1.6         # ATR ratio < 1.5
RRD_EXPLOSIVE = 1.3         # ATR ratio >= 1.5
RRD_MIN       = 1.3
RRD_MAX       = 3.0

# ── Liquidité 4h — Equal Highs/Lows ─────────────────────────────
LIQ_LOOKBACK  = 30          # Bougies 4h pour détecter equal H/L
LIQ_TOLERANCE = 0.002       # 0.2% tolérance
LIQ_TP_MARGIN = 0.3         # Marge ATR avant obstacle TP

# ── Order Blocks 4h ──────────────────────────────────────────────
OB_VOL_MULT = 2.0           # Volume > 2× moyenne = OB institutionnel
OB_LOOKBACK = 30
OB_BONUS    = 1.5           # Bonus score si dans un OB

# ── Sweep Detection 1h ───────────────────────────────────────────
SWEEP_LOOKBACK = 20
SWEEP_TOL      = 0.001      # 0.1% tolérance
SWEEP_EXPIRY   = 14400      # Sweep actif 4h max
SWEEP_BONUS    = 1.5        # Bonus score si sweep aligné

# ── Structure HH/HL 1h — remplace EMA ───────────────────────────
STRUCT_LOOKBACK = 10
STRUCT_BONUS    = 1.0       # NE PAS RÉDUIRE — couche la plus puissante

# ── Corrélation DXY 4h ───────────────────────────────────────────
DXY_LOOKBACK = 10           # Bougies 4h pour structure HH/HL DXY
DXY_BONUS    = 1.0          # Bonus si DXY confirme direction or
DXY_MALUS    = 1.5          # Malus si DXY contredit direction or
# NEUTRE : pas d'ajustement — PAS DE FILTRE DUR

# ── DD Protection institutionnel — 4 niveaux ─────────────────────
# Basé sur statistiques réelles de la stratégie V4
# Variance naturelle < 6% : NE PAS INTERVENIR
#
# Niveau 1 (< 6%)   → Rien ne change — variance naturelle
# Niveau 2 (6-10%)  → Plus sélectif uniquement (score + cooldown)
# Niveau 3 (10-15%) → Risque réduit 1.0% + très sélectif
# Niveau 4 (> 15%)  → Arrêt complet + reprise manuelle

DD_NORMAL_ZONE   = 0.06     # < 6%  → Niveau 1 : variance naturelle, RIEN
DD_ALERT_YELLOW  = 0.06     # 6-10% → Niveau 2 : plus sélectif
DD_ALERT_ORANGE  = 0.10     # 10-15%→ Niveau 3 : risque réduit
DD_STOP_TOTAL    = 0.15     # > 15% → Niveau 4 : arrêt complet + Telegram

# Risque par niveau
DD_RISK_NORMAL   = 0.020    # 2.0% — Niveaux 1 et 2 (inchangé)
DD_RISK_ORANGE   = 0.010    # 1.0% — Niveau 3 (réduit)
# Niveau 4 → arrêt, pas de risque

# Score malus par niveau
DD_SCORE_MALUS_N = 0.0      # Niveau 1 : rien
DD_SCORE_MALUS_Y = 0.5      # Niveau 2 : +0.5
DD_SCORE_MALUS_O = 1.0      # Niveau 3 : +1.0 supplémentaire (cumulatif)

# Cooldown par niveau
DD_COOLDOWN_N    = 900      # Niveau 1 : 15 min (inchangé)
DD_COOLDOWN_Y    = 1200     # Niveau 2 : 20 min
DD_COOLDOWN_O    = 1800     # Niveau 3 : 30 min

# Pauses séries perdantes (indépendantes du DD%)
DD_CONSEC_L1     = 5        # 5 pertes consécutives → pause 15 min
DD_CONSEC_L2     = 7        # 7 pertes consécutives → pause 30 min
DD_PAUSE_CONSEC1 = 900      # 15 min
DD_PAUSE_CONSEC2 = 1800     # 30 min

# ── Cooldown directionnel ────────────────────────────────────────
COOLDOWN_AFTER_SL_LONG  = 900   # 15 min après SL LONG
COOLDOWN_AFTER_SL_SHORT = 900   # 15 min après SL SHORT

# ── Fermeture weekend ─────────────────────────────────────────────
WEEKEND_CLOSE_DAY  = 4      # Vendredi (0=lundi)
WEEKEND_CLOSE_HOUR = 20     # 20h UTC

# ── Loop ─────────────────────────────────────────────────────────
LOOP_SECONDS = 30

# ── Mode ─────────────────────────────────────────────────────────
PAPER_MODE = True
