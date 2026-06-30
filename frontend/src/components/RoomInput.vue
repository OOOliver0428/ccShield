<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoomStore } from "../stores/room";

/**
 * T14 — room id input + connect/disconnect control.
 *
 * UX flow:
 *
 * 1. User types a room id (real or short) into the input.
 * 2. On blur, ``roomStore.resolve`` is called so the box shows the
 *    canonical (real) id next to the user's input.
 * 3. Clicking "连接" calls ``roomStore.connect(resolvedId)`` which
 *    issues ``POST /rooms/start``. On success the store flips to
 *    ``status='connected'``; App.vue's watcher creates the WS.
 * 4. Clicking "断开" calls ``roomStore.disconnect`` which issues
 *    ``POST /rooms/stop``; App.vue's watcher closes the WS.
 *
 * Guard badge / medal styles are intentionally NOT here — those land
 * in T22/T23 (per task scope). The status indicator is a coloured
 * dot + label, not the eventual ElementPlus tag picker.
 */
const room = useRoomStore();

const inputText = ref<string>("");

const statusLabel = computed(() => {
  switch (room.status) {
    case "connected":
      return "已连接";
    case "connecting":
      return "连接中";
    default:
      return "未连接";
  }
});

const statusClass = computed(() => `status status-${room.status}`);

const canSubmit = computed(
  () => inputText.value.trim().length > 0 && room.status === "disconnected",
);
const canDisconnect = computed(() => room.status !== "disconnected");

async function onBlur(): Promise<void> {
  const value = inputText.value.trim();
  if (!value) return;
  await room.resolve(value);
  // Mirror the resolved id back into the input so the user sees the
  // canonical (real) id once the short id has been translated.
  if (room.currentRoomId !== null) {
    inputText.value = String(room.currentRoomId);
  }
}

async function onConnect(): Promise<void> {
  const value = inputText.value.trim();
  if (!value) return;
  // Make sure the input has been resolved before we start — covers
  // the case where the user hits Enter without ever blurring.
  if (room.currentRoomId === null) {
    const resolved = await room.resolve(value);
    if (!resolved) return;
  }
  const idToStart = room.currentRoomId ?? Number(value);
  await room.connect(idToStart);
}

async function onDisconnect(): Promise<void> {
  await room.disconnect();
}
</script>

<template>
  <div class="room-input" data-testid="room-input">
    <div class="row">
      <div class="field" data-testid="room-input-field">
        <el-input
          v-model="inputText"
          placeholder="输入房间号 (real 或 short)"
          :disabled="canDisconnect"
          @blur="onBlur"
          @keyup.enter="onConnect"
        />
      </div>
      <el-button
        v-if="!canDisconnect"
        type="primary"
        :disabled="!canSubmit"
        data-testid="connect-btn"
        @click="onConnect"
      >
        连接
      </el-button>
      <el-button
        v-else
        type="danger"
        data-testid="disconnect-btn"
        @click="onDisconnect"
      >
        断开
      </el-button>
    </div>

    <div class="row meta">
      <span :class="statusClass" data-testid="status-indicator">
        <span class="dot" />
        <span class="label">{{ statusLabel }}</span>
      </span>
      <span v-if="room.resolvedShortId !== null && room.currentRoomId !== null && room.resolvedShortId !== room.currentRoomId" class="hint" data-testid="resolved-hint">
        short {{ room.resolvedShortId }} → real {{ room.currentRoomId }}
      </span>
      <span v-if="room.error" class="error" data-testid="room-error">
        {{ room.error }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.room-input {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  max-width: 520px;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.row.meta {
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
}
.field {
  flex: 1;
}
.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border-radius: 12px;
  font-weight: 500;
}
.status .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.status-disconnected {
  background: var(--el-fill-color-light, #f5f7fa);
  color: var(--el-text-color-regular, #606266);
}
.status-disconnected .dot {
  background: #c0c4cc;
}
.status-connecting {
  background: var(--el-color-warning-light-9, #fdf6ec);
  color: var(--el-color-warning-dark-2, #b88230);
}
.status-connecting .dot {
  background: var(--el-color-warning, #e6a23c);
  animation: pulse 1.2s ease-in-out infinite;
}
.status-connected {
  background: var(--el-color-success-light-9, #f0f9eb);
  color: var(--el-color-success-dark-2, #2c8a4a);
}
.status-connected .dot {
  background: var(--el-color-success, #67c23a);
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.hint {
  color: var(--el-text-color-secondary, #909399);
}
.error {
  color: var(--el-color-danger, #f56c6c);
}
</style>