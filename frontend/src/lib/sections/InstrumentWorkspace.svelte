<script lang="ts">
  /**
   * P26 — the per-instrument workspace. ONE instrument, everything the desk
   * knows about it, on one page.
   *
   * The facts were previously scattered across four surfaces that each
   * answered a different question — the scanner said a setup existed, the
   * sizer said what one contract was worth, the venue panel said where it
   * could be filled — and nothing said whether they agreed.
   *
   * THE REFUSAL IS THE HEADLINE. An instrument the desk cannot size is the
   * case where this page earns its keep: `6J=F` resolves UNSUPPORTED with
   * signals sitting behind it, and a blank panel reads as a bug while a
   * stated refusal with a count reads as a work item.
   *
   * The symbol follows the link store, so clicking a symbol anywhere in the
   * app (including in a popout on another monitor) lands here.
   */
  import ChartPane from "../components/ChartPane.svelte";
  import Pill from "../components/Pill.svelte";
  import StateNote from "../components/StateNote.svelte";
  import { api, type ChartPayload, type InstrumentWorkspace } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import { linkStore } from "../stores/link.svelte";

  const feeds = new FeedTracker();
  const KEY = "jarvis.instrument.symbol";

  let symbol = $state(localStorage.getItem(KEY) ?? "MES=F");
  let typed = $state("");
  let ws = $state<InstrumentWorkspace | null>(null);
  let available = $state<{ symbol: string; timeframes: string[] }[]>([]);
  // The chart already fetches bars; its last close prices the round trip so
  // this page never spends a quote of its own.
  let lastClose = $state<number | null>(null);

  type Tab = "spec" | "venues" | "cost" | "signals" | "exposure";
  let tab = $state<Tab>("spec");

  // Following the link store is one-way on purpose: this page is a
  // DESTINATION for a click made elsewhere. Publishing back would make the
  // page's own symbol box yank every other linked view around.
  $effect(() => {
    const linked = linkStore.symbol;
    if (linked && linked !== symbol) symbol = linked;
  });

  $effect(() => {
    feeds.load("symbols", () => api.chartSymbols()).then((r) => {
      if (r) available = r.symbols;
    });
  });

  $effect(() => {
    const sym = symbol;
    const entry = lastClose;
    localStorage.setItem(KEY, sym);
    // keepLast: false — a page still labelled 6J=F while showing MES=F's
    // multiplier is worse than a page showing nothing.
    feeds
      .load("workspace", () => api.instrument(sym, entry), { keepLast: false })
      .then((r) => (ws = r));
  });

  function pick(e: Event) {
    e.preventDefault();
    const v = typed.trim().toUpperCase();
    if (v) symbol = v;
    typed = "";
  }

  function onPayload(p: ChartPayload | null) {
    const bars = p?.bars ?? [];
    lastClose = bars.length ? bars[bars.length - 1].close : null;
  }

  const num = (v: number | null | undefined, dp = 2) =>
    v == null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: dp });
  const pct = (v: number | null | undefined, dp = 3) =>
    v == null ? "—" : `${v.toFixed(dp)}%`;

  // VERIFIED and SUPPORTED can both be traded; the difference is whether the
  // execution spec was confirmed or defaulted, and that stays visible.
  const statusTone = (s: string): "good" | "info" | "warm" | "bad" =>
    s === "VERIFIED" ? "good"
      : s === "SUPPORTED" ? "info"
      : s === "UNSUPPORTED" ? "bad"
      : "warm";

  const ident = $derived(ws?.identity ?? null);
  const ref = $derived(
    (ws?.cost?.reference ?? null) as Record<string, number | null> | null,
  );

  // The spec rows worth showing differ by product — a pip size on an equity
  // and a multiplier on spot FX are both noise. Only populated fields render.
  const specRows = $derived.by(() => {
    const i = ident;
    if (!i) return [] as { k: string; v: string; why?: string }[];
    const rows: { k: string; v: string; why?: string }[] = [
      { k: "Asset class", v: i.asset_class },
      { k: "Product", v: i.product },
      {
        k: "Quantity unit", v: i.quantity_unit,
        why: '"20" means nothing without knowing shares from contracts.',
      },
    ];
    if (i.multiplier !== 1)
      rows.push({
        k: "Multiplier", v: `${i.multiplier}×`,
        why: "Dollars per point per contract. Notional is qty × price × this.",
      });
    if (i.tick_size != null) rows.push({ k: "Tick size", v: String(i.tick_size) });
    if (i.tick_value != null) rows.push({ k: "Tick value", v: `$${num(i.tick_value, 4)}` });
    if (i.contract_size != null) rows.push({ k: "Contract size", v: num(i.contract_size, 0) });
    if (i.minimum_quantity != null) rows.push({ k: "Minimum size", v: num(i.minimum_quantity, 4) });
    if (i.quantity_step != null) rows.push({ k: "Size step", v: num(i.quantity_step, 4) });
    if (i.pip_size != null)
      rows.push({
        k: "Pip size", v: String(i.pip_size),
        why: "A JPY pair pips at the 2nd decimal; pricing it at the 4th overstates cost 100×.",
      });
    if (i.initial_margin != null) rows.push({ k: "Initial margin", v: `$${num(i.initial_margin, 0)}` });
    if (i.maintenance_margin != null)
      rows.push({ k: "Maintenance margin", v: `$${num(i.maintenance_margin, 0)}` });
    if (i.base_asset) rows.push({ k: "Base / quote", v: `${i.base_asset} / ${i.quote_asset ?? "—"}` });
    if (i.expiry) rows.push({ k: "Expiry", v: i.expiry });
    rows.push({ k: "Instrument id", v: i.instrument_id || "—" });
    rows.push({
      k: "Provenance", v: i.provenance,
      why: "Where this spec came from. `shape_only` means the symbol's form was all that was known.",
    });
    return rows;
  });

  const costRows = $derived.by(() => {
    const r = ref;
    if (!r) return [] as { k: string; r: number | null; pctv: number | null }[];
    return [
      { k: "Spread", r: r.spread_r, pctv: r.spread_pct },
      { k: "Fees", r: r.fees_r, pctv: r.fees_pct },
      { k: "Slippage", r: r.slippage_r, pctv: r.slippage_pct },
      { k: "Funding", r: r.funding_r, pctv: r.funding_pct },
      { k: "Borrow", r: r.borrow_r, pctv: r.borrow_pct },
    ].filter((row) => row.r != null || row.pctv);
  });
