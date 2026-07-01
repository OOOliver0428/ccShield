/**
 * BridgeWS tests (T14).
 *
 * WebSocket is not provided by jsdom, so we stub it globally with a
 * deterministic fake whose lifecycle is driven by the test (no real
 * network, no real timers beyond ``vi.useFakeTimers()``). The fake
 * exposes ``simulate*`` helpers so we can fire open/message/close in
 * a controlled order — essential for asserting reconnect intervals.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { BridgeWS, type BridgeEvent } from "./ws";

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
  closed: boolean;
  sent: string[];
  close(): void;
  send(data: string): void;
  simulateOpen(): void;
  simulateMessage(data: string): void;
  simulateClose(): void;
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
    closed = false;
    sent: string[] = [];
    constructor(url: string) {
      this.url = url;
      const handle = this as unknown as FakeWSHandle;
      fakeInstances.push(handle);
    }
    close(): void {
      this.closed = true;
      this.readyState = 3;
      if (this.onclose) this.onclose(new FakeCloseEvent());
    }
    send(data: string): void {
      this.sent.push(data);
    }
    simulateOpen(): void {
      this.readyState = 1;
      if (this.onopen) this.onopen(new Event("open"));
    }
    simulateMessage(data: string): void {
      if (this.onmessage) this.onmessage(new FakeMessageEvent("message", { data }));
    }
    simulateClose(): void {
      this.readyState = 3;
      if (this.onclose) this.onclose(new FakeCloseEvent());
    }
  } as unknown as { new (url: string): FakeWSHandle };
}

describe("BridgeWS", () => {
  beforeEach(() => {
    fakeInstances = [];
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", makeFakeWebSocket());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("opens a WS to /api/ws/rooms/{roomId}?token=... and parses onMessage JSON", () => {
    const received: BridgeEvent[] = [];
    const ws = new BridgeWS(
      22210347,
      "local-token-abc",
      {
        onMessage: (e): void => {
          received.push(e);
        },
        onDisconnect: (): void => {},
        onError: (): void => {},
      },
    );
    ws.connect();

    expect(fakeInstances).toHaveLength(1);
    expect(fakeInstances[0]!.url).toContain("/api/ws/rooms/22210347");
    expect(fakeInstances[0]!.url).toContain("token=local-token-abc");

    // Encode special chars: test passes a token with spaces/dashes;
    // URLSearchParams will percent-encode them.
    fakeInstances[0]!.simulateOpen();
    fakeInstances[0]!.simulateMessage(
      JSON.stringify({
        type: "danmaku",
        uid: 1,
        uname: "alice",
        text: "hello",
        ts: 100,
        guard_level: 0,
        medal: null,
      }),
    );

    expect(received).toHaveLength(1);
    expect(received[0]).toMatchObject({ type: "danmaku", uname: "alice", text: "hello" });

    ws.close();
  });

  it("schedules a reconnect after onclose, fires again after backoff", async () => {
    const ws = new BridgeWS(1, "tok", {
      onMessage: (): void => {},
      onDisconnect: (): void => {},
      onError: (): void => {},
    });
    ws.connect();

    expect(fakeInstances).toHaveLength(1);
    fakeInstances[0]!.simulateClose();
    // The reconnect timer is scheduled but hasn't fired yet.
    expect(fakeInstances).toHaveLength(1);

    // First backoff is 3s.
    await vi.advanceTimersByTimeAsync(2_999);
    expect(fakeInstances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fakeInstances).toHaveLength(2);

    // Second backoff is 6s.
    fakeInstances[1]!.simulateClose();
    expect(fakeInstances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(5_999);
    expect(fakeInstances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(fakeInstances).toHaveLength(3);

    ws.close();
  });

  it("caps at 5 reconnect attempts then gives up", async () => {
    const ws = new BridgeWS(1, "tok", {
      onMessage: (): void => {},
      onDisconnect: (): void => {},
      onError: (): void => {},
    });
    ws.connect();

    const backoffs = [3_000, 6_000, 12_000, 24_000, 30_000];

    for (let i = 0; i < backoffs.length; i++) {
      fakeInstances[fakeInstances.length - 1]!.simulateClose();
      await vi.advanceTimersByTimeAsync(backoffs[i]! + 1);
    }

    expect(fakeInstances).toHaveLength(6);

    fakeInstances[fakeInstances.length - 1]!.simulateClose();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(fakeInstances).toHaveLength(6);

    ws.close();
  });

  it("resets the retry counter after a successful open", async () => {
    const ws = new BridgeWS(1, "tok", {
      onMessage: (): void => {},
      onDisconnect: (): void => {},
      onError: (): void => {},
    });
    ws.connect();

    // Open cleanly → counter resets.
    fakeInstances[0]!.simulateOpen();
    // Now close.
    fakeInstances[0]!.simulateClose();
    // First backoff from a fresh budget should be 3s, not 6s.
    await vi.advanceTimersByTimeAsync(2_999);
    expect(fakeInstances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fakeInstances).toHaveLength(2);

    ws.close();
  });

  it("close() cancels any pending retry timer", async () => {
    const ws = new BridgeWS(1, "tok", {
      onMessage: (): void => {},
      onDisconnect: (): void => {},
      onError: (): void => {},
    });
    ws.connect();

    fakeInstances[0]!.simulateClose();
    // The 3s retry timer is pending; calling close() must cancel it.
    ws.close();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(fakeInstances).toHaveLength(1);
  });

  it("invokes onDisconnect and onError callbacks", () => {
    let disconnects = 0;
    let errors = 0;
    const ws = new BridgeWS(1, "tok", {
      onMessage: (): void => {},
      onDisconnect: (): void => {
        disconnects += 1;
      },
      onError: (): void => {
        errors += 1;
      },
    });
    ws.connect();
    fakeInstances[0]!.simulateOpen();
    if (fakeInstances[0]!.onerror) fakeInstances[0]!.onerror(new Event("error"));
    fakeInstances[0]!.simulateClose();

    expect(errors).toBe(1);
    expect(disconnects).toBe(1);

    ws.close();
  });

  it("falls back to onError when a payload cannot be parsed as JSON", () => {
    let errors = 0;
    const ws = new BridgeWS(1, "tok", {
      onMessage: (): void => {},
      onDisconnect: (): void => {},
      onError: (): void => {
        errors += 1;
      },
    });
    ws.connect();
    fakeInstances[0]!.simulateOpen();
    fakeInstances[0]!.simulateMessage("not-json-{");

    expect(errors).toBe(1);

    ws.close();
  });

  // F3 / Bug 4 regression — Intentional close() must NOT fire onDisconnect
  // (no reconnect scheduled either). Without the guard, the UI shows a
  // stale "正在重连" banner after the user clicks "断开".
  it("close() suppresses the onclose-driven onDisconnect + scheduleReconnect", async () => {
    let disconnects = 0;
    const ws = new BridgeWS(1, "tok", {
      onMessage: (): void => {},
      onDisconnect: (): void => {
        disconnects += 1;
      },
      onError: (): void => {},
    });
    ws.connect();
    fakeInstances[0]!.simulateOpen();

    // User-initiated close — the FakeWS.close() fires onclose.
    ws.close();
    expect(disconnects).toBe(0);
    expect(fakeInstances).toHaveLength(1);

    // No reconnect should be scheduled.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fakeInstances).toHaveLength(1);
  });

  // Existing reconnect-on-unintentional-close contract still holds.
  it("simulateClose() (unintentional) still fires onDisconnect and reconnects", async () => {
    let disconnects = 0;
    const ws = new BridgeWS(1, "tok", {
      onMessage: (): void => {},
      onDisconnect: (): void => {
        disconnects += 1;
      },
      onError: (): void => {},
    });
    ws.connect();
    fakeInstances[0]!.simulateClose();

    expect(disconnects).toBe(1);

    // Reconnect is scheduled — first backoff is 3s.
    await vi.advanceTimersByTimeAsync(3_001);
    expect(fakeInstances.length).toBeGreaterThanOrEqual(2);

    ws.close();
  });
});