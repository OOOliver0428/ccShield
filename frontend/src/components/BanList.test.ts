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
    uid,
    uname: `user${uid}`,
    id: `block-${uid}`,
    hour: 1,
    ctime: 1_700_000_000,
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
      makeEntry(11, { uname: "alice", hour: 1, ctime: 1_700_000_000 }),
      makeEntry(22, { uname: "bob", hour: 24, ctime: 1_700_000_500 }),
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
    store.applySnapshot([{ uid: 99, id: "x" }]);

    const wrapper = mount(BanList);
    expect(wrapper.find('[data-testid="ban-row"]').text()).toContain("uid:99");
  });

  it("renders 永久 when hour === -1", () => {
    const store = useBanStore();
    store.applySnapshot([makeEntry(33, { hour: -1 })]);

    const wrapper = mount(BanList);
    expect(wrapper.find('[data-testid="ban-row"]').text()).toContain("永久");
  });

  it("renders 本场 when hour === 0", () => {
    const store = useBanStore();
    store.applySnapshot([makeEntry(44, { hour: 0 })]);

    const wrapper = mount(BanList);
    expect(wrapper.find('[data-testid="ban-row"]').text()).toContain("本场");
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
      block_id: "block-55",
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
      makeEntry(77, { id: "block-77" }),
      makeEntry(88, { id: "block-88" }),
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

  it("解禁 with missing id → DELETE is NOT issued and error surfaces", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    const store = useBanStore();
    // Entry without an `id` — we don't have a block_id to send.
    store.applySnapshot([{ uid: 99, uname: "no-id" }]);

    let deleteCalls = 0;
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
    expect(wrapper.find('[data-testid="unban-error"]').exists()).toBe(true);
  });

  it("解禁 backend error → row remains, error surfaces, no removeBan", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    const store = useBanStore();
    store.applySnapshot([makeEntry(11, { id: "b-11" })]);

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