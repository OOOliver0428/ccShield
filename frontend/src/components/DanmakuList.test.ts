import { describe, it, expect, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { useDanmakuStore } from "../stores/danmaku";
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
  return {
    type: "sc" as const,
    uid,
    uname: `user${uid}`,
    text,
    price,
    ts: 1_700_000_000,
  };
}

describe("DanmakuList.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
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

  it("renders GuardBadge with 总督 for guard_level=3", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 3 }));

    const wrapper = mount(DanmakuList);
    const badge = wrapper.find('[data-testid="guard-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe("总督");
    expect(wrapper.text()).not.toContain("[guard3]");
  });

  it("renders GuardBadge with 提督 for guard_level=2", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 2 }));

    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="guard-badge"]').text()).toBe("提督");
  });

  it("renders GuardBadge with 舰长 for guard_level=1", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 1 }));

    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="guard-badge"]').text()).toBe("舰长");
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