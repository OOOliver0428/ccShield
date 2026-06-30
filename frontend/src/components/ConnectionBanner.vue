<script setup lang="ts">
import { computed } from "vue";

/**
 * T14 — reconnection banner.
 *
 * Pure presentational component: shows when the WS is either
 * disconnected or actively retrying. Driven by a single boolean
 * prop so the App.vue layer decides what counts as "needs the
 * banner" (we don't import BridgeWS here — single-responsibility).
 */
const props = defineProps<{ visible: boolean }>();

const text = computed(() =>
  props.visible ? "连接断开, 重连中…" : "",
);
</script>

<template>
  <div v-if="visible" class="connection-banner" data-testid="connection-banner">
    <span class="dot" />
    <span class="text">{{ text }}</span>
  </div>
</template>

<style scoped>
.connection-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  margin: 8px 0;
  background: var(--el-color-warning-light-9, #fdf6ec);
  border: 1px solid var(--el-color-warning-light-5, #e6a23c);
  border-radius: 6px;
  color: var(--el-color-warning-dark-2, #b88230);
  font-size: 13px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-warning, #e6a23c);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.text {
  font-weight: 500;
}
</style>