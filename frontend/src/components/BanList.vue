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
 * 2. Requires ``entry.id`` — without a ``block_id`` the backend
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

const entries = computed<BanEntry[]>(() => banStore.banList);

function formatDuration(hour: number | undefined): string {
  if (hour === undefined) return "—";
  if (hour === -1) return "永久";
  if (hour === 0) return "本场";
  if (hour === 168) return "7天";
  if (hour === 720) return "30天";
  return `${hour}小时`;
}

function formatCtime(ctime: number | undefined): string {
  if (ctime === undefined || ctime <= 0) return "—";
  const d: Date = new Date(ctime * 1000);
  const pad = (n: number): string => n.toString().padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function onUnban(entry: BanEntry): Promise<void> {
  errorMsg.value = null;
  if (room.currentRoomId === null) {
    errorMsg.value = "未连接房间";
    return;
  }
  if (entry.id === undefined || entry.id === "") {
    errorMsg.value = `uid:${entry.uid} 缺少 block_id,无法解禁`;
    return;
  }
  const accepted: boolean = window.confirm(
    `确认解禁 ${entry.uname ?? `uid:${entry.uid}`}？`,
  );
  if (!accepted) return;

  try {
    await httpClient.delete("/ban", {
      data: {
        room_id: room.currentRoomId,
        block_id: entry.id,
        uid: entry.uid,
      },
    });
    banStore.removeBan(entry.uid);
  } catch (err) {
    errorMsg.value = (err as Error).message || "解禁失败";
  }
}
</script>

<template>
  <div class="ban-list" data-testid="ban-list">
    <div class="header">
      <span class="title">封禁名单</span>
      <span class="count" data-testid="count">{{ entries.length }}</span>
    </div>

    <div class="scroll-root">
      <template v-if="entries.length === 0">
        <div class="empty" data-testid="empty">暂无封禁</div>
      </template>
      <template v-else>
        <div
          v-for="entry in entries"
          :key="entry.uid"
          class="ban-row"
          data-testid="ban-row"
        >
          <span class="uname">
            <strong>{{ entry.uname ?? `uid:${entry.uid}` }}</strong>
            <span class="uid">(uid:{{ entry.uid }})</span>
          </span>
          <span class="hour" data-testid="hour-label">
            {{ formatDuration(entry.hour) }}
          </span>
          <span class="ctime">{{ formatCtime(entry.ctime) }}</span>
          <el-button
            link
            type="primary"
            size="small"
            data-testid="unban-btn"
            @click="onUnban(entry)"
          >
            解禁
          </el-button>
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
  max-width: 720px;
  height: 240px;
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
.header .title {
  color: var(--el-text-color-primary, #303133);
}
.header .count {
  display: inline-flex;
  min-width: 24px;
  height: 20px;
  padding: 0 6px;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  background: var(--el-color-danger-light-9, #fef0f0);
  color: var(--el-color-danger-dark-2, #c45656);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
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
.ban-row {
  display: flex;
  gap: 12px;
  align-items: baseline;
  padding: 2px 0;
}
.ban-row .uname {
  flex: 0 0 35%;
  color: var(--el-color-primary-light-3, #79bbff);
  font-weight: 500;
  word-break: break-word;
}
.ban-row .uid {
  color: var(--el-text-color-secondary, #c0c4cc);
  font-weight: 400;
  font-size: 11px;
  margin-left: 4px;
}
.ban-row .hour {
  flex: 0 0 60px;
  font-size: 12px;
  color: var(--el-color-danger-dark-2, #c45656);
  font-variant-numeric: tabular-nums;
}
.ban-row .ctime {
  flex: 1 1 auto;
  color: var(--el-text-color-secondary, #909399);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.error {
  margin: 0;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--el-color-danger, #f56c6c);
  border-top: 1px solid var(--el-color-danger-light-9, #fef0f0);
}
</style>