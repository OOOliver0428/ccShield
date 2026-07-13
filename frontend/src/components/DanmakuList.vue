<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { httpClient } from "../api/client";
import { useBanStore } from "../stores/ban";
import { useDanmakuStore, type DanmakuItem } from "../stores/danmaku";
import { useRoomStore } from "../stores/room";
import BanControls from "./BanControls.vue";
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
const banStore = useBanStore();
const room = useRoomStore();
const scrollRoot = ref<HTMLElement | null>(null);
const userScrolledUp = ref<boolean>(false);
const selectedForBan = ref<DanmakuItem | null>(null);
const actionError = ref<string | null>(null);

const hasDanmaku = computed(() => danmaku.list.length > 0);

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
  () => danmaku.list.length,
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
          v-for="sc in danmaku.scList"
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
      data-testid="scroll-root"
      role="log"
      aria-label="实时弹幕"
      @scroll="onScroll"
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
          v-for="(item, idx) in danmaku.list"
          :key="`d-${idx}`"
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
.sc-panel {
  flex: 0 0 auto;
  max-height: 36%;
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
  display: grid;
  max-height: 210px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  overflow-y: auto;
}
.scroll-root {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 7px 8px 12px;
  font-size: 13px;
  line-height: 1.5;
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
  min-height: 54px;
  padding: 8px 9px;
  align-items: flex-start;
  gap: 10px;
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
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  margin-top: 1px;
  place-items: center;
  border: 1px solid var(--cc-border);
  border-radius: 9px;
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
  font-size: 11px;
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
  gap: 5px;
}
.row .uname {
  overflow: hidden;
  color: var(--cc-primary-emphasis);
  font-size: 11px;
  font-weight: 620;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row .text {
  color: var(--cc-message-text);
  font-size: 12px;
  word-break: break-word;
}
.row .ts {
  margin-left: auto;
  color: var(--cc-text-muted);
  font-size: 9px;
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
  .sc-cards {
    grid-template-columns: minmax(0, 1fr);
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
  .row-actions :deep(.el-button) {
    min-height: 38px;
  }
}
@media (orientation: portrait) and (min-width: 680px) and (min-height: 900px) and (hover: hover) and (pointer: fine) {
  .danmaku-list {
    height: 100%;
    min-height: 0;
  }
  .sc-panel {
    max-height: 30%;
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
