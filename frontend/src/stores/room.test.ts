import { describe, it, expect, beforeEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useRoomStore } from "./room";

describe("room store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  describe("resolve", () => {
    it("GETs /api/rooms/resolve?input=22210347 and sets currentRoomId", async () => {
      let capturedUrl: URL | null = null;
      server.use(
        http.get("*/api/rooms/resolve", ({ request }) => {
          capturedUrl = new URL(request.url);
          return HttpResponse.json({
            room_id: 22210347,
            short_id: 22210347,
            title: "Some Room",
            uname: "anchor",
          });
        }),
      );

      const store = useRoomStore();
      const result = await store.resolve("22210347");

      expect(result).not.toBeNull();
      expect(capturedUrl).not.toBeNull();
      expect(capturedUrl!.searchParams.get("input")).toBe("22210347");
      expect(store.currentRoomId).toBe(22210347);
      expect(store.resolvedTitle).toBe("Some Room");
      expect(store.resolvedUname).toBe("anchor");
    });

    it("returns null and sets error for non-numeric input", async () => {
      const store = useRoomStore();
      const result = await store.resolve("not-a-number");
      expect(result).toBeNull();
      expect(store.error).toBe("房间号无效");
      expect(store.currentRoomId).toBeNull();
    });

    it("maps a short id to the real room_id (currentRoomId follows)", async () => {
      server.use(
        http.get("*/api/rooms/resolve", () =>
          HttpResponse.json({
            room_id: 999,
            short_id: 12345,
            is_short_id: true,
          }),
        ),
      );

      const store = useRoomStore();
      await store.resolve("12345");

      expect(store.currentRoomId).toBe(999);
      expect(store.resolvedShortId).toBe(12345);
    });
  });

  describe("connect", () => {
    it("POSTs /api/rooms/start, sets currentRoomId and status=connected", async () => {
      let postedBody: unknown = null;
      server.use(
        http.post("*/api/rooms/start", async ({ request }) => {
          postedBody = await request.json();
          return HttpResponse.json({ room_id: 22210347, title: "", role: "admin" });
        }),
      );

      const store = useRoomStore();
      expect(store.status).toBe("disconnected");

      const ok = await store.connect(22210347);

      expect(ok).toBe(true);
      expect(postedBody).toEqual({ room_id: 22210347 });
      expect(store.status).toBe("connected");
      expect(store.currentRoomId).toBe(22210347);
      expect(store.currentUserRole).toBe("admin");
    });

    it("flips status to disconnected when /rooms/start fails", async () => {
      server.use(
        http.post("*/api/rooms/start", () =>
          HttpResponse.json({ detail: "boom" }, { status: 500 }),
        ),
      );

      const store = useRoomStore();
      const ok = await store.connect(22210347);

      expect(ok).toBe(false);
      expect(store.status).toBe("disconnected");
      expect(store.currentRoomId).toBeNull();
      expect(store.resolvedTitle).toBe("");
      expect(store.resolvedUname).toBe("");
      expect(store.currentUserRole).toBe("unknown");
    });
  });

  describe("disconnect", () => {
    it("POSTs /api/rooms/stop and flips status back to disconnected", async () => {
      let stopCalls = 0;
      server.use(
        http.post("*/api/rooms/stop", () => {
          stopCalls += 1;
          return HttpResponse.json({ ok: true });
        }),
      );

      const store = useRoomStore();
      // Pretend we are connected.
      store.currentRoomId = 22210347;
      store.status = "connected";
      store.currentUserRole = "anchor";

      await store.disconnect();

      expect(stopCalls).toBe(1);
      expect(store.status).toBe("disconnected");
      expect(store.currentRoomId).toBeNull();
      expect(store.currentUserRole).toBe("unknown");
    });
  });

  describe("applyRoomStatus (WS room_status dispatch)", () => {
    it("maps connected → connected, disconnected → disconnected", () => {
      const store = useRoomStore();
      store.applyRoomStatus("connected");
      expect(store.status).toBe("connected");
      store.applyRoomStatus("disconnected");
      expect(store.status).toBe("disconnected");
    });

    it("maps reconnecting → connecting, error → disconnected", () => {
      const store = useRoomStore();
      store.applyRoomStatus("reconnecting");
      expect(store.status).toBe("connecting");
      store.applyRoomStatus("error");
      expect(store.status).toBe("disconnected");
    });
  });
});
