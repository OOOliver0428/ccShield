<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { httpClient } from "../api/client";
import { useBanStore } from "../stores/ban";
import { useDanmakuStore, type DanmakuItem } from "../stores/danmaku";
import { useRoomStore } from "../stores/room";
import BanControls from "./BanControls.vue";
import FanMedal from "./FanMedal.vue";
import GuardBadge from "./GuardBadge.vue";
import SuperChatItem from "./SuperChatItem.vue";

const FONT_STORAGE_KEY = "ccshield-danmaku-font-size";
const DANMAKU_FONT_SIZES = [12, 14, 16, 18] as const;
type DanmakuFontSize = (typeof DANMAKU_FONT_SIZES)[number];

interface FontMetrics {
  uname: number;
  timestamp: number;
  badge: number;
  avatar: number;
  avatarText: number;
  rowMinHeight: number;
  rowPaddingY: number;
  rowGap: number;
}

const FONT_METRICS: Record<DanmakuFontSize, FontMetrics> = {
  12: {
    uname: 11,
    timestamp: 9,
    badge: 9,
    avatar: 30,
    avatarText: 11,
    rowMinHeight: 54,
    rowPaddingY: 8,
    rowGap: 10,
  },
  14: {
    uname: 13,
    timestamp: 10,
    badge: 10,
    avatar: 32,
    avatarText: 12,
    rowMinHeight: 60,
    rowPaddingY: 9,
    rowGap: 10,
  },
  16: {
    uname: 15,
    timestamp: 11,
    badge: 11,
    avatar: 34,
    avatarText: 13,
    rowMinHeight: 68,
    rowPaddingY: 10,
    rowGap: 11,
  },
  18: {
    uname: 17,
    timestamp: 12,
    badge: 12,
    avatar: 36,
    avatarText: 14,
    rowMinHeight: 76,
    rowPaddingY: 11,
    rowGap: 12,
  },
};

function readStoredFontSize(): DanmakuFontSize {
  try {
    const stored = Number(window.localStorage.getItem(FONT_STORAGE_KEY));
    if (DANMAKU_FONT_SIZES.some((size) => size === stored)) {
      return stored as DanmakuFontSize;
    }
  } catch {
    // The control remains usable when browser storage is unavailable.
  }
  return DANMAKU_FONT_SIZES[0];
}

/**
 * Danmaku and Super Chat list.
 *
 * Renders the live chat buffer. Each danmaku row may wear up to two
 * badges:
 *   * ``GuardBadge`` — fleet tier (舰长/提督/总督)
 *   * ``FanMedal``   — channel fan-medal chip
 *
 * SuperChats render via ``SuperChatItem`` with their own price-tagged
 * styling so they're visually distinguishable from organic chat.
 *
 * Auto-scroll behaviour: every new message follows the live edge unless the
 * user scrolls upward to review history. New messages never release review
 * mode; instead a footer action lets the moderator jump back to the latest
 * message and resume following the stream.
 */
const danmaku = useDanmakuStore();
const banStore = useBanStore();
const room = useRoomStore();
const scrollRoot = ref<HTMLElement | null>(null);
const userScrolledUp = ref<boolean>(false);
const unseenDanmakuCount = ref<number>(0);
const selectedForBan = ref<DanmakuItem | null>(null);
const actionError = ref<string | null>(null);
const initialFontSize = readStoredFontSize();
const fontSizeIndex = ref<number>(DANMAKU_FONT_SIZES.indexOf(initialFontSize));
const rowKeys = new WeakMap<DanmakuItem, number>();
let nextRowKey = 1;

