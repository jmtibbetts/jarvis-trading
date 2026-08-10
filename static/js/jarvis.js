'use strict';

const API  = (p) => fetch(`/api${p}`).then(r=>r.json());
const POST = (p,b) => fetch(`/api${p}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
const PUT  = (p,b) => fetch(`/api${p}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
const DEL  = (p)   => fetch(`/api${p}`,{method:'DELETE'}).then(r=>r.json());
// Flexible helper used by queue actions — supports GET/POST/DELETE
const api  = (p, opts={}) => {
  if (!opts.method || opts.method === 'GET') return API(p);
  if (opts.method === 'DELETE') return DEL(p);
  return POST(p, opts.body ? JSON.parse(opts.body) : {});
};


/* ── Toast Notifications ────────────────────────────────────────────────────── */
function showToast(msg, type = 'success') {
  // type: 'success' | 'danger' | 'warning' | 'info'
  const colorMap = { success: '#198754', danger: '#dc3545', warning: '#ffc107', info: '#0dcaf0' };
  const bg = colorMap[type] || colorMap.success;
  const container = document.getElementById('toast-container') || (() => {
    const div = document.createElement('div');
    div.id = 'toast-container';
    div.style.cssText = 'position:fixed;bottom:1.5rem;right:1.5rem;z-index:9999;display:flex;flex-direction:column;gap:0.5rem;';
    document.body.appendChild(div);
    return div;
  })();
  const toast = document.createElement('div');
  toast.style.cssText = `background:${bg};color:#fff;padding:0.75rem 1.25rem;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.4);font-size:.9rem;max-width:360px;word-break:break-word;animation:fadeInUp .2s ease;`;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; toast.style.transition = 'opacity .4s'; setTimeout(() => toast.remove(), 400); }, 3500);
}

let allSignals=[], allThreats=[], allNews=[], equityChart=null;
let signalAnalysisChart=null, signalAnalysisData=null;

/* ── Formatters ─────────────────────────────────────────────────────────── */
const fmt2   = v => v!=null ? Number(v).toFixed(2) : 'N/A';
const fmtPct = v => v!=null ? `${v>=0?'+':''}${Number(v).toFixed(2)}%` : 'N/A';
const fmtPrice = v => {
  if(v==null) return 'N/A'; v=Number(v);
  return v>1000?`$${v.toLocaleString('en',{maximumFractionDigits:0})}`:v>1?`$${v.toFixed(2)}`:`$${v.toFixed(6)}`;
};
const timeAgo = iso => {
  if(!iso) return '';
  const m=Math.floor((Date.now()-new Date(iso).getTime())/60000);
  if(m<1) return 'just now'; if(m<60) return `${m}m ago`;
  const h=Math.floor(m/60); if(h<24) return `${h}h ago`;
  return `${Math.floor(h/24)}d ago`;
};
const sevColor = {Critical:'danger',High:'warning',Medium:'primary',Low:'success'};
const escapeHtml = value => String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'
})[ch]);

/* ── Job Indicators ──────────────────────────────────────────────────────── */
async function refreshJobIndicators() {
  try {
    const jobs = await API('/jobs/status');
    const el = document.getElementById('job-indicators');
    if(!el) return;
    el.innerHTML = Object.entries(jobs).map(([name,info])=>{
      const c={ok:'success',running:'warning',error:'danger',idle:'secondary'}[info.status]||'secondary';
      const icon={ok:'✓',running:'⏳',error:'✗',idle:'·'}[info.status]||'·';
      return `<span class="badge bg-${c}" title="${name}: ${info.last||'never'}${info.error?' — '+info.error:''}">${icon} ${name}</span>`;
    }).join('');
  } catch(e){}
}

/* ── Regime Badge ────────────────────────────────────────────────────────── */
async function refreshRegimeBadge() {
  try {
    const r = await API('/regime');
    const el = document.getElementById('regime-badge');
    if(!el) return;
    const c = {low:'success','medium':'info','medium-high':'warning',high:'danger'}[r.risk]||'secondary';
    el.innerHTML = `<span class="badge bg-${c}">${r.label||'Unknown'}</span>`;
  } catch(e){}
}

/* ── Kill Switch ─────────────────────────────────────────────────────────── */
async function refreshKillSwitch() {
  try {
    const s = await API('/system/trading-status');
    const btn = document.getElementById('kill-switch-btn');
    const label = document.getElementById('kill-switch-label');
    if(!btn || !label) return;
    if(s.live_trading_enabled) {
      btn.className = 'btn btn-sm btn-outline-success';
      label.innerHTML = '<i class="bi bi-play-circle-fill"></i> Live Trading ON';
    } else {
      btn.className = 'btn btn-sm btn-danger';
      label.innerHTML = `<i class="bi bi-pause-circle-fill"></i> PAUSED${s.paused_reason ? ' — '+escapeHtml(s.paused_reason) : ''}`;
    }
  } catch(e){}
}

async function toggleKillSwitch() {
  try {
    const s = await API('/system/trading-status');
    if(s.live_trading_enabled) {
      if(!confirm('Pause all new live orders? Existing positions keep their stop-loss/take-profit protection.')) return;
      await POST('/system/trading-status', {enabled: false, reason: 'Paused from dashboard'});
      showToast('Live trading paused', 'warning');
    } else {
      await POST('/system/trading-status', {enabled: true});
      showToast('Live trading resumed', 'success');
    }
    refreshKillSwitch();
  } catch(e) { showToast('Could not update trading status', 'danger'); }
}

/* ── SIGNALS ──────────────────────────────────────────────────────────────── */
async function loadSignals() {
  try {
    const [data] = await Promise.all([API('/signals?limit=150'), loadTradeMode()]);
    allSignals = data;
    renderSignals();
    loadQueue();
  } catch(error) {
    document.getElementById('signal-count').textContent='Server offline';
    document.getElementById('signals-grid').innerHTML=`
      <div class="col-12"><div class="alert alert-danger mb-0">
        Jarvis is not reachable. Run <code>.\\start.ps1</code>, then refresh this page.
      </div></div>`;
  }
}

function renderSignals() {
  const status = document.getElementById('sig-filter-status').value;
  const cls    = document.getElementById('sig-filter-class').value;
  const direction = document.getElementById('sig-filter-direction').value;
  const sort   = document.getElementById('sig-sort').value;
  const search = document.getElementById('sig-search').value.trim().toLowerCase();
  const directionGroup = (s) => String(s.direction || 'Long').toLowerCase().includes('short') ? 'short' : 'long';
  const assetGroup = (s) => {
    const assetClass = (s.asset_class || '').toLowerCase();
    const symbol = (s.asset_symbol || '').toUpperCase();
    if (assetClass === 'crypto' || (symbol && !symbol.endsWith('=F') && !symbol.endsWith('=X') && (symbol.includes('/') || symbol.endsWith('-USD')))) return 'crypto';
    if (assetClass === 'futures' || assetClass === 'forex' || symbol.endsWith('=F') || symbol.endsWith('=X')) return 'futures';
    return 'stocks';
  };
  let filtered = allSignals.filter(s =>
    (!status || s.status === status) &&
    (!cls || (cls === 'paper' ? s.paper_mode : assetGroup(s) === cls)) &&
    (!direction || directionGroup(s) === direction) &&
    (!search || [s.asset_symbol, s.asset_name, s.asset_class, s.direction, s.reasoning]
      .some(value => String(value || '').toLowerCase().includes(search)))
  );
  if(sort==='score')           filtered.sort((a,b)=>(b.composite_score||b.confidence||0)-(a.composite_score||a.confidence||0));
  else if(sort==='confidence') filtered.sort((a,b)=>(b.confidence||0)-(a.confidence||0));
  else if(sort==='long-first') filtered.sort((a,b)=>(directionGroup(a)==='long'?0:1)-(directionGroup(b)==='long'?0:1)||((b.composite_score||b.confidence||0)-(a.composite_score||a.confidence||0)));
  else if(sort==='short-first') filtered.sort((a,b)=>(directionGroup(a)==='short'?0:1)-(directionGroup(b)==='short'?0:1)||((b.composite_score||b.confidence||0)-(a.composite_score||a.confidence||0)));
  else                         filtered.sort((a,b)=>new Date(b.generated_at)-new Date(a.generated_at));
  document.getElementById('signal-count').textContent=filtered.length+' signals';
  const grid=document.getElementById('signals-grid');
  if(!filtered.length){grid.innerHTML='<div class="col-12 text-center text-muted py-5">No signals match these filters</div>';return;}
  grid.innerHTML=filtered.map(function(s){
    const score  = s.composite_score||s.confidence||0;
    const dir    = (s.direction||'Long').toLowerCase();
    const isShort = dir.includes('short');
    const rawConf= s.confidence||0;
    const conf   = s.calibrated_confidence||rawConf;
    const confCls= conf>=75?'high':conf>=55?'medium':'low';
    const risk   = isShort ? s.stop_loss-s.entry_price : s.entry_price-s.stop_loss;
    const reward = isShort ? s.entry_price-s.target_price : s.target_price-s.entry_price;
    const rr     = s.entry_price&&s.target_price&&s.stop_loss&&risk>0&&reward>0
                   ? (reward/risk).toFixed(1) : 'N/A';
    const rrCls  = rr!=='N/A'&&parseFloat(rr)>=2?'text-success':rr!=='N/A'&&parseFloat(rr)>=1?'text-warning':'text-danger';
    const statusBadge={Active:'bg-success',Executed:'bg-primary',Expired:'bg-secondary',Rejected:'bg-danger',Closed:'bg-dark border border-secondary',PendingApproval:'bg-warning text-dark'}[s.status]||'bg-secondary';
    const scorePct=Math.round(score);
    const earningsBadge=s.earnings_risk?'<span class="badge bg-warning text-dark ms-1" title="Earnings risk">📅</span>':'';
    const srcBadge=s.signal_source==='opportunistic'?'<span class="badge bg-info text-dark ms-1" title="News-discovered">📰</span>':'';
    const paperBadge=s.paper_mode?'<span class="badge bg-info text-dark ms-1">Paper</span>':'';
    const isExpired=Boolean(s.expires_at&&new Date(s.expires_at).getTime()<=Date.now());
    const actionable=s.status==='Active'&&!isExpired;
    const freshnessBadge=s.freshness_score!=null?'<span class="badge bg-dark border '+(s.freshness_score>=70?'border-success text-success':s.freshness_score>=40?'border-warning text-warning':'border-danger text-danger')+' ms-1" title="Market data freshness">Fresh '+Math.round(s.freshness_score)+'</span>':'';
    const qualityBadge=s.data_quality_score!=null?'<span class="badge bg-dark border border-secondary ms-1" title="Market data quality">Data '+Math.round(s.data_quality_score)+'</span>':'';
    // default qty guess for modal prefill
    const defDollar = conf>=75?1500:conf>=55?1000:500;
    const defQty    = s.entry_price ? Math.max(1,Math.round(defDollar/s.entry_price)) : 1;
    return '<div class="col-xl-3 col-lg-4 col-md-6">' +
      '<div class="card signal-card '+dir+' h-100 signal-card-clickable" role="button" tabindex="0" '+
        'onclick="openSignalAnalysis(\''+s.id+'\')" onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();openSignalAnalysis(\''+s.id+'\')}">' +
        '<div class="card-header d-flex justify-content-between align-items-center py-2">' +
          '<div>' +
            '<span class="fw-bold">'+escapeHtml(s.asset_symbol)+'</span>' +
            '<span class="badge '+(isShort?'bg-danger':dir==='long'?'bg-success':'bg-primary')+' ms-1">'+escapeHtml(s.direction)+'</span>' +
            '<span class="badge '+(isExpired?'bg-secondary':statusBadge)+' ms-1">'+(isExpired?'Expired':escapeHtml(s.status))+'</span>' +
            paperBadge+earningsBadge+srcBadge+freshnessBadge+qualityBadge +
          '</div>' +
          '<small class="text-muted">'+timeAgo(s.generated_at)+'</small>' +
        '</div>' +
        '<div class="card-body py-2 px-3">' +
          '<div class="d-flex justify-content-between align-items-center mb-1">' +
            '<small class="text-muted">Composite Score</small>' +
            '<span class="badge '+(scorePct>=70?'bg-success':scorePct>=50?'bg-warning text-dark':'bg-danger')+'">'+scorePct+'%</span>' +
          '</div>' +
          '<div class="conf-bar '+confCls+' mb-2" style="width:'+Math.min(100,scorePct)+'%"></div>' +
          '<div class="small mb-2 text-muted">'+escapeHtml(s.asset_name||'')+' · '+escapeHtml(s.asset_class||'')+' · '+escapeHtml(s.timeframe||'')+' · <span class="text-warning">Calibrated '+Math.round(conf)+'%</span> <span class="text-muted">(model '+Math.round(rawConf)+'%)</span></div>' +
          // ── Price levels ──────────────────────────────────────────────────
          '<div class="row g-1 mb-2">' +
            '<div class="col-4 text-center p-1 rounded" style="background:rgba(13,202,240,.08)">' +
              '<div class="text-muted" style="font-size:.65rem">ENTRY</div>' +
              '<div class="fw-bold text-info" style="font-size:.8rem">'+fmtPrice(s.entry_price)+'</div>' +
            '</div>' +
            '<div class="col-4 text-center p-1 rounded" style="background:rgba(25,135,84,.08)">' +
              '<div class="text-muted" style="font-size:.65rem">TARGET</div>' +
              '<div class="fw-bold text-success" style="font-size:.8rem">'+fmtPrice(s.target_price)+'</div>' +
            '</div>' +
            '<div class="col-4 text-center p-1 rounded" style="background:rgba(220,53,69,.08)">' +
              '<div class="text-muted" style="font-size:.65rem">STOP</div>' +
              '<div class="fw-bold text-danger" style="font-size:.8rem">'+fmtPrice(s.stop_loss)+'</div>' +
            '</div>' +
          '</div>' +
          '<div class="d-flex justify-content-between small mb-2">' +
            '<span class="text-muted">R:R Ratio</span>' +
            '<span class="fw-bold '+rrCls+'">'+(rr==='N/A'?'N/A':rr+':1')+'</span>' +
          '</div>' +
          '<p class="small text-muted mb-1" style="font-size:.72rem;line-height:1.4;max-height:60px;overflow:hidden">'+escapeHtml((s.reasoning||'').slice(0,180))+((s.reasoning||'').length>180?'…':'')+'</p>' +
          (s.invalidation?'<p class="small text-danger mb-1" style="font-size:.7rem"><i class="bi bi-x-octagon"></i> '+escapeHtml(s.invalidation.slice(0,120))+'</p>':'')+
          (s.key_risks?'<p class="small text-warning mb-0" style="font-size:.7rem"><i class="bi bi-exclamation-triangle-fill"></i> '+escapeHtml(s.key_risks.slice(0,100))+'</p>':'') +
        '</div>' +
        '<div class="card-footer py-1 d-flex gap-1">' +
          (actionable&&s.paper_mode?
            '<button class="btn btn-outline-info btn-sm flex-fill py-0" style="font-size:.72rem" '+
              'onclick="event.stopPropagation();paperExecuteSignal(\''+s.id+'\',\''+s.asset_symbol+'\',\''+s.direction+'\')">'+
              '<i class="bi bi-journal-check"></i> Paper Trade</button>'
          :actionable?
            '<button class="btn btn-success btn-sm flex-fill py-0" style="font-size:.72rem" '+
              'onclick="event.stopPropagation();openTradeModal(\''+s.id+'\',\''+s.asset_symbol+'\','+s.entry_price+','+s.target_price+','+s.stop_loss+','+defDollar+','+defQty+')">'+
              '<i class="bi bi-play-fill"></i> Execute</button>'
          :'') +
          '<button class="btn btn-outline-info btn-sm py-0" style="font-size:.72rem" title="Open full analysis" onclick="event.stopPropagation();openSignalAnalysis(\''+s.id+'\')"><i class="bi bi-bar-chart-line"></i></button>' +
          '<button class="btn btn-outline-danger btn-sm py-0" style="font-size:.72rem" title="Delete signal" onclick="event.stopPropagation();deleteSignal(\''+s.id+'\')"><i class="bi bi-trash"></i></button>' +
        '</div>' +
      '</div>' +
    '</div>';
  }).join('');
}

/* ── Trade Execution Modal ────────────────────────────────────────────────── */
let _tradeModalSigId = null;

function openTradeModal(id, sym, entry, target, stop, defDollar, defQty) {
  _tradeModalSigId = id;
  document.getElementById('tm-symbol').textContent = sym;
  document.getElementById('tm-entry').textContent  = fmtPrice(entry);
  document.getElementById('tm-target').textContent = fmtPrice(target);
  document.getElementById('tm-stop').textContent   = fmtPrice(stop);
  const rr = entry&&target&&stop&&entry>stop ? ((target-entry)/(entry-stop)).toFixed(1)+':1' : 'N/A';
  document.getElementById('tm-rr').textContent = rr;
  // default to dollar mode
  document.getElementById('tm-mode-dollar').checked = true;
  document.getElementById('tm-dollar-row').style.display = '';
  document.getElementById('tm-qty-row').style.display = 'none';
  document.getElementById('tm-dollar').value = defDollar;
  document.getElementById('tm-qty').value = defQty;
  // update qty hint
  updateTradeModalHint(entry, defDollar);
  const modal = new bootstrap.Modal(document.getElementById('tradeModal'));
  modal.show();
}

function updateTradeModalHint(entry, dollars) {
  const qty = entry ? Math.max(1, Math.round(dollars/entry)) : '?';
  document.getElementById('tm-qty-hint').textContent = '≈ '+qty+' shares/units @ '+fmtPrice(entry);
}

document.addEventListener('DOMContentLoaded', function() {
  const dollarInput = document.getElementById('tm-dollar');
  if(dollarInput) {
    dollarInput.addEventListener('input', function() {
      const entry = parseFloat(document.getElementById('tm-entry').textContent.replace(/[$,]/g,''))||0;
      updateTradeModalHint(entry, parseFloat(this.value)||0);
    });
  }
  document.querySelectorAll('input[name="tm-mode"]').forEach(function(el){
    el.addEventListener('change', function() {
      const isDollar = document.getElementById('tm-mode-dollar').checked;
      document.getElementById('tm-dollar-row').style.display = isDollar ? '' : 'none';
      document.getElementById('tm-qty-row').style.display    = isDollar ? 'none' : '';
    });
  });
});

