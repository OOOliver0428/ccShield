<script setup lang="ts">
/**
 * T19 — Ban controls.
 *
 * Inline ban-action widget: duration picker + reason input + 禁言
 * button. Plugs into the danmaku list (rendered by App.vue) so a
 * moderator can ban the author of a single message.
 *
 * Duration map (UI label → ``hour`` value sent to the backend):
 *
 * | label | hour | meaning                       |
 * | ----- | ---- | ----------------------------- |
 * | 本场 | 0    | until live stream ends        |
 * | 1小时 | 1    |                               |
 * | 24小时 | 24  |                               |
 * | 7天   | 168  | 7 × 24                        |
 * | 30天  | 720  | 30 × 24                       |
 * | 永久  | -1   | indefinite (B站 max)          |
 *
 * Flow:
 *
 * 1. User picks a duration (radio-like segmented control).
 * 2. Optional reason typed in.
 * 3. Click 禁言 → ``window.confirm`` (二次确认) → ``POST /api/ban``.
 * 4. On 200, optimistic addBan + emit ``success`` so the parent can
 *    close the popover / clear the danmaku row.
 *
 * No polling, no duplicate state — the WS (T18) owns authoritative
 * state; this widget only initiates writes.
 */
import { computed, ref } from "vue";
import { httpClient } from "../api/client";
import { useRoomStore } from "../stores/room";
import { useBanStore } from "../stores/ban";

interface DurationOption {
  label: string;
  hour: number;
}

const DURATION_OPTIONS: readonly DurationOption[] = [
  { label: "本场", hour: 0 },
  { label: "1小时", hour: 1 },
  { label: "24小时", hour: 24 },
  { label: "7天", hour: 168 },
  { label: "30天", hour: 720 },
  { label: "永久", hour: -1 },
];

const props = defineProps<{
  uid: number;
  uname?: string;
}>();

const emit = defineEmits<{
  (e: "success", payload: { uid: number }): void;
}>();

const room = useRoomStore();
const banStore = useBanStore();

const selectedHour = ref<number>(1);
const reason = ref<string>("");
const submitting = ref<boolean>(false);
const errorMsg = ref<string | null>(null);

const confirmText = computed(() => {
  const label = DURATION_OPTIONS.find((o) => o.hour === selectedHour.value)?.label ?? "";
  const target = props.uname ? `${props.uname} (uid:${props.uid})` : `uid:${props.uid}`;
  return `确认禁言 ${target} ${label}？`;
});

function selectHour(hour: number): void {
  selectedHour.value = hour;
}

async function onBan(): Promise<void> {
  errorMsg.value = null;
  if (room.currentRoomId === null) {
    errorMsg.value = "未连接房间";
    return;
  }
  // 二次确认 — synchronous confirm() blocks on the user so the
  // subsequent POST cannot fire until they accept.
  const accepted: boolean = window.confirm(confirmText.value);
  if (!accepted) return;

  submitting.value = true;
  try {
    const payload: {
      room_id: number;
      uid: number;
      hour: number;
      reason: string | undefined;
    } = {
      room_id: room.currentRoomId,
      uid: props.uid,
      hour: selectedHour.value,
      reason: reason.value.trim() === "" ? undefined : reason.value.trim(),
    };
    await httpClient.post("/ban", payload);
    // Optimistic — the WS push (ban_added) will reconcile any
    // out-of-band fields the POST response omitted.
    banStore.addBan({
      uid: props.uid,
      uname: props.uname,
      hour: selectedHour.value,
      reason: payload.reason,
    });
    emit("success", { uid: props.uid });
    reason.value = "";
  } catch (err) {
    errorMsg.value = (err as Error).message || "禁言失败";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="ban-controls" data-testid="ban-controls">
    <div class="row">
      <span class="target" data-testid="target-label">
        禁言
        <strong>{{ props.uname ?? `uid:${props.uid}` }}</strong>
      </span>
    </div>

    <div class="row duration-row" data-testid="duration-row">
      <button
        v-for="opt in DURATION_OPTIONS"
        :key="opt.label"
        type="button"
        class="duration-option"
        :class="{ active: selectedHour === opt.hour }"
        :data-testid="'duration-option'"
        :data-hour="opt.hour"
        @click="selectHour(opt.hour)"
      >
        {{ opt.label }}
      </button>
    </div>

    <div class="row reason-row">
      <el-input
        v-model="reason"
        placeholder="原因（可选）"
        size="small"
        :maxlength="200"
        data-testid="reason-input"
      />
    </div>

    <div class="row action-row">
      <el-button
        type="danger"
        size="small"
        :loading="submitting"
        :disabled="room.currentRoomId === null"
        data-testid="ban-btn"
        @click="onBan"
      >
        禁言
      </el-button>
    </div>

    <p
      v-if="errorMsg"
      class="error"
      data-testid="ban-error"
    >
      {{ errorMsg }}
    </p>
  </div>
</template>

<style scoped>
.ban-controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 6px;
  background: var(--el-fill-color-blank, #ffffff);
  min-width: 240px;
}
.row {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.duration-row {
  gap: 4px;
}
.duration-option {
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 4px;
  border: 1px solid var(--el-border-color-light, #dcdfe6);
  background: var(--el-fill-color-blank, #ffffff);
  color: var(--el-text-color-regular, #606266);
  cursor: pointer;
  transition: all 0.12s ease;
}
.duration-option:hover {
  border-color: var(--el-color-primary-light-5, #409eff);
}
.duration-option.active {
  background: var(--el-color-danger-light-9, #fef0f0);
  color: var(--el-color-danger-dark-2, #c45656);
  border-color: var(--el-color-danger, #f56c6c);
  font-weight: 600;
}
.reason-row :deep(.el-input) {
  flex: 1 1 100%;
}
.target {
  font-size: 13px;
  color: var(--el-text-color-regular, #606266);
}
.error {
  margin: 0;
  font-size: 12px;
  color: var(--el-color-danger, #f56c6c);
}
</style>