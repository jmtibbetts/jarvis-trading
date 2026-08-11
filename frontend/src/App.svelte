<script lang="ts">
  import TopHud from "./lib/components/TopHud.svelte";
  import NavRail from "./lib/components/NavRail.svelte";
  import Toaster from "./lib/components/Toaster.svelte";
  import CommandPalette from "./lib/components/CommandPalette.svelte";
  import CommandCenter from "./lib/sections/CommandCenter.svelte";
  import SignalsScanner from "./lib/sections/SignalsScanner.svelte";
  import PositionsPaper from "./lib/sections/PositionsPaper.svelte";
  import Intelligence from "./lib/sections/Intelligence.svelte";
  import Performance from "./lib/sections/Performance.svelte";
  import Ops from "./lib/sections/Ops.svelte";
  import { sectionStore, SECTIONS, isPopout, popPanel } from "./lib/stores/section.svelte";
  import { killSwitchStore } from "./lib/stores/kill.svelte";
  import { wsStore } from "./lib/stores/ws.svelte";

  killSwitchStore.load();
  wsStore.connect();

  // 1-9 jump between sections — ignored while typing in a form field so it
  // doesn't fight with e.g. entering "AAPL" in the Manual Analysis symbol box.
  // Disabled entirely in popout windows, which are pinned to one section.
  function onKeydown(e: KeyboardEvent) {
    if (isPopout) return;
    const tag = (document.activeElement?.tagName ?? "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    const n = Number(e.key);
    if (n >= 1 && n <= SECTIONS.length) {
      const section = SECTIONS[n - 1];
      if (section.ready) sectionStore.go(section.id);
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="shell" class:popout={isPopout} class:panelpop={popPanel !== null}>
  {#if !isPopout}
    <TopHud />
    <NavRail />
  {/if}
  <main>
    {#if sectionStore.current === "command"}
      <CommandCenter />
    {:else if sectionStore.current === "signals"}
      <SignalsScanner />
    {:else if sectionStore.current === "positions"}
      <PositionsPaper />
    {:else if sectionStore.current === "intelligence"}
      <Intelligence view="world" />
    {:else if sectionStore.current === "smartmoney"}
      <Intelligence view="smartmoney" />
    {:else if sectionStore.current === "macro"}
      <Intelligence view="macro" />
    {:else if sectionStore.current === "cryptodesk"}
      <Intelligence view="cryptodesk" />
    {:else if sectionStore.current === "performance"}
      <Performance />
    {:else if sectionStore.current === "ops"}
      <Ops />
    {/if}
  </main>
  <Toaster />
  {#if !isPopout}
    <CommandPalette />
  {/if}
</div>

<style>
  .shell {
    display: grid;
    grid-template-columns: 64px 1fr;
    grid-template-rows: 56px 1fr;
    min-height: 100vh;
  }
  /* Popout windows are chromeless: no HUD, no rail — one section, full bleed.
     Each popout keeps its own WebSocket + polling, so it stays live on a
     second monitor without the main window. */
  .shell.popout {
    grid-template-columns: 1fr;
    grid-template-rows: 1fr;
  }
  main {
    padding: 20px 22px 40px;
    min-width: 0;
    overflow-x: auto;
  }
  .shell.popout main {
    padding: 14px 16px 28px;
  }
  /* Panel-popout: one panel per window. Hidden panels leave their grid
     wrappers empty — collapse those, and let the survivor span the full
     grid regardless of its original span class. */
  .shell.panelpop main :global(.grid > div:empty) {
    display: none;
  }
  .shell.panelpop main :global(.grid > div) {
    grid-column: span 12 !important;
  }
  .shell.panelpop main :global(.page-head),
  .shell.panelpop main :global(.kpis) {
    display: none;
  }
</style>