async function submitTradeModal() {
  const id = _tradeModalSigId;
  if(!id) return;
  const isDollar = document.getElementById('tm-mode-dollar').checked;
  const entry    = parseFloat(document.getElementById('tm-entry').textContent.replace(/[$,]/g,''))||0;
  let qty;
  if(isDollar) {
    const dollars = parseFloat(document.getElementById('tm-dollar').value)||500;
    qty = entry ? Math.max(1, Math.round(dollars/entry)) : 1;
  } else {
    qty = Math.max(1, parseInt(document.getElementById('tm-qty').value)||1);
  }
  const btn = document.getElementById('tm-submit-btn');
  btn.disabled = true; btn.textContent = 'Submitting...';
  try {
    const res = await POST('/signals/'+id+'/execute', {qty: qty});
    bootstrap.Modal.getInstance(document.getElementById('tradeModal')).hide();
    alert(res.error || 'Order submitted! Qty: '+qty);
    loadSignals();
  } catch(e) {
    alert('Error: '+e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Submit Order';
  }
}


/* ── Pending Equity Trades (off-hours queue) ────────────────────────────────── */
async function loadQueue() {
  try {
    const data = await api('/signals/pending');
    const sigs  = Array.isArray(data) ? data : [];
    const badge = document.getElementById('queue-badge');
    const grid  = document.getElementById('queue-grid');
    const summary = document.getElementById('queue-summary');
    if (badge) { badge.textContent = sigs.length; badge.style.display = sigs.length ? '' : 'none'; }

    // Market hours check (client-side, ET = UTC-4 in EDT)
    const nowUTC = new Date();
    const etOffset = -4; // EDT; use -5 for EST
    const etHour = ((nowUTC.getUTCHours() + etOffset) + 24) % 24;
    const etMin  = nowUTC.getUTCMinutes();
    const etDay  = new Date(nowUTC.getTime() + etOffset * 3600000).getUTCDay(); // 0=Sun,6=Sat
    const mktOpen = etDay >= 1 && etDay <= 5 &&
      (etHour > 9 || (etHour === 9 && etMin >= 30)) && etHour < 16;
    const mktStatus = mktOpen
      ? '<span class="badge bg-success ms-2">🟢 Market Open — signals auto-execute</span>'
      : '<span class="badge bg-warning text-dark ms-2">🔴 Market Closed — signals queue until open</span>';

    if (!sigs.length) {
      grid.innerHTML = `<div class="col-12 text-center text-muted py-5">
        <i class="bi bi-check-circle display-6 d-block mb-2 text-success opacity-50"></i>
        <div class="fw-bold">No pending signals</div>
        <div class="small mt-1">${mktOpen ? 'Market is open — signals execute automatically.' : 'Signals generated during market hours execute automatically.'}</div>
      </div>`;
      if (summary) summary.style.display = 'none';
      return;
    }

    // Summary bar
    if (summary) {
      const avgConf = Math.round(sigs.reduce((a,s)=>a+(s.confidence||0),0)/sigs.length);
      const sortedByTime = [...sigs].sort((a,b) => new Date(b.generated_at||0)-new Date(a.generated_at||0));
      summary.innerHTML = `<i class="bi bi-info-circle"></i> ${mktStatus} &nbsp;·&nbsp;
        <strong>${sigs.length}</strong> signals queued off-hours &nbsp;·&nbsp;
        Avg confidence: <strong>${avgConf}%</strong> &nbsp;·&nbsp;
        <span class="text-warning">These will auto-execute when the market opens at 9:30 AM ET.</span>
        You can reject any you don't want.`;
      summary.style.display = '';
    }

    // Sort by generated_at desc (newest first) — user can override with buttons in header
    const sorted = [...sigs].sort((a,b) => new Date(b.generated_at||0) - new Date(a.generated_at||0));

    grid.innerHTML = sorted.map(s => {
      const rr = s.entry_price && s.target_price && s.stop_loss && s.entry_price > s.stop_loss
        ? ((s.target_price - s.entry_price)/(s.entry_price - s.stop_loss)).toFixed(1) : 'N/A';
      const rrCls = rr !== 'N/A' && parseFloat(rr) >= 2 ? 'text-success' : rr !== 'N/A' && parseFloat(rr) >= 1 ? 'text-warning' : 'text-danger';
      const score = s.composite_score || s.confidence || 0;
      const genAt = s.generated_at ? new Date(s.generated_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) + ' · ' + new Date(s.generated_at).toLocaleDateString([], {month:'short',day:'numeric'}) : '—';
      return `<div class="col-xl-3 col-lg-4 col-md-6">
        <div class="card h-100 border-warning">
          <div class="card-header d-flex justify-content-between align-items-center py-2">
            <div>
              <span class="fw-bold">${s.asset_symbol}</span>
              <span class="badge bg-success ms-1">${s.direction||'Long'}</span>
              <span class="badge bg-warning text-dark ms-1">⏳ Queued</span>
            </div>
            <small class="text-muted" title="${s.generated_at||''}">${timeAgo(s.generated_at)}</small>
          </div>
          <div class="card-body py-2 px-3">
            <div class="d-flex justify-content-between mb-1">
              <small class="text-muted">Score</small>
              <span class="badge ${score>=70?'bg-success':score>=50?'bg-warning text-dark':'bg-danger'}">${Math.round(score)}%</span>
            </div>
            <div class="small text-muted mb-1">${s.asset_name||s.asset_symbol} · ${s.asset_class||''} · ${s.timeframe||''}</div>
            <div class="small text-muted mb-2" style="font-size:.7rem">Generated: ${genAt}</div>
            <div class="row g-1 mb-2">
              <div class="col-4 text-center p-1 rounded" style="background:rgba(13,202,240,.08)">
                <div class="text-muted" style="font-size:.65rem">ENTRY</div>
                <div class="fw-bold text-info" style="font-size:.8rem">${fmtPrice(s.entry_price)}</div>
              </div>
              <div class="col-4 text-center p-1 rounded" style="background:rgba(25,135,84,.08)">
                <div class="text-muted" style="font-size:.65rem">TARGET</div>
                <div class="fw-bold text-success" style="font-size:.8rem">${fmtPrice(s.target_price)}</div>
              </div>
              <div class="col-4 text-center p-1 rounded" style="background:rgba(220,53,69,.08)">
                <div class="text-muted" style="font-size:.65rem">STOP</div>
                <div class="fw-bold text-danger" style="font-size:.8rem">${fmtPrice(s.stop_loss)}</div>
              </div>
            </div>
            <div class="d-flex justify-content-between small mb-2">
              <span class="text-muted">R:R</span>
              <span class="fw-bold ${rrCls}">${rr === 'N/A' ? 'N/A' : rr+':1'}</span>
            </div>
            <p class="small text-muted mb-0" style="font-size:.72rem;line-height:1.4;max-height:55px;overflow:hidden">${(s.reasoning||'').slice(0,160)}</p>
          </div>
          <div class="card-footer py-1 d-flex gap-1">
            <button class="btn btn-outline-info btn-sm flex-fill py-0" style="font-size:.75rem" onclick="forceApproveSignal('${s.id}')" title="Execute now (bypass market hours)">
              <i class="bi bi-lightning-fill"></i> Force Now
            </button>
            <button class="btn btn-outline-secondary btn-sm py-0 px-2" style="font-size:.75rem" onclick="rejectSignal('${s.id}')" title="Remove from queue">
              <i class="bi bi-x-lg"></i>
            </button>
          </div>
        </div>
      </div>`;
    }).join('');
  } catch(e) { console.error('Queue load error:', e); }
}

async function forceApproveSignal(id) {
  if (!confirm('Force-execute this signal now, even if market is closed?\nThis will attempt a market order via Alpaca immediately.')) return;
  try {
    const r = await api('/signals/'+id+'/approve', {method:'POST'});
    toast(r.ok ? `✅ Order submitted: ${r.symbol} x${r.qty}` : '❌ Approve failed', r.ok ? 'success' : 'danger');
    loadQueue(); loadSignals();
  } catch(e) { toast('❌ ' + e.message, 'danger'); }
}

async function rejectSignal(id) {
  try {
    await api('/signals/'+id+'/reject', {method:'POST'});
    toast('Signal rejected', 'secondary');
    loadQueue(); loadSignals();
  } catch(e) { toast('❌ ' + e.message, 'danger'); }
}

async function approveAllPending() {
  const sigs = document.querySelectorAll('#queue-grid .col-xl-3');
  if (!sigs.length) { toast('No pending signals', 'secondary'); return; }
  if (!confirm(`Force-execute ALL ${sigs.length} queued signals now?\nNormally these auto-execute at market open. Proceed only if you want them submitted immediately.`)) return;
  try {
    const r = await api('/signals/approve-all', {method:'POST'});
    toast(`✅ Approved: ${r.approved} | Rejected: ${r.rejected} | BP remaining: $${r.buying_power_remaining?.toFixed(0)}`, 'success');
    loadQueue(); loadSignals();
  } catch(e) { toast('❌ ' + e.message, 'danger'); }
}

async function rejectAllPending() {
  if (!confirm('Reject ALL pending signals?')) return;
  try {
    const r = await api('/signals/reject-all', {method:'POST'});
    toast(`Rejected ${r.rejected} signals`, 'secondary');
    loadQueue(); loadSignals();
  } catch(e) { toast('❌ ' + e.message, 'danger'); }
}

async function cancelAllOrders() {
  if (!confirm('Cancel ALL open Alpaca orders? This will free up buying power but cancel any working orders.')) return;
  try {
    const r = await api('/alpaca/orders', {method:'DELETE'});
    toast(`✅ All open orders cancelled. ${r.signals_reset} signals reset to Active.`, 'success');
    loadQueue(); loadSignals(); if (typeof loadOrders === 'function') loadOrders();
  } catch(e) { toast('❌ ' + e.message, 'danger'); }
}

async function executeSignal(id) {
  // legacy fallback — kept for scanner-generated signals
  const res = await POST('/signals/'+id+'/execute', {});
  alert(res.error || 'Order submitted!');
  loadSignals();
}
async function deleteSignal(id) {
  if(!confirm('Delete this signal?')) return;
  await DEL(`/signals/${id}`); loadSignals();
}
async function clearExpiredSignals() {
  if(!confirm('Delete all Expired and Rejected signals?')) return;
  const res=await DEL('/signals/clear/expired');
  alert(`Deleted ${res.deleted||0} signals`); loadSignals();
}

document.getElementById('sig-filter-status').addEventListener('change',renderSignals);
document.getElementById('queue-tab-link')?.addEventListener('click', loadQueue);
document.getElementById('sig-filter-class').addEventListener('change',renderSignals);
document.getElementById('sig-filter-direction').addEventListener('change',renderSignals);
document.getElementById('sig-sort').addEventListener('change',renderSignals);
document.getElementById('sig-search').addEventListener('input',renderSignals);

/* ── POSITIONS ────────────────────────────────────────────────────────────── */
async function loadPositions() {
  try {
    const data=await API('/positions/with-signals');
    const acct=data.account||{};
    const plCls=(acct.unrealized_pl||0)>=0?'text-success':'text-danger';
    document.getElementById('account-summary').innerHTML=`
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Equity</div><div class="fw-bold text-info">$${(acct.equity||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Cash</div><div class="fw-bold text-success">$${(acct.cash||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Market Value</div><div class="fw-bold">$${(acct.market_value||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Unrealized P&L</div><div class="fw-bold ${plCls}">$${(acct.unrealized_pl||0).toFixed(2)} (${(acct.unrealized_plpc||0).toFixed(2)}%)</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Buying Power</div><div class="fw-bold">$${(acct.buying_power||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Day Trades</div><div class="fw-bold">${acct.day_trade_count||0}</div></div></div>`;
    const tbody=document.getElementById('positions-body');
    const positions=data.positions||[];
    if(!positions.length){tbody.innerHTML='<tr><td colspan="9" class="text-center text-muted py-4">No open positions</td></tr>';return;}
    tbody.innerHTML=positions.map(p=>{
      const plpc=p.unrealized_plpc||0; const plCls=plpc>=0?'pl-positive':'pl-negative';
      const s=p.signal;
      // Signal context row
      let sigRow='';
      if(s && !s._manual){
        const sc=s.composite_score||s.confidence||0;
        const scBadge=sc>=70?'bg-success':sc>=50?'bg-warning text-dark':'bg-danger';
        const rr=s.rr?`<span class="badge bg-dark border border-secondary ms-2">R:R ${s.rr}</span>`:'';
        const prog=s.progress_pct!=null?`<div class="mt-1"><div class="small text-muted d-flex justify-content-between"><span>Trade Progress</span><span>${s.progress_pct}% to target</span></div><div class="progress mt-1" style="height:4px"><div class="progress-bar ${s.progress_pct>=100?'bg-success':s.progress_pct>=0?'bg-info':'bg-danger'}" style="width:${Math.max(0,Math.min(100,s.progress_pct||0))}%"></div></div></div>`:'';
        const timeAgoSig=s.generated_at?timeAgo(s.generated_at):'';
        sigRow=`<tr class="signal-detail-row" style="display:none">
          <td colspan="9" class="py-0">
            <div class="signal-context-panel px-3 py-2">
              <div class="row g-2 align-items-start">
                <div class="col-lg-4">
                  <div class="d-flex align-items-center gap-2 mb-1">
                    <span class="badge ${s.direction==='Long'?'bg-success':'bg-primary'}">${s.direction}</span>
                    <span class="badge ${scBadge}">Score ${sc.toFixed(0)}%</span>
                    <span class="badge bg-secondary">${s.timeframe||''}</span>
                    ${rr}
                    <span class="text-muted small ms-auto">${timeAgoSig}</span>
                  </div>
                  <div class="d-flex gap-3 small">
                    <div><span class="text-muted">Entry</span><br><span class="text-info fw-bold">${fmtPrice(s.entry_price)}</span></div>
                    <div><span class="text-muted">Target</span><br><span class="text-success fw-bold">${fmtPrice(s.target_price)}</span></div>
                    <div><span class="text-muted">Stop</span><br><span class="text-danger fw-bold">${fmtPrice(s.stop_loss)}</span></div>
                  </div>
                  ${prog}
                </div>
                <div class="col-lg-5">
                  <div class="small text-muted mb-1"><i class="bi bi-chat-text-fill text-info me-1"></i>Reasoning</div>
                  <div class="small" style="line-height:1.4;color:#ccc">${(s.reasoning||'No reasoning recorded').slice(0,300)}${(s.reasoning||'').length>300?'…':''}</div>
                </div>
                <div class="col-lg-3">
                  ${s.key_risks?`<div class="small text-muted mb-1"><i class="bi bi-exclamation-triangle-fill text-warning me-1"></i>Key Risks</div><div class="small text-warning" style="line-height:1.4">${s.key_risks.slice(0,150)}</div>`:''}
                  ${s.momentum?`<div class="small text-muted mt-2">Momentum: <span class="text-info">${s.momentum}</span></div>`:''}
                  <div class="small text-muted mt-1">Source: ${s.signal_source||'watchlist'}</div>
                </div>
              </div>
            </div>
          </td>
        </tr>`;
      } else if(s && s._manual){
        // Manual / external order — show position data only
        const dirCls = s.direction==='Long'?'text-success':'text-danger';
        sigRow=`<tr class="signal-detail-row" style="display:none">
          <td colspan="9" class="py-0">
            <div class="signal-context-panel px-3 py-2">
              <div class="d-flex align-items-center gap-3 flex-wrap">
                <span class="badge bg-secondary"><i class="bi bi-person-fill me-1"></i>Manual Order</span>
                <span class="badge ${s.direction==='Long'?'bg-success':'bg-primary'}">${s.direction}</span>
                <div class="d-flex gap-3 small ms-2">
                  <div><span class="text-muted">Avg Entry</span><br><span class="text-info fw-bold">${fmtPrice(s.entry_price)}</span></div>
                  <div><span class="text-muted">Target</span><br><span class="text-success fw-bold">—</span></div>
                  <div><span class="text-muted">Stop</span><br><span class="text-danger fw-bold">—</span></div>
                </div>
                <div class="small text-muted ms-auto">${s.reasoning||''}</div>
              </div>
              <div class="mt-2 small text-warning"><i class="bi bi-lightbulb-fill me-1"></i>No signal linked — use the <strong>Signals</strong> tab to run a scan and generate entry/exit levels for this position.</div>
            </div>
          </td>
        </tr>`;
      } else {
        sigRow=`<tr class="signal-detail-row" style="display:none"><td colspan="9" class="py-1"><div class="signal-context-panel px-3 py-2"><span class="text-muted small"><i class="bi bi-info-circle me-1"></i>No signal record — position may have been entered manually or signal expired.</span></div></td></tr>`;
      }
      return `<tr class="position-row" style="cursor:pointer" onclick="toggleSignalRow(this)">
        <td class="fw-bold">${p.symbol} <i class="bi bi-chevron-down text-muted" style="font-size:.65rem"></i></td>
        <td><span class="badge ${p.asset_class==='Crypto'?'bg-warning text-dark':'bg-primary'}">${p.asset_class}</span></td>
        <td>${Number(p.qty).toLocaleString()}</td>
        <td>${fmtPrice(p.avg_entry)}</td>
        <td>${fmtPrice(p.current_price)}</td>
        <td>$${Number(p.market_value).toLocaleString('en',{maximumFractionDigits:2})}</td>
        <td class="${plCls}">$${Number(p.unrealized_pl).toFixed(2)}</td>
        <td class="${plCls} fw-bold">${fmtPct(plpc)}</td>
        <td><button class="btn btn-outline-danger btn-sm py-0 px-1" style="font-size:.7rem" onclick="event.stopPropagation();closePosition('${p.symbol}')"><i class="bi bi-x-circle"></i></button></td>
      </tr>${sigRow}`;
    }).join('');
  } catch(e) {
    document.getElementById('positions-body').innerHTML=`<tr><td colspan="9" class="text-danger py-3">${e.message}</td></tr>`;
  }
}

function toggleSignalRow(row) {
  const next = row.nextElementSibling;
  if(!next || !next.classList.contains('signal-detail-row')) return;
  const icon  = row.querySelector('.bi-chevron-down,.bi-chevron-up');
  const isHidden = next.style.display === 'none' || next.style.display === '';
  if(isHidden) {
    next.style.display = 'table-row';
    if(icon){icon.classList.remove('bi-chevron-down');icon.classList.add('bi-chevron-up');}
  } else {
    next.style.display = 'none';
    if(icon){icon.classList.remove('bi-chevron-up');icon.classList.add('bi-chevron-down');}
  }
}

async function closePosition(sym) {
  if(!confirm(`Close ${sym}?`)) return;
  const res=await POST(`/positions/${sym}/close`,{});
  alert(res.error||`${sym} closed`); loadPositions();
}

async function loadOrders() {
  try {
    const orders=await API('/alpaca/orders');
    const el=document.getElementById('orders-list');
    if(!orders.length){el.innerHTML='<span class="text-muted">No open orders</span>';return;}
    el.innerHTML=orders.map(o=>`<span class="badge bg-dark border border-secondary me-2">${o.symbol} ${o.side} x${o.qty} [${o.status}]
      <button class="btn btn-link btn-sm p-0 text-danger ms-1" onclick="cancelOrder('${o.id}')">✕</button></span>`).join('');
  } catch(e){document.getElementById('orders-list').innerHTML=`<span class="text-danger">${e.message}</span>`;}
}

async function cancelOrder(id) {
  await DEL(`/alpaca/orders/${id}`); loadOrders();
}

/* ── EQUITY CURVE ─────────────────────────────────────────────────────────── */
async function loadEquityCurve(hours=24) {
  try {
    const data=await API(`/portfolio/equity?hours=${hours}`);
    const canvas=document.getElementById('equity-chart');
    const noData=document.getElementById('equity-no-data');
    if(!data.length){canvas.style.display='none';noData.style.display='block';return;}
    canvas.style.display='block'; noData.style.display='none';
    const labels=data.map(d=>new Date(d.time).toLocaleTimeString('en',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
    const equities=data.map(d=>d.equity);
    const first=equities[0]||0; const last=equities[equities.length-1]||0;
    const color=last>=first?'rgba(25,135,84,1)':'rgba(220,53,69,1)';
    const fillColor=last>=first?'rgba(25,135,84,0.15)':'rgba(220,53,69,0.15)';
    if(equityChart){equityChart.destroy();}
    equityChart=new Chart(canvas,{type:'line',data:{labels,datasets:[{label:'Equity',data:equities,borderColor:color,backgroundColor:fillColor,borderWidth:2,pointRadius:1,fill:true,tension:0.3}]},options:{responsive:true,plugins:{legend:{display:false}},scales:{x:{display:false},y:{ticks:{callback:v=>`$${v.toLocaleString('en',{maximumFractionDigits:0})}`}}}}});
  } catch(e){}
}

/* ── MARKET TAB ───────────────────────────────────────────────────────────── */
async function loadMarket() {
  try {
    const [mkt, regime] = await Promise.all([API('/market/full'), API('/regime')]);
    // Regime card
    const rc=document.getElementById('regime-detail');
    if(rc&&regime){
      const risk=regime.risk||'medium';
      const em={low:'🟢',medium:'🟡','medium-high':'🟠',high:'🔴'}[risk]||'⚪';
      rc.innerHTML=`${em} <b>${regime.label||'Unknown'}</b> &nbsp;|&nbsp; SPY $${regime.spy_last||'?'} &nbsp; RSI:${regime.spy_rsi||'?'} &nbsp; ADX:${regime.spy_adx||'?'} &nbsp; Drawdown:${regime.spy_drawdown_pct||'?'}% &nbsp;|&nbsp; <span class="text-info">${regime.recommendation||''}</span>`;
    }
    const filter=document.getElementById('market-filter').value;
    // Equities
    let eq=mkt.equities||[];
    if(filter==='positive') eq=eq.filter(a=>(a.change_percent||0)>0);
    if(filter==='negative') eq=eq.filter(a=>(a.change_percent||0)<0);
    document.getElementById('equities-body').innerHTML=eq.map(a=>{
      const chg=a.change_percent||0; const cc=chg>0?'text-success':chg<0?'text-danger':'text-muted';
      return `<tr><td class="fw-bold">${a.symbol}</td><td class="text-muted small">${(a.name||'').slice(0,20)}</td><td>${fmtPrice(a.price)}</td><td class="${cc} fw-bold">${fmtPct(chg)}</td><td class="text-muted small">${a.volume?Number(a.volume).toLocaleString('en',{notation:'compact'}):'—'}</td></tr>`;
    }).join('');
    // Crypto
    document.getElementById('crypto-body').innerHTML=(mkt.crypto||[]).map(a=>{
      const chg=a.change_percent||0; const cc=chg>0?'text-success':chg<0?'text-danger':'text-muted';
      return `<tr><td class="fw-bold">${a.symbol}</td><td>${fmtPrice(a.price)}</td><td class="${cc} fw-bold">${fmtPct(chg)}</td></tr>`;
    }).join('');
  } catch(e){ console.error('Market load error',e); }
}

document.getElementById('market-filter').addEventListener('change',loadMarket);

/* ── THREATS ──────────────────────────────────────────────────────────────── */
async function loadThreats() {
  allThreats=await API('/threats?limit=80');
  renderThreats();
}
function renderThreats() {
  const sev=document.getElementById('threat-filter-sev').value;
  const reg=document.getElementById('threat-filter-region').value;
  const confirmation=document.getElementById('threat-filter-confirmation').value;
  let filtered=allThreats.filter(t=>(!sev||t.severity===sev)&&(!reg||t.region===reg)&&(!confirmation||t.confirmation_status===confirmation));
  document.getElementById('threat-count').textContent=`${filtered.length} threats`;
  const grid=document.getElementById('threats-grid');
  if(!filtered.length){grid.innerHTML='<div class="col-12 text-center text-muted py-5">No threats</div>';return;}
  grid.innerHTML=filtered.map(t=>`
    <div class="col-xl-3 col-lg-4 col-md-6">
      <div class="card h-100 border-${sevColor[t.severity]||'secondary'}">
        <div class="card-header py-2 d-flex justify-content-between">
          <span class="badge sev-${t.severity}">${t.severity}</span>
          <small class="text-muted">${t.country||''}</small>
        </div>
        <div class="card-body py-2">
          <p class="fw-bold mb-1 small">${t.source_url?`<a href="${escapeHtml(t.source_url)}" target="_blank" rel="noopener" class="text-info text-decoration-none">${escapeHtml(t.title)}</a>`:escapeHtml(t.title)}</p>
          <p class="text-muted small mb-2" style="font-size:.72rem">${escapeHtml((t.description||'').slice(0,180))}</p>
          <div class="d-flex gap-1 flex-wrap">
            <span class="badge bg-dark border border-secondary small">${escapeHtml(t.event_type||'')}</span>
            <span class="badge bg-dark border border-secondary small">${escapeHtml(t.region||'')}</span>
            <span class="badge ${t.confirmation_status==='corroborated'?'bg-success':'bg-secondary'} small">${escapeHtml((t.confirmation_status||'legacy').replaceAll('_',' '))}</span>
            ${t.claim_confidence!=null?`<span class="badge bg-dark border border-info text-info small">Evidence ${Math.round(t.claim_confidence)}%</span>`:''}
          </div>
        </div>
        <div class="card-footer py-1 small text-muted">${timeAgo(t.published_at || t.created_date)} · ${t.source||''}</div>
      </div>
    </div>`).join('');
}
document.getElementById('threat-filter-sev').addEventListener('change',renderThreats);
document.getElementById('threat-filter-region').addEventListener('change',renderThreats);
document.getElementById('threat-filter-confirmation').addEventListener('change',renderThreats);

/* ── NEWS ─────────────────────────────────────────────────────────────────── */
async function loadNews() {
  const list=document.getElementById('news-list');
  list.innerHTML='<div class="text-muted text-center py-4"><span class="spinner-border spinner-border-sm me-2"></span>Loading intelligence...</div>';
  try {
    allNews=await API('/news?limit=250');
    const sources=[...new Set(allNews.map(item=>item.source).filter(Boolean))].sort();
    const sourceFilter=document.getElementById('news-filter-source');
    const selected=sourceFilter.value;
    sourceFilter.innerHTML='<option value="">All Sources</option>'+sources.map(source=>`<option value="${escapeHtml(source)}">${escapeHtml(source)}</option>`).join('');
    sourceFilter.value=selected;
    await loadIntelligenceStatus();
    renderNews();
  } catch(e) {
    list.innerHTML='<div class="alert alert-danger">News could not be loaded. '+escapeHtml(e.message||'Server error')+'</div>';
  }
}

async function loadTradeMode() {
  try {
    const preference = await API('/preferences/trading');
    document.querySelectorAll('[data-trade-mode]').forEach(button => {
      const active = button.dataset.tradeMode === preference.trade_mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  } catch(e) {}
}

async function setTradeMode(mode) {
  try {
    const preference = await PUT('/preferences/trading', {trade_mode: mode});
    await loadTradeMode();
    showToast(`Signal horizon set to ${preference.trade_mode}`,'info');
  } catch(e) {
    showToast('Could not update signal horizon','danger');
  }
}

async function loadIntelligenceStatus() {
  const el=document.getElementById('news-health');
  try {
    const status=await API('/intelligence/status');
    const latest=status.latest_run||{};
    const cls=status.status==='healthy'?'text-success':status.status==='degraded'?'text-warning':'text-muted';
    el.innerHTML=`<i class="bi ${status.status==='healthy'?'bi-check-circle-fill':'bi-exclamation-triangle-fill'} ${cls}"></i>`+
      `<strong class="${cls}">${escapeHtml(status.status||'not run')}</strong>`+
      `<span>${status.healthy_sources||0}/${status.source_count||0} sources healthy</span>`+
      `<span>${status.recent_news||0} articles / 24h</span>`+
      `<span>${status.corroborated_recent||0} corroborated</span>`+
      `<span class="ms-auto">Last run ${escapeHtml(timeAgo(latest.finished_at))||'never'}</span>`;
  } catch(e) {
    el.innerHTML='<i class="bi bi-exclamation-circle text-warning"></i><span>Source health unavailable</span>';
  }
}

function renderNews() {
  const cat=document.getElementById('news-filter-cat').value;
  const sent=document.getElementById('news-filter-sent').value;
  const source=document.getElementById('news-filter-source').value;
  const confirmation=document.getElementById('news-filter-confirmation').value;
  const reliability=Number(document.getElementById('news-filter-reliability').value||0);
  const freshness=Number(document.getElementById('news-filter-freshness').value||0);
  const asset=document.getElementById('news-filter-asset').value.trim().toUpperCase();
  const cutoff=freshness?Date.now()-freshness*3600000:0;
  let filtered=allNews.filter(n=>(!cat||n.category===cat)&&(!sent||n.sentiment===sent)&&
    (!source||n.source===source)&&(!confirmation||n.confirmation_status===confirmation)&&
    (!reliability||Number(n.reliability_score||0)>=reliability)&&
    (!cutoff||new Date(n.published_at||n.created_date).getTime()>=cutoff)&&
    (!asset||(n.affected_assets||[]).some(value=>String(value).toUpperCase().includes(asset))));
  const sentIcon={positive:'bi-arrow-up-circle-fill text-success',negative:'bi-arrow-down-circle-fill text-danger',neutral:'bi-dash-circle text-secondary'};
  document.getElementById('news-count').textContent=`${filtered.length} articles`;
  if(!filtered.length){document.getElementById('news-list').innerHTML='<div class="text-center text-muted py-5">No articles match these filters</div>';return;}
  document.getElementById('news-list').innerHTML=filtered.map(n=>`
    <div class="d-flex align-items-start gap-2 py-2 border-bottom border-secondary">
      <i class="bi ${sentIcon[n.sentiment]||'bi-dash-circle text-muted'} mt-1 flex-shrink-0"></i>
      <div class="flex-grow-1">
        <div class="small fw-bold">${n.url?`<a href="${escapeHtml(n.url)}" target="_blank" rel="noopener" class="text-info text-decoration-none">${escapeHtml(n.title)}</a>`:escapeHtml(n.title)}</div>
        <div class="small text-muted mt-1">${escapeHtml((n.summary||'').slice(0,220))}</div>
        <div class="d-flex gap-2 mt-1 flex-wrap">
          <span class="badge bg-dark border border-secondary" style="font-size:.65rem">${escapeHtml(n.source||'')}</span>
          <span class="badge bg-dark border border-secondary" style="font-size:.65rem">${escapeHtml(n.category||'')}</span>
          <span class="badge ${n.confirmation_status==='corroborated'?'bg-success':n.confirmation_status==='unconfirmed_social'?'bg-warning text-dark':'bg-secondary'}" style="font-size:.65rem">${escapeHtml((n.confirmation_status||'legacy').replaceAll('_',' '))}</span>
          ${n.claim_confidence!=null?`<span class="badge bg-dark border border-info text-info" style="font-size:.65rem">Evidence ${Math.round(n.claim_confidence)}%</span>`:''}
          ${(n.affected_assets||[]).slice(0,4).map(a=>`<span class="badge bg-dark border border-warning text-warning" style="font-size:.65rem">${escapeHtml(a)}</span>`).join('')}
          <span class="text-muted" style="font-size:.65rem">${timeAgo(n.published_at || n.created_date)}</span>
        </div>
        ${n.corroborated_sources&&n.corroborated_sources.length?`<div class="text-muted mt-1" style="font-size:.65rem">Also reported by ${escapeHtml(n.corroborated_sources.slice(0,3).join(', '))}</div>`:''}
      </div>
    </div>`).join('');
}
document.getElementById('news-filter-cat').addEventListener('change',renderNews);
document.getElementById('news-filter-sent').addEventListener('change',renderNews);
document.getElementById('news-filter-source').addEventListener('change',renderNews);
document.getElementById('news-filter-confirmation').addEventListener('change',renderNews);
document.getElementById('news-filter-reliability').addEventListener('change',renderNews);
document.getElementById('news-filter-freshness').addEventListener('change',renderNews);
document.getElementById('news-filter-asset').addEventListener('input',renderNews);

/* ── SCANNER ──────────────────────────────────────────────────────────────── */
let _lastScanSignal = null;

async function runScan() {
  const sym = document.getElementById('scan-symbol').value.toUpperCase().trim();
  if(!sym){alert('Enter a symbol');return;}
  const tfs = [...document.querySelectorAll('[id^="tf-"]:checked')].map(function(e){return e.value;});
  const genSig = document.getElementById('gen-signal-check').checked;
  const el = document.getElementById('scan-result');
  _lastScanSignal = null;
  el.innerHTML = '<div class="text-warning py-3 text-center"><i class="bi bi-hourglass-split"></i> Fetching OHLCV + running TA engine... (10-30s)</div>';

  try {
    // POST() already prepends /api — use path without /api prefix
    const data = await POST('/analyze', {symbol:sym, timeframes:tfs, generate_signal:genSig});
    const ta = data.ta || {};
    let html = '<div class="d-flex align-items-center gap-2 mb-3">' +
      '<span class="badge bg-info text-dark fs-6">'+sym+'</span>' +
      '<span class="text-muted small">Analyzed: '+tfs.join(', ')+'</span>' +
    '</div>';

    // TA per timeframe
    const tfOrder = ['1H','4H','1D','1W'];
    const taSorted = tfOrder.filter(function(k){return ta[k];}).concat(Object.keys(ta).filter(function(k){return tfOrder.indexOf(k)===-1&&ta[k];}));
    taSorted.forEach(function(tf){
      const td = ta[tf];
      if(!td||td.error) return;
      const bias = td.bias||'neutral';
      const bc   = bias==='bullish'?'success':bias==='bearish'?'danger':'secondary';
      const p    = td.price||{};
      const emas = td.emas||{};
      const rsi  = td.rsi;
      const macd = td.macd||{};
      const bb   = td.bollinger_bands||{};
      const vol  = td.volume||{};
      const atr  = td.atr||{};
      const srUp = (td.support_resistance||{}).resistance;
      const srDn = (td.support_resistance||{}).support;
      html += '<div class="card mb-2">' +
        '<div class="card-header py-1 d-flex justify-content-between align-items-center">' +
          '<span class="fw-bold">'+tf+'</span>' +
          '<span class="badge bg-'+bc+'">'+bias.toUpperCase()+'</span>' +
        '</div>' +
        '<div class="card-body py-2" style="font-size:.8rem">' +
          '<div class="row g-1">' +
            '<div class="col-sm-6">' +
              '<table class="table table-dark table-sm mb-0" style="font-size:.78rem">' +
                '<tr><td class="text-muted">Price</td><td class="fw-bold text-info">'+fmtPrice(p.last)+'</td></tr>' +
                '<tr><td class="text-muted">EMA21</td><td>'+fmtPrice(emas.ema21)+'</td></tr>' +
                '<tr><td class="text-muted">EMA50</td><td>'+fmtPrice(emas.ema50)+'</td></tr>' +
                '<tr><td class="text-muted">RSI</td><td class="'+(rsi>70?'text-danger':rsi<30?'text-success':'')+'fw-bold">'+(rsi!=null?rsi.toFixed(1):'N/A')+'</td></tr>' +
                '<tr><td class="text-muted">ATR %</td><td>'+(atr.pct!=null?atr.pct.toFixed(2)+'%':'N/A')+'</td></tr>' +
              '</table>' +
            '</div>' +
            '<div class="col-sm-6">' +
              '<table class="table table-dark table-sm mb-0" style="font-size:.78rem">' +
                '<tr><td class="text-muted">MACD</td><td>'+(macd.macd!=null?macd.macd.toFixed(4):'N/A')+'</td></tr>' +
                '<tr><td class="text-muted">Signal</td><td>'+(macd.signal!=null?macd.signal.toFixed(4):'N/A')+'</td></tr>' +
                '<tr><td class="text-muted">BB Upper</td><td>'+(bb.upper!=null?fmtPrice(bb.upper):'N/A')+'</td></tr>' +
                '<tr><td class="text-muted">BB Lower</td><td>'+(bb.lower!=null?fmtPrice(bb.lower):'N/A')+'</td></tr>' +
                '<tr><td class="text-muted">Volume</td><td>'+(vol.surge?'<span class="text-success fw-bold">SURGE</span>':vol.dry?'<span class="text-warning">DRY</span>':'Normal')+'</td></tr>' +
              '</table>' +
            '</div>' +
            (srDn||srUp?'<div class="col-12 mt-1 small">' +
              (srDn?'<span class="text-success me-3">⬇ Support: '+fmtPrice(srDn)+'</span>':'') +
              (srUp?'<span class="text-danger">⬆ Resistance: '+fmtPrice(srUp)+'</span>':'') +
            '</div>':'') +
          '</div>' +
        '</div>' +
      '</div>';
    });

    // Generated signal
    if(data.signal){
      const sig = data.signal;
      if(sig.error){
        html += '<div class="alert alert-warning mt-2"><i class="bi bi-exclamation-triangle"></i> LLM error: '+sig.error+'</div>';
      } else {
        _lastScanSignal = sig;
        const rr = sig.entry_price&&sig.target_price&&sig.stop_loss&&sig.entry_price>sig.stop_loss
          ? ((sig.target_price-sig.entry_price)/(sig.entry_price-sig.stop_loss)).toFixed(1)+':1' : 'N/A';
        const dirCls = sig.direction==='Long'?'text-success':'text-primary';
        html += '<div class="card mt-3 border-success">' +
          '<div class="card-header py-2 d-flex justify-content-between align-items-center" style="background:rgba(25,135,84,.12)">' +
            '<span class="fw-bold"><i class="bi bi-lightning-fill text-success"></i> Generated Signal</span>' +
            '<button class="btn btn-success btn-sm" onclick="saveScannedSignal()"><i class="bi bi-bookmark-plus"></i> Save to Signals</button>' +
          '</div>' +
          '<div class="card-body">' +
            '<div class="row g-2 mb-3">' +
              '<div class="col-4 text-center p-2 rounded" style="background:rgba(13,202,240,.08)">' +
                '<div class="text-muted small">ENTRY</div><div class="fw-bold text-info">'+fmtPrice(sig.entry_price)+'</div>' +
              '</div>' +
              '<div class="col-4 text-center p-2 rounded" style="background:rgba(25,135,84,.08)">' +
                '<div class="text-muted small">TARGET</div><div class="fw-bold text-success">'+fmtPrice(sig.target_price)+'</div>' +
              '</div>' +
              '<div class="col-4 text-center p-2 rounded" style="background:rgba(220,53,69,.08)">' +
                '<div class="text-muted small">STOP</div><div class="fw-bold text-danger">'+fmtPrice(sig.stop_loss)+'</div>' +
              '</div>' +
            '</div>' +
            '<div class="row g-2 small">' +
              '<div class="col-6"><b>Direction:</b> <span class="'+dirCls+'">'+sig.direction+'</span></div>' +
              '<div class="col-6"><b>Confidence:</b> '+sig.confidence+'%</div>' +
              '<div class="col-6"><b>Timeframe:</b> '+(sig.timeframe||'N/A')+'</div>' +
              '<div class="col-6"><b>R:R:</b> <span class="fw-bold '+(rr!=='N/A'&&parseFloat(rr)>=2?'text-success':'text-warning')+'">'+rr+'</span></div>' +
              '<div class="col-12 text-muted mt-1">'+(sig.reasoning||'')+'</div>' +
              (sig.key_risks?'<div class="col-12 text-warning"><i class="bi bi-exclamation-triangle-fill"></i> '+sig.key_risks+'</div>':'') +
            '</div>' +
          '</div>' +
        '</div>';
      }
    }
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div class="alert alert-danger"><i class="bi bi-x-circle"></i> Error: '+e.message+'<br><small class="text-muted">Check that the local server is running and the symbol is valid.</small></div>';
  }
}

async function saveScannedSignal() {
  if(!_lastScanSignal){alert('No signal to save');return;}
  const res = await POST('/signals/save', _lastScanSignal);
  if(res.error) alert('Error: '+res.error);
  else { alert('Signal saved! Check the Signals tab.'); loadSignals(); }
}

/* ── SCANNER LANES ────────────────────────────────────────────────────────── */
function scannerLane(s) {
  const assetClass = (s.asset_class || '').toLowerCase();
  const symbol = (s.asset_symbol || '').toUpperCase();
  const trigger = (s.trigger_event || '').toUpperCase();
  if (assetClass === 'crypto' || trigger.includes('SCANNER:CRYPTO') || (symbol && !symbol.endsWith('=F') && !symbol.endsWith('=X') && (symbol.includes('/USD') || symbol.endsWith('-USD')))) {
    return 'crypto';
  }
  if (assetClass === 'futures' || assetClass === 'forex' || trigger.includes('SCANNER:FUTURES') || symbol.endsWith('=F') || symbol.endsWith('=X') || symbol.startsWith('^')) {
    return 'futures';
  }
  return 'equity';
}

/* Full signal analysis */
async function openSignalAnalysis(signalId) {
  const modalEl = document.getElementById('signalAnalysisModal');
  const loading = document.getElementById('sa-loading');
  const content = document.getElementById('sa-content');
  loading.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Loading current market data and technical analysis...';
  loading.style.display = '';
  content.style.display = 'none';
  document.getElementById('sa-title').textContent = 'Signal analysis';
  new bootstrap.Modal(modalEl).show();
  try {
    const data = await API('/signals/'+encodeURIComponent(signalId)+'/analysis');
    if(data.detail || data.error) throw new Error(data.detail || data.error);
    signalAnalysisData = data;
    renderSignalAnalysis(data);
    loading.style.display = 'none';
    content.style.display = '';
  } catch(e) {
    loading.innerHTML = '<div class="text-danger"><i class="bi bi-exclamation-triangle me-2"></i>'+escapeHtml(e.message || 'Analysis failed')+'</div>';
  }
}

function taMetric(value, suffix='') {
  return value == null || Number.isNaN(Number(value)) ? 'N/A' : Number(value).toFixed(2)+suffix;
}

function renderSignalAnalysis(data) {
  const s = data.signal || {};
  const c = data.confluence || {};
  document.getElementById('sa-title').textContent = (s.asset_symbol || 'Signal')+' analysis';
  document.getElementById('sa-meta').innerHTML =
    '<span class="badge '+(String(s.direction).toLowerCase().includes('short')?'bg-danger':'bg-success')+'">'+escapeHtml(s.direction || 'Long')+'</span>'+
    '<span class="badge bg-secondary">'+escapeHtml(s.asset_class || '')+'</span>'+
    '<span class="badge bg-dark border border-secondary">'+escapeHtml(s.timeframe || '')+'</span>'+
    '<span class="badge bg-info text-dark">'+escapeHtml(s.status || '')+'</span>'+
    (s.setup_type?'<span class="badge bg-dark border border-secondary">'+escapeHtml(s.setup_type)+'</span>':'')+
    (s.signal_version?'<span class="badge bg-dark border border-secondary">'+escapeHtml(s.signal_version)+'</span>':'')+
    '<span class="text-muted small">Generated '+escapeHtml(timeAgo(s.generated_at))+'</span>';

  const riskFlags = c.risk_flags || [];
  document.getElementById('sa-overview').innerHTML =
    '<div class="sa-stat"><span>Evidence score</span><strong class="'+((s.composite_score||0)>=70?'text-success':(s.composite_score||0)>=50?'text-warning':'text-danger')+'">'+Math.round(s.composite_score||s.confidence||0)+'%</strong><small>versioned composite</small></div>'+
    '<div class="sa-stat"><span>Calibrated confidence</span><strong>'+Math.round(s.calibrated_confidence||s.confidence||0)+'%</strong><small>model '+Math.round(s.confidence||0)+'%</small></div>'+
    '<div class="sa-stat"><span>Confluence</span><strong class="'+(c.score>=70?'text-success':c.score>=40?'text-warning':'text-danger')+'">'+(c.score||0)+'%</strong><small>'+escapeHtml(c.label || '')+'</small></div>'+
    '<div class="sa-stat"><span>Data / freshness</span><strong>'+Math.round(s.data_quality_score||0)+' / '+Math.round(s.freshness_score||0)+'</strong><small>quality scores</small></div>'+
    '<div class="sa-stat"><span>Entry</span><strong class="text-info">'+fmtPrice(s.entry_price)+'</strong><small>'+escapeHtml(s.timeframe || '')+' setup</small></div>'+
    '<div class="sa-stat"><span>Target</span><strong class="text-success">'+fmtPrice(s.target_price)+'</strong><small>planned exit</small></div>'+
    '<div class="sa-stat"><span>Stop</span><strong class="text-danger">'+fmtPrice(s.stop_loss)+'</strong><small>risk control</small></div>';

  const breakdown=s.score_breakdown||{};
  const evidenceKeys=[['ta_confluence','TA'],['rr','Risk/reward'],['volume','Volume'],['regime','Regime'],['data_quality','Data'],['freshness','Freshness'],['liquidity','Liquidity'],['volatility','Volatility'],['news','News']];
  document.getElementById('sa-score-breakdown').innerHTML=evidenceKeys.filter(([key])=>breakdown[key]!=null).map(([key,label])=>
    '<div><span>'+label+'</span><strong>'+Math.round(breakdown[key])+'</strong><div class="evidence-meter"><i style="width:'+Math.max(0,Math.min(100,breakdown[key]))+'%"></i></div></div>'
  ).join('')||'<span class="text-muted small">This legacy signal has no component breakdown.</span>';

  document.getElementById('sa-thesis').textContent = s.reasoning || 'No original thesis was saved.';
  document.getElementById('sa-risks').innerHTML = [s.invalidation,s.key_risks].concat(riskFlags).filter(Boolean).map(r =>
    '<div class="small text-warning mb-1"><i class="bi bi-exclamation-triangle me-1"></i>'+escapeHtml(r)+'</div>'
  ).join('') || '<div class="small text-muted">No additional risk flags.</div>';

  const timeframes = data.timeframes || [];
  document.getElementById('sa-timeframes').innerHTML = timeframes.map(tf =>
    '<button class="btn btn-outline-secondary btn-sm sa-tf-btn" data-tf="'+escapeHtml(tf)+'" onclick="selectSignalAnalysisTimeframe(\''+tf+'\')">'+escapeHtml(tf)+'</button>'
  ).join('');

  document.getElementById('sa-ta-grid').innerHTML = timeframes.map(tf => {
    const d = (data.ta || {})[tf] || {};
    if(d.error) return '<div class="sa-ta-panel unavailable"><div class="fw-bold">'+tf+'</div><div class="small text-muted">'+escapeHtml(d.error)+'</div></div>';
    const p=d.price||{}, macd=d.macd||{}, ema=d.emas||{}, bb=d.bollinger_bands||{}, adx=d.adx||{}, atr=d.atr||{}, vol=d.volume||{}, sr=d.support_resistance||{}, stoch=d.stochastic||{}, vwap=d.vwap||{};
    const biasCls=d.bias==='bullish'?'text-success':d.bias==='bearish'?'text-danger':'text-warning';
    return '<div class="sa-ta-panel">'+
      '<div class="d-flex justify-content-between align-items-center mb-2"><strong>'+tf+'</strong><span class="badge bg-dark border border-secondary '+biasCls+'">'+escapeHtml(d.bias||'unknown')+'</span></div>'+
      '<div class="sa-metric"><span>Price / change</span><b>'+fmtPrice(p.last)+' / '+fmtPct(p.pct_change)+'</b></div>'+
      '<div class="sa-metric"><span>RSI (14)</span><b>'+taMetric(d.rsi)+' <small>'+escapeHtml(d.rsi_signal||'')+'</small></b></div>'+
      '<div class="sa-metric"><span>MACD</span><b class="'+(macd.trend==='bullish'?'text-success':'text-danger')+'">'+escapeHtml(macd.trend||'N/A')+' / '+escapeHtml(macd.crossover||'none')+'</b></div>'+
      '<div class="sa-metric"><span>EMA 9 / 21 / 50 / 200</span><b>'+[ema.ema9,ema.ema21,ema.ema50,ema.ema200].map(fmtPrice).join(' / ')+'</b></div>'+
      '<div class="sa-metric"><span>VWAP</span><b>'+fmtPrice(vwap.value)+' / '+escapeHtml(vwap.position||'N/A')+'</b></div>'+
      '<div class="sa-metric"><span>ADX / ATR</span><b>'+taMetric(adx.value)+' / '+taMetric(atr.pct,'%')+'</b></div>'+
      '<div class="sa-metric"><span>Bollinger %B</span><b>'+taMetric(bb.pct_b)+' / '+escapeHtml(bb.position||'N/A')+'</b></div>'+
      '<div class="sa-metric"><span>Stochastic K / D</span><b>'+taMetric(stoch.k)+' / '+taMetric(stoch.d)+'</b></div>'+
      '<div class="sa-metric"><span>Volume</span><b>'+taMetric(vol.surge_ratio,'x')+(vol.surge?' surge':vol.dry?' dry':'')+'</b></div>'+
      '<div class="sa-metric"><span>OBV</span><b>'+escapeHtml(d.obv_trend||'N/A')+'</b></div>'+
      '<div class="sa-metric"><span>Support / resistance</span><b>'+fmtPrice(sr.support)+' / '+fmtPrice(sr.resistance)+'</b></div>'+
    '</div>';
  }).join('');

  renderSignalContext('sa-news', data.news || [], false);
  renderSignalContext('sa-threats', data.threats || [], true);
  const initial = timeframes.includes(s.timeframe) ? s.timeframe : timeframes.find(tf => (data.candles[tf]||[]).length) || '5m';
  selectSignalAnalysisTimeframe(initial);
}

function renderSignalContext(elementId, items, threats) {
  const el=document.getElementById(elementId);
  if(!items.length){el.innerHTML='<div class="text-muted small py-3">No related '+(threats?'threats':'news')+' found.</div>';return;}
  el.innerHTML=items.map(item => {
    const url=threats?item.source_url:item.url;
    const badge=threats?(item.severity||'Active'):(item.sentiment||item.category||'News');
    const color=threats?(sevColor[item.severity]||'secondary'):(String(item.sentiment).toLowerCase()==='negative'?'danger':String(item.sentiment).toLowerCase()==='positive'?'success':'secondary');
    const title=url?'<a class="text-light text-decoration-none" target="_blank" rel="noopener" href="'+escapeHtml(url)+'">'+escapeHtml(item.title)+'</a>':escapeHtml(item.title);
    const confirmation=item.confirmation_status?'<span class="badge '+(item.confirmation_status==='corroborated'?'bg-success':'bg-secondary')+'">'+escapeHtml(item.confirmation_status.replaceAll('_',' '))+'</span>':'';
    const evidence=item.claim_confidence!=null?'<span class="badge bg-dark border border-info text-info">'+Math.round(item.claim_confidence)+'% evidence</span>':'';
    return '<div class="sa-context-item"><div class="d-flex justify-content-between gap-2"><strong class="small">'+title+'</strong><span class="d-flex gap-1"><span class="badge bg-'+color+'">'+escapeHtml(badge)+'</span>'+confirmation+evidence+'</span></div>'+
      '<div class="small text-muted mt-1">'+escapeHtml(item.summary||item.description||'').slice(0,260)+'</div>'+
      '<div class="small text-muted mt-1">'+escapeHtml(item.source||'')+' · '+escapeHtml(item.relevance||'')+' · '+escapeHtml(timeAgo(item.published_at||item.created_date))+'</div></div>';
  }).join('');
}

function selectSignalAnalysisTimeframe(tf) {
  if(!signalAnalysisData) return;
  document.querySelectorAll('.sa-tf-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.tf===tf));
  document.getElementById('sa-chart-label').textContent=tf+' price and volume · '+(signalAnalysisData.sources[tf]||'market data');
  const candles=(signalAnalysisData.candles||{})[tf]||[];
  const s=signalAnalysisData.signal||{};
  if(signalAnalysisChart){signalAnalysisChart.destroy();signalAnalysisChart=null;}
  if(!candles.length){document.getElementById('sa-chart-empty').style.display='';return;}
  document.getElementById('sa-chart-empty').style.display='none';
  const labels=candles.map(c=>new Date(c.time).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
  const levels=(value)=>candles.map(()=>value==null?null:Number(value));
  signalAnalysisChart=new Chart(document.getElementById('sa-chart'),{
    type:'line',
    data:{labels:labels,datasets:[
      {label:'Close',data:candles.map(c=>c.close),borderColor:'#58a6ff',backgroundColor:'rgba(88,166,255,.08)',borderWidth:2,pointRadius:0,tension:.12,fill:true,yAxisID:'y'},
      {type:'bar',label:'Volume',data:candles.map(c=>c.volume),backgroundColor:'rgba(139,148,158,.2)',borderWidth:0,yAxisID:'volume'},
      {label:'Entry',data:levels(s.entry_price),borderColor:'#0dcaf0',borderDash:[5,4],borderWidth:1,pointRadius:0,yAxisID:'y'},
      {label:'Target',data:levels(s.target_price),borderColor:'#3fb950',borderDash:[5,4],borderWidth:1,pointRadius:0,yAxisID:'y'},
      {label:'Stop',data:levels(s.stop_loss),borderColor:'#f85149',borderDash:[5,4],borderWidth:1,pointRadius:0,yAxisID:'y'}
    ]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{color:'#c9d1d9',boxWidth:12}}},scales:{
      x:{ticks:{color:'#8b949e',maxTicksLimit:8},grid:{color:'rgba(255,255,255,.04)'}},
      y:{position:'right',ticks:{color:'#8b949e'},grid:{color:'rgba(255,255,255,.06)'}},
      volume:{display:false,position:'left',beginAtZero:true,max:Math.max(...candles.map(c=>c.volume||0))*4}
    }}
  });
}

function scannerSignalCard(s) {
  const dir = s.paper_direction || s.direction || 'Long';
  const dirLower = String(dir).toLowerCase();
  const score = Number(s.composite_score || s.confidence || 0);
  const conf = Number(s.confidence || 0);
  const statusBadge = {Active:'bg-success',Executed:'bg-primary',Expired:'bg-secondary',Rejected:'bg-danger',Closed:'bg-dark border border-secondary',PendingApproval:'bg-warning text-dark'}[s.status] || 'bg-secondary';
  const mode = (s.trigger_event || '').replace(/^SCANNER:/i, '').replace(/_/g, ' ') || (s.asset_class || 'scanner');
  let rr = 'N/A';
  if (s.entry_price && s.target_price && s.stop_loss) {
    const entry = Number(s.entry_price), target = Number(s.target_price), stop = Number(s.stop_loss);
    const risk = dirLower.includes('short') ? (stop - entry) : (entry - stop);
    const reward = dirLower.includes('short') ? (entry - target) : (target - entry);
    if (risk > 0 && reward > 0) rr = (reward / risk).toFixed(1) + ':1';
  }
  return '<div class="border border-secondary rounded p-2 bg-dark bg-opacity-50 scanner-signal-clickable" role="button" tabindex="0" '+
    'onclick="openSignalAnalysis(\''+s.id+'\')" onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();openSignalAnalysis(\''+s.id+'\')}">' +
    '<div class="d-flex justify-content-between align-items-start gap-2">' +
      '<div>' +
        '<span class="fw-bold text-white">'+(s.asset_symbol || 'N/A')+'</span>' +
        '<span class="badge '+(dirLower.includes('short') ? 'bg-danger' : 'bg-success')+' ms-1">'+dir+'</span>' +
        (s.paper_mode ? '<span class="badge bg-info text-dark ms-1">Paper</span>' : '') +
      '</div>' +
      '<small class="text-muted text-nowrap">'+timeAgo(s.generated_at)+'</small>' +
    '</div>' +
    '<div class="d-flex justify-content-between small mt-2">' +
      '<span class="text-muted">Score</span><span class="'+(score >= 70 ? 'text-success' : score >= 50 ? 'text-warning' : 'text-muted')+' fw-bold">'+score.toFixed(0)+'%</span>' +
    '</div>' +
    '<div class="d-flex justify-content-between small">' +
      '<span class="text-muted">Entry / Target / Stop</span><span>'+fmtPrice(s.entry_price)+' / '+fmtPrice(s.target_price)+' / '+fmtPrice(s.stop_loss)+'</span>' +
    '</div>' +
    '<div class="d-flex justify-content-between small">' +
      '<span class="text-muted">R:R / LLM</span><span>'+rr+' / '+conf.toFixed(0)+'%</span>' +
    '</div>' +
    '<div class="d-flex gap-1 flex-wrap mt-2">' +
      '<span class="badge '+statusBadge+'">'+(s.status || 'Unknown')+'</span>' +
      '<span class="badge bg-dark border border-secondary text-capitalize">'+mode.toLowerCase()+'</span>' +
      '<span class="badge bg-dark border border-secondary">'+(s.timeframe || 'N/A')+'</span>' +
    '</div>' +
    (s.reasoning ? '<div class="small text-muted mt-2" style="line-height:1.35;max-height:38px;overflow:hidden">'+String(s.reasoning).slice(0,150)+(String(s.reasoning).length > 150 ? '...' : '')+'</div>' : '') +
  '</div>';
}

async function loadScannerSignals() {
  const lists = {
    crypto: document.getElementById('scanner-crypto-list'),
    equity: document.getElementById('scanner-equity-list'),
    futures: document.getElementById('scanner-futures-list')
  };
  Object.values(lists).forEach(function(el){ if(el) el.innerHTML = '<div class="text-muted small">Loading scanner signals...</div>'; });
  try {
    const data = await API('/signals?limit=250');
    const scannerSignals = (data || [])
      .filter(function(s){
        const trigger = String(s.trigger_event || '').toUpperCase();
        return trigger.startsWith('SCANNER:') || String(s.signal_source || '').toLowerCase().includes('scanner');
      })
      .sort(function(a,b){ return new Date(b.generated_at) - new Date(a.generated_at); });
    const groups = {crypto: [], equity: [], futures: []};
    scannerSignals.forEach(function(s){ groups[scannerLane(s)].push(s); });
    Object.entries(groups).forEach(function(entry){
      const lane = entry[0], items = entry[1], el = lists[lane];
      if(!el) return;
      el.innerHTML = items.length
        ? items.slice(0,8).map(scannerSignalCard).join('')
        : '<div class="text-muted small">No scanner signals in this lane yet.</div>';
    });
  } catch(e) {
    Object.values(lists).forEach(function(el){ if(el) el.innerHTML = '<div class="text-danger small">Scanner load failed: '+e.message+'</div>'; });
  }
}

async function runScannerMode(mode) {
  const status = document.getElementById('scanner-run-status');
  if(status) status.innerHTML = '<span class="text-warning"><i class="bi bi-hourglass-split"></i> Running '+mode.replace(/_/g,' ')+' scanner...</span>';
  try {
    const res = await POST('/scanner/run', {mode: mode});
    if(res.error) throw new Error(res.error);
    const message = res.message || res.detail || 'Scanner started';
    if(status) status.innerHTML = '<span class="text-success">'+message+'</span>';
    setTimeout(loadScannerSignals, 3500);
  } catch(e) {
    if(status) status.innerHTML = '<span class="text-danger">Scanner failed: '+e.message+'</span>';
  }
}

/* ── JOBS TAB ─────────────────────────────────────────────────────────────── */
async function loadJobs() {
  const grid=document.getElementById('jobs-grid');
  grid.innerHTML=`<div class="col-12 text-center text-muted py-5">
    <div class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></div>
    Loading scheduler, cache, and model status...
  </div>`;
  try {
    const [jobs, cache, llm] = await Promise.all([
      API('/jobs/status'),
      API('/cache/stats').catch(()=>({})),
      API('/llm/health').catch(()=>({}))
    ]);
    const jobNames={market:'Market Data',threats:'Threat News',signals:'Signal Gen',execute:'Execute',positions:'Positions',telegram:'Telegram'};
    const schedules={market:'every 15m',threats:'every 15m +7m',signals:'every 30m',execute:'every 30m +3m',positions:'every 5m',telegram:'every 1m'};
    grid.innerHTML=Object.entries(jobs).map(([name,info])=>{
      const sc={ok:'success',running:'warning',error:'danger',idle:'secondary'}[info.status]||'secondary';
      const icon={ok:'bi-check-circle-fill',running:'bi-hourglass-split',error:'bi-x-circle-fill',idle:'bi-pause-circle'}[info.status]||'bi-pause-circle';
      return `<div class="col-lg-4 col-md-6">
        <div class="card h-100 border-${sc}">
          <div class="card-body py-3">
            <div class="d-flex justify-content-between align-items-center mb-2">
              <span class="fw-bold"><i class="bi ${icon} text-${sc} me-2"></i>${jobNames[name]||name}</span>
              <span class="badge bg-${sc}">${info.status}</span>
            </div>
            <div class="small text-muted mb-1">Schedule: ${schedules[name]||'—'}</div>
            <div class="small text-muted mb-2">Last run: ${info.last?timeAgo(info.last):'Never'}</div>
            ${info.error?`<div class="alert alert-danger py-1 small mb-2">${info.error}</div>`:''}
            <div class="d-flex gap-2">
              <button class="btn btn-outline-primary btn-sm flex-grow-1" onclick="triggerJob('${name}')"><i class="bi bi-play-fill"></i> Run Now</button>
              ${info.status==='running'?`<button class="btn btn-outline-warning btn-sm" title="Force this job's status back to idle if it's stuck (does not stop an actual hung thread)" onclick="resetJob('${name}')"><i class="bi bi-arrow-counterclockwise"></i> Reset</button>`:''}
            </div>
          </div>
        </div>
      </div>`;
    }).join('');
    // Append cache + LLM status cards
    const llmOk=llm.ok!==false;
    const cacheSymbols = cache.symbols_cached || cache.symbols || 0;
    const cacheBars    = cache.total_bars || 0;
    const cacheSize    = cache.db_size_mb != null ? cache.db_size_mb.toFixed(1) + ' MB' : '';
    const byTf         = cache.by_timeframe || {};
    // Per-TF rows with freshness
    const tfRows = Object.entries(byTf).map(([tf,v]) => {
      const latestBar = v.latest_bar_ts ? new Date(v.latest_bar_ts).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
      const lastUpd   = v.last_updated  ? timeAgo(v.last_updated) : '—';
      const stale = v.last_updated && (Date.now() - new Date(v.last_updated)) > 30*60*1000; // >30min
      return `<div class="d-flex justify-content-between small text-muted">
        <span>${tf}: ${(v.bars||0).toLocaleString()} bars</span>
        <span class="${stale?'text-warning':'text-success'}" title="Last updated: ${lastUpd}">⏱ ${latestBar}</span>
      </div>`;
    }).join('');
    // Overall freshness badge
    const lastUpd = cache.last_updated;
    const minsAgo = lastUpd ? Math.round((Date.now()-new Date(lastUpd))/60000) : null;
    const freshBadge = minsAgo === null ? '' :
      minsAgo < 20 ? `<span class="badge bg-success ms-1">Fresh (${minsAgo}m ago)</span>` :
      minsAgo < 60 ? `<span class="badge bg-warning text-dark ms-1">⚠ ${minsAgo}m ago</span>` :
                     `<span class="badge bg-danger ms-1">⛔ Stale ${minsAgo}m ago</span>`;
    const cacheRows = cacheSymbols
      ? `<div class="d-flex justify-content-between align-items-center mb-1">
           <span class="small text-muted">${cacheSymbols} symbols · ${cacheBars.toLocaleString()} bars${cacheSize?' · '+cacheSize:''}</span>
           ${freshBadge}
         </div>${tfRows}`
      : '<div class="small text-muted text-warning">No cache data yet — market job will populate on next run</div>';
    grid.innerHTML+=`
      <div class="col-lg-4 col-md-6">
        <div class="card h-100 border-${llmOk?'success':'danger'}">
          <div class="card-body py-3">
            <div class="d-flex justify-content-between mb-2">
              <span class="fw-bold"><i class="bi bi-robot me-2 text-${llmOk?'success':'danger'}"></i>LM Studio</span>
              <span class="badge bg-${llmOk?'success':'danger'}">${llmOk?'Online':'Offline'}</span>
            </div>
            <div class="small text-muted mb-1">${llm.model||llm.url||'No config'}</div>
            <div class="small text-muted">${llm.error||''}</div>
          </div>
        </div>
      </div>
      <div class="col-lg-4 col-md-6">
        <div class="card h-100 border-info">
          <div class="card-body py-3">
            <div class="d-flex justify-content-between mb-2">
              <span class="fw-bold"><i class="bi bi-database me-2 text-info"></i>OHLCV Cache</span>
              <button class="btn btn-outline-info btn-sm py-0" onclick="triggerBackfill()">Backfill</button>
            </div>
            ${cacheRows}
            <div class="small text-muted mt-1">
              yfinance fallback: <span class="text-success fw-bold">active</span>
              ${cache.db_path ? `<span class="text-muted ms-2" style="font-size:.7rem">${cache.db_path.split(/[\\/]/).pop()}</span>` : ''}
            </div>
          </div>
        </div>
      </div>`;
  } catch(e){
    grid.innerHTML=`<div class="col-12"><div class="alert alert-danger mb-0">
      Jarvis is not reachable. Run <code>.\\start.ps1</code>, then refresh this page.
    </div></div>`;
  }
}

async function triggerJob(name) {
  try {
    const res=await POST(`/jobs/${name}/trigger`,{});
    if(res.ok) { setTimeout(loadJobs,1500); }
    else { showToast(res.detail || `Could not start '${name}' — it may already be running`, 'warning'); }
  } catch(e) { showToast(`Could not start '${name}': ${e}`, 'danger'); }
}

async function resetJob(name) {
  if(!confirm(`Reset '${name}' status to idle? This only clears the tracking flag — it doesn't stop a thread that's actually still running, and if that old run eventually finishes it will overwrite this again.`)) return;
  try {
    const res=await POST(`/jobs/${name}/reset`,{});
    if(res.ok) { showToast(`'${name}' reset to idle`, 'success'); loadJobs(); }
    else { showToast(res.detail || `Could not reset '${name}'`, 'warning'); }
  } catch(e) { showToast(`Could not reset '${name}': ${e}`, 'danger'); }
}

async function triggerBackfill() {
  const res=await POST('/cache/backfill',{});
  alert(res.message||'Backfill started');
}

/* ── SETTINGS ─────────────────────────────────────────────────────────────── */
const PLATFORM_DEFS = {
  alpaca_paper:  {label:'Alpaca Paper',  fields:{api_key:'API Key (PKTEST...)',api_secret:'Secret Key',api_url:'https://paper-api.alpaca.markets',extra_field_1:'paper',extra_field_2:'Chat/Notes'},desc:'Alpaca paper trading — free tier'},
  alpaca_live:   {label:'Alpaca Live',   fields:{api_key:'API Key (PK...)',api_secret:'Secret Key',api_url:'https://api.alpaca.markets',extra_field_1:'live',extra_field_2:'Notes'},desc:'Alpaca live trading — real money'},
  lmstudio:      {label:'LM Studio',     fields:{api_url:'http://localhost:1234/v1',extra_field_1:'Model name (e.g. mistral-7b)',extra_field_2:'Notes'},desc:'Local LM Studio — OpenAI-compatible endpoint'},
  openai:        {label:'OpenAI',        fields:{api_key:'API Key (sk-...)',api_url:'https://api.openai.com/v1',extra_field_1:'Model (gpt-4o / gpt-4-turbo)',extra_field_2:'Notes'},desc:'OpenAI cloud LLM'},
  anthropic:     {label:'Anthropic',     fields:{api_key:'API Key (sk-ant-...)',api_url:'https://api.anthropic.com',extra_field_1:'Model (claude-3-5-sonnet-20241022)',extra_field_2:'Notes'},desc:'Anthropic Claude'},
  groq:          {label:'Groq',          fields:{api_key:'API Key',api_url:'https://api.groq.com/openai/v1',extra_field_1:'Model (llama-3.1-70b-versatile)',extra_field_2:'Notes'},desc:'Groq — ultra-fast inference'},
  deepseek:      {label:'DeepSeek',      fields:{api_key:'API Key',api_url:'https://api.deepseek.com/v1',extra_field_1:'Model (deepseek-reasoner / deepseek-chat)',extra_field_2:'Notes'},desc:'DeepSeek — strong reasoning model'},
  ollama:        {label:'Ollama',        fields:{api_url:'http://localhost:11434/v1',extra_field_1:'Model (llama3.2 / mistral / phi4)',extra_field_2:'Notes'},desc:'Ollama — local model runner'},
  telegram:      {label:'Telegram',      fields:{api_key:'Bot Token (from @BotFather)',extra_field_1:'Default Chat ID',extra_field_2:'Notes'},desc:'Telegram bot for alerts & commands'},
  coinbase:      {label:'Coinbase Adv.', fields:{api_key:'API Key',api_secret:'API Secret',api_url:'https://api.coinbase.com',extra_field_1:'passphrase (if CDP)',extra_field_2:'Notes'},desc:'Coinbase Advanced Trade'},
  kraken:        {label:'Kraken',        fields:{api_key:'API Key',api_secret:'Private Key',api_url:'https://api.kraken.com',extra_field_1:'Notes'},desc:'Kraken exchange'},
  binance:       {label:'Binance',       fields:{api_key:'API Key',api_secret:'Secret Key',api_url:'https://api.binance.com',extra_field_1:'testnet? (yes/no)',extra_field_2:'Notes'},desc:'Binance spot/futures'},
  interactive:   {label:'IBKR',          fields:{api_url:'http://localhost:5000/v1/api',extra_field_1:'Account ID',extra_field_2:'Notes'},desc:'Interactive Brokers — Client Portal API'},
  tradier:       {label:'Tradier',       fields:{api_key:'Access Token',api_url:'https://api.tradier.com/v1',extra_field_1:'Account ID',extra_field_2:'sandbox (yes/no)'},desc:'Tradier broker — equities + options'},
  tradovate:     {label:'Tradovate',     fields:{api_key:'Username',api_secret:'Password',api_url:'https://live.tradovateapi.com/v1',extra_field_1:'CID',extra_field_2:'Notes'},desc:'Tradovate — futures'},
};

function updatePlatformFields() {
  const sel=document.getElementById('cfg-platform').value;
  const def=PLATFORM_DEFS[sel]||{};
  const f=def.fields||{};
  if(f.api_key!==undefined){document.getElementById('cfg-key').placeholder=f.api_key;}
  if(f.extra_field_1!==undefined){document.getElementById('cfg-extra1').placeholder=f.extra_field_1;document.getElementById('extra1-label').textContent=f.extra_field_1.split('(')[0].trim()||'Extra Field 1';}
  if(f.api_url!==undefined&&!document.getElementById('cfg-url').value){document.getElementById('cfg-url').placeholder=f.api_url;}
}
document.getElementById('cfg-platform').addEventListener('change',updatePlatformFields);

function setTelegramResult(kind, message) {
  const el=document.getElementById('tg-result');
  if(!el) return;
  el.className=`telegram-result mt-3 ${kind||''}`;
  el.textContent=message||'';
}

function renderTelegramSetup(configs) {
  const cfg=configs.find(c=>c.platform==='telegram')||null;
  const idEl=document.getElementById('tg-config-id');
  const chatEl=document.getElementById('tg-chat-id');
  const activeEl=document.getElementById('tg-active');
  const badge=document.getElementById('tg-config-badge');
  const help=document.getElementById('tg-token-help');
  if(!idEl||!chatEl||!activeEl||!badge) return;
  idEl.value=cfg?.id||'';
  chatEl.value=cfg?.extra_field_1||'';
  activeEl.checked=cfg ? cfg.is_active!==false : true;
  badge.className=`badge ms-auto ${cfg?.is_active?'bg-success':cfg?'bg-warning text-dark':'bg-secondary'}`;
  badge.textContent=cfg?.is_active?'Configured':cfg?'Disabled':'Not configured';
  if(help) help.textContent=cfg?.has_api_key
    ? 'A bot token is saved. Leave this blank to keep it, or enter a new token to replace it.'
    : 'The saved token is never returned to the browser.';
}

function toggleTelegramToken() {
  const input=document.getElementById('tg-token');
  const button=document.getElementById('tg-token-toggle');
  const showing=input.type==='text';
  input.type=showing?'password':'text';
  button.title=showing?'Show token':'Hide token';
  button.setAttribute('aria-label',button.title);
  button.innerHTML=`<i class="bi bi-${showing?'eye':'eye-slash'}"></i>`;
}

function telegramSetupBody() {
  return {
    config_id:document.getElementById('tg-config-id').value,
    bot_token:document.getElementById('tg-token').value.trim(),
    chat_id:document.getElementById('tg-chat-id').value.trim(),
  };
}

async function detectTelegramChat() {
  const body=telegramSetupBody();
  if(!body.bot_token&&!body.config_id){
    setTelegramResult('error','Paste the BotFather token first.');
    return;
  }
  setTelegramResult('working','Looking for a recent message to this bot...');
  const res=await POST('/settings/telegram/detect-chat',body);
  if(!res.ok){
    setTelegramResult('error',res.detail||res.error||'No Telegram chat was found.');
    return;
  }
  document.getElementById('tg-chat-id').value=res.chat_id;
  setTelegramResult('success',`Found ${res.chat_name} (${res.chat_id}).`);
}

async function saveAndTestTelegram() {
  const setup=telegramSetupBody();
  const active=document.getElementById('tg-active').checked;
  if(!setup.config_id&&!setup.bot_token){
    setTelegramResult('error','Paste the complete token from @BotFather.');
    return;
  }
  if(!setup.chat_id){
    setTelegramResult('error','Enter a Chat ID or use Detect Chat ID first.');
    return;
  }

  const button=document.getElementById('tg-save-test');
  button.disabled=true;
  setTelegramResult('working',active?'Saving and contacting Telegram...':'Saving disabled configuration...');
  try {
    const configBody={
      label:'Jarvis Telegram Bot', platform:'telegram', config_type:'bot',
      api_key:setup.bot_token, api_secret:'', api_url:'https://api.telegram.org',
      extra_field_1:setup.chat_id, extra_field_2:'', notes:'Jarvis signal alerts',
      is_active:active, is_default:true,
    };
    const saved=setup.config_id
      ? await PUT(`/settings/${setup.config_id}`,configBody)
      : await POST('/settings',configBody);
    if(saved.detail||saved.error) throw new Error(saved.detail||saved.error);
    document.getElementById('tg-config-id').value=saved.id;
    document.getElementById('tg-token').value='';
    await loadSettings();
    if(!active){
      setTelegramResult('success','Telegram configuration saved. Alerts and commands are disabled.');
      return;
    }
    const tested=await POST('/settings/telegram/test',{
      config_id:saved.id, bot_token:'', chat_id:setup.chat_id,
    });
    if(!tested.ok) throw new Error(tested.detail||tested.error||'Telegram test failed.');
    const bot=tested.bot_username?`@${tested.bot_username}`:tested.bot_name;
    setTelegramResult('success',`${bot} is connected. A test message was sent to chat ${tested.chat_id}.`);
  } catch(error) {
    setTelegramResult('error',error.message||'Telegram setup failed.');
  } finally {
    button.disabled=false;
  }
}

async function loadSettings() {
  const configs=await API('/settings');
  renderTelegramSetup(configs);
  const el=document.getElementById('configs-list');
  const otherConfigs=configs.filter(c=>c.platform!=='telegram');
  if(!otherConfigs.length){el.innerHTML='<div class="text-muted small p-3">No other integrations configured.</div>';return;}
  const grouped={};
  otherConfigs.forEach(c=>{(grouped[c.platform]=grouped[c.platform]||[]).push(c);});
  el.innerHTML=Object.entries(grouped).map(([platform,cfgs])=>`
    <div class="card mb-3">
      <div class="card-header py-2 small fw-bold">${PLATFORM_DEFS[platform]?.label||platform}</div>
      <div class="list-group list-group-flush">
        ${cfgs.map(c=>`<div class="list-group-item bg-dark py-2">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <span class="fw-bold small">${c.label}</span>
              ${c.is_default?'<span class="badge bg-success ms-1" style="font-size:.6rem">DEFAULT</span>':''}
              ${!c.is_active?'<span class="badge bg-secondary ms-1" style="font-size:.6rem">INACTIVE</span>':''}
              <div class="small text-muted mt-1">${c.api_key?'Key: '+c.api_key.slice(0,8)+'…':''} ${c.api_url?'| URL: '+c.api_url.slice(0,30):''}${c.extra_field_1?' | '+c.extra_field_1:''}</div>
              ${c.notes?`<div class="small text-muted">${c.notes}</div>`:''}
            </div>
            <div class="d-flex gap-1 ms-2 flex-shrink-0">
              ${!c.is_default?`<button class="btn btn-outline-success btn-sm py-0 px-1" style="font-size:.7rem" onclick="setDefault('${c.id}')">★</button>`:''}
              <button class="btn btn-outline-primary btn-sm py-0 px-1" style="font-size:.7rem" onclick="editConfig(${JSON.stringify(c).replace(/"/g,'&quot;')})">✎</button>
              <button class="btn btn-outline-danger btn-sm py-0 px-1" style="font-size:.7rem" onclick="deleteConfig('${c.id}')">✕</button>
            </div>
          </div>
        </div>`).join('')}
      </div>
    </div>`).join('');
}

function editConfig(c) {
  document.getElementById('cfg-edit-id').value=c.id;
  document.getElementById('cfg-label').value=c.label||'';
  document.getElementById('cfg-platform').value=c.platform||'alpaca_paper';
  document.getElementById('cfg-key').value=c.api_key==='[REDACTED]'?'':(c.api_key||'');
  document.getElementById('cfg-secret').value=c.api_secret==='[REDACTED]'?'':(c.api_secret||'');
  document.getElementById('cfg-url').value=c.api_url||'';
  document.getElementById('cfg-extra1').value=c.extra_field_1||'';
  document.getElementById('cfg-notes').value=c.notes||'';
  document.getElementById('cfg-active').checked=c.is_active!==false;
  document.getElementById('cfg-default').checked=c.is_default===true;
  updatePlatformFields();
}

function clearConfigForm() {
  ['cfg-edit-id','cfg-label','cfg-key','cfg-secret','cfg-url','cfg-extra1','cfg-notes'].forEach(id=>{document.getElementById(id).value='';});
  document.getElementById('cfg-active').checked=true;
  document.getElementById('cfg-default').checked=false;
}

async function saveConfig() {
  const id=document.getElementById('cfg-edit-id').value;
  const body={
    label:document.getElementById('cfg-label').value,
    platform:document.getElementById('cfg-platform').value,
    api_key:document.getElementById('cfg-key').value,
    api_secret:document.getElementById('cfg-secret').value,
    api_url:document.getElementById('cfg-url').value,
    extra_field_1:document.getElementById('cfg-extra1').value,
    notes:document.getElementById('cfg-notes').value,
    is_active:document.getElementById('cfg-active').checked,
    is_default:document.getElementById('cfg-default').checked,
  };
  if(!body.label||!body.platform){alert('Label and Platform are required');return;}
  const res=id?await fetch(`/api/settings/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json())
               :await POST('/settings',body);
  if(res.error){alert('Error: '+res.error);return;}
  clearConfigForm(); loadSettings();
}

async function setDefault(id) {
  await POST(`/settings/${id}/set-default`,{}); loadSettings();
}

async function deleteConfig(id) {
  if(!confirm('Delete this config?')) return;
  await DEL(`/settings/${id}`); loadSettings();
}


/* ── PERFORMANCE ──────────────────────────────────────────────────────────── */
let perfChart = null;

async function loadPerformanceAnalytics(days=30) {
  try {
    const data = await API(`/performance/analytics?days=${days}`);
    const sharpeEl = document.getElementById('perf-sharpe');
    if(sharpeEl) {
      sharpeEl.textContent = data.sharpe_ratio != null ? Number(data.sharpe_ratio).toFixed(2) : 'N/A';
      sharpeEl.className = 'fs-5 fw-bold ' + (data.sharpe_ratio == null ? 'text-muted' : data.sharpe_ratio >= 1 ? 'text-success' : data.sharpe_ratio >= 0 ? 'text-warning' : 'text-danger');
    }
    const ddEl = document.getElementById('perf-drawdown');
    if(ddEl) {
      const dd = data.max_drawdown_pct;
      ddEl.textContent = dd != null ? `-${Number(dd).toFixed(2)}%` : 'N/A';
      ddEl.className = 'fs-5 fw-bold ' + (dd == null ? 'text-muted' : dd >= 15 ? 'text-danger' : dd >= 5 ? 'text-warning' : 'text-success');
      ddEl.title = data.drawdown_peak_date ? `Peak ${data.drawdown_peak_date} → Trough ${data.drawdown_trough_date}` : '';
    }
    const epEl = document.getElementById('perf-equity-points');
    if(epEl) epEl.textContent = data.equity_curve_points ?? '0';
    const taEl = document.getElementById('perf-trades-analyzed');
    if(taEl) taEl.textContent = data.trades_analyzed ?? '0';

    const bodyEl = document.getElementById('perf-source-body');
    if(bodyEl) {
      const rows = data.by_signal_source || [];
      bodyEl.innerHTML = rows.length ? rows.map(r => {
        const wr = r.win_rate_pct != null ? r.win_rate_pct.toFixed(1)+'%' : 'N/A';
        const wrCls = r.win_rate_pct == null ? '' : r.win_rate_pct >= 55 ? 'text-success' : r.win_rate_pct >= 45 ? 'text-warning' : 'text-danger';
        const pnlCls = r.avg_pnl_pct >= 0 ? 'text-success' : 'text-danger';
        return `<tr><td>${escapeHtml(r.signal_source)}</td><td>${r.total}</td><td>${r.wins}</td><td>${r.losses}</td>` +
               `<td class="${wrCls}">${wr}</td><td class="${pnlCls}">${fmtPct(r.avg_pnl_pct)}</td></tr>`;
      }).join('') : '<tr><td colspan="6" class="text-center text-muted py-3">No closed trades in this period</td></tr>';
    }
  } catch(e) {}
}

async function loadPerformance(days=30) {
  ['30','7','90'].forEach(d=>{
    const btn=document.getElementById(`perf-${d}d`);
    if(btn) btn.classList.toggle('active', d===String(days));
  });
  const data = await API(`/performance?days=${days}`);
  const updEl = document.getElementById('perf-updated');
  if(updEl) updEl.textContent='Last updated '+new Date().toLocaleTimeString();
  loadPerformanceAnalytics(days);

  // KPI cards
  const kpiEl = document.getElementById('perf-kpis');
  if(!kpiEl) return;
  const avgRR  = data.avg_rr != null ? Number(data.avg_rr).toFixed(2) : 'N/A';
  const avgSc  = data.avg_score != null ? Number(data.avg_score).toFixed(0)+'%' : 'N/A';
  const byClass = (data.by_class||[]).map(c=>c.class+': '+c.count).join(' · ') || '—';
  const rrCls   = data.avg_rr>=2?'text-success':data.avg_rr>=1?'text-warning':'text-danger';
  kpiEl.innerHTML =
    '<div class="col-6 col-md-3"><div class="card text-center py-3">' +
      '<div class="h3 fw-bold text-info mb-0">'+(data.executed||0)+'</div>' +
      '<div class="small text-muted">Executed Trades</div></div></div>' +
    '<div class="col-6 col-md-3"><div class="card text-center py-3">' +
      '<div class="h3 fw-bold '+rrCls+' mb-0">'+avgRR+'</div>' +
      '<div class="small text-muted">Avg R:R Ratio</div></div></div>' +
    '<div class="col-6 col-md-3"><div class="card text-center py-3">' +
      '<div class="h3 fw-bold text-primary mb-0">'+(data.good_rr_count||0)+'</div>' +
      '<div class="small text-muted">Setups R:R &ge; 2.0</div></div></div>' +
    '<div class="col-6 col-md-3"><div class="card text-center py-3">' +
      '<div class="h3 fw-bold text-warning mb-0">'+avgSc+'</div>' +
      '<div class="small text-muted">Avg Composite Score</div>' +
      '<div class="text-muted" style="font-size:.65rem">'+byClass+'</div></div></div>';

  // Daily volume bar chart
  const daily  = data.daily_volume || [];
  const labels = daily.map(function(d){return d.date.slice(5);});
  const counts = daily.map(function(d){return d.count;});
  const ctx = document.getElementById('perf-chart');
  if(ctx){
    if(perfChart) perfChart.destroy();
    perfChart = new Chart(ctx.getContext('2d'), {
      type:'bar',
      data:{labels:labels,datasets:[{
        label:'Signals executed',data:counts,
        backgroundColor:'rgba(13,202,240,0.5)',
        borderColor:'rgba(13,202,240,0.9)',
        borderWidth:1,borderRadius:3
      }]},
      options:{
        responsive:true,maintainAspectRatio:true,
        plugins:{legend:{display:false}},
        scales:{
          x:{ticks:{color:'#adb5bd',font:{size:10}},grid:{color:'rgba(255,255,255,0.05)'}},
          y:{ticks:{color:'#adb5bd',font:{size:10},stepSize:1},grid:{color:'rgba(255,255,255,0.05)'},beginAtZero:true}
        }
      }
    });
  }

  // Trade history table
  const tbody = document.getElementById('perf-trades-body');
  if(!tbody) return;
  const trades = data.recent_trades || [];
  if(!trades.length){
    tbody.innerHTML='<tr><td colspan="11" class="text-center text-muted py-4">No executed trades in this period</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(function(t){
    const dirCls = t.direction==='Long'?'text-success':'text-primary';
    var rr = '—', rrCls = 'text-muted';
    if(t.entry_price&&t.target_price&&t.stop_loss&&t.entry_price>t.stop_loss){
      rr = ((t.target_price-t.entry_price)/(t.entry_price-t.stop_loss)).toFixed(1);
      rrCls = parseFloat(rr)>=2?'text-success':parseFloat(rr)>=1?'text-warning':'text-danger';
    }
    const sc = t.composite_score||t.confidence||0;
    const scCls = sc>=70?'text-success':sc>=50?'text-warning':'text-danger';
    const src = t.signal_source==='opportunistic'?'📰 News':'📋 Watch';
    const earn = t.earnings_risk?' 📅':'';
    const statCls = t.status==='Closed'?'text-success':t.status==='Executed'?'text-info':'text-danger';
    return '<tr>'+
      '<td class="fw-bold">'+t.asset_symbol+earn+'</td>'+
      '<td class="'+dirCls+'">'+t.direction+'</td>'+
      '<td>'+fmtPrice(t.entry_price)+'</td>'+
      '<td>'+fmtPrice(t.target_price)+'</td>'+
      '<td>'+fmtPrice(t.stop_loss)+'</td>'+
      '<td class="'+rrCls+' fw-bold">'+rr+'</td>'+
      '<td class="'+scCls+'">'+Number(sc).toFixed(0)+'%</td>'+
      '<td><span class="badge bg-secondary">'+(t.timeframe||'—')+'</span></td>'+
      '<td class="text-muted" style="font-size:.75rem">'+src+'</td>'+
      '<td class="'+statCls+'">'+t.status+'</td>'+
      '<td class="text-muted" style="font-size:.75rem">'+timeAgo(t.generated_at)+'</td>'+
    '</tr>';
  }).join('');
}

/* ── AUTOMATIC PAPER-ONLY SIMULATOR ─────────────────────────────────────── */
async function loadAutoPaper() {
  const state = document.getElementById('auto-paper-state');
  if(!state) return;
  state.textContent = 'Loading virtual ledger...';
  try {
    const data = await API('/auto-paper/summary');
    const s = data.summary || {};
    const pnlClass = value => Number(value||0) >= 0 ? 'text-success' : 'text-danger';
    const money = value => `${Number(value||0)>=0?'+':''}$${Number(value||0).toLocaleString('en-US',{maximumFractionDigits:2})}`;
    document.getElementById('auto-paper-kpis').innerHTML = [
      ['Virtual equity', '$'+Number(s.equity||0).toLocaleString('en-US',{maximumFractionDigits:2}), 'text-white'],
      ['Total P/L', money(s.total_pnl), pnlClass(s.total_pnl)],
      ['Realized', money(s.realized_pnl), pnlClass(s.realized_pnl)],
      ['Open P/L', money(s.unrealized_pnl), pnlClass(s.unrealized_pnl)],
      ['Win rate', Number(s.win_rate||0).toFixed(1)+'%', 'text-info'],
      ['W / L', `${s.wins||0} / ${s.losses||0}`, 'text-white'],
      ['Gross profit', money(s.gross_profit), 'text-success'],
      ['Gross loss', money(s.gross_loss), 'text-danger'],
    ].map(([label,value,cls]) => `<div class="col-6 col-md-3"><div class="auto-sim-kpi"><span>${label}</span><strong class="${cls}">${value}</strong></div></div>`).join('');
    const positions = data.positions || [];
    document.getElementById('auto-paper-positions').innerHTML = positions.length ? positions.map(position => {
      const pnl = Number(position.unrealized_pnl||0);
      return `<tr><td class="fw-bold">${escapeHtml(position.symbol)}</td><td>${escapeHtml(position.direction)}</td><td>${fmtPrice(position.entry_price)}</td><td>${fmtPrice(position.current_price)}</td><td>${fmtPrice(position.target_price)}</td><td>${fmtPrice(position.stop_loss)}</td><td class="${pnlClass(pnl)}">${money(pnl)}</td></tr>`;
    }).join('') : '<tr><td colspan="7" class="text-center text-muted py-4">No open simulated positions</td></tr>';
    const trades = data.trades || [];
    document.getElementById('auto-paper-trades').innerHTML = trades.length ? trades.map(trade => {
      const pnl = Number(trade.realized_pnl||0);
      return `<tr><td class="fw-bold">${escapeHtml(trade.symbol)}</td><td>${escapeHtml(trade.direction)}</td><td class="${pnlClass(pnl)}">${money(pnl)}</td><td>${escapeHtml(trade.close_reason)}</td><td class="text-muted">${timeAgo(trade.closed_at)}</td></tr>`;
    }).join('') : '<tr><td colspan="5" class="text-center text-muted py-4">No completed simulated trades</td></tr>';
    state.textContent = `${s.open_positions||0} open | ${s.total_trades||0} completed | fixed $1,000 virtual margin per signal`;
  } catch(e) {
    state.textContent = 'Automatic simulation data is unavailable.';
  }
}

async function runAutoPaper(button) {
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const result = await POST('/auto-paper/run', {});
    if (result.busy) {
      showToast('Auto Sim is already running; results will refresh shortly', 'warning');
    } else {
      showToast(`Auto Sim: ${result.opened||0} opened, ${result.closed||0} closed`, 'info');
    }
    await loadAutoPaper();
  } catch(e) {
    showToast('Automatic simulator run failed', 'danger');
  } finally {
    if (button) button.disabled = false;
  }
}

/* ── GLOBAL INIT + REFRESH ───────────────────────────────────────────────── */
async function refreshAll() {
  const active=document.querySelector('.nav-link.active')?.getAttribute('href')?.replace('#tab-','');
  document.getElementById('last-refresh').textContent='Updated '+new Date().toLocaleTimeString();
  refreshJobIndicators();
  refreshRegimeBadge();
  refreshKillSwitch();
  if(active==='signals'||!active) loadSignals();
  else if(active==='positions'){loadPositions();loadEquityCurve(24);}
  else if(active==='market')   loadMarket();
  else if(active==='threats')  loadThreats();
  else if(active==='news')     loadNews();
  else if(active==='scanner')  loadScannerSignals();
  else if(active==='jobs')     loadJobs();
  else if(active==='settings') loadSettings();
  else if(active==='performance') loadPerformance(30);
  else if(active==='auto-paper') loadAutoPaper();
}

// Tab change — load relevant data
document.querySelectorAll('[data-bs-toggle="tab"]').forEach(el=>{
  el.addEventListener('shown.bs.tab',e=>{
    const tab=e.target.getAttribute('href')?.replace('#tab-','');
    if(tab==='signals')   loadSignals();
    else if(tab==='positions'){loadPositions();loadEquityCurve(24);}
    else if(tab==='market')   loadMarket();
    else if(tab==='threats')  loadThreats();
    else if(tab==='news')     loadNews();
    else if(tab==='scanner')  loadScannerSignals();
    else if(tab==='jobs')     loadJobs();
    else if(tab==='settings'){loadSettings();updatePlatformFields();}
    else if(tab==='performance') loadPerformance(30);
    else if(tab==='auto-paper') loadAutoPaper();
  });
});

// Initial load
loadSignals();
refreshJobIndicators();
refreshRegimeBadge();
refreshKillSwitch();

// Auto-refresh every 60s
setInterval(refreshAll, 60000);




// ═══════════════════════════════════════════════════════════════════════════
//  PAPER TRADING
// ═══════════════════════════════════════════════════════════════════════════

async function loadPaperTab() {
  // Start a 30-second auto-refresh when on the paper tab
  if (window._paperRefreshTimer) clearInterval(window._paperRefreshTimer);
  window._paperRefreshTimer = setInterval(() => {
    if (document.getElementById('tab-paper')?.classList.contains('active')) {
      loadPaperTab();
    } else {
      clearInterval(window._paperRefreshTimer);
      window._paperRefreshTimer = null;
    }
  }, 30000);
  try {
    const data = await API('/paper/summary');
    const p    = data.portfolio;

    // KPIs
    const retPct    = p.total_return_pct ?? 0;
    const equity    = p.equity ?? p.cash ?? 0;
    const openPnl   = p.open_pnl ?? 0;
    const realPnl   = p.realized_pnl ?? 0;
    const marginUse = p.margin_in_use ?? 0;
    const cash      = p.cash ?? 0;
    const winRate   = p.win_rate ?? 0;
    const totalTr   = p.total_trades ?? 0;
    const winTr     = p.winning_trades ?? 0;

    document.getElementById('paperEquity').textContent     = '$' + equity.toLocaleString('en-US', {maximumFractionDigits:0});
    const retEl = document.getElementById('paperReturn');
    retEl.textContent = (retPct >= 0 ? '+' : '') + retPct.toFixed(2) + '%';
    retEl.className   = 'fs-5 fw-bold ' + (retPct >= 0 ? 'text-success' : 'text-danger');

    const realEl = document.getElementById('paperRealizedPnl');
    realEl.textContent = (realPnl >= 0 ? '+$' : '-$') + Math.abs(realPnl).toLocaleString('en-US', {maximumFractionDigits:2});
    realEl.className   = 'fs-5 fw-bold ' + (realPnl >= 0 ? 'text-success' : 'text-danger');

    document.getElementById('paperWinRate').textContent    = winRate + '%';
    const opEl = document.getElementById('paperOpenPnl');
    opEl.textContent = (openPnl >= 0 ? '+$' : '-$') + Math.abs(openPnl).toLocaleString('en-US', {maximumFractionDigits:2});
    opEl.className   = 'fs-5 fw-bold ' + (openPnl >= 0 ? 'text-success' : 'text-danger');
    document.getElementById('paperCash').textContent       = '$' + cash.toLocaleString('en-US', {maximumFractionDigits:0});
    document.getElementById('paperMargin').textContent     = '$' + marginUse.toLocaleString('en-US', {maximumFractionDigits:0});
    document.getElementById('paperTrades').textContent     = totalTr + ' (' + winTr + ' W)';

    // Open positions
    const posBody = document.getElementById('paperPositionsTbody');
    if (!data.positions || data.positions.length === 0) {
      posBody.innerHTML = '<tr><td colspan="14" class="text-muted text-center">No open paper positions</td></tr>';
    } else {
      const posRows = [];
      for (const pos of data.positions) {
        try {
          const pnlCls   = (pos.unrealized_pnl||0) >= 0 ? 'text-success' : 'text-danger';
          const dirBadge = paperDirBadge(pos.direction);
          const sideBadge = (pos.side||'long') === 'long'
            ? '<span class="badge bg-success">LONG</span>'
            : '<span class="badge bg-danger">SHORT</span>';
          const fmt = (v,d=4) => (v!=null && !isNaN(v)) ? Number(v).toFixed(d) : '—';

          // Build signal detail panel (same as real positions tab)
          const s = pos.signal;
          let detailRow = '';
          if (s) {
            const sc      = s.composite_score || s.confidence || 0;
            const scBadge = sc>=70 ? 'bg-success' : sc>=50 ? 'bg-warning text-dark' : 'bg-danger';
            const rr      = pos.entry_price && pos.target_price && pos.stop_loss && pos.entry_price > pos.stop_loss
                            ? ((pos.target_price - pos.entry_price) / (pos.entry_price - pos.stop_loss)).toFixed(1)
                            : null;
            const rrBadge = rr ? `<span class="badge bg-dark border border-secondary ms-2">R:R ${rr}:1</span>` : '';
            const progPct = pos.entry_price && pos.target_price && pos.current_price
                            ? Math.round((pos.current_price - pos.entry_price) / (pos.target_price - pos.entry_price) * 100)
                            : null;
            const progBar = progPct != null ? `
              <div class="mt-2">
                <div class="small text-muted d-flex justify-content-between"><span>Trade Progress</span><span>${progPct}% to target</span></div>
                <div class="progress mt-1" style="height:4px"><div class="progress-bar ${progPct>=100?'bg-success':progPct>=0?'bg-info':'bg-danger'}" style="width:${Math.max(0,Math.min(100,progPct))}%"></div></div>
              </div>` : '';
            detailRow = `<tr class="signal-detail-row" style="display:none">
              <td colspan="14" class="py-0">
                <div class="signal-context-panel px-3 py-2">
                  <div class="row g-2 align-items-start">
                    <div class="col-lg-4">
                      <div class="d-flex align-items-center gap-2 mb-1 flex-wrap">
                        <span class="badge ${s.direction==='Long'||s.direction==='Bounce'?'bg-success':'bg-primary'}">${s.direction}</span>
                        <span class="badge ${scBadge}">Score ${sc.toFixed(0)}%</span>
                        <span class="badge bg-secondary">${s.timeframe||''}</span>
                        ${rrBadge}
                        <span class="text-muted small ms-auto">${timeAgo(s.generated_at)}</span>
                      </div>
                      <div class="d-flex gap-3 small">
                        <div><span class="text-muted">Entry</span><br><span class="text-info fw-bold">${fmtPrice(s.entry_price)}</span></div>
                        <div><span class="text-muted">Target</span><br><span class="text-success fw-bold">${fmtPrice(s.target_price)}</span></div>
                        <div><span class="text-muted">Stop</span><br><span class="text-danger fw-bold">${fmtPrice(s.stop_loss)}</span></div>
                      </div>
                      ${progBar}
                    </div>
                    <div class="col-lg-5">
                      <div class="small text-muted mb-1"><i class="bi bi-chat-text-fill text-info me-1"></i>LLM Reasoning</div>
                      <div class="small" style="line-height:1.4;color:#ccc">${(s.reasoning||'No reasoning recorded').slice(0,350)}${(s.reasoning||'').length>350?'…':''}</div>
                    </div>
                    <div class="col-lg-3">
                      ${s.key_risks ? `<div class="small text-muted mb-1"><i class="bi bi-exclamation-triangle-fill text-warning me-1"></i>Key Risks</div><div class="small text-warning" style="line-height:1.4">${s.key_risks.slice(0,200)}</div>` : ''}
                      ${s.momentum ? `<div class="small text-muted mt-2">Momentum: <span class="text-info">${s.momentum}</span></div>` : ''}
                      ${s.trigger_event ? `<div class="small text-muted mt-1">Trigger: <span class="text-light">${s.trigger_event.slice(0,80)}</span></div>` : ''}
                      <div class="small text-muted mt-1">Source: <span class="text-light">${s.signal_source||'watchlist'}</span></div>
                      <div class="small text-muted mt-1">Signal: <span class="badge bg-dark border border-secondary">${s.status||''}</span></div>
                    </div>
                  </div>
                </div>
              </td>
            </tr>`;
          } else {
            detailRow = `<tr class="signal-detail-row" style="display:none">
              <td colspan="14" class="py-1">
                <div class="signal-context-panel px-3 py-2">
                  <span class="text-muted small"><i class="bi bi-info-circle me-1"></i>No signal linked — position opened manually or signal expired.</span>
                </div>
              </td>
            </tr>`;
          }

          posRows.push(`<tr class="position-row" style="cursor:pointer" onclick="toggleSignalRow(this)">
            <td class="fw-semibold text-warning">${pos.symbol||'?'} <i class="bi bi-chevron-down text-muted" style="font-size:.65rem"></i></td>
            <td>${dirBadge}</td>
            <td>${sideBadge}</td>
            <td>${(pos.leverage||1).toFixed(1)}×</td>
            <td>${fmt(pos.qty)}</td>
            <td>$${fmt(pos.entry_price)}</td>
            <td>$${fmt(pos.current_price)}</td>
            <td class="text-success">$${fmt(pos.target_price)}</td>
            <td class="text-danger">$${fmt(pos.stop_loss)}</td>
            <td>$${(pos.notional||0).toLocaleString('en-US',{maximumFractionDigits:0})}</td>
            <td class="${pnlCls}">${(pos.unrealized_pnl||0) >= 0 ? '+' : ''}$${fmt(pos.unrealized_pnl,2)}</td>
            <td class="${pnlCls}">${(pos.unrealized_pct||0) >= 0 ? '+' : ''}${fmt(pos.unrealized_pct,2)}%</td>
            <td class="text-muted">${timeAgo(pos.opened_at)}</td>
            <td><button class="btn btn-xs btn-outline-danger py-0 px-1" onclick="event.stopPropagation();paperClose('${pos.id}')"><i class="bi bi-x-lg"></i></button></td>
          </tr>${detailRow}`);
        } catch(rowErr) {
          console.warn('[Paper] Skipped bad position row:', pos.symbol, rowErr);
        }
      }
      posBody.innerHTML = posRows.length ? posRows.join('') : '<tr><td colspan="14" class="text-muted text-center">No open paper positions</td></tr>';
    }

    // Trade history
    const trBody = document.getElementById('paperTradesTbody');
    if (!data.trades || data.trades.length === 0) {
      trBody.innerHTML = '<tr><td colspan="10" class="text-muted text-center">No completed trades</td></tr>';
    } else {
      const trRows = [];
      for (const t of data.trades) {
        try {
          const pnlCls = (t.realized_pnl||0) >= 0 ? 'text-success' : 'text-danger';
          const reasonBadge = paperReasonBadge(t.close_reason);
          const fmt = (v,d=4) => (v!=null && !isNaN(v)) ? Number(v).toFixed(d) : '—';
          trRows.push(`<tr>
            <td class="fw-semibold text-warning">${t.symbol||'?'}</td>
            <td>${paperDirBadge(t.direction)}</td>
            <td>${(t.leverage||1).toFixed(1)}×</td>
            <td>$${fmt(t.entry_price)}</td>
            <td>$${fmt(t.exit_price)}</td>
            <td class="${pnlCls} fw-semibold">${(t.realized_pnl||0) >= 0 ? '+' : ''}$${fmt(Math.abs(t.realized_pnl||0),2)}</td>
            <td class="${pnlCls}">${(t.pnl_pct||0) >= 0 ? '+' : ''}${fmt(t.pnl_pct,2)}%</td>
            <td>${reasonBadge}</td>
            <td class="text-muted small">${timeAgo(t.opened_at)}</td>
            <td class="text-muted small">${timeAgo(t.closed_at)}</td>
          </tr>`);
        } catch(rowErr) {
          console.warn('[Paper] Skipped bad trade row:', t.symbol, rowErr);
        }
      }
      trBody.innerHTML = trRows.length ? trRows.join('') : '<tr><td colspan="10" class="text-muted text-center">No completed trades</td></tr>';
    }
    // Populate compare card
    loadCompareCard();
  } catch (e) {
    console.error('[Paper] loadPaperTab error:', e);
  }
}

function paperDirBadge(dir) {
  const map = {
    'Long':             '<span class="badge bg-success">📈 Long 1×</span>',
    'Bounce':           '<span class="badge bg-success">📈 Bounce 1×</span>',
    'Long_Leveraged':   '<span class="badge bg-primary">🚀 Long 2×</span>',
    'Short':            '<span class="badge bg-danger">📉 Short 1×</span>',
    'Short_Leveraged':  '<span class="badge" style="background:#ff6600">🔻 Short 2×</span>',
  };
  return map[dir] || `<span class="badge bg-secondary">${dir||'Long'}</span>`;
}

function paperReasonBadge(r) {
  const map = {
    'stop_loss':   '<span class="badge bg-danger">Stop Loss</span>',
    'take_profit': '<span class="badge bg-success">Take Profit</span>',
    'manual':      '<span class="badge bg-secondary">Manual</span>',
    'margin_call': '<span class="badge bg-warning text-dark">Margin Call</span>',
  };
  return map[r] || `<span class="badge bg-secondary">${r||'—'}</span>`;
}


async function loadCompareCard() {
  try {
    const [paperData, realData] = await Promise.all([
      API('/paper/summary'),
      API('/positions/with-signals').catch(() => null),
    ]);

    const p     = paperData.portfolio || {};
    const acct  = (realData && realData.account) || {};
    const pos   = (realData && realData.positions) || [];

    // Paper values
    const pEq   = p.equity ?? p.cash ?? 0;
    const pOpen = p.open_pnl ?? 0;
    const pPos  = (paperData.positions || []).length;
    const pWin  = p.win_rate ?? 0;
    const pRet  = p.total_return_pct ?? 0;

    // Real values
    const rEq   = parseFloat(acct.equity || 0);
    const rOpen = parseFloat(acct.unrealized_pl || 0);
    const rPos  = pos.length;
    const rDay  = parseFloat(acct.equity_previous_close ? rEq - parseFloat(acct.equity_previous_close) : 0);
    const rStart = 100000; // same baseline for fair comparison
    const rRet  = rStart > 0 ? ((rEq - rStart) / rStart * 100) : 0;

    const fmt$ = v => (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString('en-US', {maximumFractionDigits:2});
    const cls  = v => v >= 0 ? 'text-success' : 'text-danger';

    document.getElementById('cmpRealEquity').textContent    = '$' + rEq.toLocaleString('en-US',{maximumFractionDigits:0});
    document.getElementById('cmpRealOpenPnl').textContent   = fmt$(rOpen);
    document.getElementById('cmpRealOpenPnl').className     = 'fw-bold ' + cls(rOpen);
    document.getElementById('cmpRealPositions').textContent = rPos + ' open';
    document.getElementById('cmpRealDayPnl').textContent    = fmt$(rDay);
    document.getElementById('cmpRealDayPnl').className      = 'fw-bold ' + cls(rDay);

    document.getElementById('cmpPaperEquity').textContent   = '$' + pEq.toLocaleString('en-US',{maximumFractionDigits:0});
    document.getElementById('cmpPaperOpenPnl').textContent  = fmt$(pOpen);
    document.getElementById('cmpPaperOpenPnl').className    = 'fw-bold ' + cls(pOpen);
    document.getElementById('cmpPaperPositions').textContent= pPos + ' open';
    document.getElementById('cmpPaperWinRate').textContent  = pWin + '%';

    // Progress bars — clamp to 0–20% return range for visual scale
    const scale = 20;
    const pPct  = Math.min(Math.max(pRet, -scale), scale);
    const rPct  = Math.min(Math.max(rRet, -scale), scale);
    const toWidth = v => Math.abs(v) / scale * 50 + 50; // center at 50%

    document.getElementById('cmpPaperBar').style.width = Math.min(100, Math.max(0, toWidth(pPct))) + '%';
    document.getElementById('cmpRealBar').style.width  = Math.min(100, Math.max(0, toWidth(rPct))) + '%';
    document.getElementById('cmpPaperBar').style.background = pRet >= 0 ? '#ffc107' : '#dc3545';
    document.getElementById('cmpRealBar').style.background  = rRet  >= 0 ? '#198754' : '#dc3545';

    const delta = pRet - rRet;
    const deltaEl = document.getElementById('cmpDeltaLabel');
    deltaEl.textContent  = `Paper ${delta >= 0 ? '+' : ''}${delta.toFixed(2)}% vs Real`;
    deltaEl.className    = 'small fw-bold ' + cls(delta);
  } catch(e) {
    console.warn('[Compare] Card load error:', e);
  }
}

async function paperOpenPosition() {
  const sym   = document.getElementById('paperSym').value.trim().toUpperCase();
  const dir   = document.getElementById('paperDir').value;
  const entry = parseFloat(document.getElementById('paperEntry').value) || null;
  const tgt   = parseFloat(document.getElementById('paperTarget').value) || null;
  const stp   = parseFloat(document.getElementById('paperStop').value) || null;
  if (!sym) { alert('Enter a symbol'); return; }
  try {
    const res = await POST('/paper/open', {
      symbol: sym, paper_direction: dir, asset_class: sym.includes('/') ? 'Crypto' : 'Equity',
      entry_price: entry, target_price: tgt, stop_loss: stp
    });
    if (res.ok) {
      showToast(`✅ Paper ${dir} opened on ${sym}`);
      loadPaperTab();
      document.getElementById('paperSym').value = '';
    } else {
      alert('Error: ' + (res.error || JSON.stringify(res)));
    }
  } catch(e) { alert('Error: ' + e); }
}

async function paperClose(posId) {
  if (!confirm('Close this paper position at market price?')) return;
  try {
    const res = await POST(`/paper/close/${posId}`);
    if (res.ok) {
      const sign = res.pnl >= 0 ? '+' : '';
      showToast(`✅ Closed ${res.symbol} | P&L ${sign}$${res.pnl.toFixed(2)} (${sign}${res.pnl_pct.toFixed(2)}%)`);
      loadPaperTab();
    } else {
      alert('Error: ' + (res.error || JSON.stringify(res)));
    }
  } catch(e) { alert('Error: ' + e); }
}

async function paperRunMTM() {
  try {
    const res = await POST('/paper/run-mtm');
    const closed = res.closed || [];
    let msg = `✅ MTM updated ${res.updated || 0} positions`;
    if (closed.length) msg += ` | Auto-closed: ${closed.map(c => c.symbol + ' (' + c.reason + ')').join(', ')}`;
    showToast(msg);
    loadPaperTab();
  } catch(e) { alert('Error: ' + e); }
}

async function paperReset() {
  if (!confirm('⚠️ RESET paper account to $100,000? All positions and trade history will be erased.')) return;
  try {
    const res = await POST('/paper/reset');
    showToast('✅ Paper account reset to $100,000');
    loadPaperTab();
  } catch(e) { alert('Error: ' + e); }
}

// Expose paper-execute on signal cards
async function paperExecuteSignal(signalId, symbol, suggestedDirection='Short') {
  const dir = prompt(`Paper trade direction for ${symbol}?\nOptions: Long, Long_Leveraged, Short, Short_Leveraged`, suggestedDirection);
  if (!dir) return;
  try {
    const res = await POST(`/signals/${signalId}/paper-execute?direction=${encodeURIComponent(dir)}`);
    if (res.ok) {
      showToast(`✅ Paper ${dir} opened for ${symbol}`);
      loadPaperTab();
    } else {
      alert('Error: ' + (res.error || JSON.stringify(res)));
    }
  } catch(e) { alert('Error: ' + e); }
}

// ── Learning Engine Tab ──────────────────────────────────────────────────────
let allOutcomes = [];
let allAccuracy = [];

// learningMode: 'live' | 'paper' | 'all'
let learningMode = 'all';


async function triggerBackfill() {
  const btn = document.getElementById('backfill-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Running...'; }
  try {
    const res = await POST('/learning/backfill-paper', {});
    const msg = `Backfill complete: ${res.inserted} inserted, ${res.skipped} skipped, ${res.errors} errors`;
    alert(msg);
    loadLearning(learningMode);
  } catch(e) {
    alert('Backfill failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-database-fill-up"></i> Backfill Paper'; }
  }
}

async function loadLearning(mode) {
  if (mode !== undefined) learningMode = mode;

  // Update toggle button states
  ['live','paper','all'].forEach(m => {
    const btn = document.getElementById('learn-mode-' + m);
    if (btn) btn.classList.toggle('active', m === learningMode);
  });

  const paperParam = learningMode; // 'live','paper','all'
  try {
    const [summary, outcomes, accuracy, patterns, regimes, lessons] = await Promise.all([
      API('/learning/summary?paper=' + paperParam),
      API('/learning/outcomes?limit=500&paper=' + (paperParam === 'paper' ? 'true' : paperParam === 'all' ? 'all' : 'false')),
      API('/learning/accuracy'),
      API('/learning/patterns'),
      API('/learning/regimes'),
      API('/learning/lessons?limit=30'),
    ]);

    allOutcomes = outcomes || [];
    allAccuracy = accuracy || [];

    // Populate symbol filter
    const symSel = document.getElementById('learning-filter');
    if (symSel) {
      const syms = [...new Set(allOutcomes.map(o => o.symbol))].sort();
      symSel.innerHTML = '<option value="">All Symbols</option>' +
        syms.map(s => `<option value="${s}">${s}</option>`).join('');
    }

    renderLearningSummary(summary);
    renderAccuracy(allAccuracy);
    filterOutcomes();
    renderPatterns(patterns || []);
    renderRegimes(regimes || []);
    renderLessons(lessons || []);

  } catch(e) {
    console.error('loadLearning failed', e);
  }
}

function renderLearningSummary(s) {
  if (!s || s.total === 0) {
    document.getElementById('learning-empty').style.display = '';
    document.getElementById('learning-kpis').style.display = 'none';
    return;
  }
  document.getElementById('learning-empty').style.display = 'none';
  document.getElementById('learning-kpis').style.display = '';

  const wr = ((s.win_rate || 0) * 100).toFixed(1);
  const wrColor = s.win_rate >= 0.6 ? 'text-success' : s.win_rate >= 0.4 ? 'text-warning' : 'text-danger';

  document.getElementById('kpi-total').textContent  = s.total || 0;
  const wrEl = document.getElementById('kpi-winrate');
  wrEl.textContent  = wr + '%';
  wrEl.className    = `fs-3 fw-bold ${wrColor}`;
  const avgEl = document.getElementById('kpi-avgpnl');
  avgEl.textContent = (s.avg_pnl >= 0 ? '+' : '') + (s.avg_pnl || 0).toFixed(2) + '%';
  avgEl.className   = `fs-3 fw-bold ${s.avg_pnl >= 0 ? 'text-success' : 'text-danger'}`;
  const totEl = document.getElementById('kpi-totalpnl');
  totEl.textContent = '$' + (s.total_pnl_usd >= 0 ? '+' : '') + (s.total_pnl_usd || 0).toFixed(2);
  totEl.className   = `fs-3 fw-bold ${s.total_pnl_usd >= 0 ? 'text-success' : 'text-danger'}`;
  document.getElementById('kpi-best').textContent   = '+' + (s.best_trade || 0).toFixed(2) + '%';
  document.getElementById('kpi-worst').textContent  = (s.worst_trade || 0).toFixed(2) + '%';
  const holdMin = s.avg_hold_min || 0;
  document.getElementById('kpi-hold').textContent   = holdMin >= 60
    ? (holdMin/60).toFixed(1) + 'h' : Math.round(holdMin) + 'm';
  document.getElementById('kpi-wl').textContent     = `${s.wins} / ${s.losses}`;
}

function renderAccuracy(rows) {
  const tbody = document.getElementById('accuracy-tbody');
  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-3">No data yet</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const wr = ((r.win_rate || 0) * 100).toFixed(0);
    const wrColor = r.win_rate >= 0.6 ? 'text-success' : r.win_rate >= 0.4 ? 'text-warning' : 'text-danger';
    const pnlColor = r.avg_pnl_pct >= 0 ? 'text-success' : 'text-danger';
    const holdMin = r.avg_hold_min || 0;
    const holdStr = holdMin >= 60 ? (holdMin/60).toFixed(1)+'h' : Math.round(holdMin)+'m';
    return `<tr>
      <td><span class="fw-semibold text-white">${r.symbol}</span><br>
          <span class="text-muted" style="font-size:0.7rem">${r.asset_class||''} ${r.timeframe||''}</span></td>
      <td>${r.total_trades}</td>
      <td class="${wrColor} fw-bold">${wr}%</td>
      <td class="${pnlColor}">${r.avg_pnl_pct >= 0 ? '+' : ''}${(r.avg_pnl_pct||0).toFixed(2)}%</td>
      <td class="text-muted">${holdStr}</td>
      <td class="text-success">+${(r.best_pnl_pct||0).toFixed(2)}%</td>
      <td class="text-danger">${(r.worst_pnl_pct||0).toFixed(2)}%</td>
    </tr>`;
  }).join('');
}

function filterOutcomes() {
  const sym = document.getElementById('learning-filter')?.value || '';
  const outcomeFilter = document.getElementById('learning-outcome-filter')?.value || '';
  let rows = allOutcomes;
  if (sym) rows = rows.filter(o => o.symbol === sym);
  if (outcomeFilter) rows = rows.filter(o => o.outcome === outcomeFilter);
  renderOutcomes(rows);
}

function renderOutcomes(rows) {
  const container = document.getElementById('outcomes-daily-groups');
  const tbody     = document.getElementById('outcomes-tbody');
  const empty     = document.getElementById('learning-empty');

  // Support both old table layout and new grouped layout
  const target = container || tbody;

  if (!rows || rows.length === 0) {
    if (target) target.innerHTML = target === tbody
      ? '<tr><td colspan="9" class="text-center text-muted py-3">No trades match filter</td></tr>'
      : '<div class="text-center text-muted py-4"><i class="bi bi-journal-x fs-4 d-block mb-2 opacity-40"></i>No trades match the current filter</div>';
    if (allOutcomes.length === 0 && empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';

  // Group by calendar date (local)
  const groups = {};
  rows.forEach(r => {
    const dateKey = r.exited_at
      ? new Date(r.exited_at).toLocaleDateString('en-US', {weekday:'short', year:'numeric', month:'short', day:'numeric'})
      : 'Unknown Date';
    (groups[dateKey] = groups[dateKey] || []).push(r);
  });

  const exitBadgeClass = {
    'TAKE_PROFIT': 'badge bg-success',
    'HARD_STOP':   'badge bg-danger',
    'LLM_EXIT':    'badge bg-warning text-dark',
    'MANUAL':      'badge bg-secondary',
    'TIMEOUT':     'badge bg-secondary',
  };

  function tradeRow(r) {
    const outcomeIcon = r.outcome === 'WIN' ? '✅' : r.outcome === 'LOSS' ? '❌' : '➖';
    const pnlColor    = (r.pnl_pct||0) >= 0 ? 'text-success' : 'text-danger';
    const pnlUsdColor = (r.pnl_usd||0) >= 0 ? 'text-success' : 'text-danger';
    const holdMin     = r.hold_duration_m || 0;
    const holdStr     = holdMin >= 60 ? (holdMin/60).toFixed(1)+'h' : Math.round(holdMin)+'m';
    const timeStr     = r.exited_at ? new Date(r.exited_at).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'}) : '—';
    const eBadge      = exitBadgeClass[r.exit_reason] || 'badge bg-secondary';
    const dirBadge    = (r.direction==='BUY'||r.direction==='long') ? 'bg-success' : 'bg-danger';
    return `<tr>
      <td class="fw-semibold text-white">${r.symbol}</td>
      <td><span class="badge ${dirBadge}">${r.direction||'—'}</span></td>
      <td>${outcomeIcon} <span class="fw-semibold">${r.outcome}</span></td>
      <td class="${pnlColor} fw-bold">${(r.pnl_pct||0) >= 0 ? '+' : ''}${(r.pnl_pct||0).toFixed(2)}%</td>
      <td class="${pnlUsdColor}">${(r.pnl_usd||0) >= 0 ? '+$' : '-$'}${Math.abs(r.pnl_usd||0).toFixed(2)}</td>
      <td class="text-muted">${holdStr}</td>
      <td><span class="${eBadge}" style="font-size:0.7rem">${r.exit_reason||'—'}</span></td>
      <td class="text-muted" style="font-size:0.72rem">${r.market_regime||'—'}</td>
      <td class="text-muted" style="font-size:0.72rem">${timeStr}</td>
    </tr>`;
  }

  // If container exists, render collapsible day groups
  if (container) {
    let html = '';
    let groupIdx = 0;
    Object.entries(groups).forEach(([date, trades]) => {
      const wins   = trades.filter(t => t.outcome === 'WIN').length;
      const losses = trades.filter(t => t.outcome === 'LOSS').length;
      const dayPnl = trades.reduce((s, t) => s + (t.pnl_usd||0), 0);
      const dayPct = trades.reduce((s, t) => s + (t.pnl_pct||0), 0) / trades.length;
      const dayPnlColor = dayPnl >= 0 ? 'text-success' : 'text-danger';
      const collapseId  = `outcome-day-${groupIdx++}`;
      const isFirst     = groupIdx === 1;
      html += `
        <div class="outcome-day-group mb-2">
          <div class="outcome-day-header d-flex align-items-center justify-content-between px-3 py-2 rounded"
               style="background:rgba(255,255,255,0.04);cursor:pointer;user-select:none"
               data-bs-toggle="collapse" data-bs-target="#${collapseId}" aria-expanded="${isFirst}">
            <div class="d-flex align-items-center gap-3">
              <i class="bi bi-chevron-${isFirst ? 'down' : 'right'} text-muted collapse-chevron" style="font-size:0.8rem"></i>
              <span class="fw-semibold text-white" style="font-size:0.85rem">${date}</span>
              <span class="badge bg-secondary">${trades.length} trade${trades.length !== 1 ? 's' : ''}</span>
              ${wins > 0 ? `<span class="badge bg-success bg-opacity-25 text-success">✅ ${wins}W</span>` : ''}
              ${losses > 0 ? `<span class="badge bg-danger bg-opacity-25 text-danger">❌ ${losses}L</span>` : ''}
            </div>
            <div class="d-flex gap-3 align-items-center">
              <span class="${dayPnlColor} fw-bold" style="font-size:0.82rem">${dayPnl >= 0 ? '+$' : '-$'}${Math.abs(dayPnl).toFixed(2)}</span>
              <span class="text-muted" style="font-size:0.75rem">${dayPct >= 0 ? '+' : ''}${dayPct.toFixed(2)}% avg</span>
            </div>
          </div>
          <div class="collapse ${isFirst ? 'show' : ''}" id="${collapseId}">
            <div class="table-responsive mt-1">
              <table class="table table-dark table-hover table-sm align-middle mb-0">
                <thead style="font-size:0.72rem">
                  <tr class="table-secondary">
                    <th>Symbol</th><th>Dir</th><th>Outcome</th>
                    <th>P&L %</th><th>P&L $</th><th>Hold</th>
                    <th>Exit</th><th>Regime</th><th>Time</th>
                  </tr>
                </thead>
                <tbody>${trades.map(tradeRow).join('')}</tbody>
              </table>
            </div>
          </div>
        </div>`;
    });
    container.innerHTML = html;

    // Toggle chevron icon on collapse events
    container.querySelectorAll('.outcome-day-header').forEach(header => {
      header.addEventListener('click', () => {
        const chevron = header.querySelector('.collapse-chevron');
        if (!chevron) return;
        const targetId = header.getAttribute('data-bs-target');
        const collapseEl = document.querySelector(targetId);
        if (!collapseEl) return;
        const isOpen = collapseEl.classList.contains('show');
        chevron.className = `bi bi-chevron-${isOpen ? 'right' : 'down'} text-muted collapse-chevron`;
        chevron.style.fontSize = '0.8rem';
      });
    });
    return;
  }

  // Fallback: old flat table
  tbody.innerHTML = rows.map(tradeRow).join('');
}

function expandAllOutcomeDays() {
  document.querySelectorAll('#outcomes-daily-groups .collapse').forEach(el => {
    el.classList.add('show');
  });
  document.querySelectorAll('#outcomes-daily-groups .collapse-chevron').forEach(el => {
    el.className = 'bi bi-chevron-down text-muted collapse-chevron';
    el.style.fontSize = '0.8rem';
  });
}

function collapseAllOutcomeDays() {
  document.querySelectorAll('#outcomes-daily-groups .collapse').forEach(el => {
    el.classList.remove('show');
  });
  document.querySelectorAll('#outcomes-daily-groups .collapse-chevron').forEach(el => {
    el.className = 'bi bi-chevron-right text-muted collapse-chevron';
    el.style.fontSize = '0.8rem';
  });
}

// ── Learning Engine — Tier 3 / 4 / 5 extensions ─────────────────────────────

// loadLearning v2 — merged above with mode toggle support

function renderPatterns(rows) {
  const tbody = document.getElementById('patterns-tbody');
  if (!tbody) return;
  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-3" style="font-size:0.8rem">Patterns build after trades close (need ≥3 matches)</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const wr = ((r.win_rate || 0) * 100).toFixed(0);
    const wrColor = r.win_rate >= 0.6 ? 'text-success' : r.win_rate >= 0.4 ? 'text-warning' : 'text-danger';
    const pnlColor = (r.avg_pnl_pct || 0) >= 0 ? 'text-success' : 'text-danger';
    const desc = (r.pattern_desc || '').split(' | ').join('\n');
    return `<tr title="${desc.replace(/"/g,'&quot;')}">
      <td style="font-size:0.72rem;max-width:200px;word-break:break-word" class="text-muted">${r.pattern_desc || '—'}</td>
      <td class="text-white">${r.total}</td>
      <td class="${wrColor} fw-bold">${wr}%</td>
      <td class="${pnlColor}">${(r.avg_pnl_pct||0) >= 0 ? '+' : ''}${(r.avg_pnl_pct||0).toFixed(2)}%</td>
    </tr>`;
  }).join('');
}

function renderRegimes(rows) {
  const tbody = document.getElementById('regime-tbody');
  if (!tbody) return;
  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-3" style="font-size:0.8rem">Regime data builds after trades close</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const wr = ((r.win_rate || 0) * 100).toFixed(0);
    const wrColor = r.win_rate >= 0.6 ? 'text-success' : r.win_rate >= 0.4 ? 'text-warning' : 'text-danger';
    const pnlColor = (r.avg_pnl_pct || 0) >= 0 ? 'text-success' : 'text-danger';
    const regimeBadgeClass = {
      'Risk-On Bull': 'bg-success',
      'Range-Bound':  'bg-warning text-dark',
      'Bear / Risk-Off': 'bg-danger',
      'Overbought Bull': 'bg-warning text-dark',
      'Neutral': 'bg-secondary',
    }[r.regime] || 'bg-secondary';
    return `<tr>
      <td><span class="badge ${regimeBadgeClass}" style="font-size:0.72rem">${r.regime}</span></td>
      <td class="text-white">${r.total}</td>
      <td class="${wrColor} fw-bold">${wr}%</td>
      <td class="${pnlColor}">${(r.avg_pnl_pct||0) >= 0 ? '+' : ''}${(r.avg_pnl_pct||0).toFixed(2)}%</td>
      <td class="text-muted">${(r.avg_confidence||0).toFixed(0)}</td>
    </tr>`;
  }).join('');
}

function renderLessons(rows) {
  const el = document.getElementById('lessons-list');
  if (!el) return;
  if (!rows || rows.length === 0) {
    el.innerHTML = '<div class="text-muted text-center py-3" style="font-size:0.8rem">Lessons appear after losing trades — the AI reviews its reasoning and stores what it missed.</div>';
    return;
  }
  const categoryColors = {
    'TA_MISS':     'text-warning',
    'REGIME_MISS': 'text-info',
    'NEWS_MISS':   'text-primary',
    'TIMING':      'text-secondary',
    'CORRECT_CALL':'text-success',
    'OTHER':       'text-muted',
  };
  el.innerHTML = rows.map(r => {
    const icon   = r.outcome === 'LOSS' ? '❌' : '✅';
    const catColor = categoryColors[r.lesson_category] || 'text-muted';
    const ts     = r.created_at ? new Date(r.created_at).toLocaleDateString() : '';
    return `<div class="border border-secondary rounded p-2 mb-2" style="font-size:0.78rem">
      <div class="d-flex justify-content-between mb-1">
        <span>${icon} <span class="fw-semibold text-white">${r.symbol}</span>
          <span class="${catColor} ms-1">[${r.lesson_category || 'OTHER'}]</span></span>
        <span class="text-muted">${ts}</span>
      </div>
      <div class="text-light">${r.lesson}</div>
      <div class="text-muted mt-1" style="font-size:0.7rem">Applied ${r.applied_count||0}× to future prompts</div>
    </div>`;
  }).join('');
}

// ── Futures Market Panel ─────────────────────────────────────────────────────

let _allFuturesPrices = [];

async function loadFuturesPrices() {
  const grid = document.getElementById('futures-price-grid');
  if (!grid) return;
  grid.innerHTML = '<div class="col-12 text-center text-muted py-3"><span class="spinner-border spinner-border-sm me-2"></span>Fetching futures prices via yfinance…</div>';
  try {
    const data = await API('/futures/prices?paper_only=false');
    _allFuturesPrices = Object.values(data || {});
    renderFuturesGrid(_allFuturesPrices);
  } catch(e) {
    grid.innerHTML = '<div class="col-12 text-center text-danger py-2">Failed to load futures prices</div>';
    console.error('loadFuturesPrices failed', e);
  }
}

function filterFuturesCat(cat, btn) {
  // Toggle button active state
  document.querySelectorAll('#futures-cat-tabs button').forEach(b => {
    b.className = b.className.replace('btn-secondary','btn-outline-secondary');
  });
  if (btn) {
    btn.className = btn.className.replace('btn-outline-secondary','btn-secondary');
  }
  const rows = cat === 'all' ? _allFuturesPrices : _allFuturesPrices.filter(r => r.category === cat);
  renderFuturesGrid(rows);
}

function renderFuturesGrid(rows) {
  const grid = document.getElementById('futures-price-grid');
  if (!rows || rows.length === 0) {
    grid.innerHTML = '<div class="col-12 text-center text-muted py-3" style="font-size:.8rem">No data — click Refresh first</div>';
    return;
  }

  const catIcons = { Energy:'🛢️', Metals:'🥇', Grains:'🌾', Forex:'💱', Index:'📊', Volatility:'⚡', Softs:'☕', Bonds:'📈' };

  grid.innerHTML = rows.map(r => {
    const chg = r.change_pct || 0;
    const chgColor = chg > 0 ? 'text-success' : chg < 0 ? 'text-danger' : 'text-muted';
    const chgIcon  = chg > 0 ? '▲' : chg < 0 ? '▼' : '—';
    const icon     = catIcons[r.category] || '📊';
    const priceStr = r.price > 1000 ? r.price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})
                   : r.price > 1    ? r.price.toFixed(4)
                   : r.price.toFixed(6);
    const paperBadge = r.paper_eligible
      ? '<span class="badge bg-warning text-dark ms-1" style="font-size:.6rem">PAPER</span>' : '';
    const tradeBtn = r.paper_eligible
      ? `<button class="btn btn-xs btn-outline-warning py-0 px-1 mt-1" style="font-size:.65rem"
           onclick="document.getElementById('paperSym').value='${r.symbol}';document.querySelector('[href=\\'#tab-paper\\']').click()">
           Trade</button>` : '';
    return `<div class="col-6 col-md-4 col-xl-3">
      <div class="card bg-dark border-secondary p-2 h-100">
        <div class="d-flex justify-content-between align-items-start mb-1">
          <span class="text-muted" style="font-size:.68rem">${icon} ${r.category}</span>
          ${paperBadge}
        </div>
        <div class="fw-bold text-white" style="font-size:.8rem">${r.symbol}</div>
        <div class="text-muted" style="font-size:.68rem;line-height:1.1">${r.name || ''}</div>
        <div class="fs-6 fw-bold text-white mt-1">$${priceStr}</div>
        <div class="${chgColor}" style="font-size:.75rem">${chgIcon} ${chg >= 0 ? '+' : ''}${chg.toFixed(3)}%</div>
        ${tradeBtn}
      </div>
    </div>`;
  }).join('');
}

async function loadFuturesNews() {
  const el = document.getElementById('futures-news-list');
  if (!el) return;
  el.innerHTML = '<div class="text-muted small text-center py-2"><span class="spinner-border spinner-border-sm me-2"></span>Loading futures & commodity news…</div>';
  try {
    const articles = await API('/futures/news?limit=25');
    if (!articles || articles.length === 0) {
      el.innerHTML = '<div class="text-muted small text-center py-2">No futures news available</div>';
      return;
    }
    const catIcons = { Energy:'🛢️', Metals:'🥇', Grains:'🌾', Forex:'💱', Index:'📊', General:'📰' };
    el.innerHTML = articles.map(a => {
      const icon = catIcons[a.category] || '📰';
      const url  = a.url ? `href="${a.url}" target="_blank"` : '';
      return `<div class="border-bottom border-secondary pb-1 mb-1" style="font-size:.78rem">
        <span class="text-muted me-1">${icon} <span class="badge bg-secondary" style="font-size:.6rem">${a.category||'News'}</span></span>
        <a ${url} class="text-light text-decoration-none">${a.title}</a>
        <span class="text-muted ms-1" style="font-size:.65rem">(${a.source||''})</span>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = '<div class="text-danger small text-center py-2">Failed to load futures news</div>';
    console.error('loadFuturesNews failed', e);
  }
}


