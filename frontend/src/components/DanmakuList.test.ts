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

  it("shows [guard{level}] placeholder when guard_level > 0", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 3 }));

    const wrapper = mount(DanmakuList);
    const badge = wrapper.find('[data-testid="guard-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe("[guard3]");
  });

  it("does NOT render a guard badge for guard_level=0", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { guard_level: 0 }));

    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="guard-badge"]').exists()).toBe(false);
  });

  it("shows [{name} lv{level}] medal placeholder when medal present", () => {
    const store = useDanmakuStore();
    store.addDanmaku(
      makeDanmaku(1, "hi", { medal: { name: "粉丝牌", level: 25 } }),
    );

    const wrapper = mount(DanmakuList);
    const medal = wrapper.find('[data-testid="medal-badge"]');
    expect(medal.exists()).toBe(true);
    expect(medal.text()).toBe("[粉丝牌 lv25]");
  });

  it("does NOT render a medal badge when medal is null", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "hi", { medal: null }));

    const wrapper = mount(DanmakuList);
    expect(wrapper.find('[data-testid="medal-badge"]').exists()).toBe(false);
  });

  it("renders SC items distinctly with price highlighted", () => {
    const store = useDanmakuStore();
    store.addSc(makeSc(7, "gift!", 500));

    const wrapper = mount(DanmakuList);
    const scRow = wrapper.find('[data-testid="sc-row"]');
    expect(scRow.exists()).toBe(true);
    const price = wrapper.find('[data-testid="sc-price"]');
    expect(price.exists()).toBe(true);
    expect(price.text()).toBe("¥500");
    expect(scRow.text()).toContain("user7");
    expect(scRow.text()).toContain("gift!");
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
});