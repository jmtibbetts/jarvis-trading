<script lang="ts">
  /**
   * "Is anything preventing JARVIS from operating correctly?"
   *
   * Three separate failure modes that used to be invisible from the
   * operations screen, in the order they cost you:
   *
   *   INVARIANTS  training data being written wrong RIGHT NOW. Worse than a
   *               losing trade, because a loss is self-correcting and a
   *               corrupted corpus teaches the wrong lesson forever.
   *   PRODUCERS   the attention queue's own coverage. A queue assembled
   *               from six of eight checks cannot claim "all clear".
   *   FEEDS       panels on this page whose data is stale or failed.
   *
   * Ops owns the full truth; this says only whether to go there.
   */
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import StateNote from "./StateNote.svelte";
  import { api, type IntegrityPanel } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import { attentionStore } from "../stores/attention.svelte";
  import { sectionStore } from "../stores/section.svelte";

  let { troubledFeeds = [] }: { troubledFeeds?: string[] } = $props();

  const feeds = new FeedTracker();
  let integrity = $state<IntegrityPanel | null>(null);

  $effect(() => {
    feeds.load("integrity", () => api.integrity()).then((r) => (integrity = r));
    const t = setInterval(
      () => feeds.load("integrity", () => api.integrity()).then((r) => (integrity = r)),
      120_000,
    );
    return () => clearInterval(t);
  });

  const q = $derived(attentionStore.queue);

  // CLEAN only when every check actually ran. A check that could not run is
  // not a check that passed, and the verdict string already encodes that —
  // this mirrors it rather than re-deriving a friendlier one.
  const tone = $derived(
    !integrity ? "neutral"
      : integrity.critical ? "critical"
      : integrity.violations ? "bad"
      : integrity.healthy ? "good"
      : "warm",
  );
</script>

<Panel
  title="System &amp; Data"
  dotColor={integrity?.critical ? "var(--critical)" : integrity?.violations ? "var(--bad)" : "var(--accent)"}
  status={feeds.status("integrity")}
  meta={integrity ? `${integrity.total - integrity.violations}/${integrity.total} invariants clean` : "—"}
>
  {#if !integrity}
    <StateNote status={feeds.status("integrity")} noun="integrity checks" />
  {:else}
    <button class="verdict {tone}" onclick={() => sectionStore.go("ops")}>
      <Pill tone={tone as never} label={integrity.healthy ? "CLEAN" : "ATTENTION"} />
      <span>{integrity.verdict}</span>
    </button>

    <div class="rows">
      <div class="r">
        <span>Invariants</span>
        <b class:bad={integrity.violations > 0}>
          {integrity.violations} violating
        </b>
        <em>
          {integrity.unavailable
            ? `${integrity.unavailable} could not run`
            : "all checks ran"}
        </em>
      </div>
      <div class="r">
        <span>Attention coverage</span>
        <b class:bad={q ? !q.complete : false}>
          {q ? `${q.producers_run}/${q.producers_total}` : "—"}
        </b>
        <em>
          {q && !q.complete
            ? "the queue is not a complete answer"
            : "every producer ran"}
        </em>
      </div>
      <div class="r">
        <span>Page feeds</span>
        <b class:bad={troubledFeeds.length > 0}>{troubledFeeds.length} not current</b>
        <em>{troubledFeeds.length ? troubledFeeds.join(", ") : "all current"}</em>
      </div>
    </div>

    {#if integrity.violations}
      <ul class="viol">
        {#each integrity.checks.filter((c) => c.status === "VIOLATION").slice(0, 4) as c (c.key)}
          <li>
            <Pill tone={c.severity === "CRITICAL" ? "critical" : "bad"} label={c.severity} />
            <span>
              <b>{c.title}</b>
              <em>{c.count} of {c.scanned} rows</em>
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    <p class="note">Ops holds the full detail — this only says whether to go there.</p>
  {/if}
</Panel>

<style>
  .verdict {
    display: flex; align-items: center; gap: 9px; width: 100%;
    background: none; border: 1px solid var(--line); border-radius: 8px;
    padding: 8px 10px; margin-bottom: 10px; cursor: pointer;
    color: inherit; font: inherit; text-align: left;
  }
  .verdict:hover { border-color: var(--line-bright); }
  .verdict span { font-size: 11.5px; color: var(--ink-faint); }
  .verdict.critical { border-color: color-mix(in srgb, var(--critical, #ff5c72) 50%, transparent); }
  .verdict.bad { border-color: color-mix(in srgb, var(--bad) 45%, transparent); }
  .rows { display: flex; flex-direction: column; gap: 6px; }
  .r { display: grid; grid-template-columns: 118px 88px minmax(0, 1fr); gap: 8px; align-items: baseline; }
  .r span { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-faint); }
  .r b { font-size: 13px; font-variant-numeric: tabular-nums; }
  .r b.bad { color: var(--bad); }
  .r em { font-style: normal; font-size: 10.5px; color: var(--ink-faint); }
  .viol { list-style: none; margin: 10px 0 0; padding: 9px 0 0; border-top: 1px solid var(--line); display: flex; flex-direction: column; gap: 6px; }
  .viol li { display: flex; gap: 8px; align-items: baseline; }
  .viol span { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
  .viol b { font-size: 11.5px; }
  .viol em { font-style: normal; font-size: 10px; color: var(--ink-faint); font-family: var(--mono); }
  .note { font-size: 10.5px; color: var(--ink-faint); margin: 9px 0 0; }
</style>
