<script lang="ts">
  import { notificationStore } from "../stores/notifications.svelte";

  let open = $state(false);

  function toggle() {
    open = !open;
    if (open) notificationStore.markAllRead();
  }

  function onDocClick(e: MouseEvent) {
    if (!(e.target as HTMLElement).closest(".notif-wrap")) open = false;
  }

  const fmtAgo = (iso: string) => {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };
</script>

<svelte:window onclick={onDocClick} />

<div class="notif-wrap">
  <button class="bell" onclick={toggle} title="Notifications">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
    {#if notificationStore.unreadCount}
      <span class="badge">{notificationStore.unreadCount > 9 ? "9+" : notificationStore.unreadCount}</span>
    {/if}
  </button>

  {#if open}
    <div class="dropdown">
      <div class="dropdown-head">
        <span>Notifications</span>
        {#if notificationStore.items.length}
          <button class="clear-btn" onclick={() => notificationStore.clear()}>Clear</button>
        {/if}
      </div>
      <div class="list">
        {#each notificationStore.items as n (n.id)}
          <div class="item">
            <div class="item-text">{n.text}</div>
            <div class="item-time">{fmtAgo(n.ts)}</div>
          </div>
        {:else}
          <div class="empty">No notifications yet — pushed here as the server generates signals, critical threats, or job failures.</div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .notif-wrap {
    position: relative;
  }
  .bell {
    position: relative;
    background: none;
    border: none;
    color: var(--ink-dim);
    cursor: pointer;
    padding: 6px;
    display: flex;
    align-items: center;
  }
  .bell:hover {
    color: var(--ink);
  }
  .badge {
    position: absolute;
    top: 0;
    right: 0;
    background: var(--bad);
    color: white;
    font-size: 9px;
    font-weight: 700;
    min-width: 14px;
    height: 14px;
    border-radius: 7px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 3px;
  }
  .dropdown {
    position: absolute;
    top: calc(100% + 8px);
    right: 0;
    width: 320px;
    max-height: 400px;
    background: var(--surface-raised, #12161d);
    border: 1px solid var(--line-bright, #2a3140);
    border-radius: 10px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
    z-index: 50;
    display: flex;
    flex-direction: column;
  }
  .dropdown-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
    font-size: 11.5px;
    font-weight: 650;
  }
  .clear-btn {
    background: none;
    border: none;
    color: var(--accent);
    font-size: 10.5px;
    cursor: pointer;
  }
  .list {
    overflow-y: auto;
    max-height: 340px;
  }
  .item {
    padding: 9px 14px;
    border-bottom: 1px solid var(--line);
  }
  .item:last-child {
    border-bottom: none;
  }
  .item-text {
    font-size: 12px;
    color: var(--ink-dim);
    line-height: 1.4;
  }
  .item-time {
    font-size: 10px;
    color: var(--ink-faint);
    margin-top: 3px;
  }
  .empty {
    padding: 20px 14px;
    font-size: 11.5px;
    color: var(--ink-faint);
    line-height: 1.5;
    text-align: center;
  }
</style>
