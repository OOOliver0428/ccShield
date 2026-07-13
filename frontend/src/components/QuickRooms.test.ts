import { beforeEach, describe, expect, it } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useRoomStore } from "../stores/room";
import QuickRooms from "./QuickRooms.vue";

const savedRoom = {
  room_id: 1601605,
  short_id: 123,
  uid: 42,
  uname: "测试主播",
  title: "测试直播",
  live_status: 1,
  added_at: "2026-07-13T00:00:00Z",
};

describe("QuickRooms.vue", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    setActivePinia(createPinia());
  });

  it("opens configuration, verifies a room, then enables saving", async () => {
    let addCalls = 0;
    server.use(
      http.get("*/api/quick-rooms", () => HttpResponse.json({ rooms: [] })),
      http.get("*/api/rooms/resolve", () => HttpResponse.json(savedRoom)),
      http.post("*/api/quick-rooms", () => {
        addCalls += 1;
        return HttpResponse.json({ rooms: [savedRoom] });
      }),
    );

    const wrapper = mount(QuickRooms, { attachTo: document.body });
    await flushPromises();
    await wrapper.get('[data-testid="open-quick-config"]').trigger("click");
    await flushPromises();

    const input = document.querySelector<HTMLInputElement>('[data-testid="quick-room-input"] input');
    expect(input).not.toBeNull();
    input!.value = "123";
    input!.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector<HTMLElement>('[data-testid="verify-quick-room"]')!.click();
    await flushPromises();

    expect(document.querySelector('[data-testid="verified-room"]')?.textContent).toContain("测试主播");
    const save = document.querySelector<HTMLButtonElement>('[data-testid="save-quick-room"]')!;
    expect(save.disabled).toBe(false);
    save.click();
    await flushPromises();
    expect(addCalls).toBe(1);
  });

  it("connects a configured room from its shortcut", async () => {
    server.use(
      http.get("*/api/quick-rooms", () => HttpResponse.json({ rooms: [savedRoom] })),
      http.post("*/api/rooms/start", () => HttpResponse.json({ room_id: 1601605 })),
    );
    const wrapper = mount(QuickRooms);
    await flushPromises();
    await wrapper.get('[data-testid="quick-room-1601605"]').trigger("click");
    await flushPromises();

    const room = useRoomStore();
    expect(room.status).toBe("connected");
    expect(room.resolvedUname).toBe("测试主播");
  });

  it("offers one-click add for the currently connected room", async () => {
    let posted: unknown = null;
    server.use(
      http.get("*/api/quick-rooms", () => HttpResponse.json({ rooms: [] })),
      http.post("*/api/quick-rooms", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ rooms: [savedRoom] });
      }),
    );
    const room = useRoomStore();
    room.currentRoomId = 1601605;
    room.status = "connected";

    const wrapper = mount(QuickRooms);
    await flushPromises();
    await wrapper.get('[data-testid="add-current-room"]').trigger("click");
    await flushPromises();
    expect(posted).toEqual({ room_id: 1601605 });
    expect(wrapper.text()).not.toContain("删除");
  });
});
