export type SectionId =
  | "command"
  | "signals"
  | "positions"
  | "intelligence"
  | "performance"
  | "ops";

export const SECTIONS: { id: SectionId; label: string; ready: boolean }[] = [
  { id: "command", label: "Command Center", ready: true },
  { id: "signals", label: "Signals & Scanner", ready: true },
  { id: "positions", label: "Positions & Paper", ready: false },
  { id: "intelligence", label: "Intelligence", ready: false },
  { id: "performance", label: "Performance & Learning", ready: false },
  { id: "ops", label: "Ops", ready: false },
];

function sectionFromHash(): SectionId {
  const hash = window.location.hash.replace("#", "") as SectionId;
  return SECTIONS.some((s) => s.id === hash) ? hash : "command";
}

class SectionStore {
  current = $state<SectionId>(sectionFromHash());

  constructor() {
    window.addEventListener("hashchange", () => {
      this.current = sectionFromHash();
    });
  }

  go(id: SectionId) {
    window.location.hash = id;
    this.current = id;
  }
}

export const sectionStore = new SectionStore();
