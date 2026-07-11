/**
 * App.vue tests — T19 ban-WS lifecycle additions.
 *
 * Verifies that on room-connect App.vue opens a second WS against
 * ``/api/ws/rooms/{roomId}/banlist`` (in addition to the bridge WS),
 * renders the BanList panel, and on disconnect closes it AND clears
 * the ban store. No real polling — every ban-store write is sourced
 * from the WS we opened.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "./__tests__/setup";
import { useAuthStore } from "./stores/auth";
import { useRoomStore } from "./stores/room";
import { useBanStore } from "./stores/ban";
import { useDanmakuStore } from "./stores/danmaku";
import App from "./App.vue";
import QrLogin from "./components/QrLogin.vue";

class FakeMessageEvent extends Event {
  data: string;
  constructor(type: string, data: string) {
    super(type);
    this.data = data;
  }
}

interface FakeWSHandle {
  url: string;
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: FakeMessageEvent) => void) | null;
  onclose: ((ev: Event) => void) | null;
  onerror: ((ev: Event) => void) | null;
  simulateOpen(): void;
  simulateMessage(data: string): void;
  simulateClose(): void;
  close(): void;
}

let fakeInstances: FakeWSHandle[] = [];

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
      fakeInstances.push(this as unknown as FakeWSHandle);
    }
    close(): void {
      this.readyState = 3;
      if (this.onclose) this.onclose(new Event("close"));
    }
    simulateOpen(): void {
      this.readyState = 1;
      if (this.onopen) this.onopen(new Event("open"));
    }
    simulateMessage(data: string): void {
      if (this.onmessage) {
        this.onmessage(new FakeMessageEvent("message", data));
      }
    }
    simulateClose(): void {
      this.readyState = 3;
      if (this.onclose) this.onclose(new Event("close"));
    }
  }
  vi.stubGlobal("WebSocket", FakeWS as unknown as typeof WebSocket);
}

describe("App.vue — ban-WS lifecycle (T19)", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    installFakeWebSocket();
    server.use(
      http.get("*/api/auth/bootstrap", () =>
        HttpResponse.json({ token: "local-tok" }),
      ),
      http.get("*/api/auth/status", () =>
        HttpResponse.json({ state: "authenticated" }),
      ),
      http.get("*/api/auth/qr/start", () =>
        HttpResponse.json({
          qrcode_url: "data:image/png;base64,X",
          qrcode_key: "k",
        }),
      ),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "scanning" }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens a BanlistWS after the room connects", async () => {
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
    void wrapper;
    await flushPromises();

    const room = useRoomStore();
    await room.resolve("22210347");
    await flushPromises();

    // Resolving only translates the id; /rooms/start has not installed a
    // backend RoomBridge yet, so neither local WS may open at this point.
    expect(fakeInstances).toHaveLength(0);

    await room.connect(22210347);
    await flushPromises();

    // First WS = bridge. Second = banlist.
    expect(fakeInstances.length).toBeGreaterThanOrEqual(2);
    const banWs = fakeInstances.find((w) =>
      w.url.includes("/api/ws/rooms/22210347/banlist"),
    );
    expect(banWs).toBeDefined();
    expect(banWs!.url).toContain("token=local-tok");
  });

  it("renders the BanList panel after the room connects", async () => {
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

    // Both bridge + banlist WS constructed; simulate open on both.
    fakeInstances.forEach((w) => w.simulateOpen());
    await flushPromises();

    expect(wrapper.find('[data-testid="ban-list"]').exists()).toBe(true);
  });

  it("routes active SC and delete events into the pinned block", async () => {
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
    await flushPromises();

    const room = useRoomStore();
    await room.resolve("22210347");
    await room.connect(22210347);
    await flushPromises();

    const bridgeWs = fakeInstances.find(
      (w) =>
        w.url.includes("/api/ws/rooms/22210347?") &&
        !w.url.includes("/banlist"),
    );
    expect(bridgeWs).toBeDefined();
    const now = Math.floor(Date.now() / 1000);
    bridgeWs!.simulateMessage(
      JSON.stringify({
        type: "sc",
        id: "sc-live",
        uid: 7,
        uname: "supporter",
        text: "醒目留言内容",
        price: 100,
        ts: now,
        end_ts: now + 300,
        duration: 300,
        guard_level: 3,
        medal: null,
        background_color: "#EDF5FF",
        background_bottom_color: "#2A60B2",
        background_price_color: "#7497CD",
        message_font_color: "#24476B",
      }),
    );
    await flushPromises();

    expect(useDanmakuStore().scList).toHaveLength(1);
    expect(wrapper.find('[data-testid="sc-panel"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("醒目留言内容");

    bridgeWs!.simulateMessage(
      JSON.stringify({ type: "sc_delete", ids: ["sc-live"] }),
    );
    await flushPromises();
    expect(useDanmakuStore().scList).toHaveLength(0);
    expect(wrapper.find('[data-testid="sc-panel"]').exists()).toBe(false);
  });

  it("banlist snapshot → banStore is populated (WS-driven, no polling)", async () => {
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

    const banWs = fakeInstances.find((w) =>
      w.url.includes("/api/ws/rooms/22210347/banlist"),
    );
    expect(banWs).toBeDefined();

    banWs!.simulateOpen();
    banWs!.simulateMessage(
      JSON.stringify({
        event: "snapshot",
        bans: [{ uid: 11, uname: "alice", hour: 1 }],
      }),
    );
    await flushPromises();

    const banStore = useBanStore();
    expect(banStore.banList).toHaveLength(1);
    expect(banStore.banList[0]?.uid).toBe(11);

    expect(wrapper.find('[data-testid="ban-list"]').exists()).toBe(true);
  });

  it("on disconnect: BanlistWS is closed and banStore is cleared", async () => {
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

    const banWs = fakeInstances.find((w) =>
      w.url.includes("/api/ws/rooms/22210347/banlist"),
    );
    expect(banWs).toBeDefined();
    banWs!.simulateOpen();
    banWs!.simulateMessage(
      JSON.stringify({
        event: "snapshot",
        bans: [{ uid: 22, uname: "bob", hour: 24 }],
      }),
    );
    await flushPromises();

    const banStore = useBanStore();
    expect(banStore.banList).toHaveLength(1);

    // Disconnect.
    await room.disconnect();
    await flushPromises();

    expect(banStore.banList).toHaveLength(0);
    expect(wrapper.find('[data-testid="ban-list"]').exists()).toBe(false);
  });

  // F3 / Bug 5 regression — on initial mount, App.vue must populate
  // userInfo via /api/auth/me after fetchStatus confirms authenticated.
  // Without this the welcome banner shows "用户" instead of the real name.
  //
  // Note: with the Bug-3 watcher (status → fetchMe) layered on top of
  // the existing onMounted fetchMe, the call count is >= 1 — the
  // onMounted path fires once and the watcher fires once for the same
  // status transition. The exact count is not load-bearing; what
  // matters is that fetchMe is called at least once AND userInfo is
  // populated AND the welcome banner renders the real uname.
  it("calls fetchMe() on mount when status === authenticated, renders the uname", async () => {
    let meCalled = 0;
    server.use(
      http.get("*/api/auth/me", () => {
        meCalled += 1;
        return HttpResponse.json({ uname: "alice", mid: 7777 });
      }),
    );

    const wrapper = mount(App);
    await flushPromises();
    await flushPromises();

    const auth = useAuthStore();
    expect(auth.status).toBe("authenticated");
    // The exact call count is implementation-defined (onMounted +
    // watcher can both fire); what matters is at least one call.
    expect(meCalled).toBeGreaterThanOrEqual(1);
    expect(auth.userInfo).toEqual({ uname: "alice", mid: 7777 });
    const text = wrapper.text();
    expect(text).toContain("alice");
    // mid rendered in the welcome banner.
    expect(text).toContain("7777");
    // The "用户" placeholder must NOT appear once userInfo is set.
    expect(text).not.toMatch(/欢迎,\s*用户/);
  });

  // F3 / Bug 3 regression — after QR login completes, `auth.status` flips
  // from "needs_login" to "authenticated" via pollOnce→fetchStatus. The
  // previous onMounted-only fetchMe call did NOT fire (App was already
  // mounted; no watcher triggered). The welcome banner then rendered the
  // "用户" placeholder. Add a `watch(() => auth.status, ...)` that calls
  // fetchMe whenever status becomes "authenticated" (initial load OR
  // post-QR-success). This test pins that contract.
  it("watcher: calls fetchMe when auth.status transitions to 'authenticated' after mount", async () => {
    // Pre-mount: status will become "authenticated" via the initial fetchStatus
    // (handler below). We need to specifically verify the watch fires ON a
    // post-mount transition, so stage the status as "needs_login" first and
    // only flip it after mount via the auth store directly.
    server.use(
      http.get("*/api/auth/status", () =>
        HttpResponse.json({ state: "needs_login" }),
      ),
    );

    let meCalled = 0;
    server.use(
      http.get("*/api/auth/me", () => {
        meCalled += 1;
        return HttpResponse.json({ uname: "watcher_alice", mid: 4242 });
      }),
    );

    const wrapper = mount(App);
    await flushPromises();
    await flushPromises();

    const auth = useAuthStore();
    expect(auth.status).toBe("needs_login");
    // fetchMe should NOT have been called yet (status is not authenticated).
    expect(meCalled).toBe(0);
    expect(auth.userInfo).toBeNull();

    // Now simulate the QR-login success path: status flips to authenticated.
    auth.status = "authenticated";
    await flushPromises();
    await flushPromises();

    // The watcher must have fired fetchMe at least once.
    expect(meCalled).toBeGreaterThanOrEqual(1);
    expect(auth.userInfo).toEqual({ uname: "watcher_alice", mid: 4242 });
    const text = wrapper.text();
    expect(text).toContain("watcher_alice");
    expect(text).toContain("4242");
  });

  // -----------------------------------------------------------------------
  // Bug B regression — the 401 race on first load. a.log showed:
  //   POST /api/auth/qr/start 401  ← fires before bootstrap sets the token
  //   GET  /api/auth/bootstrap 200
  //   GET  /api/auth/status     200
  // Root cause: QrLogin's onMounted ran startQr while auth.status was still
  // "loading" (the template rendered QrLogin because v-if only excluded
  // "authenticated"), so the POST had no Authorization header. Fix: gate
  // the QrLogin mount on auth.status !== "loading" so it cannot call
  // startQr until bootstrap has set the token, and render an explicit
  // "加载中…" placeholder during the loading state.
  // -----------------------------------------------------------------------
  it("Bug B: while auth.status === 'loading' QrLogin is not mounted and the loading placeholder is shown", async () => {
    let startCalls = 0;
    let release!: () => void;
    const ready = new Promise<void>((r) => {
      release = r;
    });

    server.use(
      http.get("*/api/auth/bootstrap", async () => {
        await ready;
        return HttpResponse.json({ token: "local-tok" });
      }),
      http.get("*/api/auth/status", async () => {
        await ready;
        return HttpResponse.json({ state: "needs_login" });
      }),
      http.post("*/api/auth/qr/start", () => {
        startCalls += 1;
        return HttpResponse.json({
          qrcode_url: "data:image/png;base64,X",
          qrcode_key: "k",
        });
      }),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "scanning" }),
      ),
    );

    const wrapper = mount(App);

    const auth = useAuthStore();
    expect(auth.status).toBe("loading");
    expect(auth.token).toBe("");

    // Flush microtasks a few times — if the bug were unfixed, QrLogin's
    // onMounted would have fired startQr by now (without a token, which
    // would 401 in production). After the gate fix, QrLogin must NOT
    // exist in the DOM yet and /qr/start must not have been called.
    await flushPromises();
    await flushPromises();
    await flushPromises();

    // Loading placeholder is visible (replaces the would-be QrLogin).
    const placeholder = wrapper.find('[data-testid="loading-placeholder"]');
    expect(placeholder.exists()).toBe(true);
    expect(placeholder.text()).toContain("加载中");
    // QrLogin is not rendered.
    expect(wrapper.findComponent(QrLogin).exists()).toBe(false);
    // No premature POST /qr/start with empty token.
    expect(startCalls).toBe(0);

    // Now release bootstrap+status so the App's onMounted can finish.
    release();
    await flushPromises();
    await flushPromises();
    await flushPromises();

    // Status flipped to needs_login (NOT "authenticated") — so QrLogin
    // must now be mounted AND startQr must have been called once.
    expect(auth.status).toBe("needs_login");
    expect(auth.token).toBe("local-tok");
    expect(wrapper.findComponent(QrLogin).exists()).toBe(true);
    expect(startCalls).toBe(1);
    expect(wrapper.find('[data-testid="loading-placeholder"]').exists()).toBe(false);
  });
});
