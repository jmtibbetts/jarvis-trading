export type SectionId =
  | "brief"
  | "command"
  | "signals"
  // THE TRADING SURFACES. Previously one "positions" section with
  // Live / Paper / Auto Sim tabs — a product hierarchy from a live-first
  // phase this platform is no longer in. Routing is not decided by
  // direction any more, so "the book Alpaca refused" is not a category.
  | "virtualcex"
  | "virtualdex"
  | "shadowlab"
  | "positions"
  | "charts"
  | "instrument"
  | "intelligence"
  | "smartmoney"
  | "macro"
  | "cryptodesk"
  | "onchain"
  | "performance"
  | "ops";

export const SECTIONS: { id: SectionId; label: string; ready: boolean }[] = [
  { id: "brief", label: "Morning Brief", ready: true },
  { id: "command", label: "Command Center", ready: true },
  { id: "signals", label: "Signals & Scanner", ready: true },
  // Virtual CEX and Virtual DEX are separate because their MECHANICS
  // differ — a pool is not an order book — while both normalise into the
  // same RealizedOutcome. Shadow Lab is the control arm, not a third book.
  { id: "virtualcex", label: "Virtual CEX", ready: true },
  { id: "virtualdex", label: "Virtual DEX", ready: true },
  { id: "shadowlab", label: "Shadow Lab", ready: true },
  { id: "positions", label: "Accounts & Exposure", ready: true },
  { id: "charts", label: "Charts", ready: true },
  // ONE instrument, everything known about it — including the instruments
  // the desk refuses to size, which is where it earns its keep.
  { id: "instrument", label: "Instrument", ready: true },
  { id: "intelligence", label: "Intelligence", ready: true },
  { id: "smartmoney", label: "Smart Money", ready: true },
  { id: "macro", label: "Macro Desk", ready: true },
  { id: "cryptodesk", label: "Crypto Desk", ready: true },
  { id: "onchain", label: "On-Chain Desk", ready: true },
  { id: "performance", label: "Performance & Learning", ready: true },
  { id: "ops", label: "Ops", ready: true },
];

const LAST_SECTION_KEY = "jarvis.lastSection";

function sectionFromHash(): SectionId {
  const hash = window.location.hash.replace("#", "") as SectionId;
  if (SECTIONS.some((s) => s.id === hash)) return hash;
  // No (or unknown) hash: restore the last section this window used, so a
  // plain reload lands where the user was instead of resetting to Command.
  try {
    const saved = localStorage.getItem(LAST_SECTION_KEY) as SectionId | null;
    if (saved && SECTIONS.some((s) => s.id === saved)) return saved;
  } catch {
    /* storage unavailable — default below */
  }
  return "command";
}

/** True when this window was opened as a popout (chromeless single-section
 * view for multi-monitor use). Checked once at load — a popout stays a
 * popout for its lifetime. */
export const isPopout = new URLSearchParams(window.location.search).has("popout");

/** Panel-level popout target (slugified panel title), or null. When set, the
 * section renders but every Panel except the matching one returns nothing —
 * so ANY panel in the app can live in its own window with zero per-panel
 * wiring. */
export const popPanel: string | null =
  new URLSearchParams(window.location.search).get("panel");

export function openPopout(id: SectionId) {
  const url = `${window.location.pathname}?popout=1#${id}`;
  window.open(url, `jarvis-${id}`, "popup=yes,width=1180,height=860");
}

export function openPanelPopout(section: SectionId, panelId: string) {
  const url = `${window.location.pathname}?popout=1&panel=${encodeURIComponent(panelId)}#${section}`;
  window.open(url, `jarvis-${section}-${panelId}`, "popup=yes,width=920,height=760");
}

class SectionStore {
  current = $state<SectionId>(sectionFromHash());

  constructor() {
    window.addEventListener("hashchange", () => {
      this.current = sectionFromHash();
      this.#persist();
    });
    this.#persist();
  }

  go(id: SectionId) {
    window.location.hash = id;
    this.current = id;
    this.#persist();
  }

  #persist() {
    try {
      localStorage.setItem(LAST_SECTION_KEY, this.current);
    } catch {
      /* best-effort */
    }
  }
}

export const sectionStore = new SectionStore();
