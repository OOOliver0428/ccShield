/**
 * BanlistWS — frontend WebSocket client for the ban-list bridge.
 *
 * Connects to ``/api/ws/rooms/{room_id}/banlist?token=<local_token>``
 * and dispatches every push (``snapshot`` / ``ban_added`` /
 * ``ban_removed``) directly into the Pinia :class:`useBanStore`. The
 * transport layer never touches the UI; the UI only ever reads from
 * the store.
 *
 * Why this is not polling: polling ``GET /api/ban-list/{room_id}`` every
 * 2–3s and re-applying the result would cause out-of-order updates
 * (bans the operator just issued showed up one tick late), wasted
 * CPU, and—worst—duplicated moderation state across tabs. We do
 * exactly the opposite: one WS per room, server pushes deltas, store
 * is the single source of truth.
 *
 * Reconnect policy mirrors :class:`BridgeWS`: 3s → 6s → 12s →
 * 24s → 30s cap, max 5 attempts, counter resets on a successful open,
 * ``close()`` is the kill switch that cancels pending retries.
 */
import { useBanStore } from "../stores/ban";
import type { BanListMessage } from "../stores/ban";

/** Backoff schedule — index = retry number (0-based). */
const BACKOFF_MS: readonly number[] = [3_000, 6_000, 12_000, 24_000, 30_000];
const MAX_RETRIES: number = BACKOFF_MS.length; // 5

export class BanlistWS {
  private readonly roomId: number;
  private readonly token: string;
  private ws: WebSocket | null = null;
  private retryCount: number = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private closed: boolean = false;

  constructor(roomId: number, token: string) {
    this.roomId = roomId;
    this.token = token;
  }

  /** Open the WebSocket. No-op if already open or if close() was called. */
  connect(): void {
    if (this.closed) return;
    if (this.ws !== null) return;
    const wsBase: string = computeWsBase();
    const url: string = `${wsBase}/api/ws/rooms/${this.roomId}/banlist?token=${encodeURIComponent(this.token)}`;
    const ws: WebSocket = new WebSocket(url);
    this.ws = ws;

    ws.onopen = (): void => {
      // Successful handshake resets the backoff budget.
      this.retryCount = 0;
    };
    ws.onmessage = (ev: MessageEvent<string>): void => {
      this.dispatch(ev.data);
    };
    ws.onerror = (): void => {
      // Error path is handled implicitly via the close that follows.
    };
    ws.onclose = (): void => {
      this.ws = null;
      this.scheduleReconnect();
    };
  }

  /** Permanently close the WS and cancel any pending retry. Idempotent. */
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

  private dispatch(payload: string): void {
    let parsed: BanListMessage;
    try {
      const raw: unknown = JSON.parse(payload);
      // Cheap shape guard — drop anything that isn't one of our three
      // known event types. Forward-compatible: a future event arrives
      // here as a no-op rather than crashing the panel.
      if (
        raw === null ||
        typeof raw !== "object" ||
        typeof (raw as { event?: unknown }).event !== "string"
      ) {
        return;
      }
      parsed = raw as BanListMessage;
    } catch {
      return;
    }
    const store = useBanStore();
    switch (parsed.event) {
      case "snapshot":
        store.applySnapshot(parsed.bans);
        return;
      case "ban_added":
        store.addBan(parsed.ban);
        return;
      case "ban_removed":
        store.removeBan(parsed.uid);
        return;
      default:
        return;
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
