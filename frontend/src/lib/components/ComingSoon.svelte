<script lang="ts">
  let { label, phase }: { label: string; phase: string } = $props();
</script>

<div class="wrap">
  <div class="eyebrow">Phase {phase}</div>
  <h2>{label}</h2>
  <p>Not built yet — see the rebuild plan for what lands here.</p>
</div>

<style>
  .wrap {
    height: calc(100vh - 56px);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    gap: 6px;
    color: var(--ink-dim);
  }
  h2 {
    margin: 0;
    font-size: 20px;
    color: var(--ink);
  }
  p {
    margin: 0;
    font-size: 13px;
  }
</style>
