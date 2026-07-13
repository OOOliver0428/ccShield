<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
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
import ThemeToggle from "./components/ThemeToggle.vue";

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
 * 4. We watch ``currentRoomId`` together with room status. Resolving an
 *    id updates ``currentRoomId`` before the room is started, so the local
 *    WebSockets open only after ``POST /rooms/start`` succeeds.
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
 *
 * Bug B regression (a.log): QrLogin's onMounted called
 * ``auth.startQr()`` while ``auth.status === 'loading'``, racing the
 * parent's ``await bootstrap(); await auth.fetchStatus();`` chain.
 * The POST went out without the LOCAL_TOKEN bearer header → 401 +
 * visible "生成二维码失败" error. We now gate QrLogin's mount on
 * ``status !== 'loading'`` so it cannot call startQr until bootstrap
 * has set the token, and render a "加载中…" placeholder during the
 * loading window.
 */
const auth = useAuthStore();
const room = useRoomStore();
const danmaku = useDanmakuStore();
const ban = useBanStore();

const { currentRoomId, status: roomStatus } = storeToRefs(room);

const wsVisible = ref<boolean>(false);
const userInitial = computed(() =>
  (auth.userInfo?.uname?.trim().slice(0, 1) || "U").toUpperCase(),
);
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

// F3 / Bug 3: after QR login completes, `auth.status` flips from
// "needs_login" to "authenticated" via pollOnce→fetchStatus. The
// onMounted fetchMe above does NOT fire on that transition (App.vue
// was already mounted, so onMounted never runs again) — without this
// watcher, the welcome banner rendered the "用户" placeholder even
// though the user just successfully logged in. Watching auth.status
// and calling fetchMe on every transition to "authenticated" covers
// BOTH the initial-load case (handled redundantly by the onMounted
// block above) and the post-QR-success case (only this watcher).
const onAuthStatusChange = (s: string): void => {
  if (s === "authenticated") {
    void auth.fetchMe().catch(() => {
      // Logged out between status transition and fetchMe — leave userInfo
      // null and the UI falls back to the "用户" placeholder.
    });
  }
};
watch(() => auth.status, onAuthStatusChange);

