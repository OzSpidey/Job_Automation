// Opt-Oz Dashboard
let surfaceChart = null;
let paused = false;
const REFRESH_MS = 10000;

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) return null;
  return r.json();
}

async function refresh() {
  const [status, positions, greeks, stress, trades] = await Promise.all([
    api('/api/status'),
    api('/api/positions'),
    api('/api/greeks'),
    api('/api/stress'),
    api('/api/trades'),
  ]);

  if (status) renderStatus(status);
  if (greeks) renderGreeks(greeks);
  if (positions) renderPositions(positions);
  if (stress) renderStress(stress);
  if (trades) renderTrades(trades);
  loadSurface();
}

function renderStatus(s) {
  document.getElementById('nav-display').textContent = `NAV: $${s.nav.toLocaleString('en', {minimumFractionDigits: 2})}`;
  document.getElementById('last-update').textContent = s.last_update ? s.last_update.substring(0, 19) : '—';
  document.getElementById('mode-badge').textContent = (s.mode || 'PAPER').toUpperCase();
  document.getElementById('s-positions').textContent = s.open_positions ?? '—';

  const btn = document.getElementById('pause-btn');
  btn.textContent = s.paused ? 'Resume' : 'Pause';
  btn.className = s.paused ? 'paused' : '';

  const banner = document.getElementById('risk-banner');
  if (s.blocks_new_trades) {
    banner.textContent = 'RISK BLOCK: New trade generation is paused due to portfolio risk limit breach.';
    banner.className = 'risk-banner';
  } else if (s.risk_violations > 0) {
    banner.textContent = `${s.risk_violations} risk warning(s) active — review positions.`;
    banner.className = 'risk-banner warn';
  } else {
    banner.className = 'risk-banner hidden';
  }
}

function renderGreeks(g) {
  const d = g.delta ?? 0;
  const t = g.theta ?? 0;
  const v = g.vega ?? 0;
  const gm = g.gamma ?? 0;

  const dEl = document.getElementById('s-delta');
  dEl.textContent = d.toFixed(1);
  dEl.className = 'stat-value' + (Math.abs(d) > 50 ? ' red' : '');

  const tEl = document.getElementById('s-theta');
  tEl.textContent = `$${t.toFixed(1)}`;
  tEl.className = 'stat-value' + (t >= 0 ? ' green' : ' red');

  document.getElementById('s-vega').textContent = v.toFixed(1);
  document.getElementById('s-gamma').textContent = gm.toFixed(4);
}

