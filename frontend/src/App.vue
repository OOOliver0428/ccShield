<script setup lang="ts">
import { ref, onMounted } from "vue";

const backendStatus = ref<string>("checking…");

onMounted(async () => {
  try {
    const res = await fetch("/api/health");
    if (!res.ok) {
      backendStatus.value = `unhealthy (${res.status})`;
      return;
    }
    const data: unknown = await res.json();
    if (
      typeof data === "object" &&
      data !== null &&
      "status" in data &&
      (data as { status: unknown }).status === "ok"
    ) {
      backendStatus.value = "ok";
    } else {
      backendStatus.value = "unexpected response";
    }
  } catch (err) {
    backendStatus.value = `unreachable: ${(err as Error).message}`;
  }
});
</script>

<template>
  <main class="scaffold">
    <h1>reccshield</h1>
    <p>Bilibili live-room moderator tool — frontend scaffold.</p>
    <p>
      Backend health: <strong>{{ backendStatus }}</strong>
    </p>
  </main>
</template>

<style scoped>
.scaffold {
  font-family:
    system-ui,
    -apple-system,
    Segoe UI,
    Roboto,
    sans-serif;
  max-width: 720px;
  margin: 4rem auto;
  padding: 0 1rem;
}
</style>