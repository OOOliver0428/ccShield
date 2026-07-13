import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useQuickRoomsStore } from "./quickRooms";

describe("quick rooms store", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("verifies a short room number without starting a room", async () => {
    let startCalls = 0;
    server.use(
      http.get("*/api/rooms/resolve", ({ request }) => {
        expect(new URL(request.url).searchParams.get("input")).toBe("123");
        return HttpResponse.json({
          room_id: 1601605,
          short_id: 123,
          uname: "测试主播",
          title: "测试直播",
        });
      }),
      http.post("*/api/rooms/start", () => {
        startCalls += 1;
        return HttpResponse.json({ room_id: 1601605 });
      }),
    );

    const result = await useQuickRoomsStore().verify("123");
    expect(result?.room_id).toBe(1601605);
    expect(startCalls).toBe(0);
  });

  it("loads and adds local shortcuts", async () => {
    const saved = {
      room_id: 1601605,
      short_id: 123,
      uid: 42,
      uname: "测试主播",
      title: "测试直播",
      live_status: 1,
      added_at: "2026-07-13T00:00:00Z",
    };
    server.use(
      http.get("*/api/quick-rooms", () => HttpResponse.json({ rooms: [] })),
      http.post("*/api/quick-rooms", async ({ request }) => {
        expect(await request.json()).toEqual({ room_id: 1601605 });
        return HttpResponse.json({ rooms: [saved] });
      }),
    );

    const store = useQuickRoomsStore();
    await store.load();
    expect(store.rooms).toEqual([]);
    expect(await store.add(1601605)).toBe(true);
    expect(store.rooms[0].uname).toBe("测试主播");
  });
});