function renderPositions(positions) {
  const tbody = document.getElementById('positions-body');
  tbody.innerHTML = '';

  positions.forEach(p => {
    const pnl = p.unrealized_pnl ?? 0;
    const pnlClass = pnl >= 0 ? 'positive' : 'negative';

    // Summarise leg structure
    const legDesc = (p.legs || []).map(l =>
      `${l.side === 'SELL' ? '−' : '+'}${l.right}${l.strike}`
    ).join(' / ');

    // Net delta of position
    const netDelta = (p.legs || []).reduce((sum, l) => {
      const sign = l.side === 'SELL' ? -1 : 1;
      return sum + sign * (l.delta ?? 0) * (l.quantity ?? 1) * 100;
    }, 0);
    const netTheta = (p.legs || []).reduce((sum, l) => {
      const sign = l.side === 'SELL' ? -1 : 1;
      return sum + sign * (l.theta ?? 0) * (l.quantity ?? 1) * 100;
    }, 0);
    const netVega = (p.legs || []).reduce((sum, l) => {
      const sign = l.side === 'SELL' ? -1 : 1;
      return sum + sign * (l.vega ?? 0) * (l.quantity ?? 1) * 100;
    }, 0);

    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${p.id}</td>
      <td>${p.strategy}</td>
      <td><strong>${p.underlying}</strong></td>
      <td>${legDesc}</td>
      <td>${p.dte ?? '—'}</td>
      <td>$${(p.entry_credit ?? 0).toFixed(2)}</td>
      <td class="${pnlClass}">$${pnl.toFixed(2)}</td>
      <td class="negative">$${(p.max_loss ?? 0).toFixed(2)}</td>
      <td>${netDelta.toFixed(1)}</td>
      <td class="${netTheta >= 0 ? 'positive' : 'negative'}">$${netTheta.toFixed(2)}</td>
      <td>${netVega.toFixed(1)}</td>
    `;
    tbody.appendChild(row);
  });

  if (positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:var(--muted)">No open positions</td></tr>';
  }
}

function renderStress(s) {
  const grid = document.getElementById('stress-grid');
  grid.innerHTML = '';
  const stressEl = document.getElementById('s-stress');

  if (!s.scenarios) {
    stressEl.textContent = '—';
    return;
  }

  stressEl.textContent = `${(s.worst_case_pct * 100).toFixed(1)}%`;
  stressEl.className = 'stat-value' + (s.blocks_new_trades ? ' red' : ' green');

  s.scenarios.forEach(sc => {
    const card = document.createElement('div');
    card.className = 'stress-card ' + (sc.breaches_limit ? 'breach' : 'ok');
    card.innerHTML = `
      <div class="stress-name">${sc.name.replace(/_/g, ' ')}</div>
      <div class="stress-pnl ${sc.portfolio_pnl >= 0 ? 'positive' : 'negative'}">
        $${sc.portfolio_pnl.toFixed(0)}
      </div>
      <div style="color:var(--muted);font-size:11px">
        (${(sc.pnl_pct_nav * 100).toFixed(1)}% NAV)
      </div>
    `;
    grid.appendChild(card);
  });
}

function renderTrades(trades) {
  const tbody = document.getElementById('trades-body');
  tbody.innerHTML = '';

  [...trades].reverse().forEach(t => {
    const row = document.createElement('tr');
    const legSummary = (t.legs || []).map(l =>
      `${l.side === 'SELL' ? 'SELL' : 'BUY'} ${l.right}${l.strike}`
    ).join(', ');

    row.innerHTML = `
      <td>${(t.timestamp || '').substring(0, 19)}</td>
      <td>${(t.legs && t.legs[0]) ? t.legs[0].symbol : '—'}</td>
      <td>${t.strategy || '—'}</td>
      <td>${legSummary}</td>
      <td>$${(t.fill_price ?? 0).toFixed(2)}</td>
      <td>$${(t.commission ?? 0).toFixed(2)}</td>
    `;
    tbody.appendChild(row);
  });

  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted)">No trades yet</td></tr>';
  }
}

async function loadSurface() {
  const symbol = document.getElementById('surface-symbol').value;
  const data = await api(`/api/surface/${symbol}`);

  const ctx = document.getElementById('surface-chart').getContext('2d');
  if (!data || !data.term_structure) {
    if (surfaceChart) surfaceChart.destroy();
    surfaceChart = null;
    return;
  }

  const labels = data.term_structure.map(p => `${p.dte}d`);
  const values = data.term_structure.map(p => (p.iv * 100).toFixed(2));

  if (surfaceChart) surfaceChart.destroy();
  surfaceChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: `${symbol} ATM IV (%)`,
        data: values,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,0.1)',
        borderWidth: 2,
        pointRadius: 4,
        tension: 0.3,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { labels: { color: '#e6edf3', font: { family: 'monospace', size: 12 } } },
        tooltip: { mode: 'index' },
      },
      scales: {
        x: { ticks: { color: '#8b949e' }, grid: { color: '#30363d' } },
        y: {
          ticks: { color: '#8b949e', callback: v => `${v}%` },
          grid: { color: '#30363d' },
        }
      }
    }
  });
}

async function togglePause() {
  const s = await api('/api/status');
  const endpoint = s && s.paused ? '/api/resume' : '/api/pause';
  await fetch(endpoint, { method: 'POST' });
  refresh();
}

// Auto-refresh
refresh();
setInterval(refresh, REFRESH_MS);
