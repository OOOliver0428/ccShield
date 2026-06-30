<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { storeToRefs } from "pinia";
import { useAuthStore } from "./stores/auth";
import { useRoomStore } from "./stores/room";
import { useDanmakuStore } from "./stores/danmaku";
import { bootstrap } from "./api/client";
import { BridgeWS, type BridgeEvent } from "./api/ws";
import QrLogin from "./components/QrLogin.vue";
import RoomInput from "./components/RoomInput.vue";
import DanmakuList from "./components/DanmakuList.vue";
import ConnectionBanner from "./components/ConnectionBanner.vue";

/**
 * T14 — top-level shell.
 *
 * Wires the auth flow (T10) to the room lifecycle (T14):
 *
 * 1. On mount, bootstrap the local token + fetch auth status.
 * 2. While ``auth.status !== 'authenticated'``, render the QR login.
 * 3. Once authenticated, mount the room input. The user resolves a
 *    short id → presses "连接" → the store calls ``POST /rooms/start``
 *    and flips to ``status='connected'``.
 * 4. We watch ``roomStore.currentRoomId``: when it transitions from
 *    ``null`` to a number, we open a ``BridgeWS`` for that room and
 *    dispatch every received event into the danmaku/room stores. The
 *    inverse transition (number → null) closes the WS.
 * 5. The ConnectionBanner is shown whenever the WS is disconnected
 *    or actively retrying — driven by ``wsVisible`` ref, set by the
 *    BridgeWS callbacks.
 *
 * NB: we deliberately do NOT touch the danmaku list or banner on
 * auth changes — they're scoped to the room lifecycle only.
 */
const auth = useAuthStore();
const room = useRoomStore();
const danmaku = useDanmakuStore();

const { currentRoomId } = storeToRefs(room);

const wsVisible = ref<boolean>(false);
let bridge: BridgeWS | null = null;

onMounted(async () => {
  await bootstrap();
  await auth.fetchStatus();
});

watch(currentRoomId, (newId, oldId) => {
  if (oldId !== null && newId === null) {
    // Disconnected — close WS, drop banner.
    bridge?.close();
    bridge = null;
    wsVisible.value = false;
    danmaku.clear();
    return;
  }
  if (oldId === null && newId !== null) {
    // Just connected — open a WS for this room.
    if (bridge !== null) {
      bridge.close();
    }
    bridge = new BridgeWS(newId, auth.token, {
      onMessage: handleBridgeEvent,
      onDisconnect: (): void => {
        wsVisible.value = true;
      },
      onError: (): void => {
        wsVisible.value = true;
      },
    });
    bridge.connect();
  }
});

function handleBridgeEvent(event: BridgeEvent): void {
  switch (event.type) {
    case "danmaku":
      danmaku.addDanmaku(event);
      return;
    case "sc":
      danmaku.addSc(event);
      return;
    case "room_status":
      room.applyRoomStatus(event.status);
      if (event.status === "connected") {
        wsVisible.value = false;
      } else if (event.status === "disconnected" || event.status === "error") {
        wsVisible.value = true;
      }
      return;
    case "error":
      wsVisible.value = true;
      return;
  }
}
</script>

<template>
  <main class="app-shell">
    <QrLogin v-if="auth.status !== 'authenticated'" />
    <section v-else class="authenticated-shell" data-testid="authenticated-shell">
      <header class="topbar">
        <h1 class="brand">reccshield</h1>
        <span class="welcome">
          欢迎,
          <strong>{{ auth.userInfo?.uname ?? "用户" }}</strong>
          (mid: {{ auth.userInfo?.mid ?? "?" }})
        </span>
      </header>

      <RoomInput />
      <ConnectionBanner :visible="wsVisible" />
      <DanmakuList v-if="room.status === 'connected'" />
    </section>
  </main>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  padding: 24px;
  box-sizing: border-box;
  display: flex;
  align-items: flex-start;
  justify-content: center;
}
.authenticated-shell {
  width: 100%;
  max-width: 880px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.topbar {
  display: flex;
  align-items: baseline;
  gap: 16px;
}
.brand {
  margin: 0;
  font-size: 20px;
  letter-spacing: 0.5px;
}
.welcome {
  color: var(--el-text-color-secondary, #606266);
  font-size: 13px;
}
</style>