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
  <div v-if="visible" class="connection-banner" data-testid="connection-banner" role="status" aria-live="polite">
    <span class="dot" aria-hidden="true" />
    <span class="text">{{ text }}</span>
  </div>
</template>

<style scoped>
.connection-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid rgb(242 185 95 / 20%);
  border-radius: 10px;
  color: var(--cc-warning);
  background: var(--cc-warning-soft);
  font-size: 11px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--cc-warning);
  box-shadow: 0 0 10px rgb(242 185 95 / 36%);
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
