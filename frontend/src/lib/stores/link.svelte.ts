/**
 * Symbol-link store — the "click NVDA anywhere, every linked view follows"
 * mechanism, ACROSS WINDOWS. Selections broadcast over a BroadcastChannel,
 * so a popout on a second monitor follows clicks made in the main window
 * (and vice versa). Same-origin only, which is exactly the popout case.
 *
 * Deliberately one link group for now: the multi-group (A/B/C) design in the
 * original spec adds chrome to every panel for a workflow the app doesn't
 * have yet. One shared group covers "watchlist click drives the signals
 * view" — more groups can layer on later without changing subscribers.
 */

const CHANNEL = "jarvis-symbol-link";

class LinkStore {
  /** Currently linked symbol, null when nothing is linked. */
  symbol = $state<string | null>(null);

  #channel: BroadcastChannel | null = null;

  constructor() {
    try {
      this.#channel = new BroadcastChannel(CHANNEL);
      this.#channel.onmessage = (e) => {
        const sym = typeof e.data?.symbol === "string" ? e.data.symbol : null;
        this.symbol = sym;
      };
    } catch {
      // BroadcastChannel unavailable — linking still works within this window.
    }
  }

  /** Link a symbol everywhere (this window + all popouts). */
  link(symbol: string) {
    this.symbol = symbol;
    this.#channel?.postMessage({ symbol });
  }

  /** Clear the link everywhere. */
  clear() {
    this.symbol = null;
    this.#channel?.postMessage({ symbol: null });
  }
}

export const linkStore = new LinkStore();