const hasDanmaku = computed(() => danmaku.list.length > 0);
const sortedScList = computed(() =>
  [...danmaku.scList].sort((left, right) => right.price - left.price),
);
const selectedFontSize = computed<DanmakuFontSize>(
  () => DANMAKU_FONT_SIZES[fontSizeIndex.value] ?? DANMAKU_FONT_SIZES[0],
);
const canDecreaseFont = computed(() => fontSizeIndex.value > 0);
const canIncreaseFont = computed(
  () => fontSizeIndex.value < DANMAKU_FONT_SIZES.length - 1,
);
const danmakuFontStyle = computed<Record<string, string>>(() => {
  const metrics = FONT_METRICS[selectedFontSize.value];
  return {
    "--danmaku-text-size": `${selectedFontSize.value}px`,
    "--danmaku-uname-size": `${metrics.uname}px`,
    "--danmaku-time-size": `${metrics.timestamp}px`,
    "--danmaku-badge-size": `${metrics.badge}px`,
    "--danmaku-avatar-size": `${metrics.avatar}px`,
    "--danmaku-avatar-text-size": `${metrics.avatarText}px`,
    "--danmaku-row-min-height": `${metrics.rowMinHeight}px`,
    "--danmaku-row-padding-y": `${metrics.rowPaddingY}px`,
    "--danmaku-row-gap": `${metrics.rowGap}px`,
  };
});

interface ReviewAnchor {
  key: string;
  offset: number;
}

function captureReviewAnchor(el: HTMLElement): ReviewAnchor | null {
  const rootTop = el.getBoundingClientRect().top;
  const rows = Array.from(
    el.querySelectorAll<HTMLElement>("[data-row-key]"),
  );
  const row = rows.find(
    (candidate) => candidate.getBoundingClientRect().bottom > rootTop,
  ) ?? rows[0];
  if (row === undefined) return null;
  return {
    key: row.dataset.rowKey ?? "",
    offset: row.getBoundingClientRect().top - rootTop,
  };
}

function persistFontSize(size: DanmakuFontSize): void {
  try {
    window.localStorage.setItem(FONT_STORAGE_KEY, String(size));
  } catch {
    // In-memory adjustment still works when storage is unavailable.
  }
}

async function changeFontSize(direction: -1 | 1): Promise<void> {
  const nextIndex = fontSizeIndex.value + direction;
  if (nextIndex < 0 || nextIndex >= DANMAKU_FONT_SIZES.length) return;

  const el = scrollRoot.value;
  const wasPinned = el !== null
    && (!userScrolledUp.value || isPinnedToBottom(el));
  const reviewAnchor = el !== null && !wasPinned
    ? captureReviewAnchor(el)
    : null;

  fontSizeIndex.value = nextIndex;
  persistFontSize(selectedFontSize.value);
  await nextTick();

  if (el === null) return;
  if (wasPinned) {
    el.scrollTop = el.scrollHeight;
    return;
  }
  if (reviewAnchor === null || reviewAnchor.key === "") return;
  const anchoredRow = el.querySelector<HTMLElement>(
    `[data-row-key="${reviewAnchor.key}"]`,
  );
  if (anchoredRow === null) return;
  const currentOffset = anchoredRow.getBoundingClientRect().top
    - el.getBoundingClientRect().top;
  el.scrollTop += currentOffset - reviewAnchor.offset;
}

function rowKey(item: DanmakuItem): number {
  const existing = rowKeys.get(item);
  if (existing !== undefined) return existing;
  const key = nextRowKey;
  nextRowKey += 1;
  rowKeys.set(item, key);
  return key;
}

function isPinnedToBottom(el: HTMLElement): boolean {
  // 8px tolerance — sub-pixel scroll heights vary by browser.
  return el.scrollHeight - el.scrollTop - el.clientHeight < 8;
}

function onScroll(): void {
  const el = scrollRoot.value;
  if (el === null) return;
  const pinned = isPinnedToBottom(el);
  userScrolledUp.value = !pinned;
  if (pinned) unseenDanmakuCount.value = 0;
}

function onWheel(event: WheelEvent): void {
  const el = scrollRoot.value;
  if (
    event.deltaY < 0
    && el !== null
    && el.scrollHeight > el.clientHeight
  ) {
    userScrolledUp.value = true;
  }
}

watch(
  () => danmaku.list.at(-1),
  async (latest, previous) => {
    if (latest === undefined || latest === previous) return;
    if (userScrolledUp.value) {
      unseenDanmakuCount.value += 1;
      return;
    }
    await nextTick();
    const el = scrollRoot.value;
    if (el !== null) {
      el.scrollTop = el.scrollHeight;
    }
  },
);

