import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { useDanmakuStore, DANMAKU_CAP, SC_CAP } from "./danmaku";
import type { BridgeMessageEvent, BridgeScEvent } from "../api/ws";

function makeDanmaku(uid: number, text: string): BridgeMessageEvent {
  return {
    type: "danmaku",
    uid,
    uname: `user${uid}`,
    text,
    ts: 1_700_000_000,
    guard_level: 0,
    medal: null,
  };
}

function makeSc(uid: number, text: string, price: number): BridgeScEvent {
  const now = Math.floor(Date.now() / 1000);
  return {
    type: "sc",
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

describe("danmaku store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("addDanmaku appends events and exposes them in the list", () => {
    const store = useDanmakuStore();
    const ev = makeDanmaku(1, "hi");
    store.addDanmaku(ev);

    expect(store.list).toHaveLength(1);
    expect(store.list[0]).toEqual(ev);
  });

  it("caps the list at 500 — feeding 501 keeps only the newest 500", () => {
    const store = useDanmakuStore();
    expect(DANMAKU_CAP).toBe(500);

    for (let i = 0; i < 501; i++) {
      store.addDanmaku(makeDanmaku(i, `msg-${i}`));
    }

    expect(store.list).toHaveLength(500);
    // Oldest entry (uid=0) must have been dropped.
    expect(store.list[0]?.uid).toBe(1);
    // Newest entry preserved.
    expect(store.list[499]?.uid).toBe(500);
  });

  it("caps at 500 with guard_level/medal fields preserved through trim", () => {
    const store = useDanmakuStore();
    const rich: BridgeMessageEvent = {
      type: "danmaku",
      uid: 7,
      uname: "rich",
      text: "rich text",
      ts: 1_700_000_000,
      guard_level: 3,
      medal: { name: "粉丝牌", level: 25 },
    };
    for (let i = 0; i < 500; i++) {
      store.addDanmaku(makeDanmaku(i, `msg-${i}`));
    }
    store.addDanmaku(rich);

    expect(store.list).toHaveLength(500);
    expect(store.list[499]).toEqual(rich);
  });

  it("keeps only the newest active SC cards", () => {
    const store = useDanmakuStore();
    for (let i = 0; i < SC_CAP + 1; i++) {
      store.addSc(makeSc(i, `sc-${i}`, 50));
    }
    expect(store.scList).toHaveLength(SC_CAP);
    expect(store.scList[0]?.id).toBe(`sc-${SC_CAP}`);
  });

  it("expires SC at the end_ts supplied by B站", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    const store = useDanmakuStore();
    store.addSc(makeSc(1, "paid", 30));
    expect(store.scList).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(299_999);
    expect(store.scList).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(store.scList).toHaveLength(0);
  });

  it("removes SC immediately on SUPER_CHAT_MESSAGE_DELETE", () => {
    const store = useDanmakuStore();
    store.addSc(makeSc(1, "one", 30));
    store.addSc(makeSc(2, "two", 100));
    store.removeSc(["sc-1"]);
    expect(store.scList.map((item) => item.id)).toEqual(["sc-2"]);
  });

  it("clear empties both lists", () => {
    const store = useDanmakuStore();
    store.addDanmaku(makeDanmaku(1, "x"));
    store.addSc(makeSc(1, "x", 30));
    expect(store.list).toHaveLength(1);
    expect(store.scList).toHaveLength(1);

    store.clear();

    expect(store.list).toHaveLength(0);
    expect(store.scList).toHaveLength(0);
  });
});
