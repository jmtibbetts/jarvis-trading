/**
 * The data-state standard from §29 and §64, and the tracker that enforces it.
 *
 * The problem this replaces: every section loader ran its requests through
 * `.catch(() => null)`. That is one line and it throws away the entire
 * distinction the spec cares about — a panel showing nothing could mean the
 * dataset is genuinely empty, the provider is down, the key was never
 * configured, the endpoint does not exist on this plan, or the request simply
 * failed once. §4 singles this out as especially dangerous in a trading
 * dashboard: "0 positions" and "we could not reach the broker" render
 * identically, and only one of them means you are flat.
 *
 * `PositionsPaper.svelte` already handled this correctly for the live book —
 * a failed fetch kept the last good data on screen instead of flashing an
 * empty state. This module is that pattern, generalized, plus the vocabulary
 * to say which of the failure modes actually happened.
 */

import { ApiError } from "./api";

/**
 * §64's vocabulary, narrowed to the states this frontend can actually
 * distinguish and therefore honestly claim.
 *
 * Deliberately NOT included: LIVE / CONNECTING / FRESH / DELAYED, which
 * belong to streaming feeds and would be guesses on a polled REST call, and
 * PLAN_UNAVAILABLE, which the backend does not yet report separately from a
 * plain 404. Adding a state the loader cannot actually detect just moves the
 * lie somewhere new.
 */
export type DataState =
  | "loading"
  | "ready"
  | "empty"
  | "stale"
  | "degraded"
  | "error"
  | "not_configured"
  | "unsupported";

export type FeedStatus = {
  state: DataState;
  /** One actionable line, §65 — what happened and what it implies. */
  detail: string;
  /** When this feed last returned successfully; null if it never has. */
  lastGoodAt: number | null;
  /** Consecutive failures. Resets on any success. */
  failures: number;
  /** HTTP status of the last failure, 0 for "never reached the server". */
  status: number | null;
};

const INITIAL: FeedStatus = {
  state: "loading",
  detail: "",
  lastGoodAt: null,
  failures: 0,
  status: null,
};

/** States that mean "this will keep failing until something changes". */
const TERMINAL: ReadonlySet<DataState> = new Set(["not_configured", "unsupported"]);

/** True for a payload that carries no information, as opposed to bad news. */
export function isEmptyPayload(v: unknown): boolean {
  if (v == null) return true;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === "string") return v.trim() === "";
  if (typeof v === "object") return Object.keys(v as object).length === 0;
  return false;
}

/**
 * What a thrown error means, in the spec's vocabulary.
 *
 * The mapping follows how this backend actually uses statuses — see
 * `app/routers/intel.py`, where 503 is raised for "provider gave us nothing"
 * (FINRA, FRED, order books, derivatives) rather than for a server fault.
 * That is a degraded dependency, not a bug in JARVIS, and it reads
 * differently to an operator deciding whether to trust the screen.
 */
export function classify(e: unknown): { state: DataState; detail: string; status: number | null } {
  if (e instanceof ApiError) {
    const where = `${e.path}`;
    if (e.status === 0) {
      return {
        state: "error",
        detail: `API unreachable — the JARVIS server may be down (${where})`,
        status: 0,
      };
    }
    if (e.status === 401 || e.status === 403) {
      return {
        state: "not_configured",
        detail: e.detail ?? `Not authorized for ${where} — credentials missing or rejected`,
        status: e.status,
      };
    }
    if (e.status === 404 || e.status === 501) {
      return {
        state: "unsupported",
        detail: e.detail ?? `${where} is not available on this deployment`,
        status: e.status,
      };
    }
    if (e.status === 503) {
      // The backend's own word for "the upstream had nothing for us".
      return {
        state: "degraded",
        detail: e.detail ?? `Upstream unavailable (${where})`,
        status: 503,
      };
    }
    if (e.status === 429) {
      return { state: "degraded", detail: `Rate limited on ${where} — backing off`, status: 429 };
    }
    return {
      state: "error",
      detail: e.detail ?? `Request failed (${e.status}) on ${where}`,
      status: e.status,
    };
  }
  return {
    state: "error",
    detail: e instanceof Error ? e.message : String(e),
    status: null,
  };
}

export function stateLabel(s: DataState): string {
  switch (s) {
    case "loading": return "LOADING";
    case "ready": return "READY";
    case "empty": return "EMPTY";
    case "stale": return "STALE";
    case "degraded": return "DEGRADED";
    case "error": return "ERROR";
    case "not_configured": return "NOT CONFIGURED";
    case "unsupported": return "UNSUPPORTED";
  }
}

