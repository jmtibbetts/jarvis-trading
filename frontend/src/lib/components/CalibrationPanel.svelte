<script lang="ts">
  // What the desk has MEASURED about itself, rather than what it claims.
  //
  // All of this already existed server-side — lib/calibration.py has been
  // computing it and lib/llm_router.py recording it — and none of it was
  // readable without opening a SQL prompt. A measurement nobody can see
  // does not change any decision.
  //
  // Every rate is shown with its sample size, and rates below the trust
  // threshold are marked provisional rather than quietly rendered next to
  // well-evidenced ones at the same visual weight. A win rate without a
  // denominator is a rumour.
  import Panel from "./Panel.svelte";
  import { api, type Calibration, type LlmRouting } from "../api";

  let cal = $state<Calibration | null>(null);
  let routing = $state<LlmRouting | null>(null);
  let loading = $state(true);
  let failed = $state<string | null>(null);

  async function load() {
    loading = true;
    failed = null;
    try {
      const [c, r] = await Promise.all([
        api.calibration().catch((e) => { throw e; }),
        api.llmRouting(30).catch(() => null),
      ]);
      cal = c;
      routing = r;
    } catch (e) {
      failed = String(e);
    } finally {
      loading = false;
    }
  }
  $effect(() => { load(); });

  const pct = (n: number | null | undefined) =>
    n === null || n === undefined ? "—" : `${n.toFixed(1)}%`;
  const n0 = (n: number | null | undefined) =>
    n === null || n === undefined ? "—" : Math.round(n).toLocaleString();

  // A rate is only as good as its denominator. Below min_sample the number
  // is displayed but explicitly marked, so a 100% win rate over 3 trades
  // cannot be mistaken for an edge.
  const trust = (sample: number) => {
    if (!cal) return "provisional";
    if (sample >= cal.full_trust_sample) return "measured";
    if (sample >= cal.min_sample) return "indicative";
    return "provisional";
  };

  const rateColor = (r: number) =>
    r >= 55 ? "var(--good)" : r >= 45 ? "var(--warm)" : "var(--bad)";

  // The headline. Score bands should rise left to right; if the low band
  // beats the high band, the score is not measuring what it claims to.
  const scoreOrder = ["<60", "60-69", "70-79", "80+"];
  const orderedScores = $derived(
    (cal?.by_score ?? [])
      .filter((b) => scoreOrder.includes(b.band))
      .sort((a, b) => scoreOrder.indexOf(a.band) - scoreOrder.indexOf(b.band)),
  );
  const inversion = $derived.by(() => {
    const s = orderedScores.filter((b) => b.sample >= (cal?.min_sample ?? 30));
    if (s.length < 2) return null;
    const lo = s[0], hi = s[s.length - 1];
    return { lo, hi, delta: +(hi.win_rate - lo.win_rate).toFixed(1) };
  });

  const maxRate = $derived(
    Math.max(40, ...(cal?.by_score ?? []).map((b) => b.win_rate)),
  );
</script>

