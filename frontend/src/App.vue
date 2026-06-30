<script setup lang="ts">
import { onMounted } from "vue";
import { useAuthStore } from "./stores/auth";
import { bootstrap } from "./api/client";
import QrLogin from "./components/QrLogin.vue";

const auth = useAuthStore();

onMounted(async () => {
  await bootstrap();
  await auth.fetchStatus();
});
</script>

<template>
  <main class="app-shell">
    <QrLogin v-if="auth.status !== 'authenticated'" />
    <section v-else class="authenticated-placeholder">
      <h1>已登录</h1>
      <p>
        欢迎,
        <strong>{{ auth.userInfo?.uname ?? "用户" }}</strong>
        (mid: {{ auth.userInfo?.mid ?? "?" }})
      </p>
      <p class="hint">主界面将在 T14 接入。</p>
    </section>
  </main>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  box-sizing: border-box;
}
.authenticated-placeholder {
  text-align: center;
}
.hint {
  color: #888;
  font-size: 14px;
}
</style>