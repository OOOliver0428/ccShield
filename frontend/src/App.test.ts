/**
 * App.vue tests (T14).
 *
 * We stub ``WebSocket`` globally so the App.vue's BridgeWS lifecycle
 * is exercised against a deterministic fake — no real sockets, no
 * real timers beyond fake ones (we DON'T need fake timers here
 * because no reconnect cycles are triggered in these short tests).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "./__tests__/setup";
import { useAuthStore } from "./stores/auth";
import { useRoomStore } from "./stores/room";
import App from "./App.vue";

class FakeMessageEvent extends Event {
  data: string;
  constructor(type: string, data: string) {
    super(type);
    this.data = data;
  }
}

let fakeInstances: Array<{
  url: string;
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: FakeMessageEvent) => void) | null;
  onclose: ((ev: Event) => void) | null;
  onerror: ((ev: Event) => void) | null;
  close(): void;
  send(): void;
  simulateOpen(): void;
  simulateMessage(data: string): void;
}> = [];

function installFakeWebSocket(): void {
  fakeInstances = [];
  class FakeWS {
    static OPEN = 1;
    static CLOSED = 3;
    url: string;
    readyState = 0;
    onopen: ((ev: Event) => void) | null = null;
    onmessage: ((ev: FakeMessageEvent) => void) | null = null;
    onclose: ((ev: Event) => void) | null = null;
    onerror: ((ev: Event) => void) | null = null;
    constructor(url: string) {
      this.url = url;
      fakeInstances.push(this);
    }
    close(): void {
      this.readyState = 3;
      if (this.onclose) this.onclose(new Event("close"));
    }
    send(): void {}
    simulateOpen(): void {
      this.readyState = 1;
      if (this.onopen) this.onopen(new Event("open"));
    }
    simulateMessage(data: string): void {
      if (this.onmessage) this.onmessage(new FakeMessageEvent("message", data));
    }
  }
  vi.stubGlobal("WebSocket", FakeWS as unknown as typeof WebSocket);
}

describe("App.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    installFakeWebSocket();

    // Default /api/auth/bootstrap + /api/auth/status → authenticated.
    server.use(
      http.get("*/api/auth/bootstrap", () =>
        HttpResponse.json({ token: "local-tok" }),
      ),
      http.get("*/api/auth/status", () =>
        HttpResponse.json({ state: "authenticated" }),
      ),
      http.get("*/api/auth/qr/start", () =>
        HttpResponse.json({ qrcode_url: "data:image/png;base64,X", qrcode_key: "k" }),
      ),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "scanning" }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders RoomInput when authStore.status === 'authenticated'", async () => {
    const auth = useAuthStore();
    auth.status = "authenticated";
    auth.token = "local-tok";
    auth.userInfo = { uname: "tester", mid: 1 };

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.find('[data-testid="authenticated-shell"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="room-input"]').exists()).toBe(true);
  });

  it("renders DanmakuList after the room store is connected", async () => {
    server.use(
      http.post("*/api/rooms/start", () =>
        HttpResponse.json({ room_id: 22210347, title: "" }),
      ),
      http.post("*/api/rooms/stop", () => HttpResponse.json({ ok: true })),
      http.get("*/api/rooms/resolve", () =>
        HttpResponse.json({ room_id: 22210347, short_id: 22210347 }),
      ),
    );

    const auth = useAuthStore();
    auth.status = "authenticated";
    auth.token = "local-tok";
    auth.userInfo = { uname: "tester", mid: 1 };

    const wrapper = mount(App);
    await flushPromises();

    // Drive the connect flow through the RoomInput component.
    const room = useRoomStore();
    await room.resolve("22210347");
    await room.connect(22210347);
    await flushPromises();

    // Simulate the WS finishing the handshake.
    expect(fakeInstances.length).toBeGreaterThanOrEqual(1);
    fakeInstances[0]!.simulateOpen();
    await flushPromises();

    expect(wrapper.find('[data-testid="danmaku-list"]').exists()).toBe(true);
  });

  it("WS messages dispatch: danmaku event appends to danmakuStore", async () => {
    server.use(
      http.post("*/api/rooms/start", () =>
        HttpResponse.json({ room_id: 22210347, title: "" }),
      ),
      http.post("*/api/rooms/stop", () => HttpResponse.json({ ok: true })),
      http.get("*/api/rooms/resolve", () =>
        HttpResponse.json({ room_id: 22210347, short_id: 22210347 }),
      ),
    );

    const auth = useAuthStore();
    auth.status = "authenticated";
    auth.token = "local-tok";

    const wrapper = mount(App);
    void wrapper;
    await flushPromises();

    const room = useRoomStore();
    await room.resolve("22210347");
    await room.connect(22210347);
    await flushPromises();

    fakeInstances[0]!.simulateOpen();
    fakeInstances[0]!.simulateMessage(
      JSON.stringify({
        type: "danmaku",
        uid: 11,
        uname: "fan",
        text: "great stream!",
        ts: 1_700_000_000,
        guard_level: 0,
        medal: null,
      }),
    );
    await flushPromises();

    // Importing here keeps the top of the file dependency-light.
    const { useDanmakuStore } = await import("./stores/danmaku");
    const danmaku = useDanmakuStore();
    expect(danmaku.list).toHaveLength(1);
    expect(danmaku.list[0]?.uname).toBe("fan");
  });
});