<Panel title="Calibration" meta={cal ? `${n0(cal.sample)} outcomes` : ""}>
  {#if loading}
    <p class="muted">Reading measured outcomes…</p>
  {:else if failed}
    <p class="err">Could not load calibration: {failed}</p>
  {:else if cal}
    <div class="headline">
      <div class="big">
        <span class="val" style:color={rateColor(cal.overall_win_rate)}>
          {pct(cal.overall_win_rate)}
        </span>
        <span class="lbl">measured win rate · {n0(cal.sample)} outcomes</span>
      </div>
      {#if inversion}
        <div class="verdict" class:bad={inversion.delta < 0}>
          {#if inversion.delta < 0}
            <strong>The score is inverted.</strong>
            <span>
              Signals scoring {inversion.hi.band} win {pct(inversion.hi.win_rate)}
              ({n0(inversion.hi.sample)} trades) while {inversion.lo.band} win
              {pct(inversion.lo.win_rate)} ({n0(inversion.lo.sample)}). A higher
              score is currently evidence <em>against</em> the trade, not for it.
            </span>
          {:else}
            <strong>The score ranks correctly.</strong>
            <span>
              {inversion.hi.band} wins {pct(inversion.hi.win_rate)} against
              {inversion.lo.band} at {pct(inversion.lo.win_rate)} —
              {inversion.delta.toFixed(1)} points of real separation.
            </span>
          {/if}
        </div>
      {/if}
    </div>

    <h4>By composite score</h4>
    <div class="bars">
      {#each orderedScores as b (b.band)}
        <div class="bar-row">
          <span class="k">{b.band}</span>
          <div class="track">
            <div
              class="fill"
              style:width={`${(b.win_rate / maxRate) * 100}%`}
              style:background={rateColor(b.win_rate)}
            ></div>
          </div>
          <span class="v" style:color={rateColor(b.win_rate)}>{pct(b.win_rate)}</span>
          <span class="n {trust(b.sample)}">{n0(b.sample)}</span>
        </div>
      {/each}
    </div>

    <h4>By timeframe</h4>
    <table>
      <thead>
        <tr><th>Timeframe</th><th class="r">Win rate</th><th class="r">Sample</th><th>Evidence</th></tr>
      </thead>
      <tbody>
        {#each cal.by_timeframe as t (t.timeframe)}
          <tr>
            <td class="mono">{t.timeframe}</td>
            <td class="r" style:color={rateColor(t.win_rate)}>{pct(t.win_rate)}</td>
            <td class="r mono">{n0(t.sample)}</td>
            <td><span class="tag {trust(t.sample)}">{trust(t.sample)}</span></td>
          </tr>
        {/each}
      </tbody>
    </table>

    <h4>By strategy</h4>
    {#if cal.by_strategy.length}
      <table>
        <thead>
          <tr><th>Strategy</th><th class="r">Win rate</th><th class="r">Sample</th><th>Evidence</th></tr>
        </thead>
        <tbody>
          {#each cal.by_strategy as s (s.strategy)}
            <tr>
              <td class="mono">{s.strategy}</td>
              <td class="r" style:color={rateColor(s.win_rate)}>{pct(s.win_rate)}</td>
              <td class="r mono">{n0(s.sample)}</td>
              <td><span class="tag {trust(s.sample)}">{trust(s.sample)}</span></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else}
      <p class="muted">
        No closed trades carry a strategy label yet. The scanner produces most
        signals and was classifying every setup, then discarding the label
        before it was saved — so nothing reached this table. Fixed; rows
        appear as newly-tagged signals close.
      </p>
    {/if}
  {/if}
</Panel>

<Panel
  title="Model routing"
  meta={routing?.coverage ? `${n0(routing.coverage.routed_calls)} calls / ${routing.coverage.days}d` : ""}
>
  {#if !routing}
    <p class="muted">Routing telemetry unavailable.</p>
  {:else if routing.error}
    <p class="err">{routing.error}</p>
  {:else}
    <div class="cov">
      <div><span class="cv">{pct(routing.coverage.thinking_pct)}</span><span class="cl">reasoning</span></div>
      <div><span class="cv">{n0(routing.coverage.thinking)}</span><span class="cl">deep calls</span></div>
      <div><span class="cv">{n0(routing.coverage.non_thinking)}</span><span class="cl">fast calls</span></div>
      <div><span class="cv">{n0(routing.coverage.known_tasks)}</span><span class="cl">task types</span></div>
    </div>

    <h4>Cost per task</h4>
    <table>
      <thead>
        <tr>
          <th>Task</th><th>Mode</th><th class="r">Calls</th>
          <th class="r">Avg latency</th><th class="r">Avg tokens</th><th class="r">Failed</th>
        </tr>
      </thead>
      <tbody>
        {#each routing.usage as u (u.task + u.thinking)}
          <tr>
            <td class="mono">{u.task}</td>
            <td>
              <span class="tag" class:deep={u.thinking} class:fast={!u.thinking}>
                {u.thinking ? "DEEP" : "FAST"}
              </span>
            </td>
            <td class="r mono">{n0(u.calls)}</td>
            <td class="r mono">{(u.avg_latency_ms / 1000).toFixed(1)}s</td>
            <td class="r mono">{n0(u.avg_completion_tokens)}</td>
            <td class="r mono" style:color={u.failures ? "var(--bad)" : "inherit"}>
              {n0(u.failures)}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    <h4>Does reasoning pay for itself?</h4>
    {#if routing.effectiveness.length}
      <table>
        <thead>
          <tr>
            <th>Task</th><th class="r">Deep win</th><th class="r">Fast win</th>
            <th class="r">Δ win</th><th class="r">Δ P&amp;L</th><th class="r">Extra cost</th>
          </tr>
        </thead>
        <tbody>
          {#each routing.effectiveness as e (e.task)}
            <tr>
              <td class="mono">{e.task}</td>
              <td class="r mono">{pct(e.deep.win_rate)} <span class="sub">/{n0(e.deep.trades)}</span></td>
              <td class="r mono">{pct(e.fast.win_rate)} <span class="sub">/{n0(e.fast.trades)}</span></td>
              <td class="r mono" style:color={e.win_rate_delta > 0 ? "var(--good)" : "var(--bad)"}>
                {e.win_rate_delta > 0 ? "+" : ""}{e.win_rate_delta.toFixed(1)}pp
              </td>
              <td class="r mono" style:color={e.pnl_delta > 0 ? "var(--good)" : "var(--bad)"}>
                {e.pnl_delta > 0 ? "+" : ""}{e.pnl_delta.toFixed(2)}%
              </td>
              <td class="r mono sub">+{(e.extra_ms / 1000).toFixed(1)}s / +{n0(e.extra_tokens)}tok</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else}
      <p class="muted">{routing.effectiveness_note}</p>
    {/if}

    <h4>Routing policy</h4>
    <div class="policy">
      {#each routing.tasks as t (t.task)}
        <div class="prow">
          <span class="tag {t.default_mode.toLowerCase()}">{t.default_mode}</span>
          <span class="mono pt">{t.task}</span>
          <span class="sub">{t.why}</span>
        </div>
      {/each}
    </div>
  {/if}
</Panel>

<style>
  h4 {
    margin: var(--space-lg) 0 var(--space-sm);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-dim);
  }
  .muted { color: var(--ink-dim); font-size: 13px; line-height: 1.55; margin: 0; }
  .err { color: var(--bad); font-size: 13px; margin: 0; }
  .sub { color: var(--ink-faint); font-size: 11px; }

  .headline {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-md);
    align-items: stretch;
  }
  .big { display: flex; flex-direction: column; gap: 2px; min-width: 150px; }
  .big .val { font-size: 34px; font-weight: 600; line-height: 1; font-variant-numeric: tabular-nums; }
  .big .lbl { font-size: 11px; color: var(--ink-dim); }

  .verdict {
    flex: 1 1 320px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: var(--space-sm) var(--space-md);
    border-left: 3px solid var(--good);
    background: color-mix(in srgb, var(--good) 7%, transparent);
    border-radius: var(--radius-sm);
    font-size: 12px;
    line-height: 1.5;
    color: var(--ink-dim);
  }
  .verdict.bad {
    border-left-color: var(--bad);
    background: color-mix(in srgb, var(--bad) 8%, transparent);
  }
  .verdict strong { color: var(--ink); font-size: 13px; }

  .bars { display: flex; flex-direction: column; gap: 5px; }
  .bar-row {
    display: grid;
    grid-template-columns: 56px 1fr 56px 64px;
    gap: var(--space-sm);
    align-items: center;
    font-size: 12px;
  }
  .bar-row .k { font-family: var(--mono); color: var(--ink-dim); }
  .track { height: 14px; background: var(--surface-raised); border-radius: 3px; overflow: hidden; }
  .fill { height: 100%; border-radius: 3px; transition: width 240ms ease; }
  .bar-row .v { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
  .bar-row .n {
    text-align: right;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-faint);
  }
  .bar-row .n.provisional { color: var(--warm); }

  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th {
    text-align: left;
    font-weight: 500;
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-faint);
    padding: 4px 8px 4px 0;
    border-bottom: 1px solid var(--line);
  }
  td { padding: 5px 8px 5px 0; border-bottom: 1px solid var(--line); }
  tbody tr:last-child td { border-bottom: none; }
  .r { text-align: right; }
  .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }

  .tag {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 10px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    border: 1px solid var(--line-bright);
    color: var(--ink-dim);
  }
  .tag.measured { border-color: var(--good); color: var(--good); }
  .tag.indicative { border-color: var(--accent-dim); color: var(--accent); }
  .tag.provisional { border-color: var(--warm); color: var(--warm); }
  .tag.deep { border-color: var(--accent); color: var(--accent); }
  .tag.fast { border-color: var(--line-bright); color: var(--ink-dim); }
  .tag.auto { border-color: var(--warm); color: var(--warm); }

  .cov { display: flex; flex-wrap: wrap; gap: var(--space-lg); }
  .cov > div { display: flex; flex-direction: column; gap: 1px; }
  .cv { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .cl { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-faint); }

  .policy { display: flex; flex-direction: column; gap: 3px; }
  .prow {
    display: grid;
    grid-template-columns: 54px 168px 1fr;
    gap: var(--space-sm);
    align-items: baseline;
    font-size: 11px;
    padding: 2px 0;
  }
  .prow .pt { color: var(--ink); }
</style>
