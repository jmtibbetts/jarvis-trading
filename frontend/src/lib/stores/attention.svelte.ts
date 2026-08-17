/**
 * ONE fetch of the attention queue, shared by every panel that reads it.
 *
 * The Attention Queue panel and the At-Risk Positions panel are two views of
 * the same answer — the second is the POSITION_RISK slice of the first. Two
 * components each fetching it would poll twice and, worse, could disagree:
 * one panel showing four at-risk positions beside another showing three is
 * the exact class of "which number do I believe" the Command Center rework
 * exists to remove.
 *
 * So the fetch lives here, once, and the panels are pure views of it.
 */
import { api, type AttentionQueue } from "../api";
import { FeedTracker } from "../dataState.svelte";

// Attention is a claim about the PRESENT. Snapshot-serving it would make a
// stale queue indistinguishable from a quiet desk, so it is polled.
const POLL_MS = 45_000;

class AttentionStore {
  queue = $state<AttentionQueue | null>(null);
  feeds = new FeedTracker();

  #timer: ReturnType<typeof setInterval> | null = null;
  #subscribers = 0;

  async load() {
    this.queue = await this.feeds.load("attention", () => api.attention(60));
  }

  /** Called by each mounted consumer; polling runs while at least one is
   *  alive and stops when the last unmounts. Returns the teardown. */
  subscribe(): () => void {
    this.#subscribers += 1;
    if (this.#subscribers === 1) {
      this.load();
      this.#timer = setInterval(() => this.load(), POLL_MS);
    }
    return () => {
      this.#subscribers -= 1;
      if (this.#subscribers <= 0 && this.#timer !== null) {
        clearInterval(this.#timer);
        this.#timer = null;
      }
    };
  }

  /** Items in one category, in the queue's own ranked order. */
  inCategory(category: string) {
    return (this.queue?.items ?? []).filter((i) => i.category === category);
  }

  get status() {
    return this.feeds.status("attention");
  }
}

export const attentionStore = new AttentionStore();
