/**
 * BridgeWS — frontend WebSocket client for the room event bridge.
 *
 * Wraps a single ``new WebSocket(...)`` against the backend's
 * ``/api/ws/rooms/{room_id}`` endpoint and surfaces a tiny callback
 * surface (``onMessage`` / ``onDisconnect`` / ``onError``) so the UI
 * layer never touches a raw ``WebSocket`` instance.
 *
 * Reconnect policy:
 *
 * * Exponential backoff — 3s, 6s, 12s, 24s, then cap at 30s.
 * * Up to ``MAX_RETRIES`` (5) attempts before giving up.
 * * The retry counter resets the moment a connection opens
 *   (``onopen``), so a single flapping connection does not eat into
 *   the budget for the next outage.
 * * ``close()`` is the explicit kill switch — the WS is closed AND
 *   any pending retry timer is cancelled (no zombie reconnect after
 *   the user clicks "断开").
 *
 * Auth: token is sent via the ``?token=`` query-string fallback so the
 * backend's ``LocalTokenMiddleware`` accepts the upgrade. The HTTP
 * REST client (``api/client.ts``) already attaches ``Authorization:
 * Bearer``; WS can't set custom headers after construction in a
 * browser, so we fall back to the query string per the spec.
 */
export type BridgeEventType =
  | "danmaku"
  | "sc"
  | "sc_delete"
  | "room_status"
  | "error";

export interface BridgeMessageEvent {
  type: "danmaku";
  uid: number;
  uname: string;
  text: string;
  ts: number;
  guard_level: number;
  medal: { name: string; level: number } | null;
}

export interface BridgeScEvent {
  type: "sc";
  id: string;
  uid: number;
  uname: string;
  text: string;
  price: number;
  ts: number;
  end_ts: number;
  duration: number;
  guard_level: number;
  medal: { name: string; level: number } | null;
  background_color: string;
  background_bottom_color: string;
  background_price_color: string;
  message_font_color: string;
}

export interface BridgeScDeleteEvent {
  type: "sc_delete";
  ids: string[];
}

export interface BridgeRoomStatusEvent {
  type: "room_status";
  status: "connected" | "disconnected" | "reconnecting" | "error";
}

export interface BridgeErrorEvent {
  type: "error";
  message: string;
}

export type BridgeEvent =
  | BridgeMessageEvent
  | BridgeScEvent
  | BridgeScDeleteEvent
  | BridgeRoomStatusEvent
  | BridgeErrorEvent;

export interface BridgeWSOptions {
  onMessage: (event: BridgeEvent) => void;
  onDisconnect: () => void;
  onError: () => void;
}

/** Backoff schedule — index = retry number (0-based). */
const BACKOFF_MS: readonly number[] = [3_000, 6_000, 12_000, 24_000, 30_000];
const MAX_RETRIES: number = BACKOFF_MS.length; // 5

export class BridgeWS {
  private readonly roomId: number;
  private readonly token: string;
  private readonly opts: BridgeWSOptions;
  private ws: WebSocket | null = null;
  private retryCount: number = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private closed: boolean = false;

  constructor(roomId: number, token: string, opts: BridgeWSOptions) {
    this.roomId = roomId;
    this.token = token;
    this.opts = opts;
  }

  /** Open the WebSocket. Idempotent — no-op if already connected. */
  connect(): void {
    if (this.closed) return;
    if (this.ws !== null) return;
    const wsBase: string = computeWsBase();
    const url: string = `${wsBase}/api/ws/rooms/${this.roomId}?token=${encodeURIComponent(this.token)}`;
    const ws: WebSocket = new WebSocket(url);
    this.ws = ws;

    ws.onopen = (): void => {
      // A successful open resets the backoff budget.
      this.retryCount = 0;
    };
    ws.onmessage = (ev: MessageEvent<string>): void => {
      let parsed: BridgeEvent;
      try {
        parsed = JSON.parse(ev.data) as BridgeEvent;
      } catch {
        // Malformed payload — surface as an error event so the UI can
        // log/ignore without crashing the whole stream.
        this.opts.onError();
        return;
      }
      this.opts.onMessage(parsed);
    };
    ws.onerror = (): void => {
      this.opts.onError();
    };
    ws.onclose = (): void => {
      this.ws = null;
      // A user-initiated close() must not raise the
      // "正在重连" banner — scheduleReconnect() guards on `closed` so
      // it won't reopen anyway, but onDisconnect() is a UI callback
      // (shows the banner). Guard it explicitly.
      if (this.closed) return;
      this.opts.onDisconnect();
      this.scheduleReconnect();
    };
  }

  /**
   * Permanently close the WebSocket and cancel any pending retry.
   * Safe to call multiple times.
   */
  close(): void {
    this.closed = true;
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    if (this.ws !== null) {
      this.ws.close();
      this.ws = null;
    }
  }

  private scheduleReconnect(): void {
    if (this.closed) return;
    if (this.retryCount >= MAX_RETRIES) return;
    const delay: number = BACKOFF_MS[this.retryCount] ?? BACKOFF_MS[BACKOFF_MS.length - 1]!;
    this.retryCount += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }
}

function computeWsBase(): string {
  const loc: Location | undefined =
    typeof window !== "undefined" ? window.location : undefined;
  if (loc === undefined) {
    return "ws://localhost:8000";
  }
  const protocol: "ws" | "wss" = loc.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${loc.host}`;
}
