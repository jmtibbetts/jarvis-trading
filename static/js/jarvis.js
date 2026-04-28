'use strict';

const API  = (p) => fetch(`/api${p}`).then(r=>r.json());
const POST = (p,b) => fetch(`/api${p}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
const DEL  = (p)   => fetch(`/api${p}`,{method:'DELETE'}).then(r=>r.json());
// Flexible helper used by queue actions — supports GET/POST/DELETE
const api  = (p, opts={}) => {
  if (!opts.method || opts.method === 'GET') return API(p);
  if (opts.method === 'DELETE') return DEL(p);
  return POST(p, opts.body ? JSON.parse(opts.body) : {});
};

let allSignals=[], allThreats=[], allNews=[], equityChart=null;

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

/* ── SIGNALS ──────────────────────────────────────────────────────────────── */
async function loadSignals() {
  const data = await API('/signals?limit=150');
  allSignals = data;
  renderSignals();
  loadQueue();
}

function renderSignals() {
  const status = document.getElementById('sig-filter-status').value;
  const cls    = document.getElementById('sig-filter-class').value;
  const sort   = document.getElementById('sig-sort').value;
  let filtered = allSignals.filter(s=>(!status||s.status===status)&&(!cls||s.asset_class===cls));
  if(sort==='score')           filtered.sort((a,b)=>(b.composite_score||b.confidence||0)-(a.composite_score||a.confidence||0));
  else if(sort==='confidence') filtered.sort((a,b)=>(b.confidence||0)-(a.confidence||0));
  else                         filtered.sort((a,b)=>new Date(b.generated_at)-new Date(a.generated_at));
  document.getElementById('signal-count').textContent=filtered.length+' signals';
  const grid=document.getElementById('signals-grid');
  if(!filtered.length){grid.innerHTML='<div class="col-12 text-center text-muted py-5">No signals</div>';return;}
  grid.innerHTML=filtered.map(function(s){
    const score  = s.composite_score||s.confidence||0;
    const dir    = (s.direction||'Long').toLowerCase();
    const conf   = s.confidence||0;
    const confCls= conf>=75?'high':conf>=55?'medium':'low';
    const rr     = s.entry_price&&s.target_price&&s.stop_loss&&s.entry_price>s.stop_loss
                   ? ((s.target_price-s.entry_price)/(s.entry_price-s.stop_loss)).toFixed(1) : 'N/A';
    const rrCls  = rr!=='N/A'&&parseFloat(rr)>=2?'text-success':rr!=='N/A'&&parseFloat(rr)>=1?'text-warning':'text-danger';
    const statusBadge={Active:'bg-success',Executed:'bg-primary',Expired:'bg-secondary',Rejected:'bg-danger',Closed:'bg-dark border border-secondary',PendingApproval:'bg-warning text-dark'}[s.status]||'bg-secondary';
    const scorePct=Math.round(score);
    const earningsBadge=s.earnings_risk?'<span class="badge bg-warning text-dark ms-1" title="Earnings risk">📅</span>':'';
    const srcBadge=s.signal_source==='opportunistic'?'<span class="badge bg-info text-dark ms-1" title="News-discovered">📰</span>':'';
    // default qty guess for modal prefill
    const defDollar = conf>=75?1500:conf>=55?1000:500;
    const defQty    = s.entry_price ? Math.max(1,Math.round(defDollar/s.entry_price)) : 1;
    return '<div class="col-xl-3 col-lg-4 col-md-6">' +
      '<div class="card signal-card '+dir+' h-100">' +
        '<div class="card-header d-flex justify-content-between align-items-center py-2">' +
          '<div>' +
            '<span class="fw-bold">'+s.asset_symbol+'</span>' +
            '<span class="badge '+(dir==='long'?'bg-success':'bg-primary')+' ms-1">'+s.direction+'</span>' +
            '<span class="badge '+statusBadge+' ms-1">'+s.status+'</span>' +
            earningsBadge+srcBadge +
          '</div>' +
          '<small class="text-muted">'+timeAgo(s.generated_at)+'</small>' +
        '</div>' +
        '<div class="card-body py-2 px-3">' +
          '<div class="d-flex justify-content-between align-items-center mb-1">' +
            '<small class="text-muted">Composite Score</small>' +
            '<span class="badge '+(scorePct>=70?'bg-success':scorePct>=50?'bg-warning text-dark':'bg-danger')+'">'+scorePct+'%</span>' +
          '</div>' +
          '<div class="conf-bar '+confCls+' mb-2" style="width:'+conf+'%"></div>' +
          '<div class="small mb-2 text-muted">'+( s.asset_name||'')+' · '+(s.asset_class||'')+' · '+(s.timeframe||'')+' · <span class="text-warning">LLM:'+conf+'%</span></div>' +
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
          '<p class="small text-muted mb-1" style="font-size:.72rem;line-height:1.4;max-height:60px;overflow:hidden">'+(s.reasoning||'').slice(0,180)+((s.reasoning||'').length>180?'…':'')+'</p>' +
          (s.key_risks?'<p class="small text-warning mb-0" style="font-size:.7rem"><i class="bi bi-exclamation-triangle-fill"></i> '+s.key_risks.slice(0,100)+'</p>':'') +
        '</div>' +
        '<div class="card-footer py-1 d-flex gap-1">' +
          (s.status==='Active'?
            '<button class="btn btn-success btn-sm flex-fill py-0" style="font-size:.72rem" '+
              'onclick="openTradeModal(\''+s.id+'\',\''+s.asset_symbol+'\','+s.entry_price+','+s.target_price+','+s.stop_loss+','+defDollar+','+defQty+')">'+
              '<i class="bi bi-play-fill"></i> Execute</button>'
          :'') +
          '<button class="btn btn-outline-danger btn-sm py-0" style="font-size:.72rem" onclick="deleteSignal(\''+s.id+'\')"><i class="bi bi-trash"></i></button>' +
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
document.getElementById('sig-sort').addEventListener('change',renderSignals);

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
  let filtered=allThreats.filter(t=>(!sev||t.severity===sev)&&(!reg||t.region===reg));
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
          <p class="fw-bold mb-1 small">${t.source_url?`<a href="${t.source_url}" target="_blank" class="text-info text-decoration-none">${t.title}</a>`:t.title}</p>
          <p class="text-muted small mb-2" style="font-size:.72rem">${(t.description||'').slice(0,180)}</p>
          <div class="d-flex gap-1 flex-wrap">
            <span class="badge bg-dark border border-secondary small">${t.event_type||''}</span>
            <span class="badge bg-dark border border-secondary small">${t.region||''}</span>
          </div>
        </div>
        <div class="card-footer py-1 small text-muted">${timeAgo(t.published_at)} · ${t.source||''}</div>
      </div>
    </div>`).join('');
}
document.getElementById('threat-filter-sev').addEventListener('change',renderThreats);
document.getElementById('threat-filter-region').addEventListener('change',renderThreats);

