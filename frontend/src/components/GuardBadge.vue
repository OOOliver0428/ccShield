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
 *   1 → 舰长 (captain) — blue
 *   2 → 提督 (admiral)  — purple
 *   3 → 总督 (governor) — gold
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
      return { label: "舰长", cls: "guard-1" };
    case 2:
      return { label: "提督", cls: "guard-2" };
    case 3:
      return { label: "总督", cls: "guard-3" };
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
  padding: 0 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.6;
  letter-spacing: 0.5px;
  user-select: none;
}
.guard-1 {
  background: var(--el-color-primary-light-9, #ecf5ff);
  color: var(--el-color-primary-dark-2, #337ecc);
  border: 1px solid var(--el-color-primary-light-5, #409eff);
}
.guard-2 {
  background: #f4eaff;
  color: #6a3aa0;
  border: 1px solid #9b6bd6;
}
.guard-3 {
  background: #fff5d9;
  color: #a06a00;
  border: 1px solid #d6a93b;
}
</style>