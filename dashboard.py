# ═══════════════════════════════════════════════════════
# DASHBOARD WEB — Volume Profile Scalper Bot
# Flask server qui affiche l'état du bot en temps réel
# ═══════════════════════════════════════════════════════

from flask import Flask, jsonify, render_template_string
import os

app = Flask(__name__)

# Référence à l'état du bot (injecté depuis bot.py)
_state = None
_trades = []

def init(state, trades):
    global _state, _trades
    _state = state
    _trades = trades

HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VP Scalper Bot · XAU/USDT</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#07090c; color:#bdd0e0; font-family:'Inter',system-ui,sans-serif; min-height:100vh; }

  .header {
    background:#0b0e14; border-bottom:1px solid #18202e;
    padding:14px 24px; display:flex; align-items:center; gap:14px;
  }
  .logo {
    width:36px; height:36px; border-radius:8px;
    background:linear-gradient(135deg,#22d3ee,#60a5fa);
    display:flex; align-items:center; justify-content:center;
    font-size:14px; font-weight:900; color:#fff;
  }
  .header h1 { font-size:16px; font-weight:800; color:#f5c842; }
  .header p  { font-size:10px; color:#3a5060; letter-spacing:2px; text-transform:uppercase; }
  .badge {
    margin-left:auto; padding:5px 12px; border-radius:20px; font-size:11px; font-weight:700;
    background:#0a2a1a; color:#34d399; border:1px solid #34d39944;
  }
  .badge.paper { background:#1a2a0a; color:#d4a832; border-color:#d4a83244; }

  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; padding:16px 20px; }
  .card {
    background:#0f1218; border:1px solid #18202e; border-radius:10px; padding:14px 16px;
  }
  .card .label { font-size:9px; color:#3a5060; text-transform:uppercase; letter-spacing:1.2px; margin-bottom:6px; }
  .card .value { font-size:22px; font-weight:800; font-family:monospace; line-height:1; }
  .card .sub   { font-size:10px; color:#3a5060; margin-top:4px; }

  .green  { color:#34d399; }
  .red    { color:#f87171; }
  .gold   { color:#f5c842; }
  .blue   { color:#60a5fa; }
  .purple { color:#a78bfa; }
  .cyan   { color:#22d3ee; }

  .section { padding:0 20px 16px; }
  .section h2 { font-size:11px; color:#3a5060; text-transform:uppercase; letter-spacing:1.5px; margin-bottom:10px; }

  .pos-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:10px; }

  .position-card {
    background:#0f1218; border-radius:12px; padding:16px 18px;
    border:1px solid #18202e; position:relative; overflow:hidden;
  }
  .position-card.long  { border-color:#34d39944; }
  .position-card.short { border-color:#f8717144; }
  .position-card.flat  { border-color:#18202e; }

  .pos-header { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
  .pos-badge {
    padding:4px 12px; border-radius:20px; font-size:12px; font-weight:800;
  }
  .pos-badge.long  { background:#34d39922; color:#34d399; border:1px solid #34d39944; }
  .pos-badge.short { background:#f8717122; color:#f87171; border:1px solid #f8717144; }
  .pos-badge.flat  { background:#3a506022; color:#3a5060; border:1px solid #3a506044; }
  .setup-tag { font-size:10px; color:#22d3ee; background:#22d3ee15; padding:2px 8px; border-radius:6px; font-weight:700; }

  .pos-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
  .pos-item .pl { font-size:10px; color:#3a5060; margin-bottom:3px; }
  .pos-item .pv { font-size:15px; font-weight:800; font-family:monospace; }

  .progress-bar {
    height:6px; background:#18202e; border-radius:3px; margin-top:14px; overflow:hidden; position:relative;
  }
  .progress-fill { height:100%; border-radius:3px; transition:width .5s; }

  table { width:100%; border-collapse:collapse; font-size:11px; }
  th { text-align:left; padding:6px 10px; color:#3a5060; font-weight:600; font-size:9px; text-transform:uppercase; letter-spacing:1px; border-bottom:1px solid #18202e; }
  td { padding:8px 10px; border-bottom:1px solid #0b0e14; }
  tr:hover td { background:#0b0e14; }
  .tp-badge { padding:2px 7px; border-radius:4px; font-size:9px; font-weight:700; }
  .tp-badge.tp { background:#34d39922; color:#34d399; }
  .tp-badge.sl { background:#f8717122; color:#f87171; }
  .tp-badge.be { background:#3a506033; color:#9ca8b4; }

  .refresh { font-size:9px; color:#3a5060; text-align:center; padding:10px; }
  #dot { display:inline-block; width:6px; height:6px; border-radius:50%; background:#34d399; margin-right:4px; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
</style>
</head>
<body>

<div class="header">
  <div class="logo">VP</div>
  <div>
    <h1>Volume Profile Scalper</h1>
    <p>XAU/USDT Perpetual · MEXC Futures · 1m</p>
  </div>
  <div class="badge paper" id="mode-badge">📄 PAPER MODE</div>
</div>

<!-- KPIs -->
<div class="grid" id="kpis">
  <div class="card"><div class="label">Capital</div><div class="value gold" id="capital">$–</div><div class="sub">Départ: $500</div></div>
  <div class="card"><div class="label">P&L Total</div><div class="value" id="pnl">$–</div></div>
  <div class="card"><div class="label">Win Rate</div><div class="value" id="wr">–%</div><div class="sub" id="wl">0W / 0L / 0BE</div></div>
  <div class="card"><div class="label">Trades</div><div class="value blue" id="trades">0</div></div>
  <div class="card"><div class="label">Positions</div><div class="value cyan" id="pos-count">0/2</div></div>
</div>

<!-- Positions en cours -->
<div class="section">
  <h2>Positions en cours</h2>
  <div class="pos-row" id="pos-row">
    <div class="position-card flat">
      <div class="pos-header">
        <span class="pos-badge flat">FLAT</span>
        <span style="font-size:12px;color:#3a5060">En attente de signal…</span>
      </div>
    </div>
  </div>
</div>

<!-- Historique -->
<div class="section">
  <h2>Historique des trades</h2>
  <div style="background:#0f1218;border:1px solid #18202e;border-radius:12px;overflow:hidden">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Date</th><th>Setup</th><th>Côté</th>
          <th>Entrée</th><th>Sortie</th><th>P&L</th><th>Résultat</th>
        </tr>
      </thead>
      <tbody id="trades-tbody">
        <tr><td colspan="8" style="text-align:center;color:#3a5060;padding:20px">Aucun trade pour l'instant…</td></tr>
      </tbody>
    </table>
  </div>
</div>

<div class="refresh"><span id="dot"></span>Mise à jour automatique toutes les 5 secondes</div>

<script>
function posCardHTML(p, currentPrice) {
  const isLong = p.side === 'long';
  const pnlNow = isLong
    ? (currentPrice - p.entry) / p.entry * 100
    : (p.entry - currentPrice) / p.entry * 100;
  const total = Math.abs(p.tp - p.sl);
  const current = isLong ? currentPrice - p.sl : p.sl - currentPrice;
  const pct = Math.max(0, Math.min(100, total ? (current / total) * 100 : 0));
  const fillColor = pct > 50 ? '#34d399' : pct > 25 ? '#f5c842' : '#f87171';

  return `
    <div class="position-card ${p.side}">
      <div class="pos-header">
        <span class="pos-badge ${p.side}">${isLong ? '▲ LONG' : '▼ SHORT'}</span>
        <span class="setup-tag">${p.setup || 'VP'}</span>
      </div>
      <div class="pos-grid">
        <div class="pos-item"><div class="pl">Entrée</div><div class="pv">$${p.entry.toFixed(2)}</div></div>
        <div class="pos-item"><div class="pl">Stop Loss</div><div class="pv red">$${p.sl.toFixed(2)}</div></div>
        <div class="pos-item"><div class="pl">Take Profit</div><div class="pv green">$${p.tp.toFixed(2)}</div></div>
        <div class="pos-item"><div class="pl">P&L actuel</div><div class="pv ${pnlNow >= 0 ? 'green' : 'red'}">${pnlNow >= 0 ? '+' : ''}${pnlNow.toFixed(2)}%</div></div>
        <div class="pos-item"><div class="pl">Contrats</div><div class="pv">${p.contracts}</div></div>
        <div class="pos-item"><div class="pl">BE actif</div><div class="pv ${p.be_activated ? 'green' : ''}">${p.be_activated ? 'Oui' : 'Non'}</div></div>
      </div>
      <div class="progress-bar">
        <div class="progress-fill" style="width:${pct}%;background:${fillColor}"></div>
      </div>
    </div>
  `;
}

async function update() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    // KPIs
    document.getElementById('capital').textContent = '$' + d.capital.toFixed(2);
    document.getElementById('pnl').textContent = (d.pnl >= 0 ? '+' : '') + '$' + d.pnl.toFixed(2);
    document.getElementById('pnl').className = 'value ' + (d.pnl >= 0 ? 'green' : 'red');
    document.getElementById('wr').textContent = d.wr.toFixed(0) + '%';
    document.getElementById('wr').className = 'value ' + (d.wr >= 65 ? 'green' : d.wr >= 50 ? 'gold' : 'red');
    document.getElementById('wl').textContent = d.wins + 'W / ' + d.losses + 'L / ' + d.breakevens + 'BE';
    document.getElementById('trades').textContent = d.total_trades;
    document.getElementById('pos-count').textContent = d.positions.length + '/' + d.max_positions;

    document.getElementById('mode-badge').textContent = d.paper_mode ? '📄 PAPER MODE' : '💰 LIVE MODE';
    document.getElementById('mode-badge').className = 'badge' + (d.paper_mode ? ' paper' : '');

    // Positions
    const posRow = document.getElementById('pos-row');
    if (d.positions && d.positions.length > 0) {
      posRow.innerHTML = d.positions.map(p => posCardHTML(p, d.current_price)).join('');
    } else {
      posRow.innerHTML = `
        <div class="position-card flat">
          <div class="pos-header">
            <span class="pos-badge flat">FLAT</span>
            <span style="font-size:12px;color:#3a5060">${d.reason || 'En attente de signal…'}</span>
          </div>
        </div>`;
    }

    // Historique
    const tbody = document.getElementById('trades-tbody');
    if (d.trades && d.trades.length > 0) {
      tbody.innerHTML = d.trades.slice().reverse().slice(0, 20).map((t, i) => `
        <tr>
          <td style="color:#3a5060">${d.trades.length - i}</td>
          <td style="color:#3a5060;font-size:10px">${t.date || '–'}</td>
          <td style="color:#22d3ee;font-size:10px;font-weight:700">${t.setup || 'VP'}</td>
          <td style="color:${t.side === 'long' ? '#34d399' : '#f87171'};font-weight:700">
            ${t.side === 'long' ? '▲ L' : '▼ S'}
          </td>
          <td style="font-family:monospace">$${t.entry.toFixed(1)}</td>
          <td style="font-family:monospace">$${t.exit.toFixed(1)}</td>
          <td style="color:${t.pnl >= 0 ? '#34d399' : '#f87171'};font-weight:700">
            ${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}
          </td>
          <td>
            <span class="tp-badge ${t.res === 'TP' || t.res === 'TP1' ? 'tp' : t.res === 'SL entrée' ? 'be' : 'sl'}">
              ${t.res === 'TP' ? '✅ TP' : t.res === 'TP1' ? '🎯 TP1' : t.res === 'SL entrée' ? '⚪ SL entrée' : '❌ SL'}
            </span>
          </td>
        </tr>
      `).join('');
    }

  } catch(e) {
    console.error('Erreur refresh:', e);
  }
}

update();
setInterval(update, 5000);
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/status')
def status():
    if not _state:
        return jsonify({"error": "Bot non initialisé"}), 503

    positions = [
        {
            "side":         p["side"],
            "entry":        p["entry"],
            "sl":           p["sl"],
            "tp":           p["tp"],
            "contracts":    p["contracts"],
            "setup":        p.get("setup", "VP"),
            "be_activated": p.get("be_activated", False),
        }
        for p in _state.positions
    ]

    try:
        from config import MAX_POSITIONS as _MAXP
    except Exception:
        _MAXP = 2

    return jsonify({
        "positions":      positions,
        "max_positions":  _MAXP,
        "current_price":  _state.last_price,
        "capital":        round(_state.paper_balance, 2),
        "pnl":            round(_state.paper_pnl, 2),
        "wr":             round(_state.wr, 1),
        "wins":           _state.wins,
        "losses":         _state.losses,
        "breakevens":     getattr(_state, "breakevens", 0),
        "total_trades":   _state.total_trades,
        "daily_pnl":      round(_state.daily_pnl, 2),
        "paper_mode":     _state.paper_mode,
        "trades":         [
            {
                "entry": t["e"],
                "exit":  t["x"],
                "side":  t["side"],
                "pnl":   t["pnl"],
                "res":   t["res"],
                "setup": t.get("setup", "VP"),
                "date":  t.get("date", "")
            }
            for t in _trades[-50:]
        ]
    })


def run(host="0.0.0.0", port=None):
    port = port or int(os.environ.get("PORT", 8080))
    app.run(host=host, port=port, debug=False, use_reloader=False)
