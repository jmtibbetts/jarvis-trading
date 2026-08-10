<script lang="ts">
  import { api, type PlatformConfig } from "../api";
  import { toastStore } from "../stores/toast.svelte";

  let { configs, onSaved }: { configs: PlatformConfig[]; onSaved: () => void } = $props();

  const existing = $derived(configs.find((c) => c.platform === "telegram"));

  let token = $state("");
  let chatId = $state("");
  let showToken = $state(false);
  let detecting = $state(false);
  let saving = $state(false);
  let resultMsg = $state<{ tone: "good" | "bad" | "working"; text: string } | null>(null);

  async function detectChat() {
    if (!token.trim()) {
      resultMsg = { tone: "bad", text: "Enter a bot token first." };
      return;
    }
    detecting = true;
    resultMsg = { tone: "working", text: "Looking for a recent /start message…" };
    try {
      const res = await api.telegramDetectChat({ bot_token: token, config_id: existing?.id });
      chatId = res.chat_id;
      resultMsg = { tone: "good", text: `Found chat: ${res.chat_name} (${res.chat_id})` };
    } catch (e) {
      resultMsg = { tone: "bad", text: `${e}` };
    } finally {
      detecting = false;
    }
  }

  async function saveAndTest() {
    if (!token.trim() && !existing) {
      resultMsg = { tone: "bad", text: "Enter a bot token first." };
      return;
    }
    saving = true;
    resultMsg = { tone: "working", text: "Saving and sending a test message…" };
    try {
      const body = { label: "Telegram", platform: "telegram", api_key: token || undefined, extra_field_1: chatId, is_active: true };
      if (existing) {
        await api.settingsUpdate(existing.id, body);
      } else {
        await api.settingsCreate(body);
      }
      const testRes = await api.telegramTest({ bot_token: token, chat_id: chatId, config_id: existing?.id });
      resultMsg = { tone: "good", text: `Connected to @${testRes.bot_username} — test message sent.` };
      toastStore.ok("Telegram connected");
      onSaved();
    } catch (e) {
      resultMsg = { tone: "bad", text: `${e}` };
    } finally {
      saving = false;
    }
  }
</script>

<div class="wizard">
  <div class="wiz-head">
    <div>
      <h3><span class="tg-icon">✈</span> Telegram Bot Setup</h3>
      <p>Send Jarvis signals, position updates, and threat alerts to one private Telegram chat.</p>
    </div>
    <span class="badge" class:configured={!!existing}>{existing ? "Configured" : "Not configured"}</span>
  </div>

  <div class="wiz-body">
    <ol class="steps">
      <li>Open <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">@BotFather ↗</a> in Telegram and tap Start.</li>
      <li>Send <code>/newbot</code>, choose a display name, then a username ending in <code>bot</code>.</li>
      <li>Copy the HTTP API token BotFather gives you and paste it below.</li>
      <li>Open your new bot, tap Start, and send <code>/start</code>.</li>
      <li>Click Detect Chat ID, then Save &amp; Send Test.</li>
    </ol>

    <div class="form">
      <label for="tg-token">Bot Token</label>
      <div class="token-row">
        <input id="tg-token" type={showToken ? "text" : "password"} bind:value={token} placeholder={existing ? "•••••••• (saved — leave blank to keep)" : "123456789:AA..."} />
        <button class="btn tiny" onclick={() => (showToken = !showToken)}>{showToken ? "Hide" : "Show"}</button>
      </div>

      <label for="tg-chat">Chat ID</label>
      <div class="token-row">
        <input id="tg-chat" bind:value={chatId} placeholder="123456789" />
        <button class="btn tiny outline" disabled={detecting} onclick={detectChat}>{detecting ? "…" : "Detect Chat ID"}</button>
      </div>

      <button class="btn primary" disabled={saving} onclick={saveAndTest}>{saving ? "Working…" : "Save & Send Test"}</button>

      {#if resultMsg}
        <div class="result {resultMsg.tone}">{resultMsg.text}</div>
      {/if}

      <div class="commands">
        After setup: <code>/signals</code> <code>/paper</code> <code>/positions</code> <code>/threats</code> <code>/regime</code>
        <code>/pnl</code> <code>/risk</code> <code>/status</code> <code>/help</code>
      </div>
    </div>
  </div>
</div>

<style>
  .wizard {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 14px;
  }
  .wiz-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 10px;
    margin-bottom: 12px;
  }
  .wiz-head h3 {
    margin: 0 0 4px;
    font-size: 14px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .tg-icon {
    color: var(--accent);
  }
  .wiz-head p {
    margin: 0;
    font-size: 11.5px;
    color: var(--ink-faint);
    max-width: 46ch;
  }
  .badge {
    font-size: 10px;
    padding: 3px 8px;
    border-radius: 100px;
    background: var(--surface-raised);
    color: var(--ink-faint);
    white-space: nowrap;
  }
  .badge.configured {
    background: rgba(61, 220, 151, 0.12);
    color: var(--good);
  }

  .wiz-body {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
  }
  .steps {
    margin: 0;
    padding-left: 18px;
    font-size: 11.5px;
    color: var(--ink-dim);
    line-height: 1.7;
  }
  .steps a {
    color: var(--accent);
  }

  .form label {
    display: block;
    font-size: 10.5px;
    color: var(--ink-dim);
    margin-bottom: 4px;
    margin-top: 8px;
  }
  .form label:first-child {
    margin-top: 0;
  }
  .token-row {
    display: flex;
    gap: 6px;
  }
  input {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    color: var(--ink);
    padding: 6px 9px;
    font-size: 12px;
    font-family: var(--mono);
    min-width: 0;
  }
  .btn {
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    color: var(--ink);
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 11px;
    cursor: pointer;
    white-space: nowrap;
  }
  .btn.tiny {
    padding: 5px 9px;
  }
  .btn.outline {
    background: transparent;
    border-color: var(--accent);
    color: var(--accent);
  }
  .btn.primary {
    width: 100%;
    margin-top: 10px;
    background: rgba(124, 154, 255, 0.15);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
    padding: 8px;
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .result {
    margin-top: 10px;
    padding: 7px 10px;
    border-radius: 6px;
    font-size: 11px;
  }
  .result.good {
    background: rgba(61, 220, 151, 0.08);
    border: 1px solid rgba(61, 220, 151, 0.3);
    color: var(--good);
  }
  .result.bad {
    background: rgba(255, 92, 114, 0.08);
    border: 1px solid rgba(255, 92, 114, 0.3);
    color: var(--bad);
  }
  .result.working {
    background: rgba(255, 180, 84, 0.08);
    border: 1px solid rgba(255, 180, 84, 0.3);
    color: var(--warm);
  }

  .commands {
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
    font-size: 10.5px;
    color: var(--ink-faint);
    line-height: 1.8;
  }
  .commands code {
    margin-right: 3px;
  }

  @media (max-width: 700px) {
    .wiz-body {
      grid-template-columns: 1fr;
    }
  }
</style>
