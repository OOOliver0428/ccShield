<script setup lang="ts">
/**
 * T22 — fan medal chip.
 *
 * Bilibili fans wear a per-streamer "粉丝牌" (fan medal) whose level
 * rises with loyalty. The bridge ships the denormalised shape
 * ``{ name, level } | null`` already — we just style it.
 *
 * Null medal → render nothing (the surrounding row loses the chip
 * silently). Present medal → show the name as the chip's label and
 * the level as a small subsquare.
 */
import { computed } from "vue";

export interface Medal {
  name: string;
  level: number;
}

interface Props {
  medal: Medal | null;
}

const props = defineProps<Props>();

const hasMedal = computed(() => props.medal !== null);
</script>

<template>
  <span
    v-if="hasMedal && medal"
    class="medal"
    data-testid="medal-badge"
  >
    <span class="medal-name">{{ medal.name }}</span>
    <span class="medal-level" data-testid="medal-level">{{ medal.level }}</span>
  </span>
</template>

<style scoped>
.medal {
  display: inline-flex;
  align-items: stretch;
  border-radius: 4px;
  overflow: hidden;
  font-size: 11px;
  line-height: 1.6;
  user-select: none;
  border: 1px solid var(--el-color-primary-light-5, #409eff);
}
.medal-name {
  padding: 0 6px;
  background: var(--el-color-primary-light-9, #ecf5ff);
  color: var(--el-color-primary-dark-2, #337ecc);
  font-weight: 500;
}
.medal-level {
  padding: 0 5px;
  background: var(--el-color-primary, #409eff);
  color: #fff;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
</style>