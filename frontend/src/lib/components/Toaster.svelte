<script lang="ts">
  import { toastStore } from "../stores/toast.svelte";
</script>

<div class="toaster">
  {#each toastStore.items as t (t.id)}
    <div class="toast {t.tone}">{t.message}</div>
  {/each}
</div>

<style>
  .toaster {
    position: fixed;
    right: 18px;
    bottom: 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 100;
  }
  .toast {
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 12.5px;
    max-width: 320px;
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    color: var(--ink);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  }
  .toast.good {
    border-color: rgba(61, 220, 151, 0.4);
    color: var(--good);
  }
  .toast.bad {
    border-color: rgba(255, 92, 114, 0.4);
    color: var(--bad);
  }
</style>
