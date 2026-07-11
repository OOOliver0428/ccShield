/**
 * BanlistWS tests (T19).
 *
 * Mirrors the BridgeWS test pattern (T14): a deterministic in-process
 * WebSocket fake drives open / message / close so we can assert the
 * dispatch + reconnect schedule without real sockets.
 *
 * The dispatch contract is verified by spying on the ban store's
 * actions: snapshot → ``applySnapshot``, ``ban_added`` → ``addBan``,
 * ``ban_removed`` → ``removeBan``. Malformed payloads surface as
 * ``onError`` calls (not exceptions), matching BridgeWS.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { BanlistWS } from "./banWs";
import { useBanStore, type BanListMessage } from "../stores/ban";

class FakeMessageEvent<T = string> extends Event {
  data: T;
  constructor(type: string, init: { data: T }) {
    super(type);
    this.data = init.data;
  }
}

class FakeCloseEvent extends Event {
  code: number;
  reason: string;
  wasClean: boolean;
  constructor(code = 1006, reason = "") {
    super("close");
    this.code = code;
    this.reason = reason;
    this.wasClean = false;
  }
}

interface FakeWSHandle {
  url: string;
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: FakeMessageEvent) => void) | null;
  onclose: ((ev: FakeCloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  simulateOpen(): void;
  simulateMessage(data: string): void;
  simulateClose(): void;
  close(): void;
}

let fakeInstances: FakeWSHandle[] = [];

function makeFakeWebSocket(): { new (url: string): FakeWSHandle } {
  return class {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    url: string;
    readyState = 0;
    onopen: ((ev: Event) => void) | null = null;
    onmessage: ((ev: FakeMessageEvent) => void) | null = null;
    onclose: ((ev: FakeCloseEvent) => void) | null = null;
    onerror: ((ev: Event) => void) | null = null;
    constructor(url: string) {
      this.url = url;
      fakeInstances.push(this as unknown as FakeWSHandle);
    }
    close(): void {
      this.readyState = 3;
      if (this.onclose) this.onclose(new FakeCloseEvent());
    }
    simulateOpen(): void {
      this.readyState = 1;
      if (this.onopen) this.onopen(new Event("open"));
    }
    simulateMessage(data: string): void {
      if (this.onmessage) {
        this.onmessage(new FakeMessageEvent("message", { data }));
      }
    }
    simulateClose(): void {
      this.readyState = 3;
      if (this.onclose) this.onclose(new FakeCloseEvent());
    }
  } as unknown as { new (url: string): FakeWSHandle };
}

describe("BanlistWS", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    fakeInstances = [];
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", makeFakeWebSocket());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("opens a WS to /api/ws/rooms/{roomId}/banlist?token=...", () => {
    const ws = new BanlistWS(22210347, "local-tok");
    ws.connect();

    expect(fakeInstances).toHaveLength(1);
    expect(fakeInstances[0]!.url).toContain("/api/ws/rooms/22210347/banlist");
    expect(fakeInstances[0]!.url).toContain("token=local-tok");

    ws.close();
  });

  it("dispatches a snapshot message to banStore.applySnapshot", () => {
    const ws = new BanlistWS(1, "tok");
    ws.connect();
    fakeInstances[0]!.simulateOpen();

    const snapshot: BanListMessage = {
      event: "snapshot",
      bans: [
        { uid: 11, uname: "alice" },
        { uid: 22, uname: "bob" },
      ],
    };
    fakeInstances[0]!.simulateMessage(JSON.stringify(snapshot));

    const store = useBanStore();
    expect(store.banList.map((b) => b.uid).sort()).toEqual([11, 22]);

    ws.close();
  });

  it("dispatches ban_added to banStore.addBan", () => {
    const ws = new BanlistWS(1, "tok");
    ws.connect();
    fakeInstances[0]!.simulateOpen();

    fakeInstances[0]!.simulateMessage(
      JSON.stringify({
        event: "ban_added",
        ban: { uid: 33, uname: "carol", hour: 1, block_id: 33 },
      }),
    );

    const store = useBanStore();
    expect(store.banList).toHaveLength(1);
    expect(store.banList[0]?.uid).toBe(33);
    expect(store.banList[0]?.block_id).toBe(33);

    ws.close();
  });

  it("dispatches ban_removed to banStore.removeBan", () => {
    const ws = new BanlistWS(1, "tok");
    ws.connect();
    fakeInstances[0]!.simulateOpen();

    const store = useBanStore();
    store.applySnapshot([
      { uid: 11, uname: "alice" },
      { uid: 22, uname: "bob" },
    ]);

    fakeInstances[0]!.simulateMessage(
      JSON.stringify({ event: "ban_removed", uid: 11 }),
    );

    expect(store.banList.map((b) => b.uid)).toEqual([22]);

    ws.close();
  });

  it("ignores malformed payloads (no throw, no state mutation)", () => {
    const ws = new BanlistWS(1, "tok");
    ws.connect();
    fakeInstances[0]!.simulateOpen();
    fakeInstances[0]!.simulateMessage("not-json-{");

    const store = useBanStore();
    expect(store.banList).toHaveLength(0);

    ws.close();
  });

  it("ignores unknown event types (forward-compat)", () => {
    const ws = new BanlistWS(1, "tok");
    ws.connect();
    fakeInstances[0]!.simulateOpen();
    fakeInstances[0]!.simulateMessage(
      JSON.stringify({ event: "future_event", foo: "bar" }),
    );

    const store = useBanStore();
    expect(store.banList).toHaveLength(0);

    ws.close();
  });

  it("schedules a reconnect on close with exponential backoff", async () => {
    const ws = new BanlistWS(1, "tok");
    ws.connect();

    fakeInstances[0]!.simulateClose();
    expect(fakeInstances).toHaveLength(1);

    // 3s first backoff.
    await vi.advanceTimersByTimeAsync(2_999);
    expect(fakeInstances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fakeInstances).toHaveLength(2);

    // 6s second backoff.
    fakeInstances[1]!.simulateClose();
    await vi.advanceTimersByTimeAsync(5_999);
    expect(fakeInstances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(fakeInstances).toHaveLength(3);

    ws.close();
  });

  it("close() cancels any pending retry timer", async () => {
    const ws = new BanlistWS(1, "tok");
    ws.connect();
    fakeInstances[0]!.simulateClose();

    ws.close();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(fakeInstances).toHaveLength(1);
  });

  // F3 / Bug 4 regression — Intentional close() must NOT schedule a
  // reconnect (banlist panel stays cleared after disconnect).
  it("close() suppresses the onclose-driven reconnect schedule", async () => {
    const ws = new BanlistWS(1, "tok");
    ws.connect();
    fakeInstances[0]!.simulateOpen();

    // User-initiated close — FakeWS.close() fires onclose.
    ws.close();
    expect(fakeInstances).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(fakeInstances).toHaveLength(1);
  });
});
