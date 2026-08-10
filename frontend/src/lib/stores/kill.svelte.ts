// Backed by the existing GET/POST /api/system/trading-status endpoints
// (app/routes.py) and lib/kill_switch.py — this store just mirrors that
// state client-side and pushes toggles back through the same API.

type KillState = {
  live_trading_enabled: boolean;
  paused_reason: string | null;
  paused_at: string | null;
  updated_at: string | null;
};

class KillSwitchStore {
  state = $state<KillState | null>(null);
  loading = $state(false);

  async load() {
    this.loading = true;
    try {
      const res = await fetch("/api/system/trading-status");
      this.state = await res.json();
    } finally {
      this.loading = false;
    }
  }

  async toggle(reason?: string) {
    if (!this.state) return;
    const enabled = !this.state.live_trading_enabled;
    const res = await fetch("/api/system/trading-status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, reason }),
    });
    this.state = await res.json();
  }
}

export const killSwitchStore = new KillSwitchStore();