/** Colour role per state. Only the states worth interrupting for are loud. */
export function stateColor(s: DataState): string {
  switch (s) {
    case "ready": return "var(--good)";
    case "empty": return "var(--ink-faint)";
    case "loading": return "var(--ink-faint)";
    case "stale": return "var(--warm)";
    case "degraded": return "var(--warm)";
    case "not_configured": return "var(--ink-dim)";
    case "unsupported": return "var(--ink-dim)";
    case "error": return "var(--bad)";
  }
}

export function ageText(since: number): string {
  const s = Math.max(0, Math.round((Date.now() - since) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.round(m / 60)}h ago`;
}

/**
 * Per-feed load state for one section.
 *
 * A section makes one tracker and routes each request through `load`. On
 * failure the previous value is returned unchanged, so the panel keeps
 * showing the last good data rather than flashing an empty state — and the
 * status records that what is on screen is now stale, with its age.
 */
export class FeedTracker {
  feeds = $state<Record<string, FeedStatus>>({});

  /**
   * Last successful value per feed, held OUTSIDE the reactive graph on
   * purpose.
   *
   * The obvious API was `load(key, fn, prev)` with the caller passing its own
   * `$state` variable. That reads the state synchronously at the call site —
   * and since these loaders run inside an `$effect` that then writes those
   * same variables, every load re-triggered the effect: measured at 34
   * requests in 10 seconds against a 20-second poll. Keeping last-good in a
   * plain Map means the loader reads nothing reactive, so the effect depends
   * only on what the section actually wants to re-run on.
   */
  #lastGood = new Map<string, unknown>();

  status(key: string): FeedStatus {
    return this.feeds[key] ?? INITIAL;
  }

  /** The last value this feed returned successfully, or null. */
  lastGood<T>(key: string): T | null {
    return (this.#lastGood.get(key) as T | undefined) ?? null;
  }

  state(key: string): DataState {
    return this.status(key).state;
  }

  private set(key: string, patch: Partial<FeedStatus>) {
    this.feeds = { ...this.feeds, [key]: { ...this.status(key), ...patch } };
  }

  /**
   * Run one request and record what happened.
   *
   * On a transient failure the last good value for this key is returned
   * unchanged and the feed goes STALE, so the panel keeps showing what was
   * true a moment ago instead of flashing empty. A feed that has never loaded
   * shows the hard failure state, because there is nothing else to show, and
   * terminal failures (not configured, unsupported) always report themselves
   * — those do not become less true with age.
   *
   * Pass `{ keepLast: false }` where showing yesterday's answer would be
   * worse than showing none: a chart still labelled BTC while drawing the
   * previous symbol's candles misinforms in a way an empty chart does not.
   */
  async load<T>(key: string, fn: () => Promise<T>, opts?: { keepLast?: boolean }): Promise<T | null> {
    const keepLast = opts?.keepLast !== false;
    try {
      const v = await fn();
      const empty = isEmptyPayload(v);
      // A payload that says `configured: false` is the backend telling us the
      // provider was never set up. That is not an empty result.
      const unconfigured =
        v != null && typeof v === "object" && (v as Record<string, unknown>).configured === false;
      this.set(key, {
        state: unconfigured ? "not_configured" : empty ? "empty" : "ready",
        detail: unconfigured ? "Not configured — credentials missing" : "",
        lastGoodAt: Date.now(),
        failures: 0,
        status: null,
      });
      this.#lastGood.set(key, v);
      return v;
    } catch (e) {
      const { state, detail, status } = classify(e);
      const prior = this.status(key);
      const prev = keepLast ? (this.#lastGood.get(key) as T | undefined) : undefined;
      // STALE means "what you are looking at was true a moment ago". It needs
      // an actual prior success to be true — an initial value that never came
      // from the server is not stale data, it is no data, and calling it
      // stale would dress a hard failure up as a mild one.
      const keepable = prev != null && prior.lastGoodAt != null && !TERMINAL.has(state);
      this.set(key, {
        state: keepable ? "stale" : state,
        detail: keepable
          ? `${detail} — showing data from ${ageText(prior.lastGoodAt!)}`
          : detail,
        failures: prior.failures + 1,
        status,
      });
      return keepable ? prev : null;
    }
  }

  /** Feeds that are not currently showing trustworthy fresh data. */
  get troubled(): string[] {
    return Object.entries(this.feeds)
      .filter(([, f]) => f.state === "stale" || f.state === "degraded" || f.state === "error")
      .map(([k]) => k);
  }

  /** The worst state across every tracked feed — for a section-level badge. */
  get worst(): DataState {
    const rank: DataState[] = [
      "ready", "empty", "loading", "not_configured", "unsupported", "stale", "degraded", "error",
    ];
    let worst: DataState = "ready";
    for (const f of Object.values(this.feeds)) {
      if (rank.indexOf(f.state) > rank.indexOf(worst)) worst = f.state;
    }
    return worst;
  }
}
