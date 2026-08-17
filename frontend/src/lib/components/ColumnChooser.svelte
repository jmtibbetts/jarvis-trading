<script lang="ts">
  /**
   * Which columns a table shows, and in what order.
   *
   * Deliberately checkbox-and-arrows rather than drag-and-drop. Drag
   * reordering needs a pointer, a steady hand and a target the operator can
   * see — none of which are guaranteed on a dense trading page being driven
   * one-handed, and a mis-drop that silently reorders a table the operator
   * reads prices from is a worse failure than two extra clicks.
   *
   * Owns no state. The parent holds `order` and `hidden` because they belong
   * to the saved view, and a chooser that kept its own copy would drift from
   * whatever a preset just applied.
   */
  let {
    columns,
    order,
    hidden,
    onchange,
    onreset,
  }: {
    /** Every column that exists, keyed. */
    columns: { key: string; label: string; locked?: boolean }[];
    /** Column keys, in display order. Keys absent here fall in at the end. */
    order: string[];
    /** Column keys currently switched off. */
    hidden: string[];
    onchange: (next: { order: string[]; hidden: string[] }) => void;
    onreset: () => void;
  } = $props();

  let open = $state(false);

  // Unknown keys (a column added since the view was saved) append rather
  // than vanish: a saved view must not be able to hide a column the
  // operator has never seen and cannot know to look for.
  const ordered = $derived.by(() => {
    const known = new Map(columns.map((c) => [c.key, c]));
    const out = order.map((k) => known.get(k)).filter((c) => c !== undefined);
    for (const c of columns) if (!order.includes(c.key)) out.push(c);
    return out as { key: string; label: string; locked?: boolean }[];
  });

  const isHidden = (key: string) => hidden.includes(key);

  function toggle(key: string) {
    onchange({
      order: ordered.map((c) => c.key),
      hidden: isHidden(key) ? hidden.filter((k) => k !== key) : [...hidden, key],
    });
  }

  function move(key: string, delta: number) {
    const keys = ordered.map((c) => c.key);
    const i = keys.indexOf(key);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= keys.length) return;
    [keys[i], keys[j]] = [keys[j], keys[i]];
    onchange({ order: keys, hidden });
  }

  const shownCount = $derived(columns.length - hidden.length);
</script>

<div class="cc">
  <button class="btn small outline" onclick={() => (open = !open)} aria-expanded={open}>
    Columns {shownCount}/{columns.length}
  </button>

  {#if open}
    <!-- Click-away, so the popover does not sit over the table it configures. -->
    <button class="scrim" aria-label="Close column chooser" onclick={() => (open = false)}></button>
    <div class="pop" role="group" aria-label="Column chooser">
      <div class="pop-head">
        <b>Columns</b>
        <button class="reset" onclick={onreset}>reset</button>
      </div>
      <ul>
        {#each ordered as c, i (c.key)}
          <li>
            <label class:off={isHidden(c.key)}>
              <input
                type="checkbox"
                checked={!isHidden(c.key)}
                disabled={c.locked}
                onchange={() => toggle(c.key)}
              />
              <span>{c.label}</span>
            </label>
            <span class="moves">
              <button
                aria-label="Move {c.label} up"
                disabled={i === 0}
                onclick={() => move(c.key, -1)}>↑</button>
              <button
                aria-label="Move {c.label} down"
                disabled={i === ordered.length - 1}
                onclick={() => move(c.key, 1)}>↓</button>
            </span>
          </li>
        {/each}
      </ul>
      {#if columns.some((c) => c.locked)}
        <p class="note">Locked columns identify the row and cannot be hidden.</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .cc { position: relative; display: inline-block; }
  .scrim {
    position: fixed; inset: 0; z-index: 40;
    background: none; border: none; cursor: default; padding: 0;
  }
  .pop {
    position: absolute; top: calc(100% + 5px); left: 0; z-index: 41;
    width: 250px; max-height: 330px; overflow-y: auto;
    background: var(--surface-raised); border: 1px solid var(--line-bright);
    border-radius: 9px; padding: 9px 10px;
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.45);
  }
  .pop-head {
    display: flex; align-items: center; justify-content: space-between;
    font-size: 11.5px; margin-bottom: 6px;
  }
  .reset {
    background: none; border: none; color: var(--ink-faint);
    font-size: 10.5px; cursor: pointer; padding: 0; text-decoration: underline;
  }
  .reset:hover { color: var(--accent); }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 1px; }
  li { display: flex; align-items: center; gap: 6px; }
  label {
    display: flex; align-items: center; gap: 7px; flex: 1; min-width: 0;
    font-size: 11.5px; cursor: pointer; padding: 2px 0;
  }
  label.off span { color: var(--ink-faint); }
  label span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .moves { display: flex; gap: 1px; flex: none; }
  .moves button {
    background: none; border: 1px solid transparent; color: var(--ink-faint);
    border-radius: 4px; width: 18px; height: 18px; font-size: 10px;
    cursor: pointer; padding: 0; line-height: 1;
  }
  .moves button:hover:not(:disabled) { color: var(--accent); border-color: var(--line); }
  .moves button:disabled { opacity: 0.25; cursor: default; }
  .note { font-size: 10px; color: var(--ink-faint); margin: 7px 0 0; line-height: 1.4; }
</style>
