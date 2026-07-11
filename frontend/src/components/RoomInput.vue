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
  <section
    class="room-input"
    :class="{ 'is-connected': room.status === 'connected' }"
    data-testid="room-input"
  >
    <header class="room-heading">
      <div class="heading-copy">
        <span class="eyebrow">{{ room.status === "connected" ? "ACTIVE ROOM" : "ROOM CONNECTION" }}</span>
        <h2>{{ room.status === "connected" ? (room.resolvedTitle || "直播间已连接") : "连接直播间" }}</h2>
        <p>{{ room.status === "connected" ? "弹幕与房管名单正在实时同步" : "输入真实房间号或短号，进入实时房管工作台" }}</p>
      </div>
      <span
        :class="statusClass"
        data-testid="status-indicator"
        role="status"
        aria-live="polite"
      >
        <span class="dot" aria-hidden="true" />
        <span class="label">{{ statusLabel }}</span>
      </span>
    </header>

    <div class="row control-row">
      <div class="field" data-testid="room-input-field">
        <el-input
          v-model="inputText"
          placeholder="输入房间号 (real 或 short)"
          aria-label="直播间房间号"
          :disabled="canDisconnect"
          size="large"
          @blur="onBlur"
          @keyup.enter="onConnect"
        />
      </div>
      <el-button
        v-if="!canDisconnect"
        type="primary"
        size="large"
        :disabled="!canSubmit"
        data-testid="connect-btn"
        @click="onConnect"
      >
        连接房间
      </el-button>
      <el-button
        v-else
        type="danger"
        plain
        size="large"
        data-testid="disconnect-btn"
        @click="onDisconnect"
      >
        断开连接
      </el-button>
    </div>

    <div class="row meta">
      <span v-if="room.resolvedShortId !== null && room.resolvedShortId > 0 && room.currentRoomId !== null && room.resolvedShortId !== room.currentRoomId" class="hint" data-testid="resolved-hint">
        short {{ room.resolvedShortId }} → real {{ room.currentRoomId }}
      </span>
      <span v-if="room.error" class="error" data-testid="room-error" role="alert">
        {{ room.error }}
      </span>
    </div>

    <section
      v-if="room.status === 'connected' && room.currentRoomId !== null"
      class="room-info"
      data-testid="room-info"
      aria-label="直播间信息"
    >
      <div class="room-info-item anchor-item">
        <span class="info-icon" aria-hidden="true">主</span>
        <span class="info-copy">
        <span class="room-info-label">主播</span>
        <strong data-testid="room-anchor">{{ room.resolvedUname || "—" }}</strong>
        </span>
      </div>
      <div class="room-info-item">
        <span class="info-icon" aria-hidden="true">#</span>
        <span class="info-copy">
        <span class="room-info-label">房间号</span>
        <strong data-testid="room-number">{{ room.currentRoomId }}</strong>
        </span>
      </div>
      <div class="room-info-item room-title-item">
        <span class="info-icon title-icon" aria-hidden="true">播</span>
        <span class="info-copy">
        <span class="room-info-label">直播标题</span>
        <strong data-testid="room-title">{{ room.resolvedTitle || "—" }}</strong>
        </span>
      </div>
    </section>
  </section>
</template>

<style scoped>
.room-input {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 100%;
  padding: 16px;
  border: 1px solid var(--cc-border);
  border-radius: var(--cc-radius-panel);
  background: var(--cc-room-background);
  box-shadow: var(--cc-soft-shadow);
}
.room-input.is-connected {
  padding: 14px 16px;
}
.room-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.heading-copy {
  min-width: 0;
}
.eyebrow {
  display: block;
  margin-bottom: 5px;
  color: var(--cc-primary);
  font-size: 9px;
  font-weight: 760;
  letter-spacing: 1.7px;
}
.heading-copy h2 {
  overflow: hidden;
  margin: 0;
  color: var(--cc-text);
  font-size: 17px;
  font-weight: 680;
  letter-spacing: -0.2px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.heading-copy p {
  margin: 4px 0 0;
  color: var(--cc-text-muted);
  font-size: 11px;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.control-row {
  max-width: 680px;
}
.is-connected .control-row {
  position: absolute;
  z-index: 1;
  top: 94px;
  right: 16px;
  width: auto;
}
.is-connected .field {
  display: none;
}
.row.meta {
  min-height: 18px;
  font-size: 12px;
  color: var(--cc-text-secondary);
}
.field {
  flex: 1;
}
.status {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid var(--cc-border);
  border-radius: 999px;
  font-size: 11px;
  font-weight: 650;
}
.status .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.status-disconnected {
  background: var(--cc-fill-subtle);
  color: var(--cc-text-secondary);
}
.status-disconnected .dot {
  background: var(--cc-text-muted);
}
.status-connecting {
  background: var(--cc-warning-soft);
  color: var(--cc-warning);
}
.status-connecting .dot {
  background: var(--cc-warning);
  animation: pulse 1.2s ease-in-out infinite;
}
.status-connected {
  border-color: rgb(66 211 146 / 22%);
  background: var(--cc-success-soft);
  color: var(--cc-success);
}
.status-connected .dot {
  background: var(--cc-success);
  box-shadow: 0 0 12px rgb(66 211 146 / 50%);
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.hint {
  color: var(--cc-text-secondary);
}
.error {
  color: var(--cc-danger);
}
.room-info {
  display: grid;
  grid-template-columns: minmax(160px, 0.8fr) minmax(130px, 0.55fr) minmax(280px, 2fr);
  gap: 10px;
  width: 100%;
  padding-right: 118px;
}
.room-info-item {
  display: flex;
  min-width: 0;
  padding: 10px 12px;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--cc-border);
  border-radius: 11px;
  background: var(--cc-fill-faint);
}
.info-icon {
  display: grid;
  width: 29px;
  height: 29px;
  flex: 0 0 29px;
  place-items: center;
  border-radius: 8px;
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
  font-size: 11px;
  font-weight: 750;
}
.title-icon {
  color: var(--cc-success-emphasis);
  background: var(--cc-success-soft);
}
.info-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.room-info-label {
  color: var(--cc-text-muted);
  font-size: 10px;
}
.room-info-item strong {
  overflow: hidden;
  color: var(--cc-text);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 900px) {
  .is-connected .control-row {
    position: static;
  }
  .room-info {
    padding-right: 0;
  }
}
@media (max-width: 640px) {
  .room-input {
    padding: 14px;
  }
  .control-row {
    flex-wrap: wrap;
  }
  .control-row .field,
  .control-row :deep(.el-button) {
    width: 100%;
  }
  .room-info {
    grid-template-columns: 1fr 1fr;
  }
  .room-title-item {
    grid-column: 1 / -1;
  }
}
</style>