/* ── NEWS ─────────────────────────────────────────────────────────────────── */
async function loadNews() {
  allNews=await API('/news?limit=80');
  renderNews();
}
function renderNews() {
  const cat=document.getElementById('news-filter-cat').value;
  const sent=document.getElementById('news-filter-sent').value;
  let filtered=allNews.filter(n=>(!cat||n.category===cat)&&(!sent||n.sentiment===sent));
  const sentIcon={positive:'bi-arrow-up-circle-fill text-success',negative:'bi-arrow-down-circle-fill text-danger',neutral:'bi-dash-circle text-secondary'};
  document.getElementById('news-list').innerHTML=filtered.map(n=>`
    <div class="d-flex align-items-start gap-2 py-2 border-bottom border-secondary">
      <i class="bi ${sentIcon[n.sentiment]||'bi-dash-circle text-muted'} mt-1 flex-shrink-0"></i>
      <div class="flex-grow-1">
        <div class="small fw-bold">${n.url?`<a href="${n.url}" target="_blank" class="text-info text-decoration-none">${n.title}</a>`:n.title}</div>
        <div class="small text-muted mt-1">${(n.summary||'').slice(0,160)}</div>
        <div class="d-flex gap-2 mt-1 flex-wrap">
          <span class="badge bg-dark border border-secondary" style="font-size:.65rem">${n.source||''}</span>
          <span class="badge bg-dark border border-secondary" style="font-size:.65rem">${n.category||''}</span>
          ${(n.affected_assets||[]).slice(0,3).map(a=>`<span class="badge bg-dark border border-warning text-warning" style="font-size:.65rem">${a}</span>`).join('')}
          <span class="text-muted" style="font-size:.65rem">${timeAgo(n.published_at)}</span>
        </div>
      </div>
    </div>`).join('');
}
document.getElementById('news-filter-cat').addEventListener('change',renderNews);
document.getElementById('news-filter-sent').addEventListener('change',renderNews);

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
      const rsi  = td.rsi;
      const macd = td.macd||{};
      const bb   = td.bollinger||{};
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
                '<tr><td class="text-muted">EMA20</td><td>'+fmtPrice(p.ema20)+'</td></tr>' +
                '<tr><td class="text-muted">EMA50</td><td>'+fmtPrice(p.ema50)+'</td></tr>' +
                '<tr><td class="text-muted">RSI</td><td class="'+(rsi>70?'text-danger':rsi<30?'text-success':'')+'fw-bold">'+(rsi!=null?rsi.toFixed(1):'N/A')+'</td></tr>' +
                '<tr><td class="text-muted">ATR %</td><td>'+(atr.pct!=null?atr.pct.toFixed(2)+'%':'N/A')+'</td></tr>' +
              '</table>' +
            '</div>' +
            '<div class="col-sm-6">' +
              '<table class="table table-dark table-sm mb-0" style="font-size:.78rem">' +
                '<tr><td class="text-muted">MACD</td><td>'+(macd.value!=null?macd.value.toFixed(4):'N/A')+'</td></tr>' +
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

