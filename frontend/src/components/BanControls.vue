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
 * | 2小时 | 2    |                               |
 * | 4小时 | 4    |                               |
 * | 24小时 | 24  |                               |
 * | 7天   | 168  | 7 × 24                        |
 * | 永久  | -1   | no automatic expiry           |
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
import { ElMessage } from "element-plus";
import { httpClient } from "../api/client";
import { useRoomStore } from "../stores/room";
import { useBanStore } from "../stores/ban";

interface DurationOption {
  label: string;
  hour: number;
}

const DURATION_OPTIONS: readonly DurationOption[] = [
  { label: "本场", hour: 0 },
  { label: "2小时", hour: 2 },
  { label: "4小时", hour: 4 },
  { label: "24小时", hour: 24 },
  { label: "7天", hour: 168 },
  { label: "永久", hour: -1 },
];

const props = defineProps<{
  uid: number;
  uname?: string;
  message?: string;
}>();

const emit = defineEmits<{
  (e: "success", payload: { uid: number }): void;
}>();

const room = useRoomStore();
const banStore = useBanStore();

const selectedHour = ref<number>(2);
const reason = ref<string>("");
const submitting = ref<boolean>(false);
const errorMsg = ref<string | null>(null);

const confirmText = computed(() => {
  const label = DURATION_OPTIONS.find((o) => o.hour === selectedHour.value)?.label ?? "";
  const target = props.uname ? `${props.uname} (uid:${props.uid})` : `uid:${props.uid}`;
  const evidence = props.message ? `\n发言：${props.message}` : "";
  return `确认禁言 ${target}？\n期限：${label}${evidence}`;
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
  if (!banStore.beginSubmission(props.uid)) return;
  // 二次确认 — synchronous confirm() blocks on the user so the
  // subsequent POST cannot fire until they accept.
  const accepted: boolean = window.confirm(confirmText.value);
  if (!accepted) {
    banStore.endSubmission(props.uid);
    return;
  }

  submitting.value = true;
  try {
    const payload: {
      room_id: number;
      uid: number;
      hour: number;
      reason: string | undefined;
      uname: string;
    } = {
      room_id: room.currentRoomId,
      uid: props.uid,
      hour: selectedHour.value,
      reason: reason.value.trim() === "" ? undefined : reason.value.trim(),
      uname: props.uname ?? "",
    };
    await httpClient.post("/ban", payload);
    // Optimistic — the WS push (ban_added) will reconcile any
    // out-of-band fields the POST response omitted.
    banStore.addBan({
      block_id: null,
      uid: props.uid,
      uname: props.uname ?? "",
      hour: selectedHour.value,
      reason: payload.reason ?? "",
      created_at: Math.floor(Date.now() / 1000),
      expires_at:
        selectedHour.value > 0
          ? Math.floor(Date.now() / 1000) + selectedHour.value * 3600
          : null,
      pending: true,
    });
    const durationLabel = DURATION_OPTIONS.find(
      (option) => option.hour === selectedHour.value,
    )?.label ?? `${selectedHour.value}小时`;
    ElMessage.success({
      message: `已成功禁言 ${props.uname || `uid:${props.uid}`}（${durationLabel}）`,
      duration: 3000,
    });
    emit("success", { uid: props.uid });
    reason.value = "";
  } catch (err) {
    errorMsg.value = (err as Error).message || "禁言失败";
  } finally {
    submitting.value = false;
    banStore.endSubmission(props.uid);
  }
}
</script>

<template>
  <div class="ban-controls" data-testid="ban-controls">
    <div class="row control-heading">
      <span class="target" data-testid="target-label">
        <small>设置禁言</small>
        <strong>{{ props.uname ?? `uid:${props.uid}` }}</strong>
        <span>UID {{ props.uid }}</span>
      </span>
    </div>

    <div v-if="props.message" class="evidence">
      <span>原始弹幕</span>
      <p>{{ props.message }}</p>
    </div>

    <div class="row duration-row" data-testid="duration-row" role="group" aria-label="禁言期限">
      <button
        v-for="opt in DURATION_OPTIONS"
        :key="opt.label"
        type="button"
        class="duration-option"
        :class="{ active: selectedHour === opt.hour }"
        :data-testid="'duration-option'"
        :data-hour="opt.hour"
        :aria-pressed="selectedHour === opt.hour"
        @click="selectHour(opt.hour)"
      >
        {{ opt.label }}
      </button>
    </div>

    <div class="row reason-row">
      <el-input
        v-model="reason"
        placeholder="原因（可选）"
        aria-label="禁言原因"
        :maxlength="200"
        data-testid="reason-input"
      />
    </div>

    <div class="row action-row">
      <el-button
        type="danger"
        :loading="submitting"
        :disabled="room.currentRoomId === null || banStore.isSubmitting(props.uid)"
        data-testid="ban-btn"
        @click="onBan"
      >
        确认禁言
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
  min-width: 0;
  gap: 11px;
  padding: 16px;
  border-radius: 12px;
  background: var(--cc-surface-raised);
}
.row {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.duration-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 5px;
}
.duration-option {
  min-height: 34px;
  padding: 5px 7px;
  border: 1px solid var(--cc-border);
  border-radius: 8px;
  color: var(--cc-text-secondary);
  background: var(--cc-fill-subtle);
  font-size: 10px;
  cursor: pointer;
  transition: border-color 140ms ease, background-color 140ms ease, color 140ms ease;
}
.duration-option:hover {
  border-color: rgb(124 140 255 / 48%);
  color: var(--cc-text);
}
.duration-option.active {
  border-color: rgb(255 107 122 / 42%);
  color: var(--cc-danger-emphasis);
  background: var(--cc-danger-soft);
  font-weight: 650;
}
.reason-row :deep(.el-input) {
  flex: 1 1 100%;
}
.target {
  display: flex;
  min-width: 0;
  flex-direction: column;
  color: var(--cc-text-secondary);
}
.target small {
  margin-bottom: 3px;
  color: var(--cc-text-muted);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1px;
}
.target strong {
  color: var(--cc-text);
  font-size: 13px;
}
.target > span {
  color: var(--cc-text-muted);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
}
.evidence {
  padding: 8px 10px;
  border: 1px solid var(--cc-border);
  border-radius: 8px;
  background: var(--cc-surface-inset);
}
.evidence > span {
  display: block;
  margin-bottom: 3px;
  color: var(--cc-text-muted);
  font-size: 9px;
}
.evidence p {
  display: -webkit-box;
  overflow: hidden;
  margin: 0;
  color: var(--cc-text-secondary);
  font-size: 11px;
  line-height: 1.5;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}
.action-row {
  justify-content: flex-end;
}
.error {
  margin: 0;
  font-size: 12px;
  color: var(--cc-danger);
}
@media (max-width: 520px) {
  .duration-row {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .action-row :deep(.el-button) {
    width: 100%;
    min-height: 42px;
  }
}
</style>