</script>

<div class="page">
  <header class="head">
    <form onsubmit={pick}>
      <input
        list="workspace-symbols"
        placeholder={symbol}
        bind:value={typed}
        aria-label="Instrument symbol"
      />
      <datalist id="workspace-symbols">
        {#each available as a (a.symbol)}
          <option value={a.symbol}>{a.timeframes.join(" ")}</option>
        {/each}
      </datalist>
    </form>

    <h1>{ws?.canonical_symbol ?? symbol}</h1>

    {#if ident}
      <Pill tone={statusTone(ident.status)} label={ident.status} />
      <Pill tone="neutral" label={ident.product} />
      <Pill tone="neutral" label={ident.quantity_unit.toLowerCase()} />
      {#if ident.multiplier !== 1}
        <Pill tone="info" label="{ident.multiplier}× multiplier" />
      {/if}
      {#if ws && ws.spellings.length > 1}
        <span class="spellings" title="Every spelling a venue might use for this instrument">
          {ws.spellings.join(" · ")}
        </span>
      {/if}
    {/if}

    {#if lastClose != null}
      <span class="last">last {num(lastClose, 4)}</span>
    {/if}
  </header>

  <!--
    The refusal banner. Deliberately above the chart: research on an
    unsizeable instrument is legitimate, and the operator should know which
    kind of page they are reading before they read a price off it.
  -->
  {#if ws?.refusal}
    <div class="refusal">
      <div class="rtop">
        <Pill tone="bad" label={ws.refusal.status} />
        <b>{ws.refusal.reason}</b>
        {#if ws.refusal.signals_recorded}
          <span class="blocked">
            {ws.refusal.signals_recorded} signals recorded against it,
            {ws.refusal.signals_active ?? 0} still active
          </span>
        {/if}
      </div>
      <p>{ws.refusal.detail}</p>
    </div>
  {/if}

  <div class="body">
    <div class="chartcol">
      <ChartPane
        paneId="workspace"
        symbolOverride={symbol}
        {available}
        height="360px"
        onPayload={onPayload}
        onSymbol={(s) => (symbol = s)}
      />
    </div>

    <div class="tabcol">
      <div class="tabs" role="tablist">
        {#each [
          ["spec", "Contract"],
          ["cost", "Cost"],
          ["venues", "Venues"],
          ["signals", "Signals"],
          ["exposure", "Exposure"],
        ] as [id, label] (id)}
          <button
            role="tab"
            aria-selected={tab === id}
            class:on={tab === id}
            onclick={() => (tab = id as Tab)}
          >
            {label}
          </button>
        {/each}
      </div>

      <div class="tabbody">
        {#if !ws}
          <StateNote status={feeds.status("workspace")} noun="instrument" />
        {:else if tab === "spec"}
          <table class="kv">
            <tbody>
              {#each specRows as row (row.k)}
                <tr>
                  <th>{row.k}</th>
                  <td class="mono">{row.v}</td>
                </tr>
                {#if row.why}
                  <tr class="whyrow"><td colspan="2">{row.why}</td></tr>
                {/if}
              {/each}
            </tbody>
          </table>
          {#if ident?.reason}
            <p class="why">{ident.reason}</p>
          {/if}

        {:else if tab === "cost"}
          {#if !ws.cost.available}
            <p class="why">{ws.cost.reason}</p>
          {:else}
            <div class="floor">
              <span>Cost floor</span>
              <b>{pct(ws.cost.min_viable_stop_pct)}</b>
              <em>tightest stop that can still pay for itself, at {ws.cost.max_cost_r}R</em>
            </div>
            <p class="why">{ws.cost.note}</p>
            {#if ref}
              <div class="sub">
                Round trip priced at that floor — entry {num(ws.cost.reference_entry, 4)},
                stop {num(ws.cost.reference_stop, 4)}
              </div>
              <table class="kv">
                <tbody>
                  {#each costRows as c (c.k)}
                    <tr>
                      <th>{c.k}</th>
                      <td class="mono">
                        {c.r == null ? "—" : `${c.r.toFixed(3)}R`}
                        <span class="dim">{c.pctv ? `(${(c.pctv * 100).toFixed(3)}%)` : ""}</span>
                      </td>
                    </tr>
                  {/each}
                  <tr class="total">
                    <th>Round trip</th>
                    <td class="mono">
                      {ref.total_r == null ? "—" : `${ref.total_r.toFixed(3)}R`}
                      <span class="dim">
                        {ref.total_pct ? `(${(Number(ref.total_pct) * 100).toFixed(3)}%)` : ""}
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
            {:else}
              <p class="why">{ws.cost.reference_reason}</p>
            {/if}
          {/if}

        {:else if tab === "venues"}
          {#if ws.venues.length}
            <table class="rows">
              <thead>
                <tr><th>Venue</th><th>Status</th><th>API</th><th>Depth</th></tr>
              </thead>
              <tbody>
                {#each ws.venues as v (v.venue + v.product)}
                  <tr>
                    <td class="mono">{v.venue}</td>
                    <td>
                      <Pill tone={v.executable ? "good" : v.status === "UNSUPPORTED" ? "bad" : "warm"}
                            label={v.status} />
                    </td>
                    <td class="mono dim">{v.api_surface ?? "—"}</td>
                    <td class="mono dim">{v.data_entitlement}</td>
                  </tr>
                  {#if v.reason}
                    <tr class="whyrow"><td colspan="4">{v.reason}</td></tr>
                  {/if}
                {/each}
              </tbody>
            </table>
            <p class="why">
              UI availability is not API availability. Only DOCUMENTED and
              DISCOVERED may back a simulated fill that claims venue realism.
            </p>
          {:else}
            <p class="why">
              No venue has been characterised for {ws.identity.product}. That is
              not the same as no venue existing — it means nobody has verified
              one, and an unverified venue cannot carry execution.
            </p>
          {/if}

        {:else if tab === "signals"}
          {#if ws.activity.unavailable}
            <p class="why">Activity unavailable — {ws.activity.reason}</p>
          {:else}
            <div class="counts">
              <div><span>Active</span><b>{ws.activity.signals_active ?? 0}</b></div>
              <div><span>Signals</span><b>{ws.activity.signals_total ?? 0}</b></div>
              <div><span>Considered</span><b>{ws.activity.candidates_considered ?? 0}</b></div>
              <div><span>Closed</span><b>{ws.activity.outcomes_closed ?? 0}</b></div>
              <div>
                <span>Win rate</span>
                <b>{ws.activity.win_rate_pct == null ? "—" : `${ws.activity.win_rate_pct}%`}</b>
              </div>
            </div>
            {#if ws.activity.win_rate_reason}
              <p class="why">{ws.activity.win_rate_reason}</p>
            {/if}
            {#if ws.activity.recent_signals?.length}
              <table class="rows">
                <thead>
                  <tr><th>When</th><th>Dir</th><th>TF</th><th>Status</th><th class="n">Entry</th><th class="n">Stop</th></tr>
                </thead>
                <tbody>
                  {#each ws.activity.recent_signals as s (s.id)}
                    <tr>
                      <td class="mono dim">{(s.generated_at ?? "").slice(0, 16).replace("T", " ")}</td>
                      <td class="mono">{s.direction ?? "—"}</td>
                      <td class="mono dim">{s.timeframe ?? "—"}</td>
                      <td class="mono dim">{s.status ?? "—"}</td>
                      <td class="n mono">{num(s.entry_price, 4)}</td>
                      <td class="n mono">{num(s.stop_loss, 4)}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            {:else}
              <p class="why">No signals recorded on this instrument.</p>
            {/if}
          {/if}

        {:else if tab === "exposure"}
          {#if ws.exposure.length}
            <table class="rows">
              <thead>
                <tr><th>Book</th><th>Dir</th><th class="n">Qty</th><th class="n">Entry</th><th class="n">Stop</th></tr>
              </thead>
              <tbody>
                {#each ws.exposure as p (p.book + p.symbol + (p.opened_at ?? ""))}
                  <tr>
                    <td class="mono">{p.book}</td>
                    <td class="mono">{p.direction ?? "—"}</td>
                    <td class="n mono">{num(p.qty, 4)}</td>
                    <td class="n mono">{num(p.entry_price, 4)}</td>
                    <td class="n mono">
                      {num(p.stop_loss, 4)}
                      {#if p.initial_stop_loss != null && p.initial_stop_loss !== p.stop_loss}
                        <span class="dim" title="the stop as PLACED — R is measured against this">
                          ← {num(p.initial_stop_loss, 4)}
                        </span>
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {:else}
            <p class="why">Flat — no open position in this instrument on any book.</p>
          {/if}
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 12px; }
  .head {
    display: flex; align-items: center; gap: 9px; flex-wrap: wrap;
  }
  .head h1 { margin: 0; font-size: 19px; letter-spacing: -.01em; font-family: var(--mono); }
  .head input {
    background: var(--surface-raised); border: 1px solid var(--line);
    color: var(--ink); border-radius: 6px; padding: 4px 9px;
    font-size: 12px; width: 130px; font-family: var(--mono);
  }
  .head input:focus { outline: none; border-color: var(--accent-dim); }
  .spellings, .last { font-size: 10.5px; color: var(--ink-faint); font-family: var(--mono); }
  .last { margin-left: auto; }

  .refusal {
    border: 1px solid color-mix(in srgb, var(--bad) 40%, transparent);
    background: color-mix(in srgb, var(--bad) 8%, transparent);
    border-radius: 8px; padding: 10px 12px;
    display: flex; flex-direction: column; gap: 5px;
  }
  .rtop { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; font-size: 12.5px; }
  .blocked { font-size: 11px; color: var(--bad); font-family: var(--mono); }
  .refusal p { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.5; max-width: 80ch; }

  .body { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(320px, 1fr); gap: 12px; }
  @media (max-width: 1100px) { .body { grid-template-columns: 1fr; } }
  .chartcol { min-width: 0; }
  .tabcol {
    border: 1px solid var(--line); border-radius: 10px;
    display: flex; flex-direction: column; min-width: 0; background: var(--surface);
  }
  .tabs { display: flex; gap: 2px; padding: 7px 8px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .tabs button {
    background: none; border: 1px solid transparent; color: var(--ink-faint);
    border-radius: 6px; padding: 3px 10px; font-size: 11.5px; cursor: pointer;
  }
  .tabs button.on {
    color: var(--accent); border-color: var(--line-bright); background: var(--surface-raised);
  }
  .tabbody { padding: 10px 12px; overflow-x: auto; }

  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .kv th {
    text-align: left; font-weight: 500; color: var(--ink-faint);
    padding: 4px 8px 4px 0; white-space: nowrap;
  }
  .kv td { padding: 4px 0; text-align: right; }
  .kv tr.total th, .kv tr.total td {
    border-top: 1px solid var(--line); padding-top: 6px; font-weight: 650;
  }
  .rows th {
    text-align: left; padding: 5px 8px 5px 0; color: var(--ink-faint);
    font-size: 10px; text-transform: uppercase; letter-spacing: .07em;
    border-bottom: 1px solid var(--line); font-weight: 600;
  }
  .rows td { padding: 5px 8px 5px 0; border-bottom: 1px solid var(--line); }
  .rows .n { text-align: right; }
  .whyrow td {
    font-size: 10.5px; color: var(--ink-faint); padding: 0 0 6px;
    border-bottom: 1px solid var(--line); line-height: 1.45;
  }
  .mono { font-family: var(--mono); font-size: 11.5px; }
  .dim { color: var(--ink-faint); }
  .why { font-size: 11px; color: var(--ink-faint); line-height: 1.5; margin: 8px 0 0; max-width: 72ch; }
  .sub { font-size: 10.5px; color: var(--ink-faint); font-family: var(--mono); margin: 10px 0 4px; }
  .floor { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .floor span { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-faint); }
  .floor b { font-size: 18px; font-variant-numeric: tabular-nums; }
  .floor em { font-style: normal; font-size: 10.5px; color: var(--ink-faint); }
  .counts { display: grid; grid-template-columns: repeat(auto-fit, minmax(74px, 1fr)); gap: 8px; }
  .counts div { display: flex; flex-direction: column; gap: 1px; }
  .counts span { font-size: 9.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-faint); }
  .counts b { font-size: 15px; font-variant-numeric: tabular-nums; }
</style>