async function jumpToLatest(): Promise<void> {
  userScrolledUp.value = false;
  unseenDanmakuCount.value = 0;
  await nextTick();
  const el = scrollRoot.value;
  if (el !== null) el.scrollTop = el.scrollHeight;
}

function onClear(): void {
  danmaku.clear();
  userScrolledUp.value = false;
  unseenDanmakuCount.value = 0;
}

function formatTs(ts: number): string {
  // The bridge contract is seconds, but tolerate a raw millisecond value so
  // mixed frontend/backend hot reloads cannot recreate the jumping clock bug.
  const epochMs = ts >= 100_000_000_000 ? ts : ts * 1000;
  const d = new Date(epochMs);
  const pad = (n: number): string => n.toString().padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function openMoreBan(item: DanmakuItem): void {
  actionError.value = null;
  selectedForBan.value = item;
}

function closeMoreBan(): void {
  selectedForBan.value = null;
}

async function quickSessionBan(item: DanmakuItem): Promise<void> {
  actionError.value = null;
  if (room.currentRoomId === null) {
    actionError.value = "未连接房间";
    return;
  }
  if (!banStore.beginSubmission(item.uid)) return;

  const accepted = window.confirm(
    `确认本场禁言？\n用户：${item.uname} (uid:${item.uid})\n发言：${item.text}\n期限：本场直播`,
  );
  if (!accepted) {
    banStore.endSubmission(item.uid);
    return;
  }

  try {
    await httpClient.post("/ban", {
      room_id: room.currentRoomId,
      uid: item.uid,
      uname: item.uname,
      hour: 0,
      reason: "",
    });
    banStore.addBan({
      block_id: null,
      uid: item.uid,
      uname: item.uname,
      hour: 0,
      reason: "",
      created_at: Math.floor(Date.now() / 1000),
      expires_at: null,
      pending: true,
    });
    ElMessage.success({
      message: `已成功禁言 ${item.uname}（本场直播）`,
      duration: 3000,
    });
  } catch (err) {
    actionError.value = (err as Error).message || "禁言失败";
  } finally {
    banStore.endSubmission(item.uid);
  }
}
</script>

<template>
  <div class="danmaku-list" data-testid="danmaku-list">
    <div class="header">
      <div class="panel-heading">
        <span class="eyebrow">LIVE MESSAGE STREAM</span>
        <div class="title-line">
          <h2 class="title">实时弹幕</h2>
          <span class="message-count">{{ danmaku.list.length }}</span>
        </div>
      </div>
      <div class="header-actions">
        <span class="live-chip"><i aria-hidden="true" /> 实时</span>
        <div class="font-control" role="group" aria-label="实时弹幕字号">
          <button
            type="button"
            class="font-button"
            :disabled="!canDecreaseFont"
            data-testid="font-decrease-btn"
            aria-label="减小实时弹幕字号"
            @click="changeFontSize(-1)"
          >A−</button>
          <span class="font-size-label" data-testid="font-size-label">
            {{ selectedFontSize }}px
          </span>
          <button
            type="button"
            class="font-button"
            :disabled="!canIncreaseFont"
            data-testid="font-increase-btn"
            aria-label="增大实时弹幕字号"
            @click="changeFontSize(1)"
          >A+</button>
        </div>
        <el-button
          link
          size="small"
          type="primary"
          data-testid="clear-btn"
          @click="onClear"
        >
          清空记录
        </el-button>
      </div>
    </div>

    <section
      v-if="danmaku.scList.length > 0"
      class="sc-panel"
      data-testid="sc-panel"
    >
      <div class="sc-panel-header">
        <span><i aria-hidden="true">◆</i> 醒目留言</span>
        <span class="sc-count">展示中 {{ danmaku.scList.length }}</span>
      </div>
      <div class="sc-cards">
        <SuperChatItem
          v-for="sc in sortedScList"
          :key="sc.id"
          :sc="sc"
        />
      </div>
    </section>

    <section
      v-if="selectedForBan"
      class="ban-panel"
      data-testid="ban-panel"
    >
      <button
        type="button"
        class="ban-panel-close"
        aria-label="关闭禁言面板"
        @click="closeMoreBan"
      >
        ×
      </button>
      <BanControls
        :uid="selectedForBan.uid"
        :uname="selectedForBan.uname"
        :message="selectedForBan.text"
        @success="closeMoreBan"
      />
    </section>

    <p v-if="actionError" class="action-error" data-testid="ban-action-error">
      {{ actionError }}
    </p>

    <div
      ref="scrollRoot"
      class="scroll-root"
      :style="danmakuFontStyle"
      :data-font-size="selectedFontSize"
      data-testid="scroll-root"
      role="log"
      aria-label="实时弹幕"
      @scroll="onScroll"
      @wheel.passive="onWheel"
    >
      <template v-if="!hasDanmaku">
        <div class="empty" data-testid="empty">
          <span class="empty-icon" aria-hidden="true">⌁</span>
          <strong>等待第一条弹幕</strong>
          <span>连接稳定，消息将在这里实时出现</span>
        </div>
      </template>
      <template v-else>
        <div
          v-for="item in danmaku.list"
          :key="rowKey(item)"
          :data-row-key="rowKey(item)"
          class="row"
          data-testid="danmaku-row"
        >
          <span class="avatar" aria-hidden="true">{{ item.uname.slice(0, 1) || "?" }}</span>
          <span class="message-body">
            <span class="message-meta">
              <GuardBadge :level="item.guard_level" />
              <FanMedal :medal="item.medal" />
              <span class="uname">{{ item.uname }}</span>
              <span class="ts">{{ formatTs(item.ts) }}</span>
            </span>
            <span class="text">{{ item.text }}</span>
          </span>
          <span class="row-actions">
            <el-button
              link
              type="danger"
              size="small"
              :disabled="item.uid <= 0 || banStore.isSubmitting(item.uid)"
              data-testid="quick-ban-btn"
              :aria-label="`本场禁言 ${item.uname}`"
              @click.stop="quickSessionBan(item)"
            >
              本场禁言
            </el-button>
            <el-button
              link
              type="primary"
              size="small"
              :disabled="item.uid <= 0 || banStore.isSubmitting(item.uid)"
              data-testid="more-ban-btn"
              :aria-label="`更多禁言选项 ${item.uname}`"
              @click.stop="openMoreBan(item)"
            >
              更多禁言
            </el-button>
          </span>
        </div>
      </template>
    </div>

    <div
      v-if="userScrolledUp && unseenDanmakuCount > 0"
      class="review-footer"
      data-testid="review-footer"
    >
      <button
        type="button"
        class="latest-button"
        data-testid="view-latest-btn"
        @click="jumpToLatest"
      >
        <span>查看最新弹幕</span>
        <strong>{{ unseenDanmakuCount }}</strong>
      </button>
    </div>
  </div>
</template>

<style scoped>
.danmaku-list {
  position: relative;
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
  border: 1px solid var(--cc-border);
  border-radius: var(--cc-radius-panel);
  overflow: hidden;
  background: var(--cc-panel-background);
  box-shadow: var(--cc-shadow-panel);
}
.header {
  display: flex;
  min-height: 62px;
  align-items: center;
  justify-content: space-between;
  padding: 11px 15px;
  border-bottom: 1px solid var(--cc-border);
  background: var(--cc-fill-faint);
}
.panel-heading {
  min-width: 0;
}
.eyebrow {
  display: block;
  margin-bottom: 3px;
  color: var(--cc-text-muted);
  font-size: 8px;
  font-weight: 750;
  letter-spacing: 1.35px;
}
.title-line,
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.title {
  margin: 0;
  color: var(--cc-text);
  font-size: 15px;
  font-weight: 680;
}
.message-count {
  display: inline-flex;
  min-width: 23px;
  height: 19px;
  padding: 0 6px;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.live-chip {
  display: inline-flex;
  padding: 5px 8px;
  align-items: center;
  gap: 6px;
  border: 1px solid rgb(66 211 146 / 18%);
  border-radius: 999px;
  color: var(--cc-success);
  background: var(--cc-success-soft);
  font-size: 10px;
  font-weight: 650;
}
.live-chip i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 10px currentColor;
}
.font-control {
  display: inline-flex;
  height: 28px;
  align-items: center;
  overflow: hidden;
  border: 1px solid var(--cc-border-strong);
  border-radius: 8px;
  background: var(--cc-fill-soft);
}
.font-button {
  display: grid;
  width: 28px;
  height: 100%;
  padding: 0;
  place-items: center;
  border: 0;
  color: var(--cc-text-secondary);
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 10px;
  font-weight: 720;
}
.font-button:hover:not(:disabled) {
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
}
.font-button:disabled {
  color: var(--cc-text-muted);
  cursor: not-allowed;
  opacity: 0.42;
}
.font-size-label {
  display: grid;
  min-width: 37px;
  height: 100%;
  padding: 0 4px;
  place-items: center;
  border-right: 1px solid var(--cc-border);
  border-left: 1px solid var(--cc-border);
  color: var(--cc-text-secondary);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.sc-panel {
  flex: 0 0 auto;
  overflow: hidden;
  padding: 10px 12px 12px;
  border-bottom: 1px solid rgb(242 185 95 / 16%);
  background: linear-gradient(180deg, rgb(242 185 95 / 8%), rgb(242 185 95 / 3%));
}
.sc-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
  color: var(--cc-warning);
  font-size: 11px;
  font-weight: 680;
}
.sc-panel-header i {
  margin-right: 3px;
  font-size: 8px;
}
.sc-count {
  color: var(--cc-text-muted);
  font-size: 10px;
  font-weight: 400;
}
.sc-cards {
  display: flex;
  height: 100px;
  align-items: flex-start;
  gap: 8px;
  overflow-x: auto;
  overflow-y: hidden;
  padding-bottom: 7px;
  overscroll-behavior-inline: contain;
  scroll-snap-type: x proximity;
  scrollbar-gutter: stable;
}
.sc-cards :deep(.sc-card) {
  width: clamp(260px, 42vw, 340px);
  height: 84px;
  flex: 0 0 clamp(260px, 42vw, 340px);
  scroll-snap-align: start;
}
.scroll-root {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 7px 8px 12px;
  font-size: var(--danmaku-text-size, 12px);
  line-height: 1.55;
}
.review-footer {
  display: flex;
  flex: 0 0 auto;
  justify-content: center;
  padding: 8px 12px 10px;
  border-top: 1px solid var(--cc-border);
  background: var(--cc-fill-faint);
}
.latest-button {
  display: inline-flex;
  min-height: 32px;
  padding: 6px 12px;
  align-items: center;
  gap: 8px;
  border: 1px solid rgb(124 140 255 / 32%);
  border-radius: 999px;
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
  cursor: pointer;
  font: inherit;
  font-size: 11px;
  font-weight: 650;
  transition: border-color 140ms ease, background-color 140ms ease;
}
.latest-button:hover {
  border-color: rgb(124 140 255 / 58%);
  background: rgb(124 140 255 / 18%);
}
.latest-button strong {
  display: inline-grid;
  min-width: 19px;
  height: 19px;
  padding: 0 5px;
  place-items: center;
  border-radius: 999px;
  color: var(--cc-on-primary, #fff);
  background: var(--cc-primary);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
}
.ban-panel {
  position: absolute;
  z-index: 5;
  top: 72px;
  right: 14px;
  width: min(430px, calc(100% - 28px));
  padding: 0;
  border: 1px solid var(--cc-border-strong);
  border-radius: 13px;
  background: var(--cc-surface-raised);
  box-shadow: var(--cc-shadow-float);
}
.ban-panel :deep(.ban-controls) {
  min-width: 0;
}
.ban-panel-close {
  position: absolute;
  z-index: 1;
  top: 8px;
  right: 10px;
  width: 30px;
  height: 30px;
  border: 0;
  border-radius: 8px;
  background: var(--cc-fill-soft);
  color: var(--cc-text-secondary);
  cursor: pointer;
  font-size: 18px;
}
.action-error {
  margin: 0;
  padding: 8px 12px;
  border-bottom: 1px solid rgb(255 107 122 / 18%);
  background: var(--cc-danger-soft);
  color: var(--cc-danger);
  font-size: 12px;
}
.empty {
  display: flex;
  height: 100%;
  min-height: 260px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  text-align: center;
  color: var(--cc-text-muted);
}
.empty-icon {
  display: grid;
  width: 43px;
  height: 43px;
  margin-bottom: 3px;
  place-items: center;
  border: 1px solid var(--cc-border);
  border-radius: 13px;
  color: var(--cc-primary);
  background: var(--cc-primary-soft);
  font-size: 21px;
}
.empty strong {
  color: var(--cc-text-secondary);
  font-size: 13px;
}
.empty > span:last-child {
  font-size: 11px;
}
.row {
  position: relative;
  display: flex;
  min-height: var(--danmaku-row-min-height, 54px);
  padding: var(--danmaku-row-padding-y, 8px) 9px;
  align-items: flex-start;
  gap: var(--danmaku-row-gap, 10px);
  border-bottom: 1px solid var(--cc-separator);
  border-radius: 9px;
  transition: background-color 160ms ease;
}
.row:hover,
.row:focus-within {
  background: var(--cc-surface-hover);
}
.avatar {
  display: grid;
  width: var(--danmaku-avatar-size, 30px);
  height: var(--danmaku-avatar-size, 30px);
  flex: 0 0 var(--danmaku-avatar-size, 30px);
  margin-top: 1px;
  place-items: center;
  border: 1px solid var(--cc-border);
  border-radius: 9px;
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
  font-size: var(--danmaku-avatar-text-size, 11px);
  font-weight: 720;
}
.message-body {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 3px;
}
.message-meta {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  row-gap: 3px;
  transition: padding-right 140ms ease;
}
.row .uname {
  overflow: hidden;
  color: var(--cc-primary-emphasis);
  font-size: var(--danmaku-uname-size, 11px);
  font-weight: 620;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row .text {
  color: var(--cc-message-text);
  font-size: var(--danmaku-text-size, 12px);
  word-break: break-word;
}
.row .ts {
  margin-left: auto;
  color: var(--cc-text-muted);
  font-size: var(--danmaku-time-size, 9px);
  font-variant-numeric: tabular-nums;
}
.row-actions {
  position: absolute;
  z-index: 1;
  top: 7px;
  right: 7px;
  display: inline-flex;
  padding: 2px 4px;
  border: 1px solid var(--cc-border);
  border-radius: 8px;
  background: var(--cc-floating-background);
  opacity: 0;
  box-shadow: var(--cc-shadow-float);
  transition: opacity 140ms ease;
}
.row:hover .row-actions,
.row:focus-within .row-actions {
  opacity: 1;
}
.row:hover .message-meta,
.row:focus-within .message-meta {
  padding-right: 140px;
}
@media (hover: none), (max-width: 768px) {
  .row-actions {
    position: static;
    margin-left: 40px;
    border: 0;
    background: transparent;
    opacity: 1;
    box-shadow: none;
  }
  .row {
    flex-wrap: wrap;
  }
  .row:hover .message-meta,
  .row:focus-within .message-meta {
    padding-right: 0;
  }
  .sc-cards :deep(.sc-card) {
    width: min(280px, 82vw);
    flex-basis: min(280px, 82vw);
  }
}
@media (max-width: 900px) {
  .danmaku-list {
    height: min(70dvh, 700px);
    min-height: 520px;
  }
}
@media (max-width: 520px) {
  .header {
    padding: 10px 12px;
  }
  .live-chip {
    display: none;
  }
  .header-actions {
    gap: 5px;
  }
  .row-actions :deep(.el-button) {
    min-height: 38px;
  }
}
@media (orientation: portrait) and (min-width: 680px) and (min-height: 900px) and (hover: hover) and (pointer: fine) {
  .danmaku-list {
    height: 100%;
    min-height: 0;
  }
  .row {
    flex-wrap: nowrap;
  }
  .row-actions {
    position: absolute;
    margin-left: 0;
    border: 1px solid var(--cc-border);
    background: var(--cc-floating-background);
    opacity: 0;
    box-shadow: var(--cc-shadow-float);
  }
  .row:hover .row-actions,
  .row:focus-within .row-actions {
    opacity: 1;
  }
}
</style>
