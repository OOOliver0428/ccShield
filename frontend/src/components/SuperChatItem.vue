<script setup lang="ts">
/**
 * T23 — SuperChat row.
 *
 * SuperChats are paid pinned messages — they persist on screen longer
 * than regular danmaku and carry a price badge. The bridge ships a
 * dedicated ``sc`` event type so we render them with their own row
 * style (warm background, left border accent, prominent price tag).
 *
 * Layout: price (bold) · uname · text · ts — price-first so a scan
 * of the chat buffer surfaces new revenue immediately.
 */
import { computed } from "vue";
import type { BridgeScEvent } from "../api/ws";

interface Props {
  sc: BridgeScEvent;
}

const props = defineProps<Props>();

const priceText = computed(() => `¥${props.sc.price}`);

const tsText = computed(() => {
  const d = new Date(props.sc.ts * 1000);
  const pad = (n: number): string => n.toString().padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
});
</script>

<template>
  <div class="sc-row" data-testid="sc-row">
    <span class="sc-price" data-testid="sc-price">{{ priceText }}</span>
    <span class="uname" data-testid="sc-uname">{{ sc.uname }}</span>
    <span class="sep">:</span>
    <span class="text" data-testid="sc-text">{{ sc.text }}</span>
    <span class="ts" data-testid="sc-ts">{{ tsText }}</span>
  </div>
</template>

<style scoped>
.sc-row {
  display: flex;
  gap: 6px;
  align-items: baseline;
  flex-wrap: wrap;
  padding: 4px 8px;
  background: var(--el-color-danger-light-9, #fef0f0);
  border-left: 3px solid var(--el-color-danger, #f56c6c);
  border-radius: 4px;
  margin: 2px 0;
  font-size: 13px;
  line-height: 1.6;
}
.sc-row .sc-price {
  font-weight: 700;
  color: var(--el-color-danger, #f56c6c);
  font-variant-numeric: tabular-nums;
}
.sc-row .uname {
  color: var(--el-color-danger-dark-2, #c45656);
  font-weight: 600;
}
.sc-row .sep {
  color: var(--el-text-color-secondary, #c0c4cc);
}
.sc-row .text {
  color: var(--el-text-color-primary, #303133);
  flex: 1 1 auto;
  word-break: break-word;
}
.sc-row .ts {
  color: var(--el-text-color-secondary, #c0c4cc);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
</style>