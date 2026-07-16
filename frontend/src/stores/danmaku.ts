/**
 * Danmaku store.
 *
 * Holds the in-memory chat stream the UI renders. The store is a
 * dumb append target — it doesn't know about WebSockets, the bridge,
 * or anything transport-shaped; the App.vue dispatcher pushes events
 * into it as they arrive.
 *
 * Cap rationale (1000): moderators may need a deeper review window in
 * high-traffic rooms. Older
 * messages are dropped (slice oldest) so memory + DOM cost stay
 * bounded.
 *
 * SuperChats are kept in a separate active list because they're rendered
 * in a pinned block. B站 supplies an authoritative ``end_ts`` based on the
 * paid tier, so each card schedules its own expiry instead of lingering
 * forever or guessing a duration from the price.
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import type { BridgeMessageEvent, BridgeScEvent } from "../api/ws";

export type DanmakuItem = BridgeMessageEvent;
export type ScItem = BridgeScEvent;

const DANMAKU_CAP = 1000;
const SC_CAP = 20;

export const useDanmakuStore = defineStore("danmaku", () => {
  const list = ref<DanmakuItem[]>([]);
  const scList = ref<ScItem[]>([]);
  const scExpiryTimers = new Map<string, ReturnType<typeof setTimeout>>();

  function addDanmaku(event: DanmakuItem): void {
    list.value.push(event);
    if (list.value.length > DANMAKU_CAP) {
      // slice(-CAP) keeps the NEWEST CAP items (drops the oldest).
      list.value = list.value.slice(-DANMAKU_CAP);
    }
  }

  function addSc(event: ScItem): void {
    removeSc([event.id]);

    const remainingMs = event.end_ts * 1000 - Date.now();
    if (remainingMs <= 0) return;

    scList.value = [event, ...scList.value].slice(0, SC_CAP);
    const retainedIds = new Set(scList.value.map((item) => item.id));
    for (const [id, timer] of scExpiryTimers) {
      if (!retainedIds.has(id)) {
        clearTimeout(timer);
        scExpiryTimers.delete(id);
      }
    }

    scExpiryTimers.set(
      event.id,
      setTimeout(() => removeSc([event.id]), remainingMs),
    );
  }

  function removeSc(ids: readonly string[]): void {
    if (ids.length === 0) return;
    const idSet = new Set(ids);
    scList.value = scList.value.filter((item) => !idSet.has(item.id));
    for (const id of idSet) {
      const timer = scExpiryTimers.get(id);
      if (timer !== undefined) {
        clearTimeout(timer);
        scExpiryTimers.delete(id);
      }
    }
  }

  function clear(): void {
    list.value = [];
    scList.value = [];
    for (const timer of scExpiryTimers.values()) clearTimeout(timer);
    scExpiryTimers.clear();
  }

  return { list, scList, addDanmaku, addSc, removeSc, clear };
});

export { DANMAKU_CAP, SC_CAP };
