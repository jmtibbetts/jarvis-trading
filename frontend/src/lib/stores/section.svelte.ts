export type SectionId =
  | "command"
  | "signals"
  | "positions"
  | "intelligence"
  | "smartmoney"
  | "macro"
  | "cryptodesk"
  | "performance"
  | "ops";

export const SECTIONS: { id: SectionId; label: string; ready: boolean }[] = [
  { id: "command", label: "Command Center", ready: true },
  { id: "signals", label: "Signals & Scanner", ready: true },
  { id: "positions", label: "Positions & Paper", ready: true },
  { id: "intelligence", label: "Intelligence", ready: true },
  { id: "smartmoney", label: "Smart Money", ready: true },
  { id: "macro", label: "Macro Desk", ready: true },
  { id: "cryptodesk", label: "Crypto Desk", ready: true },
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
