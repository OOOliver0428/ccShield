/**
 * danmaku store (T14).
 *
 * Holds the in-memory chat stream the UI renders. The store is a
 * dumb append target — it doesn't know about WebSockets, the bridge,
 * or anything transport-shaped; the App.vue dispatcher pushes events
 * into it as they arrive.
 *
 * Cap rationale (500): a chatty room can easily emit >1k danmaku per
 * minute; rendering 500 fixed-height rows comfortably fits a tall
 * viewport and keeps the React-ish virtual DOM diff cheap. Older
 * messages are dropped (slice oldest) so memory + DOM cost stay
 * bounded.
 *
 * SuperChats are kept in a separate list because they're rendered
 * distinctly (price badge, persistent until cleared).
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import type { BridgeMessageEvent, BridgeScEvent } from "../api/ws";

export type DanmakuItem = BridgeMessageEvent;
export type ScItem = BridgeScEvent;

const DANMAKU_CAP = 500;

export const useDanmakuStore = defineStore("danmaku", () => {
  const list = ref<DanmakuItem[]>([]);
  const scList = ref<ScItem[]>([]);

  function addDanmaku(event: DanmakuItem): void {
    list.value.push(event);
    if (list.value.length > DANMAKU_CAP) {
      // slice(-CAP) keeps the NEWEST CAP items (drops the oldest).
      list.value = list.value.slice(-DANMAKU_CAP);
    }
  }

  function addSc(event: ScItem): void {
    scList.value.push(event);
  }

  function clear(): void {
    list.value = [];
    scList.value = [];
  }

  return { list, scList, addDanmaku, addSc, clear };
});

export { DANMAKU_CAP };