import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useBanStore } from "../stores/ban";
import { useDanmakuStore } from "../stores/danmaku";
import { useRoomStore } from "../stores/room";
import DanmakuList from "./DanmakuList.vue";

function makeDanmaku(uid: number, text: string, extras: Record<string, unknown> = {}) {
  return {
    type: "danmaku" as const,
    uid,
    uname: `user${uid}`,
    text,
    ts: 1_700_000_000 + uid,
    guard_level: 0,
    medal: null,
    ...extras,
  };
}

function makeSc(uid: number, text: string, price: number) {
  const now = Math.floor(Date.now() / 1000);
  return {
    type: "sc" as const,
    id: `sc-${uid}`,
    uid,
    uname: `user${uid}`,
    text,
    price,
    ts: now,
    end_ts: now + 300,
    duration: 300,
    guard_level: 0,
    medal: null,
    background_color: "#EDF5FF",
    background_bottom_color: "#2A60B2",
    background_price_color: "#7497CD",
    message_font_color: "#24476B",
  };
}

describe("DanmakuList.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders uname + text from the danmaku list", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hello world"));

    const wrapper = mount(DanmakuList);
    const rows = wrapper.findAll('[data-testid="danmaku-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0]!.text()).toContain("user1");
    expect(rows[0]!.text()).toContain("hello world");
  });

  it("renders millisecond danmaku timestamps without multiplying them again", () => {
    const timestampMs = 1_700_000_000_123;
    const expected = new Date(timestampMs);
    const pad = (value: number): string => String(value).padStart(2, "0");
    const expectedText = `${pad(expected.getHours())}:${pad(expected.getMinutes())}:${pad(expected.getSeconds())}`;
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "timestamp", { ts: timestampMs }));

    const wrapper = mount(DanmakuList);

    expect(wrapper.get(".ts").text()).toBe(expectedText);
  });

  it("renders GuardBadge with 舰长 for guard_level=3", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 3 }));

    const wrapper = mount(DanmakuList);
    const badge = wrapper.find('[data-testid="guard-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe("舰长");
    expect(wrapper.text()).not.toContain("[guard3]");
  });

  it("renders GuardBadge with 提督 for guard_level=2", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 2 }));

    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="guard-badge"]').text()).toBe("提督");
  });

  it("renders GuardBadge with 总督 for guard_level=1", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 1 }));

    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="guard-badge"]').text()).toBe("总督");
  });

  it("does NOT render a guard badge for guard_level=0", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 0 }));

    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="guard-badge"]').exists()).toBe(false);
  });

  it("renders FanMedal with name + level when medal is present", () => {
    const store = useDanmakuStore();
    store.addDanmaku(
      makeDanmaku(1, "hi", { medal: { name: "粉丝牌", level: 25 } }),
    );

    const wrapper = mount(DanmakuList);
    const medal = wrapper.find('[data-testid="medal-badge"]');
    expect(medal.exists()).toBe(true);
    expect(medal.text()).toContain("粉丝牌");
    expect(medal.text()).toContain("25");
    expect(wrapper.text()).not.toContain("[粉丝牌 lv25]");
  });

  it("does NOT render a medal badge when medal is null", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { medal: null }));

    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="medal-badge"]').exists()).toBe(false);
  });

  it("renders SC items via SuperChatItem with price highlighted", () => {
    const store = useDanmakuStore();
    store.addSc(makeSc(7, "gift!", 500));

    const wrapper = mount(DanmakuList);
    const scRow = wrapper.find('[data-testid="sc-row"]');
    expect(scRow.exists()).toBe(true);
    expect(wrapper.find('[data-testid="sc-panel"]').exists()).toBe(true);
    const price = wrapper.find('[data-testid="sc-price"]');
    expect(price.exists()).toBe(true);
    expect(price.text()).toBe("¥500");
    expect(scRow.find('[data-testid="sc-uname"]').text()).toBe("user7");
    expect(scRow.find('[data-testid="sc-text"]').text()).toBe("gift!");
  });

  it("renders empty-state placeholder when no events", () => {
    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="empty"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="danmaku-row"]').exists()).toBe(false);
  });

  it("clear button empties both lists", async () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi"));
    store.addSc(makeSc(1, "sc", 30));

    const wrapper = mount(DanmakuList);
    expect(wrapper.findAll('[data-testid="danmaku-row"]')).toHaveLength(1);

    await wrapper.find('[data-testid="clear-btn"]').trigger("click");
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-testid="empty"]').exists()).toBe(true);
    expect(store.list).toHaveLength(0);
    expect(store.scList).toHaveLength(0);
  });

  it("quick 本场禁言 confirms evidence and POSTs hour=0", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;
    const danmaku = useDanmakuStore();
    danmaku.addDanmaku(makeDanmaku(7, "重复刷屏内容"));
    let postedBody: unknown = null;
    server.use(
      http.post("*/api/ban", async ({ request }) => {
        postedBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = mount(DanmakuList);
    await wrapper.find('[data-testid="quick-ban-btn"]').trigger("click");
    await flushPromises();

    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("用户：user7 (uid:7)"),
    );
    expect(confirm.mock.calls[0]?.[0]).toContain("发言：重复刷屏内容");
    expect(confirm.mock.calls[0]?.[0]).toContain("期限：本场直播");
    expect(postedBody).toEqual({
      room_id: 12345,
      uid: 7,
      uname: "user7",
      hour: 0,
      reason: "",
    });
    const banStore = useBanStore();
    expect(banStore.banList[0]).toMatchObject({
      uid: 7,
      hour: 0,
      pending: true,
      block_id: null,
    });
  });

  it("quick 本场禁言 cancel never calls the backend", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;
    const danmaku = useDanmakuStore();
    danmaku.addDanmaku(makeDanmaku(8, "cancel me"));
    let calls = 0;
    server.use(
      http.post("*/api/ban", () => {
        calls += 1;
        return HttpResponse.json({ ok: true });
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(false);

    const wrapper = mount(DanmakuList);
    await wrapper.find('[data-testid="quick-ban-btn"]').trigger("click");
    await flushPromises();

    expect(calls).toBe(0);
    expect(useBanStore().isSubmitting(8)).toBe(false);
  });

  it("locks both moderation buttons while the same uid is submitting", () => {
    const danmaku = useDanmakuStore();
    danmaku.addDanmaku(makeDanmaku(18, "locked"));
    const banStore = useBanStore();
    expect(banStore.beginSubmission(18)).toBe(true);

    const wrapper = mount(DanmakuList);

    expect(
      wrapper.find('[data-testid="quick-ban-btn"]').attributes("disabled"),
    ).toBeDefined();
    expect(
      wrapper.find('[data-testid="more-ban-btn"]').attributes("disabled"),
    ).toBeDefined();
  });

  it("more 禁言 opens one shared panel with the selected message", async () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(9, "evidence text"));
    const wrapper = mount(DanmakuList);

    await wrapper.find('[data-testid="more-ban-btn"]').trigger("click");

    const panel = wrapper.find('[data-testid="ban-panel"]');
    expect(panel.exists()).toBe(true);
    expect(panel.text()).toContain("user9");
    expect(panel.findAll('[data-testid="duration-option"]')).toHaveLength(5);
  });

  it("auto-scrolls to the bottom on new danmaku when pinned", async () => {
    const store = useDanmakuStore();

    const wrapper = mount(DanmakuList, { attachTo: document.body });
    const scrollRoot = wrapper.find('[data-testid="scroll-root"]').element as HTMLElement;

    // Force scrollHeight > clientHeight so a meaningful scrollTop is possible.
    Object.defineProperty(scrollRoot, "scrollHeight", {
      configurable: true,
      get: () => 500,
    });
    Object.defineProperty(scrollRoot, "clientHeight", {
      configurable: true,
      get: () => 100,
    });
    scrollRoot.scrollTop = 400; // pinned to bottom (500 - 400 - 100 = 0)

    store.addDanmaku(makeDanmaku(1, "first"));
    await wrapper.vm.$nextTick();
    store.addDanmaku(makeDanmaku(2, "second"));
    await wrapper.vm.$nextTick();

    expect(scrollRoot.scrollTop).toBe(scrollRoot.scrollHeight);

    wrapper.unmount();
  });

  it("respects user scroll-up and does NOT auto-scroll", async () => {
    const store = useDanmakuStore();

    const wrapper = mount(DanmakuList, { attachTo: document.body });
    const scrollRoot = wrapper.find('[data-testid="scroll-root"]').element as HTMLElement;

    Object.defineProperty(scrollRoot, "scrollHeight", {
      configurable: true,
      get: () => 500,
    });
    Object.defineProperty(scrollRoot, "clientHeight", {
      configurable: true,
      get: () => 100,
    });
    // User scrolled up — not pinned (500 - 0 - 100 = 400 > tolerance).
    scrollRoot.scrollTop = 0;
    await scrollRoot.dispatchEvent(new Event("scroll"));
    await wrapper.vm.$nextTick();

    store.addDanmaku(makeDanmaku(1, "after-scroll-up"));
    await wrapper.vm.$nextTick();

    // Should NOT have been moved to the bottom.
    expect(scrollRoot.scrollTop).toBe(0);

    wrapper.unmount();
  });
});
