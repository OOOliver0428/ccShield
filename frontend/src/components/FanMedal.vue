<script setup lang="ts">
/**
 * Fan-medal chip.
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
  border-radius: 5px;
  overflow: hidden;
  font-size: var(--danmaku-badge-size, 9px);
  line-height: 1.45;
  user-select: none;
  border: 1px solid rgb(124 140 255 / 42%);
}
.medal-name {
  padding: 1px 5px;
  background: var(--cc-primary-soft);
  color: var(--cc-primary-emphasis);
  font-weight: 560;
}
.medal-level {
  padding: 1px 4px;
  background: rgb(124 140 255 / 42%);
  color: #fff;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
</style>