watch([currentRoomId, roomStatus], ([newId, newStatus], [oldId]) => {
  if (newId === null) {
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
  // resolve() also sets currentRoomId, but the backend bridge does not exist
  // until /rooms/start succeeds. Connecting earlier races the REST request.
  if (newStatus === "connected" && (oldId !== newId || bridge === null)) {
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
    case "sc_delete":
      danmaku.removeSc(event.ids);
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
    <div
      v-if="auth.status === 'loading'"
      class="loading-placeholder"
      data-testid="loading-placeholder"
    >
      加载中…
    </div>
    <section v-else-if="auth.status !== 'authenticated'" class="login-shell">
      <ThemeToggle class="login-theme-toggle" />
      <div class="login-brand">
        <span class="brand-mark" aria-hidden="true">CS</span>
        <div>
          <span class="brand-name">ccShield</span>
          <span class="brand-caption">LIVE MODERATION CONSOLE</span>
        </div>
      </div>
      <QrLogin />
      <p class="login-footnote">本地运行 · 登录凭据仅保存在你的设备</p>
    </section>
    <section v-else class="authenticated-shell" data-testid="authenticated-shell">
      <header class="topbar">
        <div class="brand-lockup">
          <span class="brand-mark" aria-hidden="true">CS</span>
          <div>
            <h1 class="brand">ccShield</h1>
            <span class="brand-caption">LIVE MODERATION CONSOLE</span>
          </div>
        </div>

        <div class="topbar-status" role="status" aria-live="polite">
          <span class="status-orb" :class="`status-${room.status}`" aria-hidden="true" />
          <span>{{ room.status === "connected" ? "房间监控中" : room.status === "connecting" ? "正在连接" : "等待连接" }}</span>
        </div>

        <ThemeToggle />

        <div class="user-card">
          <span class="user-avatar" aria-hidden="true">{{ userInitial }}</span>
          <span class="welcome">
            <strong>{{ auth.userInfo?.uname ?? "用户" }}</strong>
            <span>mid: {{ auth.userInfo?.mid ?? "?" }}</span>
          </span>
        </div>
      </header>

      <div class="console-content">
        <RoomInput />
        <ConnectionBanner :visible="wsVisible" />
        <section
          v-if="room.status === 'connected'"
          class="moderator-workspace"
          data-testid="moderator-workspace"
        >
          <DanmakuList />
          <aside class="moderation-sidebar" aria-label="房管名单侧栏">
            <BanList />
          </aside>
        </section>
        <section v-else class="workspace-placeholder" aria-hidden="true">
          <div class="placeholder-grid" />
          <div class="placeholder-copy">
            <span class="placeholder-icon">◎</span>
            <strong>连接直播间后进入房管工作台</strong>
            <span>实时弹幕、醒目留言与禁言名单会在这里同步显示</span>
          </div>
        </section>
      </div>
    </section>
  </main>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  padding: 18px;
}
.loading-placeholder {
  width: min(420px, calc(100vw - 32px));
  margin: 16vh auto 0;
  padding: 44px 24px;
  text-align: center;
  color: var(--cc-text-secondary);
  font-size: 14px;
  border: 1px solid var(--cc-border);
  border-radius: var(--cc-radius-panel);
  background: var(--cc-card-background);
  box-shadow: var(--cc-shadow-panel);
}
.login-shell {
  position: relative;
  display: flex;
  min-height: calc(100vh - 36px);
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
}
.login-theme-toggle {
  position: fixed;
  z-index: 2;
  top: 18px;
  right: 18px;
}
.login-brand,
.brand-lockup {
  display: flex;
  align-items: center;
  gap: 12px;
}
.login-brand > div,
.brand-lockup > div {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.brand-mark {
  display: grid;
  width: 38px;
  height: 38px;
  flex: 0 0 38px;
  place-items: center;
  border: 1px solid var(--cc-brand-border);
  border-radius: 11px;
  color: #fff;
  background: linear-gradient(145deg, #8795ff, #5868d9);
  box-shadow: var(--cc-brand-shadow);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: -0.4px;
}
.brand-name,
.brand {
  margin: 0;
  color: var(--cc-text);
  font-size: 18px;
  font-weight: 720;
  letter-spacing: -0.35px;
}
.brand-caption {
  color: var(--cc-text-muted);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1.7px;
}
.login-footnote {
  margin: 0;
  color: var(--cc-text-muted);
  font-size: 11px;
}
.authenticated-shell {
  width: 100%;
  max-width: 1580px;
  margin: 0 auto;
}
.topbar {
  display: flex;
  min-height: 58px;
  padding: 10px 14px;
  align-items: center;
  gap: 18px;
  border: 1px solid var(--cc-border);
  border-radius: 15px;
  background: var(--cc-glass-background);
  box-shadow: var(--cc-soft-shadow);
  backdrop-filter: blur(18px);
}
.topbar-status {
  display: inline-flex;
  margin-left: auto;
  padding: 7px 10px;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--cc-border);
  border-radius: 999px;
  color: var(--cc-text-secondary);
  background: var(--cc-fill-faint);
  font-size: 11px;
  font-weight: 650;
}
.status-orb {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--cc-text-muted);
  box-shadow: 0 0 0 4px rgb(111 122 140 / 10%);
}
.status-orb.status-connected {
  background: var(--cc-success);
  box-shadow: 0 0 0 4px var(--cc-success-soft), 0 0 14px rgb(66 211 146 / 35%);
}
.status-orb.status-connecting {
  background: var(--cc-warning);
  box-shadow: 0 0 0 4px var(--cc-warning-soft);
}
.user-card {
  display: flex;
  min-width: 0;
  padding-left: 14px;
  align-items: center;
  gap: 9px;
  border-left: 1px solid var(--cc-border);
}
.user-avatar {
  display: grid;
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  place-items: center;
  border: 1px solid var(--cc-border-strong);
  border-radius: 50%;
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
  font-size: 12px;
  font-weight: 750;
}
.welcome {
  display: flex;
  min-width: 0;
  flex-direction: column;
  color: var(--cc-text-secondary);
  font-size: 10px;
  line-height: 1.35;
}
.welcome strong {
  overflow: hidden;
  max-width: 150px;
  color: var(--cc-text);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.console-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 12px;
}
.moderator-workspace {
  display: grid;
  height: clamp(580px, calc(100dvh - 238px), 830px);
  min-height: 0;
  grid-template-columns: minmax(0, 1fr) minmax(340px, 380px);
  gap: 12px;
}
.moderation-sidebar {
  min-width: 0;
  min-height: 0;
}
.workspace-placeholder {
  position: relative;
  display: grid;
  min-height: 360px;
  overflow: hidden;
  place-items: center;
  border: 1px solid var(--cc-border);
  border-radius: var(--cc-radius-panel);
  background: var(--cc-placeholder-background);
}
.placeholder-grid {
  position: absolute;
  inset: 0;
  opacity: 0.28;
  background-image:
    linear-gradient(var(--cc-border) 1px, transparent 1px),
    linear-gradient(90deg, var(--cc-border) 1px, transparent 1px);
  background-size: 34px 34px;
  mask-image: radial-gradient(circle at center, #000, transparent 72%);
}
.placeholder-copy {
  position: relative;
  display: flex;
  padding: 30px;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  text-align: center;
}
.placeholder-icon {
  display: grid;
  width: 48px;
  height: 48px;
  margin-bottom: 4px;
  place-items: center;
  border: 1px solid var(--cc-border-strong);
  border-radius: 15px;
  color: var(--cc-primary);
  background: var(--cc-primary-soft);
  font-size: 24px;
}
.placeholder-copy strong {
  color: var(--cc-text-secondary);
  font-size: 14px;
}
.placeholder-copy > span:last-child {
  color: var(--cc-text-muted);
  font-size: 12px;
}
@media (max-width: 900px) {
  .app-shell {
    padding: 12px;
  }
  .moderator-workspace {
    height: auto;
    grid-template-columns: minmax(0, 1fr);
  }
  .moderation-sidebar {
    min-height: 480px;
  }
  .workspace-placeholder {
    min-height: 280px;
  }
}
@media (max-width: 640px) {
  .topbar {
    gap: 10px;
  }
  .topbar-status {
    padding: 7px;
  }
  .topbar-status > span:last-child {
    display: none;
  }
  .user-card {
    padding-left: 0;
    border-left: 0;
  }
  .welcome {
    display: none;
  }
  .brand-caption {
    letter-spacing: 1px;
  }
}
/*
 * A rotated desktop monitor can report only 720-864 CSS pixels of width
 * when Windows scaling is set to 125%-150%. Pointer capability keeps this
 * layout separate from touch-first tablets with the same viewport size.
 */
@media (orientation: portrait) and (min-width: 680px) and (min-height: 900px) and (hover: hover) and (pointer: fine) {
  .app-shell {
    padding: 12px;
  }
  .authenticated-shell {
    max-width: none;
  }
  .topbar {
    gap: 10px;
  }
  .topbar-status {
    margin-left: auto;
  }
  .user-card {
    padding-left: 10px;
  }
  .welcome strong {
    max-width: 108px;
  }
  .brand-caption {
    letter-spacing: 1.15px;
  }
  .moderator-workspace {
    height: max(720px, calc(100dvh - 339px));
    min-height: 720px;
    grid-template-columns: minmax(0, 1fr) clamp(288px, 30vw, 332px);
  }
  .moderation-sidebar {
    min-height: 0;
  }
  .workspace-placeholder {
    min-height: max(560px, calc(100dvh - 164px));
  }
}
</style>
