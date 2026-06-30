<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useDanmakuStore } from "../stores/danmaku";
import FanMedal from "./FanMedal.vue";
import GuardBadge from "./GuardBadge.vue";
import SuperChatItem from "./SuperChatItem.vue";

/**
 * T24 — danmaku + SC list, fully assembled.
 *
 * Renders the live chat buffer. Each danmaku row may wear up to two
 * badges:
 *   * ``GuardBadge`` — fleet tier (舰长/提督/总督)
 *   * ``FanMedal``   — channel fan-medal chip
 *
 * SuperChats render via ``SuperChatItem`` with their own price-tagged
 * styling so they're visually distinguishable from organic chat.
 *
 * Auto-scroll behaviour: every time the list grows, we scroll the
 * container to the bottom — except when the user has scrolled up
 * to read history. The ``userScrolledUp`` flag flips on a wheel/
 * scroll event and resets whenever a NEW event arrives AND the
 * container was already pinned to the bottom.
 */
const danmaku = useDanmakuStore();
const scrollRoot = ref<HTMLElement | null>(null);
const userScrolledUp = ref<boolean>(false);

const hasContent = computed(
  () => danmaku.list.length > 0 || danmaku.scList.length > 0,
);

function isPinnedToBottom(el: HTMLElement): boolean {
  // 8px tolerance — sub-pixel scroll heights vary by browser.
  return el.scrollHeight - el.scrollTop - el.clientHeight < 8;
}

function onScroll(): void {
  const el = scrollRoot.value;
  if (el === null) return;
  userScrolledUp.value = !isPinnedToBottom(el);
}

watch(
  () => danmaku.list.length + danmaku.scList.length,
  async () => {
    if (userScrolledUp.value) return;
    await nextTick();
    const el = scrollRoot.value;
    if (el !== null) {
      el.scrollTop = el.scrollHeight;
    }
  },
);

function onClear(): void {
  danmaku.clear();
  userScrolledUp.value = false;
}

function formatTs(ts: number): string {
  const d = new Date(ts * 1000);
  const pad = (n: number): string => n.toString().padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
</script>

<template>
  <div class="danmaku-list" data-testid="danmaku-list">
    <div class="header">
      <span class="title">实时弹幕</span>
      <el-button
        link
        size="small"
        type="primary"
        data-testid="clear-btn"
        @click="onClear"
      >
        清空
      </el-button>
    </div>

    <div
      ref="scrollRoot"
      class="scroll-root"
      data-testid="scroll-root"
      @scroll="onScroll"
    >
      <template v-if="!hasContent">
        <div class="empty" data-testid="empty">还没有弹幕…</div>
      </template>
      <template v-else>
        <SuperChatItem
          v-for="(sc, idx) in danmaku.scList"
          :key="`sc-${idx}`"
          :sc="sc"
        />
        <div
          v-for="(item, idx) in danmaku.list"
          :key="`d-${idx}`"
          class="row"
          data-testid="danmaku-row"
        >
          <GuardBadge :level="item.guard_level" />
          <FanMedal :medal="item.medal" />
          <span class="uname">{{ item.uname }}</span>
          <span class="sep">:</span>
          <span class="text">{{ item.text }}</span>
          <span class="ts">{{ formatTs(item.ts) }}</span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.danmaku-list {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 720px;
  height: 360px;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 8px;
  overflow: hidden;
  background: var(--el-bg-color, #ffffff);
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter, #ebeef5);
  font-weight: 600;
  font-size: 13px;
}
.title {
  color: var(--el-text-color-primary, #303133);
}
.scroll-root {
  flex: 1;
  overflow-y: auto;
  padding: 6px 12px;
  font-size: 13px;
  line-height: 1.6;
}
.empty {
  text-align: center;
  color: var(--el-text-color-secondary, #909399);
  padding: 32px 0;
}
.row {
  display: flex;
  gap: 6px;
  align-items: baseline;
  flex-wrap: wrap;
  padding: 2px 0;
}
.row .uname {
  color: var(--el-color-primary-light-3, #79bbff);
  font-weight: 500;
}
.row .sep {
  color: var(--el-text-color-secondary, #c0c4cc);
}
.row .text {
  color: var(--el-text-color-primary, #303133);
  flex: 1 1 auto;
  word-break: break-word;
}
.row .ts {
  color: var(--el-text-color-secondary, #c0c4cc);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
</style>