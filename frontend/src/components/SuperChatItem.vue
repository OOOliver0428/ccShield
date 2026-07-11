<script setup lang="ts">
/**
 * Active SuperChat card.
 *
 * SuperChats are paid pinned messages — they persist on screen longer
 * than regular danmaku and carry a price badge. The bridge ships a
 * dedicated ``sc`` event type so we render them with their own row
 * style (warm background, left border accent, prominent price tag).
 *
 * Colors and expiry come from the server payload. The countdown is only a
 * presentation aid; the store removes the card at the authoritative end_ts.
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import type { BridgeScEvent } from "../api/ws";
import FanMedal from "./FanMedal.vue";
import GuardBadge from "./GuardBadge.vue";

interface Props {
  sc: BridgeScEvent;
}

const props = defineProps<Props>();

const priceText = computed(() => `¥${props.sc.price}`);
const nowSeconds = ref(Math.floor(Date.now() / 1000));
let ticker: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  ticker = setInterval(() => {
    nowSeconds.value = Math.floor(Date.now() / 1000);
  }, 1_000);
});

onBeforeUnmount(() => {
  if (ticker !== null) clearInterval(ticker);
});

const remainingText = computed(() => {
  const seconds = Math.max(0, props.sc.end_ts - nowSeconds.value);
  if (seconds < 60) return `${seconds}秒`;
  if (seconds < 3_600) return `${Math.ceil(seconds / 60)}分钟`;
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.ceil((seconds % 3_600) / 60);
  return minutes > 0 ? `${hours}小时${minutes}分钟` : `${hours}小时`;
});

const cardStyle = computed<Record<string, string>>(() => ({
  "--sc-body-bg": props.sc.background_color || "#edf5ff",
  "--sc-accent": props.sc.background_bottom_color || "#2a60b2",
  "--sc-price-bg": props.sc.background_price_color || "#7497cd",
  "--sc-message-color": props.sc.message_font_color || "#24476b",
}));

const tsText = computed(() => {
  const d = new Date(props.sc.ts * 1000);
  const pad = (n: number): string => n.toString().padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
});
</script>

<template>
  <article class="sc-card" :style="cardStyle" data-testid="sc-row">
    <header class="sc-meta">
      <span class="sc-price" data-testid="sc-price">{{ priceText }}</span>
      <GuardBadge :level="sc.guard_level" />
      <FanMedal :medal="sc.medal" />
      <span class="uname" data-testid="sc-uname">{{ sc.uname }}</span>
      <span class="remaining" data-testid="sc-remaining">剩余 {{ remainingText }}</span>
    </header>
    <div class="sc-message">
      <span class="text" data-testid="sc-text">{{ sc.text }}</span>
      <span class="ts" data-testid="sc-ts">{{ tsText }}</span>
    </div>
  </article>
</template>

<style scoped>
.sc-card {
  overflow: hidden;
  background: var(--sc-body-bg);
  border: 1px solid color-mix(in srgb, var(--sc-accent) 74%, white 8%);
  border-radius: 10px;
  box-shadow: 0 8px 22px rgb(0 0 0 / 18%);
  font-size: 11px;
  line-height: 1.5;
}
.sc-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  color: #fff;
  background: var(--sc-accent);
}
.sc-card .sc-price {
  padding: 2px 6px;
  border-radius: 6px;
  background: var(--sc-price-bg);
  font-weight: 700;
  color: #fff;
  font-variant-numeric: tabular-nums;
}
.sc-card .uname {
  font-weight: 600;
}
.sc-card .remaining {
  margin-left: auto;
  white-space: nowrap;
  font-size: 9px;
  opacity: 0.9;
}
.sc-message {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 9px 10px;
  color: var(--sc-message-color);
}
.sc-card .text {
  flex: 1;
  word-break: break-word;
}
.sc-card .ts {
  white-space: nowrap;
  font-size: 9px;
  font-variant-numeric: tabular-nums;
  opacity: 0.7;
}
</style>
