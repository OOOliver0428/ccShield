import { describe, it, expect, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useRoomStore } from "../stores/room";
import RoomInput from "./RoomInput.vue";

describe("RoomInput.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("renders status indicator showing '未连接' initially", () => {
    const wrapper = mount(RoomInput);
    const indicator = wrapper.find('[data-testid="status-indicator"]');
    expect(indicator.exists()).toBe(true);
    expect(indicator.text()).toContain("未连接");
  });

  it("on blur, resolves a short id and shows the resolved real id", async () => {
    server.use(
      http.get("*/api/rooms/resolve", () =>
        HttpResponse.json({ room_id: 999, short_id: 12345, is_short_id: true }),
      ),
    );

    const wrapper = mount(RoomInput);
    const input = wrapper.find('[data-testid="room-input-field"] input');
    expect(input.exists()).toBe(true);
    await input.setValue("12345");
    await input.trigger("blur");
    await flushPromises();

    const store = useRoomStore();
    expect(store.currentRoomId).toBe(999);
    expect(wrapper.find('[data-testid="resolved-hint"]').text()).toContain("short 12345");
  });

  it("clicking 连接 calls /rooms/start and flips status to 已连接", async () => {
    server.use(
      http.get("*/api/rooms/resolve", () =>
        HttpResponse.json({ room_id: 22210347, short_id: 22210347 }),
      ),
      http.post("*/api/rooms/start", () =>
        HttpResponse.json({ room_id: 22210347, title: "" }),
      ),
    );

    const wrapper = mount(RoomInput);
    const input = wrapper.find('[data-testid="room-input-field"] input');
    await input.setValue("22210347");
    await input.trigger("blur");
    await flushPromises();

    const store = useRoomStore();
    expect(store.currentRoomId).toBe(22210347);

    await wrapper.find('[data-testid="connect-btn"]').trigger("click");
    await flushPromises();
    await flushPromises();

    expect(store.status).toBe("connected");
    expect(wrapper.find('[data-testid="status-indicator"]').text()).toContain("已连接");
  });

  it("switches to the disconnect button while connected", async () => {
    server.use(
      http.post("*/api/rooms/stop", () => HttpResponse.json({ ok: true })),
    );

    const store = useRoomStore();
    store.currentRoomId = 22210347;
    store.status = "connected";

    const wrapper = mount(RoomInput);
    expect(wrapper.find('[data-testid="disconnect-btn"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="connect-btn"]').exists()).toBe(false);
  });

  it("shows anchor, canonical room id and title after connecting", async () => {
    server.use(
      http.get("*/api/rooms/resolve", () =>
        HttpResponse.json({
          room_id: 1601605,
          short_id: 0,
          uname: "测试主播",
          title: "测试直播标题",
        }),
      ),
      http.post("*/api/rooms/start", () =>
        HttpResponse.json({
          room_id: 1601605,
          title: "测试直播标题",
          role: "admin",
        }),
      ),
    );

    const wrapper = mount(RoomInput);
    const input = wrapper.find('[data-testid="room-input-field"] input');
    await input.setValue("1601605");
    await input.trigger("blur");
    await flushPromises();
    await wrapper.find('[data-testid="connect-btn"]').trigger("click");
    await flushPromises();

    expect(wrapper.find('[data-testid="room-info"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="room-anchor"]').text()).toBe("测试主播");
    expect(wrapper.find('[data-testid="room-number"]').text()).toBe("1601605");
    expect(wrapper.find('[data-testid="room-title"]').text()).toBe("测试直播标题");
    expect(wrapper.find('[data-testid="room-role"]').text()).toBe("房管");
    expect(wrapper.find('[data-testid="resolved-hint"]').exists()).toBe(false);
  });

  it.each([
    ["anchor", "主播"],
    ["admin", "房管"],
    ["viewer", "观众"],
    ["unknown", "暂未识别"],
  ] as const)("renders connected room role %s as %s", (role, label) => {
    const store = useRoomStore();
    store.currentRoomId = 1601605;
    store.status = "connected";
    store.currentUserRole = role;

    const wrapper = mount(RoomInput);
    expect(wrapper.find('[data-testid="room-role"]').text()).toBe(label);
  });

  it("clicking 断开 calls /rooms/stop and flips status back", async () => {
    let stopCalled = false;
    server.use(
      http.post("*/api/rooms/stop", () => {
        stopCalled = true;
        return HttpResponse.json({ ok: true });
      }),
    );

    const store = useRoomStore();
    store.currentRoomId = 22210347;
    store.status = "connected";

    const wrapper = mount(RoomInput);
    await wrapper.find('[data-testid="disconnect-btn"]').trigger("click");
    await flushPromises();

    expect(stopCalled).toBe(true);
    expect(store.status).toBe("disconnected");
    expect(wrapper.find('[data-testid="status-indicator"]').text()).toContain("未连接");
  });

  it("renders error message when resolve fails", async () => {
    server.use(
      http.get("*/api/rooms/resolve", () =>
        HttpResponse.json({ detail: "not found" }, { status: 404 }),
      ),
    );

    const wrapper = mount(RoomInput);
    const input = wrapper.find('[data-testid="room-input-field"] input');
    await input.setValue("555");
    await input.trigger("blur");
    await flushPromises();

    expect(wrapper.find('[data-testid="room-error"]').exists()).toBe(true);
  });
});
