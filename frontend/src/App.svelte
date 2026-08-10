<script lang="ts">
  import TopHud from "./lib/components/TopHud.svelte";
  import NavRail from "./lib/components/NavRail.svelte";
  import CommandCenter from "./lib/sections/CommandCenter.svelte";
  import SignalsScanner from "./lib/sections/SignalsScanner.svelte";
  import PositionsPaper from "./lib/sections/PositionsPaper.svelte";
  import Intelligence from "./lib/sections/Intelligence.svelte";
  import Performance from "./lib/sections/Performance.svelte";
  import Ops from "./lib/sections/Ops.svelte";
  import { sectionStore } from "./lib/stores/section.svelte";
  import { killSwitchStore } from "./lib/stores/kill.svelte";
  import { wsStore } from "./lib/stores/ws.svelte";

  killSwitchStore.load();
  wsStore.connect();
</script>

<div class="shell">
  <TopHud />
  <NavRail />
  <main>
    {#if sectionStore.current === "command"}
      <CommandCenter />
    {:else if sectionStore.current === "signals"}
      <SignalsScanner />
    {:else if sectionStore.current === "positions"}
      <PositionsPaper />
    {:else if sectionStore.current === "intelligence"}
      <Intelligence />
    {:else if sectionStore.current === "performance"}
      <Performance />
    {:else if sectionStore.current === "ops"}
      <Ops />
    {/if}
  </main>
</div>

<style>
  .shell {
    display: grid;
    grid-template-columns: 64px 1fr;
    grid-template-rows: 56px 1fr;
    min-height: 100vh;
  }
  main {
    padding: 20px 22px 40px;
    min-width: 0;
    overflow-x: auto;
  }
</style>