/* ── JOBS TAB ─────────────────────────────────────────────────────────────── */
async function loadJobs() {
  try {
    const [jobs, cache, llm] = await Promise.all([
      API('/jobs/status'),
      API('/cache/stats').catch(()=>({})),
      API('/llm/health').catch(()=>({}))
    ]);
    const jobNames={market:'Market Data',threats:'Threat News',signals:'Signal Gen',execute:'Execute',positions:'Positions',telegram:'Telegram'};
    const schedules={market:'every 15m',threats:'every 15m +7m',signals:'every 30m',execute:'every 30m +3m',positions:'every 5m',telegram:'every 1m'};
    const grid=document.getElementById('jobs-grid');
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
            <button class="btn btn-outline-primary btn-sm w-100" onclick="triggerJob('${name}')"><i class="bi bi-play-fill"></i> Run Now</button>
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
    const tfSummary    = Object.entries(byTf).map(([tf,v])=>`${tf}: ${v.bars?.toLocaleString()||0} bars`).join(' · ');
    const cacheRows    = cacheSymbols
      ? `<div class="small text-muted">${cacheSymbols} symbols · ${cacheBars.toLocaleString()} bars${cacheSize?' · '+cacheSize:''}</div>`+
        (tfSummary ? `<div class="small text-muted">${tfSummary}</div>` : '')
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
  } catch(e){ document.getElementById('jobs-grid').innerHTML=`<div class="col-12 text-danger">${e.message}</div>`; }
}

async function triggerJob(name) {
  const res=await POST(`/jobs/${name}/trigger`,{});
  if(res.ok) { setTimeout(loadJobs,1500); }
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

async function loadSettings() {
  const configs=await API('/settings');
  const el=document.getElementById('configs-list');
  if(!configs.length){el.innerHTML='<div class="text-muted small p-3">No configurations yet. Add your API credentials on the left.</div>';return;}
  const grouped={};
  configs.forEach(c=>{(grouped[c.platform]=grouped[c.platform]||[]).push(c);});
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
  document.getElementById('cfg-key').value=c.api_key||'';
  document.getElementById('cfg-secret').value=c.api_secret||'';
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

async function loadPerformance(days=30) {
  ['30','7','90'].forEach(d=>{
    const btn=document.getElementById(`perf-${d}d`);
    if(btn) btn.classList.toggle('active', d===String(days));
  });
  const data = await API(`/performance?days=${days}`);
  const updEl = document.getElementById('perf-updated');
  if(updEl) updEl.textContent='Last updated '+new Date().toLocaleTimeString();

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

/* ── GLOBAL INIT + REFRESH ───────────────────────────────────────────────── */
async function refreshAll() {
  const active=document.querySelector('.nav-link.active')?.getAttribute('href')?.replace('#tab-','');
  document.getElementById('last-refresh').textContent='Updated '+new Date().toLocaleTimeString();
  refreshJobIndicators();
  refreshRegimeBadge();
  if(active==='signals'||!active) loadSignals();
  else if(active==='positions'){loadPositions();loadEquityCurve(24);}
  else if(active==='market')   loadMarket();
  else if(active==='threats')  loadThreats();
  else if(active==='news')     loadNews();
  else if(active==='jobs')     loadJobs();
  else if(active==='settings') loadSettings();
  else if(active==='performance') loadPerformance(30);
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
    else if(tab==='scanner')  {} 
    else if(tab==='jobs')     loadJobs();
    else if(tab==='settings'){loadSettings();updatePlatformFields();}
    else if(tab==='performance') loadPerformance(30);
  });
});

// Initial load
loadSignals();
refreshJobIndicators();
refreshRegimeBadge();

// Auto-refresh every 60s
setInterval(refreshAll, 60000);

