import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useRoomStore } from "../stores/room";
import { useBanStore, type BanEntry } from "../stores/ban";
import BanList from "./BanList.vue";

function makeEntry(uid: number, extras: Partial<BanEntry> = {}): BanEntry {
  return {
    block_id: uid,
    uid,
    uname: `user${uid}`,
    hour: 1,
    reason: "",
    created_at: 1_700_000_000,
    expires_at: 1_700_003_600,
    pending: false,
    ...extras,
  };
}

describe("BanList.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the empty state when banStore is empty", () => {
    const wrapper = mount(BanList);
    expect(wrapper.find('[data-testid="empty"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-testid="ban-row"]')).toHaveLength(0);
  });

  it("renders one row per ban entry (uid, uname, ban time)", () => {
    const store = useBanStore();
    store.applySnapshot([
      makeEntry(11, { uname: "alice", hour: 1, created_at: 1_700_000_000 }),
      makeEntry(22, { uname: "bob", hour: 24, created_at: 1_700_000_500 }),
    ]);

    const wrapper = mount(BanList);
    const rows = wrapper.findAll('[data-testid="ban-row"]');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.text()).toContain("alice");
    expect(rows[0]!.text()).toContain("uid:11");
    expect(rows[0]!.text()).toContain("1小时");
    expect(rows[1]!.text()).toContain("bob");
    expect(rows[1]!.text()).toContain("uid:22");
    expect(rows[1]!.text()).toContain("24小时");
  });

  it("falls back to uid-only label when uname is absent", () => {
    const store = useBanStore();
    store.applySnapshot([{ uid: 99 }]);

    const wrapper = mount(BanList);
    expect(wrapper.find('[data-testid="ban-row"]').text()).toContain("uid:99");
  });

  it("filters the list locally by username, uid or reason", async () => {
    const store = useBanStore();
    store.applySnapshot([
      makeEntry(11, { uname: "alice", reason: "重复刷屏" }),
      makeEntry(22, { uname: "bob", reason: "广告" }),
    ]);
    const wrapper = mount(BanList);
    const input = wrapper.find('[data-testid="ban-search-input"]');

    expect(input.exists()).toBe(true);
    await input.setValue("广告");

    const rows = wrapper.findAll('[data-testid="ban-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0]!.text()).toContain("bob");
  });

  it("renders 本场 when hour === 0", () => {
    const store = useBanStore();
    store.applySnapshot([makeEntry(44, { hour: 0 })]);

    const wrapper = mount(BanList);
    expect(wrapper.find('[data-testid="ban-row"]').text()).toContain("本场");
  });

  it("renders an existing upstream permanent record without offering creation", () => {
    const store = useBanStore();
    store.applySnapshot([makeEntry(33, { hour: -1 })]);
    const wrapper = mount(BanList);
    expect(wrapper.find('[data-testid="ban-row"]').text()).toContain("永久");
  });

  it("clicking 解禁 → 二次确认 + DELETE /api/ban", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    const store = useBanStore();
    store.applySnapshot([makeEntry(55)]);

    let deletedBody: unknown = null;
    server.use(
      http.delete("*/api/ban", async ({ request }) => {
        deletedBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );

    const callOrder: string[] = [];
    vi.spyOn(window, "confirm").mockImplementation(() => {
      callOrder.push("confirm");
      return true;
    });

    const wrapper = mount(BanList);
    await wrapper.find('[data-testid="unban-btn"]').trigger("click");
    await flushPromises();

    expect(callOrder).toEqual(["confirm"]);
    expect(deletedBody).toEqual({
      room_id: 12345,
      block_id: 55,
      uid: 55,
    });
  });

  it("解禁 cancel → no DELETE is issued", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    const store = useBanStore();
    store.applySnapshot([makeEntry(66)]);

    let deleteCalls = 0;
    vi.spyOn(window, "confirm").mockReturnValue(false);
    server.use(
      http.delete("*/api/ban", async () => {
        deleteCalls += 1;
        return HttpResponse.json({ ok: true });
      }),
    );

    const wrapper = mount(BanList);
    await wrapper.find('[data-testid="unban-btn"]').trigger("click");
    await flushPromises();

    expect(deleteCalls).toBe(0);
  });

  it("解禁 success → banStore.removeBan(uid) called (optimistic)", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    const store = useBanStore();
    store.applySnapshot([
      makeEntry(77, { block_id: 77 }),
      makeEntry(88, { block_id: 88 }),
    ]);

    server.use(
      http.delete("*/api/ban", () => HttpResponse.json({ ok: true })),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = mount(BanList);
    const rows = wrapper.findAll('[data-testid="ban-row"]');
    expect(rows).toHaveLength(2);

    await wrapper.findAll('[data-testid="unban-btn"]')[0]!.trigger("click");
    await flushPromises();

    expect(store.banList.map((b) => b.uid)).toEqual([88]);
  });

  it("missing block_id disables 解禁 and never issues DELETE", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    const store = useBanStore();
    store.applySnapshot([makeEntry(99, { block_id: null })]);

    let deleteCalls = 0;
    server.use(
      http.delete("*/api/ban", async () => {
        deleteCalls += 1;
        return HttpResponse.json({ ok: true });
      }),
    );

    const wrapper = mount(BanList);
    const button = wrapper.find('[data-testid="unban-btn"]');
    expect(button.attributes("disabled")).toBeDefined();
    await button.trigger("click");
    await flushPromises();

    expect(deleteCalls).toBe(0);
  });

  it("pending row shows 正在同步 and disables 解禁", () => {
    const store = useBanStore();
    store.applySnapshot([
      makeEntry(101, { block_id: null, pending: true, reason: "刷屏" }),
    ]);

    const wrapper = mount(BanList);

    expect(wrapper.find('[data-testid="pending-label"]').text()).toBe("正在同步");
    expect(wrapper.find('[data-testid="ban-row"]').text()).toContain("刷屏");
    expect(
      wrapper.find('[data-testid="unban-btn"]').attributes("disabled"),
    ).toBeDefined();
  });

  it("manual refresh requests refresh=true and replaces the snapshot", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;
    const store = useBanStore();
    store.applySnapshot([makeEntry(1)]);
    let refreshParam: string | null = null;
    server.use(
      http.get("*/api/ban-list/12345", ({ request }) => {
        refreshParam = new URL(request.url).searchParams.get("refresh");
        return HttpResponse.json({
          room_id: 12345,
          bans: [makeEntry(2, { uname: "fresh-user" })],
        });
      }),
    );

    const wrapper = mount(BanList);
    await wrapper.find('[data-testid="refresh-ban-list-btn"]').trigger("click");
    await flushPromises();

    expect(refreshParam).toBe("true");
    expect(store.banList.map((entry) => entry.uid)).toEqual([2]);
    expect(wrapper.text()).toContain("fresh-user");
  });

  it("解禁 backend error → row remains, error surfaces, no removeBan", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    const store = useBanStore();
    store.applySnapshot([makeEntry(11, { block_id: 11 })]);

    server.use(
      http.delete("*/api/ban", () =>
        HttpResponse.json({ detail: "unban failed" }, { status: 400 }),
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = mount(BanList);
    await wrapper.find('[data-testid="unban-btn"]').trigger("click");
    await flushPromises();

    expect(store.banList).toHaveLength(1);
    expect(wrapper.find('[data-testid="unban-error"]').exists()).toBe(true);
  });
});
