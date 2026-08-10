<script lang="ts">
  import { sectionStore, SECTIONS } from "../stores/section.svelte";
  import { killSwitchStore } from "../stores/kill.svelte";
  import { toastStore } from "../stores/toast.svelte";
  import { api } from "../api";

  let open = $state(false);
  let query = $state("");
  let input = $state<HTMLInputElement | undefined>();
  let symbolResults = $state<{ symbol: string; where: string; section: (typeof SECTIONS)[number]["id"] }[]>([]);
  let loadingSymbols = $state(false);

  type Action = { label: string; hint: string; run: () => void };

  const staticActions: Action[] = [
    ...SECTIONS.map((s) => ({ label: `Go to ${s.label}`, hint: "navigate", run: () => sectionStore.go(s.id) })),
    {
      label: "Toggle live trading kill switch",
      hint: "action",
      run: async () => {
        const enabled = killSwitchStore.state?.live_trading_enabled;
        if (enabled === undefined || enabled === null) return;
        const reason = enabled ? window.prompt("Reason for pausing live trading?") ?? "Manually paused" : undefined;
        if (enabled && reason === null) return;
        await killSwitchStore.toggle(reason);
        toastStore.ok(enabled ? "Live trading paused" : "Live trading resumed");
      },
    },
    { label: "Run Pre-Market scan", hint: "scanner", run: () => api.runScanner("pre_market").then(() => toastStore.ok("Pre-Market scan started")) },
    { label: "Run Intraday scan", hint: "scanner", run: () => api.runScanner("intraday").then(() => toastStore.ok("Intraday scan started")) },
    { label: "Run Crypto 24/7 scan", hint: "scanner", run: () => api.runScanner("crypto").then(() => toastStore.ok("Crypto scan started")) },
    { label: "Run Futures/Forex scan", hint: "scanner", run: () => api.runScanner("futures").then(() => toastStore.ok("Futures/Forex scan started")) },
  ];

  const filteredActions = $derived(
    query.trim().length
      ? staticActions.filter((a) => a.label.toLowerCase().includes(query.trim().toLowerCase()))
      : staticActions.slice(0, 6),
  );

  async function searchSymbols(q: string) {
    if (q.trim().length < 2) {
      symbolResults = [];
      return;
    }
    loadingSymbols = true;
    try {
      const upper = q.trim().toUpperCase();
      const [signals, live, paper] = await Promise.all([
        api.signals("Active", 300).catch(() => []),
        api.positionsWithSignals().catch(() => null),
        api.paperSummary().catch(() => null),
      ]);
      const results: { symbol: string; where: string; section: (typeof SECTIONS)[number]["id"] }[] = [];
      const seen = new Set<string>();
      for (const s of signals) {
        if (s.asset_symbol.toUpperCase().includes(upper) && !seen.has(s.asset_symbol + ":signal")) {
          seen.add(s.asset_symbol + ":signal");
          results.push({ symbol: s.asset_symbol, where: "active signal", section: "signals" });
        }
      }
      for (const p of live?.positions ?? []) {
        if (p.symbol.toUpperCase().includes(upper) && !seen.has(p.symbol + ":live")) {
          seen.add(p.symbol + ":live");
          results.push({ symbol: p.symbol, where: "live position", section: "positions" });
        }
      }
      for (const p of paper?.positions ?? []) {
        if (p.symbol.toUpperCase().includes(upper) && !seen.has(p.symbol + ":paper")) {
          seen.add(p.symbol + ":paper");
          results.push({ symbol: p.symbol, where: "paper position", section: "positions" });
        }
      }
      symbolResults = results.slice(0, 8);
    } finally {
      loadingSymbols = false;
    }
  }

  $effect(() => {
    const q = query;
    const timer = setTimeout(() => searchSymbols(q), 200);
    return () => clearTimeout(timer);
  });

  function close() {
    open = false;
    query = "";
    symbolResults = [];
  }

  function runAction(fn: () => void) {
    fn();
    close();
  }

  function onGlobalKeydown(e: KeyboardEvent) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      open = !open;
      if (open) queueMicrotask(() => input?.focus());
      else close();
    } else if (e.key === "Escape" && open) {
      close();
    }
  }
</script>

<svelte:window onkeydown={onGlobalKeydown} />

{#if open}
  <div class="overlay" role="button" tabindex="-1" onclick={close} onkeydown={(e) => e.key === "Escape" && close()}>
    <div
      class="palette"
      onclick={(e) => e.stopPropagation()}
      onkeydown={(e) => e.stopPropagation()}
      role="dialog"
      aria-label="Command palette"
      tabindex="-1"
    >
      <input
        bind:this={input}
        bind:value={query}
        placeholder="Jump to a section, run an action, or search a symbol…"
        autocomplete="off"
      />
      <div class="results">
        {#if symbolResults.length}
          <div class="group-label">Symbols</div>
          {#each symbolResults as r (r.symbol + r.where)}
            <button class="result" onclick={() => runAction(() => sectionStore.go(r.section))}>
              <span class="result-sym">{r.symbol}</span>
              <span class="result-hint">{r.where}</span>
            </button>
          {/each}
        {:else if loadingSymbols}
          <div class="group-label">Searching…</div>
        {/if}

        <div class="group-label">Actions</div>
        {#each filteredActions as a (a.label)}
          <button class="result" onclick={() => runAction(a.run)}>
            <span class="result-sym">{a.label}</span>
            <span class="result-hint">{a.hint}</span>
          </button>
        {:else}
          <div class="empty">No matching actions</div>
        {/each}
      </div>
      <div class="footer">Ctrl/⌘K to toggle · Esc to close</div>
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(5, 7, 10, 0.6);
    backdrop-filter: blur(2px);
    z-index: 100;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 12vh;
  }
  .palette {
    width: 560px;
    max-width: 90vw;
    background: var(--surface-raised, #12161d);
    border: 1px solid var(--line-bright, #2a3140);
    border-radius: 12px;
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
    overflow: hidden;
  }
  input {
    width: 100%;
    box-sizing: border-box;
    background: transparent;
    border: none;
    border-bottom: 1px solid var(--line);
    color: var(--ink);
    padding: 16px 18px;
    font-size: 14px;
    outline: none;
  }
  .results {
    max-height: 360px;
    overflow-y: auto;
    padding: 6px;
  }
  .group-label {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-faint);
    padding: 8px 10px 4px;
  }
  .result {
    width: 100%;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: none;
    border: none;
    color: var(--ink);
    padding: 9px 10px;
    border-radius: 7px;
    font-size: 12.5px;
    cursor: pointer;
    text-align: left;
  }
  .result:hover {
    background: rgba(124, 154, 255, 0.1);
  }
  .result-sym {
    font-weight: 600;
  }
  .result-hint {
    font-size: 10.5px;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .empty {
    padding: 14px 10px;
    font-size: 11.5px;
    color: var(--ink-faint);
  }
  .footer {
    border-top: 1px solid var(--line);
    padding: 8px 14px;
    font-size: 10px;
    color: var(--ink-faint);
    text-align: right;
  }
</style>
