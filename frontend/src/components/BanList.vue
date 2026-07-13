<script setup lang="ts">
/**
 * T19 — Ban list panel.
 *
 * Renders every entry currently in :class:`useBanStore`. The panel
 * is purely WS-driven: nothing in here ever polls the backend.
 *
 * Each row exposes a 解禁 button that:
 *
 * 1. Runs ``window.confirm`` (二次确认) — must accept before the
 *    DELETE is even formed.
 * 2. Requires ``entry.block_id`` — without it the backend
 *    can't address the ban, so we surface an inline error rather
 *    than issue a malformed request.
 * 3. ``DELETE /api/ban`` with ``{room_id, block_id, uid}``.
 * 4. On 200 → optimistic ``removeBan(uid)`` (the WS push will also
 *    arrive shortly and be a no-op).
 * 5. On error → row stays put, ``unban-error`` slot populated.
 */
import { computed, ref } from "vue";
import { httpClient } from "../api/client";
import { useRoomStore } from "../stores/room";
import { useBanStore, type BanEntry } from "../stores/ban";

const room = useRoomStore();
const banStore = useBanStore();

const errorMsg = ref<string | null>(null);
const refreshing = ref<boolean>(false);
const query = ref<string>("");

const entries = computed<BanEntry[]>(() => banStore.banList);
const filteredEntries = computed<BanEntry[]>(() => {
  const needle = query.value.trim().toLocaleLowerCase();
  if (!needle) return entries.value;
  return entries.value.filter((entry) =>
    entry.uname.toLocaleLowerCase().includes(needle)
    || String(entry.uid).includes(needle)
    || entry.reason.toLocaleLowerCase().includes(needle),
  );
});

function formatDuration(hour: number | null): string {
  if (hour === null) return "—";
  // Existing B站 lists may already contain permanent records even though
  // this phase deliberately does not expose a permanent-ban action.
  if (hour === -1) return "永久";
  if (hour === 0) return "本场";
  if (hour === 168) return "7天";
  if (hour === 720) return "30天";
  return `${hour}小时`;
}

