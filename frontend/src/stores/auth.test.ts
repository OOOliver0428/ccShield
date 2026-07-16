import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useAuthStore } from "./auth";

describe("auth store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe("fetchStatus", () => {
    it("GETs /api/auth/status and maps state to needs_login", async () => {
      server.use(
        http.get("*/api/auth/status", () =>
          HttpResponse.json({ state: "needs_login" }),
        ),
      );

      const store = useAuthStore();
      expect(store.status).toBe("loading");

      const promise = store.fetchStatus();
      await promise;

      expect(store.status).toBe("needs_login");
    });

    it("maps state=authenticated to status=authenticated", async () => {
      server.use(
        http.get("*/api/auth/status", () =>
          HttpResponse.json({ state: "authenticated" }),
        ),
      );

      const store = useAuthStore();
      await store.fetchStatus();

      expect(store.status).toBe("authenticated");
    });

    it("maps state=expired and clears the stale user identity", async () => {
      server.use(
        http.get("*/api/auth/status", () =>
          HttpResponse.json({ state: "expired" }),
        ),
      );
      const store = useAuthStore();
      store.status = "authenticated";
      store.userInfo = { uname: "alice", mid: 99 };

      await store.fetchStatus();

      expect(store.status).toBe("expired");
      expect(store.userInfo).toBeNull();
      expect(store.expiredMessage).toContain("重新扫码登录");
    });
  });

  describe("startQr + pollQr", () => {
    it("POSTs /api/auth/qr/start, sets qrcodeUrl/qrKey, polls every 2s", async () => {
      let pollCalls = 0;
      server.use(
        http.post("*/api/auth/qr/start", () =>
          HttpResponse.json({
            qrcode_url: "data:image/png;base64,FAKE",
            qrcode_key: "key-1",
          }),
        ),
        http.get("*/api/auth/qr/poll", () => {
          pollCalls += 1;
          return HttpResponse.json({ status: "scanning" });
        }),
      );

      const store = useAuthStore();
      await store.startQr();

      expect(store.qrcodeUrl).toBe("data:image/png;base64,FAKE");
      expect(store.qrKey).toBe("key-1");
      expect(store.qrPollStatus).toBe("scanning");
      expect(pollCalls).toBe(1);

      await vi.advanceTimersByTimeAsync(2000);
      expect(pollCalls).toBeGreaterThan(1);
      expect(store.qrPollStatus).toBe("scanning");
    });

    it("success: stops polling and calls fetchStatus", async () => {
      // First /qr/poll call returns "success"; subsequent /auth/status
      // returns "authenticated".
      let pollCalls = 0;
      server.use(
        http.post("*/api/auth/qr/start", () =>
          HttpResponse.json({
            qrcode_url: "data:image/png;base64,FAKE",
            qrcode_key: "key-2",
          }),
        ),
        http.get("*/api/auth/qr/poll", () => {
          pollCalls += 1;
          return HttpResponse.json({ status: "success" });
        }),
        http.get("*/api/auth/status", () =>
          HttpResponse.json({ state: "authenticated" }),
        ),
      );

      const store = useAuthStore();
      await store.startQr();

      // startQr already invoked /qr/poll once (it was success); polling
      // must have stopped, and /auth/status must have been hit.
      expect(pollCalls).toBe(1);
      expect(store.qrPollStatus).toBe("success");

      // Allow any microtasks for fetchStatus to complete.
      await vi.runAllTimersAsync();
      // Wait for the fetchStatus promise chain to settle.
      await Promise.resolve();
      await Promise.resolve();

      expect(store.status).toBe("authenticated");

      // Advance time — polling must NOT fire again.
      const beforeMorePolls = pollCalls;
      await vi.advanceTimersByTimeAsync(10_000);
      expect(pollCalls).toBe(beforeMorePolls);
    });

    it("expired: stops polling and sets qrPollStatus=expired", async () => {
      server.use(
        http.post("*/api/auth/qr/start", () =>
          HttpResponse.json({
            qrcode_url: "data:image/png;base64,FAKE",
            qrcode_key: "key-3",
          }),
        ),
        http.get("*/api/auth/qr/poll", () =>
          HttpResponse.json({ status: "expired" }),
        ),
      );

      const store = useAuthStore();
      await store.startQr();

      expect(store.qrPollStatus).toBe("expired");
    });

    it("regression: pollOnce rejection does NOT fail startQr or clear qrcodeUrl", async () => {
      // Backend can return non-2xx on intermediate poll states (e.g.
      // 500-on-86101 before the data.code fix landed, or any transient
      // network blip). startQr's initial poll is fire-and-forget — a
      // rejection must not propagate to the caller, which would set
      // QrLogin's startError and hide the already-rendered QR behind
      // a "生成二维码失败" message. The setInterval owns long-term
      // recovery; this test pins that contract.
      let pollCalls = 0;
      server.use(
        http.post("*/api/auth/qr/start", () =>
          HttpResponse.json({
            qrcode_url: "data:image/png;base64,FAKE",
            qrcode_key: "key-poll-fail",
          }),
        ),
        http.get("*/api/auth/qr/poll", () => {
          pollCalls += 1;
          return HttpResponse.json(
            { detail: "internal server error" },
            { status: 500 },
          );
        }),
      );

      const store = useAuthStore();
      // startQr must RESOLVE — never reject, regardless of what pollOnce
      // would do — because the QR is already rendered and the interval
      // will keep polling.
      await expect(store.startQr()).resolves.toBeUndefined();

      // The QR image is still set on the store (the failure is non-fatal).
      expect(store.qrcodeUrl).toBe("data:image/png;base64,FAKE");
      expect(store.qrKey).toBe("key-poll-fail");
      // Initial scan was attempted exactly once.
      expect(pollCalls).toBe(1);

      // The interval keeps retrying: advancing time fires more polls,
      // proving the failure didn't disable polling or stop propagation.
      await vi.advanceTimersByTimeAsync(2000);
      expect(pollCalls).toBeGreaterThan(1);
    });

    it("duplicate expiry responses do not stop a newly-started QR poll", async () => {
      let pollCalls = 0;
      server.use(
        http.post("*/api/auth/qr/start", () =>
          HttpResponse.json({
            qrcode_url: "https://example.test/fresh-qr",
            qrcode_key: "fresh-key",
          }),
        ),
        http.get("*/api/auth/qr/poll", () => {
          pollCalls += 1;
          return HttpResponse.json({ status: "scanning" });
        }),
      );
      const store = useAuthStore();
      store.status = "authenticated";
      store.markExpired();
      await store.startQr();
      const callsAfterStart = pollCalls;

      store.markExpired("late duplicate response");
      await vi.advanceTimersByTimeAsync(2000);

      expect(pollCalls).toBeGreaterThan(callsAfterStart);
      expect(store.qrcodeUrl).toBe("https://example.test/fresh-qr");
      expect(store.expiredMessage).not.toBe("late duplicate response");
    });
  });

  describe("loginManual", () => {
    it("POSTs /api/auth/manual and then fetchStatus returns authenticated", async () => {
      let postedBody: unknown = null;
      server.use(
        http.post("*/api/auth/manual", async ({ request }) => {
          postedBody = await request.json();
          return HttpResponse.json({ uname: "tester", mid: 12345 });
        }),
        http.get("*/api/auth/status", () =>
          HttpResponse.json({ state: "authenticated" }),
        ),
      );

      const store = useAuthStore();
      await store.loginManual("sess", "jct", "buv");

      expect(postedBody).toEqual({
        sessdata: "sess",
        bili_jct: "jct",
        buvid3: "buv",
      });
      expect(store.status).toBe("authenticated");
      expect(store.userInfo).toEqual({ uname: "tester", mid: 12345 });
    });
  });

  // userInfo must be populated on QR login
  // (not just manual). The QR path was: startQr → pollOnce success
  // → fetchStatus; nothing ever called /auth/me, so userInfo stayed
  // null and App.vue rendered "用户" instead of the real name.
  describe("fetchMe", () => {
    it("GETs /api/auth/me and populates userInfo", async () => {
      server.use(
        http.get("*/api/auth/me", () =>
          HttpResponse.json({ uname: "alice", mid: 999 }),
        ),
      );

      const store = useAuthStore();
      expect(store.userInfo).toBeNull();

      await store.fetchMe();

      expect(store.userInfo).toEqual({ uname: "alice", mid: 999 });
    });

    it("leaves userInfo untouched when /auth/me returns 401", async () => {
      server.use(
        http.get("*/api/auth/me", () =>
          HttpResponse.json({ detail: "unauth" }, { status: 401 }),
        ),
      );

      const store = useAuthStore();
      await expect(store.fetchMe()).rejects.toBeTruthy();
      expect(store.userInfo).toBeNull();
    });
  });
});
