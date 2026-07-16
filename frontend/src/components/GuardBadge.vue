<script setup lang="ts">
/**
 * T21 — fleet guard badge.
 *
 * Bilibili's three fleet tiers surface as guard_level 1/2/3 in the
 * bridge ``DanmakuEvent``. Anything else (0 = none, >3 = unknown /
 * future tier) renders nothing — the surrounding row just drops the
 * badge silently so the chat layout stays unbroken.
 *
 * Colors follow the bilibili convention so users can pattern-match
 * at a glance:
 *   1 → 总督 (governor) — orange
 *   2 → 提督 (admiral)  — purple
 *   3 → 舰长 (captain) — blue
 */
import { computed } from "vue";

interface Props {
  level: number;
}

const props = defineProps<Props>();

interface GuardMeta {
  label: string;
  cls: string;
}

const meta = computed<GuardMeta | null>(() => {
  switch (props.level) {
    case 1:
      return { label: "总督", cls: "guard-1" };
    case 2:
      return { label: "提督", cls: "guard-2" };
    case 3:
      return { label: "舰长", cls: "guard-3" };
    default:
      return null;
  }
});
</script>

<template>
  <span
    v-if="meta"
    class="guard-badge"
    :class="meta.cls"
    data-testid="guard-badge"
  >{{ meta.label }}</span>
</template>

<style scoped>
.guard-badge {
  display: inline-block;
  padding: 1px 5px;
  border-radius: 5px;
  font-size: var(--danmaku-badge-size, 9px);
  font-weight: 680;
  line-height: 1.45;
  letter-spacing: 0.3px;
  user-select: none;
}
.guard-1 {
  background: var(--cc-guard-governor-bg);
  color: var(--cc-guard-governor-text);
  border: 1px solid var(--cc-guard-governor-border);
}
.guard-2 {
  background: var(--cc-guard-admiral-bg);
  color: var(--cc-guard-admiral-text);
  border: 1px solid var(--cc-guard-admiral-border);
}
.guard-3 {
  background: var(--cc-guard-captain-bg);
  color: var(--cc-guard-captain-text);
  border: 1px solid var(--cc-guard-captain-border);
}
</style>
