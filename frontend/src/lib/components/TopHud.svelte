<script lang="ts">
  import { killSwitchStore } from "../stores/kill.svelte";
  import { wsStore } from "../stores/ws.svelte";
  import { api } from "../api";
  import { sectionStore } from "../stores/section.svelte";
  import NotificationCenter from "./NotificationCenter.svelte";

  let providers = $state<{ name: string; ok: boolean; detail: string }[]>([]);
  $effect(() => {
    const load = () => api.providerStatus().then((r) => (providers = r.providers)).catch(() => {});
    load();
    const id = setInterval(load, 120_000);
    return () => clearInterval(id);
  });
  const downCount = $derived(providers.filter((p) => !p.ok).length);

  let clock = $state(new Date().toTimeString().slice(0, 8));
  $effect(() => {
    const id = setInterval(() => {
      clock = new Date().toTimeString().slice(0, 8);
    }, 1000);
    return () => clearInterval(id);
  });

  const enabled = $derived(killSwitchStore.state?.live_trading_enabled ?? null);

  async function onToggle() {
    if (enabled === null) return;
    const reason = enabled ? window.prompt("Reason for pausing live trading?") ?? "Manually paused" : undefined;
    if (enabled && reason === null) return; // user cancelled the prompt
    await killSwitchStore.toggle(reason);
  }
</script>

<div class="hud">
  <div class="brand">
    <div class="mark"></div>
    <div>
      <b>JARVIS</b>
      <div><span>COMMAND CENTER</span></div>
    </div>
  </div>

  <div class="hud-right">
    {#if providers.length}
      <button
        class="providers"
        title={providers.map((p) => `${p.ok ? "●" : "○"} ${p.name}: ${p.detail}`).join(" | ")}
        onclick={() => sectionStore.go("ops")}
      >
        {#each providers as p (p.name)}
          <span class="prov" class:down={!p.ok} title={`${p.name}: ${p.detail}`}>
            <i></i><em>{p.name}</em>
          </span>
        {/each}
        {#if downCount > 0}<span class="prov-warn num">{downCount} down</span>{/if}
      </button>
    {/if}
    <div class="ws-pill" class:live={wsStore.connected} title={wsStore.connected ? "Live feed connected" : "Reconnecting…"}>
      <i></i>
      {wsStore.connected ? "LIVE" : "RECONNECTING"}
    </div>

    <NotificationCenter />

    <button
      class="kill"
      class:off={enabled === false}
      onclick={onToggle}
      disabled={enabled === null}
      title="Pause/resume all new live orders. Existing positions keep their stop-loss/take-profit protection."
    >
      <i></i>
      {enabled === null ? "…" : enabled ? "LIVE TRADING ON" : "TRADING PAUSED"}
    </button>

    <div class="clock num">{clock}</div>
  </div>
</div>

<style>
  .hud {
    grid-column: 1 / 3;
    display: flex;
    align-items: center;
    gap: 22px;
    padding: 0 18px;
    height: 56px;
    border-bottom: 1px solid var(--line);
    background: rgba(15, 21, 29, 0.7);
    backdrop-filter: blur(10px);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .brand .mark {
    width: 26px;
    height: 26px;
    border-radius: 7px;
    background: conic-gradient(from 220deg, var(--accent), var(--warm), var(--accent));
    box-shadow: 0 0 16px rgba(124, 154, 255, 0.5);
    animation: spin 7s linear infinite;
    display: grid;
    place-items: center;
  }
  .brand .mark::after {
    content: "";
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--bg);
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
  .brand b {
    font-family: var(--mono);
    font-weight: 700;
    letter-spacing: 0.09em;
    font-size: 14.5px;
  }
  .brand span {
    font-size: 10px;
    color: var(--ink-faint);
    letter-spacing: 0.08em;
  }

  .hud-right {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .providers {
    display: flex;
    align-items: center;
    gap: 10px;
    background: none;
    border: none;
    padding: 4px 6px;
    cursor: pointer;
    border-radius: 7px;
  }
  .providers:hover {
    background: rgba(124, 154, 255, 0.07);
  }
  .prov {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .prov i {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--good);
    box-shadow: 0 0 6px var(--good);
  }
  .prov.down i {
    background: var(--bad);
    box-shadow: 0 0 6px var(--bad);
  }
  .prov em {
    font-style: normal;
    font-size: 9px;
    letter-spacing: 0.05em;
    color: var(--ink-faint);
    font-family: var(--mono);
    text-transform: uppercase;
  }
  .prov.down em {
    color: var(--bad);
  }
  .prov-warn {
    font-size: 9.5px;
    color: var(--bad);
    font-weight: 700;
  }
  @media (max-width: 1450px) {
    .prov em {
      display: none;
    }
    .providers {
      gap: 6px;
    }
  }

  .ws-pill {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    letter-spacing: 0.06em;
    color: var(--ink-faint);
    font-family: var(--mono);
  }
  .ws-pill i {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--ink-faint);
  }
  .ws-pill.live {
    color: var(--good);
  }
  .ws-pill.live i {
    background: var(--good);
    box-shadow: 0 0 8px var(--good);
    animation: pulse 2s ease-in-out infinite;
  }

  .kill {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 14px;
    border-radius: 8px;
    background: rgba(61, 220, 151, 0.08);
    border: 1px solid rgba(61, 220, 151, 0.35);
    color: var(--good);
    font-size: 11.5px;
    font-weight: 600;
    letter-spacing: 0.03em;
    cursor: pointer;
  }
  .kill:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .kill.off {
    background: rgba(255, 92, 114, 0.08);
    border-color: rgba(255, 92, 114, 0.35);
    color: var(--bad);
  }
  .kill i {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
    box-shadow: 0 0 9px currentColor;
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
  }

  .clock {
    font-size: 12px;
    color: var(--ink-dim);
    min-width: 74px;
    text-align: right;
  }
</style>
