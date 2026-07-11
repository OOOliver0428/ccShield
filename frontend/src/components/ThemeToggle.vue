<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

type Theme = "dark" | "light";

const STORAGE_KEY = "ccshield-theme";

function getInitialTheme(): Theme {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

const theme = ref<Theme>(getInitialTheme());
const isDark = computed(() => theme.value === "dark");
const targetLabel = computed(() => (isDark.value ? "浅色" : "深色"));

function applyTheme(value: Theme, persist = true): void {
  document.documentElement.dataset.theme = value;
  document.documentElement.style.colorScheme = value;

  const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (themeColor !== null) {
    themeColor.content = value === "light" ? "#f2f5fa" : "#090c12";
  }

  if (persist) {
    try {
      window.localStorage.setItem(STORAGE_KEY, value);
    } catch {
      // Theme switching still works when storage is unavailable.
    }
  }
}

function toggleTheme(): void {
  theme.value = isDark.value ? "light" : "dark";
  applyTheme(theme.value);
}

onMounted(() => applyTheme(theme.value, false));
</script>

<template>
  <button
    type="button"
    class="theme-toggle"
    data-testid="theme-toggle"
    :aria-label="`切换到${targetLabel}主题`"
    :title="`切换到${targetLabel}主题`"
    @click="toggleTheme"
  >
    <span class="theme-icon" aria-hidden="true">{{ isDark ? "☀" : "☾" }}</span>
    <span class="theme-label">{{ targetLabel }}</span>
  </button>
</template>

<style scoped>
.theme-toggle {
  display: inline-flex;
  min-height: 34px;
  padding: 6px 10px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid var(--cc-border-strong);
  border-radius: 999px;
  color: var(--cc-text-secondary);
  background: var(--cc-fill-soft);
  font: inherit;
  font-size: 11px;
  font-weight: 650;
  cursor: pointer;
  transition:
    border-color 150ms ease,
    color 150ms ease,
    background-color 150ms ease,
    transform 150ms ease;
}
.theme-toggle:hover {
  border-color: var(--cc-primary);
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
}
.theme-toggle:active {
  transform: translateY(1px);
}
.theme-toggle:focus-visible {
  outline: 2px solid var(--cc-primary);
  outline-offset: 2px;
}
.theme-icon {
  display: grid;
  width: 18px;
  height: 18px;
  place-items: center;
  color: var(--cc-primary-emphasis);
  font-size: 15px;
  line-height: 1;
}
.theme-label {
  white-space: nowrap;
}
@media (max-width: 640px) {
  .theme-toggle {
    width: 34px;
    padding: 6px;
  }
  .theme-label {
    display: none;
  }
}
</style>
