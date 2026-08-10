// Rolling log of server-pushed WS events, surfaced via the bell icon in the
// HUD bar. Distinct from Toaster: toasts are ephemeral feedback for actions
// *this* browser tab just took; this is a persistent (session-lifetime) log
// of things the *server* pushed, so a user who steps away can catch up.
import { wsStore, type WsEnvelope } from "./ws.svelte";

export type NotificationItem = {
  id: string;
  type: string;
  ts: string;
  text: string;
  read: boolean;
};

const MAX_ITEMS = 50;

// Only these WS message types are worth surfacing as notifications — others
// (e.g. routine job_status "ok" transitions) are too frequent/low-signal.
function describe(msg: WsEnvelope): string | null {
  const data = msg.data as Record<string, unknown>;
  switch (msg.type) {
    case "new_signals":
      return `${data.count} new signal${(data.count as number) > 1 ? "s" : ""} generated (${data.regime ?? "regime unknown"})`;
    case "critical_threat": {
      const titles = (data.titles as string[]) ?? [];
      return `${data.count} new critical threat${(data.count as number) > 1 ? "s" : ""}: ${titles[0] ?? ""}`;
    }
    case "job_status": {
      const entries = Object.entries(data as Record<string, { status: string; error?: string | null }>);
      const [name, job] = entries[0] ?? [];
      if (!name || job?.status !== "error") return null;
      return `${name} job failed: ${job.error ?? "unknown error"}`;
    }
    default:
      return null;
  }
}

class NotificationStore {
  items = $state<NotificationItem[]>([]);
  unreadCount = $derived(this.items.filter((i) => !i.read).length);

  constructor() {
    wsStore.on("*", (msg) => {
      const text = describe(msg);
      if (!text) return;
      this.items = [{ id: `${msg.type}-${msg.ts}`, type: msg.type, ts: msg.ts, text, read: false }, ...this.items].slice(0, MAX_ITEMS);
    });
  }

  markAllRead() {
    this.items = this.items.map((i) => ({ ...i, read: true }));
  }

  clear() {
    this.items = [];
  }
}

export const notificationStore = new NotificationStore();
