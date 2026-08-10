// Thin reconnecting WebSocket client. The backend broadcasts a small typed
// envelope — {type, ts, data} — over one shared connection; components
// subscribe to the types they care about instead of each polling their own
// endpoint. See app/ws.py for the server side and the message types it emits.

export type WsEnvelope = { type: string; ts: string; data: unknown };
type Listener = (msg: WsEnvelope) => void;

const WS_PATH = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;

class WsStore {
  connected = $state(false);
  lastMessageAt = $state<string | null>(null);

  private socket: WebSocket | null = null;
  private listeners = new Map<string, Set<Listener>>();
  private reconnectDelay = RECONNECT_BASE_MS;
  private reconnectTimer: number | undefined;

  connect() {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const socket = new WebSocket(WS_PATH);
    this.socket = socket;

    socket.onopen = () => {
      this.connected = true;
      this.reconnectDelay = RECONNECT_BASE_MS;
    };

    socket.onmessage = (event) => {
      let msg: WsEnvelope;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      this.lastMessageAt = msg.ts ?? new Date().toISOString();
      const forType = this.listeners.get(msg.type);
      forType?.forEach((fn) => fn(msg));
      const forAll = this.listeners.get("*");
      forAll?.forEach((fn) => fn(msg));
    };

    socket.onclose = () => {
      this.connected = false;
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      socket.close();
    };
  }

  private scheduleReconnect() {
    window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectDelay = Math.min(this.reconnectDelay * 1.6, RECONNECT_MAX_MS);
      this.connect();
    }, this.reconnectDelay);
  }

  on(type: string, fn: Listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(fn);
    return () => this.listeners.get(type)?.delete(fn);
  }
}

export const wsStore = new WsStore();