function formatTime(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (value <= 0) return "—";
  const d: Date = new Date(value * 1000);
  const pad = (n: number): string => n.toString().padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function onRefresh(): Promise<void> {
  errorMsg.value = null;
  if (room.currentRoomId === null) {
    errorMsg.value = "未连接房间";
    return;
  }
  refreshing.value = true;
  banStore.setLoading(true);
  try {
    const response = await httpClient.get<{ bans: BanEntry[] }>(
      `/ban-list/${room.currentRoomId}`,
      { params: { refresh: true } },
    );
    banStore.applySnapshot(response.data.bans);
  } catch (err) {
    errorMsg.value = (err as Error).message || "刷新禁言列表失败";
  } finally {
    refreshing.value = false;
    banStore.setLoading(false);
  }
}

async function onUnban(entry: BanEntry): Promise<void> {
  errorMsg.value = null;
  if (room.currentRoomId === null) {
    errorMsg.value = "未连接房间";
    return;
  }
  if (entry.block_id === null || entry.pending) {
    errorMsg.value = `uid:${entry.uid} 缺少 block_id,无法解禁`;
    return;
  }
  if (!banStore.beginSubmission(entry.uid)) return;
  const accepted: boolean = window.confirm(
    `确认解禁 ${entry.uname || `uid:${entry.uid}`}？`,
  );
  if (!accepted) {
    banStore.endSubmission(entry.uid);
    return;
  }

  try {
    await httpClient.delete("/ban", {
      data: {
        room_id: room.currentRoomId,
        block_id: entry.block_id,
        uid: entry.uid,
      },
    });
    banStore.removeBan(entry.uid);
  } catch (err) {
    errorMsg.value = (err as Error).message || "解禁失败";
  } finally {
    banStore.endSubmission(entry.uid);
  }
}
</script>

<template>
  <div class="ban-list" data-testid="ban-list">
    <div class="header">
      <span class="panel-heading">
        <span class="eyebrow">MODERATION RECORDS</span>
        <span class="title-line">
          <h2 class="title">禁言名单</h2>
          <span class="count" data-testid="count">{{ entries.length }}</span>
        </span>
      </span>
      <span class="header-actions">
        <el-button
          link
          type="primary"
          size="small"
          :loading="refreshing"
          :disabled="room.currentRoomId === null"
          data-testid="refresh-ban-list-btn"
          @click="onRefresh"
        >
          刷新名单
        </el-button>
      </span>
    </div>

    <div class="search-bar">
      <el-input
        v-model="query"
        clearable
        size="small"
        aria-label="搜索禁言名单"
        placeholder="搜索用户名、UID 或原因"
        data-testid="ban-search-input"
      />
    </div>

    <div class="scroll-root">
      <div v-if="banStore.loading" class="loading" data-testid="ban-list-loading">
        正在刷新禁言名单…
      </div>
      <template v-else-if="filteredEntries.length === 0">
        <div class="empty" data-testid="empty">
          <span class="empty-icon" aria-hidden="true">⊘</span>
          <strong>{{ entries.length === 0 ? "暂无禁言记录" : "没有匹配的记录" }}</strong>
          <span>{{ entries.length === 0 ? "名单变化会自动同步" : "换个关键词再试试" }}</span>
        </div>
      </template>
      <template v-else>
        <div
          v-for="entry in filteredEntries"
          :key="entry.uid"
          class="ban-row"
          data-testid="ban-row"
        >
          <div class="ban-row-top">
            <span class="identity">
              <span class="avatar" aria-hidden="true">{{ entry.uname.slice(0, 1) || "?" }}</span>
              <span class="uname">
                <strong>{{ entry.uname || `uid:${entry.uid}` }}</strong>
                <span class="uid">uid:{{ entry.uid }}</span>
              </span>
            </span>
            <span class="hour" data-testid="hour-label">
              {{ formatDuration(entry.hour) }}
            </span>
            <el-button
              link
              type="primary"
              size="small"
              :loading="banStore.isSubmitting(entry.uid)"
              :disabled="entry.pending || entry.block_id === null || banStore.isSubmitting(entry.uid)"
              data-testid="unban-btn"
              :aria-label="`解禁 ${entry.uname || `uid:${entry.uid}`}`"
              @click="onUnban(entry)"
            >
              解禁
            </el-button>
          </div>
          <div class="ban-row-meta">
            <span class="ctime">{{ formatTime(entry.created_at) }}</span>
            <span v-if="entry.pending" class="pending" data-testid="pending-label">
              正在同步
            </span>
          </div>
          <p v-if="entry.reason" class="reason" :title="entry.reason">
            {{ entry.reason }}
          </p>
        </div>
      </template>
    </div>

    <p
      v-if="errorMsg"
      class="error"
      data-testid="unban-error"
    >
      {{ errorMsg }}
    </p>
  </div>
</template>

<style scoped>
.ban-list {
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
  padding: 11px 14px;
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
  letter-spacing: 1.25px;
}
.title-line {
  display: flex;
  align-items: center;
  gap: 7px;
}
.header .title {
  margin: 0;
  color: var(--cc-text);
  font-size: 15px;
  font-weight: 680;
}
.header .count {
  display: inline-flex;
  min-width: 23px;
  height: 19px;
  padding: 0 6px;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  background: var(--cc-danger-soft);
  color: var(--cc-danger-emphasis);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.header-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.search-bar {
  padding: 10px 12px;
  border-bottom: 1px solid var(--cc-border);
  background: var(--cc-fill-inset);
}
.scroll-root {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 7px;
  font-size: 13px;
  line-height: 1.45;
}
.empty {
  display: flex;
  height: 100%;
  min-height: 250px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  text-align: center;
  color: var(--cc-text-muted);
}
.empty-icon {
  display: grid;
  width: 42px;
  height: 42px;
  margin-bottom: 3px;
  place-items: center;
  border: 1px solid var(--cc-border);
  border-radius: 13px;
  color: var(--cc-text-secondary);
  background: var(--cc-fill-subtle);
  font-size: 18px;
}
.empty strong {
  color: var(--cc-text-secondary);
  font-size: 12px;
}
.empty > span:last-child {
  font-size: 10px;
}
.ban-row {
  padding: 10px;
  border: 1px solid transparent;
  border-bottom-color: var(--cc-separator);
  border-radius: 10px;
  transition: border-color 150ms ease, background-color 150ms ease;
}
.ban-row:hover,
.ban-row:focus-within {
  border-color: var(--cc-border);
  background: var(--cc-surface-hover);
}
.ban-row-top,
.ban-row-meta,
.identity {
  display: flex;
  align-items: center;
}
.ban-row-top {
  gap: 8px;
}
.identity {
  min-width: 0;
  flex: 1;
  gap: 8px;
}
.avatar {
  display: grid;
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  place-items: center;
  border: 1px solid var(--cc-border);
  border-radius: 8px;
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
  font-size: 10px;
  font-weight: 720;
}
.ban-row .uname {
  display: flex;
  min-width: 0;
  flex-direction: column;
  color: var(--cc-text);
  font-weight: 580;
}
.ban-row .uname strong {
  overflow: hidden;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ban-row .uid {
  color: var(--cc-text-muted);
  font-weight: 400;
  font-size: 9px;
  font-variant-numeric: tabular-nums;
}
.ban-row .hour {
  flex: 0 0 auto;
  padding: 3px 6px;
  border-radius: 6px;
  color: var(--cc-danger-emphasis);
  background: var(--cc-danger-soft);
  font-size: 9px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.ban-row-meta {
  min-height: 18px;
  padding: 5px 0 0 36px;
  gap: 7px;
}
.ban-row .ctime {
  color: var(--cc-text-muted);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
}
.loading {
  padding: 44px 0;
  color: var(--cc-text-secondary);
  text-align: center;
}
.ban-row .reason {
  overflow: hidden;
  margin: 3px 0 0 36px;
  padding: 5px 7px;
  border-radius: 6px;
  color: var(--cc-text-secondary);
  background: var(--cc-fill-subtle);
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ban-row .pending {
  color: var(--cc-warning);
  font-size: 9px;
}
.error {
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--cc-danger);
  border-top: 1px solid rgb(255 107 122 / 16%);
  background: var(--cc-danger-soft);
}
@media (max-width: 900px) {
  .ban-list {
    min-height: 480px;
  }
}
@media (max-width: 520px) {
  .header {
    padding: 10px 12px;
  }
  .ban-row :deep(.el-button) {
    min-height: 36px;
  }
}
@media (orientation: portrait) and (min-width: 680px) and (min-height: 900px) and (hover: hover) and (pointer: fine) {
  .ban-list {
    height: 100%;
    min-height: 0;
  }
}
</style>
