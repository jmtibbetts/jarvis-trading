<script lang="ts">
  import { sectionStore, SECTIONS } from "../stores/section.svelte";

  const icons: Record<string, string> = {
    command:
      '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    signals: '<path d="M3 12l4-7 5 14 3-9 2 5h4"/>',
    positions:
      '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    intelligence: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a13 13 0 0 1 0 18M12 3a13 13 0 0 0 0 18"/>',
    performance: '<path d="M4 20V10M12 20V4M20 20v-7"/>',
    ops: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34H9a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87V9a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z"/>',
  };
</script>

<nav class="rail">
  {#each SECTIONS as section, i (section.id)}
    {#if i === SECTIONS.length - 1}<div class="sep"></div>{/if}
    <a
      href="#{section.id}"
      class:on={sectionStore.current === section.id}
      class:disabled={!section.ready}
      title={section.ready ? section.label : `${section.label} — coming soon`}
      onclick={(e) => {
        e.preventDefault();
        if (section.ready) sectionStore.go(section.id);
      }}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
        {@html icons[section.id]}
      </svg>
    </a>
  {/each}
</nav>

<style>
  .rail {
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 16px 0;
    gap: 4px;
    background: rgba(15, 21, 29, 0.4);
  }
  .rail a {
    width: 40px;
    height: 40px;
    border-radius: 9px;
    display: grid;
    place-items: center;
    color: var(--ink-faint);
    position: relative;
    text-decoration: none;
  }
  .rail a svg {
    width: 18px;
    height: 18px;
  }
  .rail a.on {
    color: var(--accent);
    background: rgba(124, 154, 255, 0.1);
  }
  .rail a.on::before {
    content: "";
    position: absolute;
    left: -16px;
    top: 8px;
    bottom: 8px;
    width: 2px;
    background: var(--accent);
    border-radius: 2px;
    box-shadow: 0 0 8px var(--accent);
  }
  .rail a.disabled {
    opacity: 0.35;
    cursor: default;
  }
  .sep {
    width: 26px;
    height: 1px;
    background: var(--line);
    margin: 8px 0;
  }
</style>
