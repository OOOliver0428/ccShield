<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { storeToRefs } from "pinia";
import { useAuthStore } from "./stores/auth";
import { useRoomStore } from "./stores/room";
import { useDanmakuStore } from "./stores/danmaku";
import { useBanStore } from "./stores/ban";
import { bootstrap } from "./api/client";
import { BridgeWS, type BridgeEvent } from "./api/ws";
import { BanlistWS } from "./api/banWs";
import QrLogin from "./components/QrLogin.vue";
import RoomInput from "./components/RoomInput.vue";
import DanmakuList from "./components/DanmakuList.vue";
import ConnectionBanner from "./components/ConnectionBanner.vue";
import BanList from "./components/BanList.vue";

/**
 * T14 + T19 — top-level shell.
 *
 * Wires the auth flow (T10) to the room lifecycle (T14) and the
 * ban-list WS bridge (T19):
 *
 * 1. On mount, bootstrap the local token + fetch auth status.
 * 2. While ``auth.status !== 'authenticated'``, render the QR login.
 * 3. Once authenticated, mount the room input. The user resolves a
 *    short id → presses "连接" → the store calls ``POST /rooms/start``
 *    and flips to ``status='connected'``.
 * 4. We watch ``roomStore.currentRoomId``: when it transitions from
 *    ``null`` to a number, we open a ``BridgeWS`` AND a ``BanlistWS``
 *    for that room. The inverse transition (number → null) closes
 *    BOTH WSs and clears BOTH the danmaku and ban stores.
 * 5. The ConnectionBanner is shown whenever the bridge WS is
 *    disconnected or actively retrying — driven by ``wsVisible`` ref.
 *
 * T19 (ban) notes:
 *
 * * The ban store is owned by the WS — we never poll. Every
 *   snapshot / ban_added / ban_removed delta is dispatched into
 *   ``useBanStore`` from inside ``BanlistWS``.
 * * On disconnect, ``banStore.clear()`` is called alongside
 *   ``danmaku.clear()`` so the panel never shows stale entries from
 *   a previous room.
 * * BanControls is intentionally NOT mounted at the App level —
 *   it lives inline per danmaku row (the hover/popover wiring is
 *   owned by App.vue but rendered by a per-row slot on DanmakuList).
 */
const auth = useAuthStore();
const room = useRoomStore();
const danmaku = useDanmakuStore();
const ban = useBanStore();

const { currentRoomId } = storeToRefs(room);

const wsVisible = ref<boolean>(false);
let bridge: BridgeWS | null = null;
let banWs: BanlistWS | null = null;

onMounted(async () => {
  await bootstrap();
  await auth.fetchStatus();
  // F3 / Bug 5: the QR path never calls loginManual (which is the only
  // place userInfo was being set). On page-reload while already authed
  // we also have nothing in userInfo — so populate it from /auth/me
  // whenever the backend says we're authenticated. 401 is ignored
  // (user is logged out; userInfo stays null and the UI falls back to
  // "用户").
  if (auth.status === "authenticated") {
    try {
      await auth.fetchMe();
    } catch {
      // Logged out between fetchStatus and fetchMe — leave userInfo null.
    }
  }
});

watch(currentRoomId, (newId, oldId) => {
  if (oldId !== null && newId === null) {
    // Disconnected — close both WSs, drop banner, clear stores.
    bridge?.close();
    bridge = null;
    banWs?.close();
    banWs = null;
    wsVisible.value = false;
    danmaku.clear();
    ban.clear();
    return;
  }
  if (oldId === null && newId !== null) {
    // Just connected — open both WSs for this room.
    if (bridge !== null) {
      bridge.close();
    }
    if (banWs !== null) {
      banWs.close();
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
    banWs = new BanlistWS(newId, auth.token);
    banWs.connect();
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
      <BanList v-if="room.status === 'connected'" />
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