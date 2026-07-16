<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useQuickRoomsStore, type QuickRoom } from "../stores/quickRooms";
import { useRoomStore, type ResolveRoomResponse } from "../stores/room";

const props = withDefaults(defineProps<{ demoMode?: boolean }>(), {
  demoMode: false,
});

const quickRooms = useQuickRoomsStore();
const room = useRoomStore();

const dialogVisible = ref(false);
const inputText = ref("");
const verified = ref<ResolveRoomResponse | null>(null);
const verifiedInput = ref("");
const localMessage = ref("");

const canSaveVerified = computed(
  () =>
    verified.value !== null &&
    verifiedInput.value === inputText.value.trim() &&
    !quickRooms.saving,
);

const currentAlreadySaved = computed(() =>
  room.currentRoomId === null
    ? false
    : quickRooms.rooms.some((item) => item.room_id === room.currentRoomId),
);

watch(inputText, () => {
  if (inputText.value.trim() !== verifiedInput.value) {
    verified.value = null;
    localMessage.value = "";
  }
});

onMounted(() => {
  if (props.demoMode) return;
  void quickRooms.load();
});

function openConfiguration(): void {
  inputText.value = "";
  verified.value = null;
  verifiedInput.value = "";
  localMessage.value = "";
  dialogVisible.value = true;
}

async function verifyRoom(): Promise<void> {
  localMessage.value = "";
  const normalizedInput = inputText.value.trim();
  const result = await quickRooms.verify(normalizedInput);
  if (result !== null) {
    verified.value = result;
    verifiedInput.value = normalizedInput;
  }
}

async function saveVerified(): Promise<void> {
  if (!canSaveVerified.value || verified.value === null) return;
  const ok = await quickRooms.add(verified.value.room_id);
  if (ok) {
    localMessage.value = "快捷房间已保存到本地配置";
  }
}

async function addCurrentRoom(): Promise<void> {
  if (room.currentRoomId === null || quickRooms.saving) return;
  const ok = await quickRooms.add(room.currentRoomId);
  if (ok) localMessage.value = "当前房间已加入快捷入口";
}

async function connectQuickRoom(item: QuickRoom): Promise<void> {
  if (room.status !== "disconnected") return;
  room.prepareShortcut(item);
  await room.connect(item.room_id);
}

function liveLabel(status: number | undefined): string {
  return status === 1 ? "直播中" : "未开播";
}
</script>

<template>
  <section
    v-if="room.status === 'disconnected'"
    class="quick-room-panel"
    data-testid="quick-room-panel"
    aria-label="快捷房间"
  >
    <div class="quick-heading">
      <div>
        <span class="eyebrow">QUICK ACCESS</span>
        <strong>快捷房间</strong>
        <span class="quick-description">选择一个常用直播间直接连接</span>
      </div>
      <el-button plain data-testid="open-quick-config" @click="openConfiguration">
        ＋ 配置快捷房间
      </el-button>
    </div>

    <div v-if="quickRooms.loading" class="quick-state">正在读取本地配置…</div>
    <div v-else-if="quickRooms.rooms.length" class="quick-grid">
      <button
        v-for="item in quickRooms.rooms"
        :key="item.room_id"
        class="quick-card"
        type="button"
        :aria-label="`连接 ${item.uname || item.room_id} 的直播间`"
        :data-testid="`quick-room-${item.room_id}`"
        @click="connectQuickRoom(item)"
      >
        <span class="quick-avatar" aria-hidden="true">{{ (item.uname || "房").slice(0, 1) }}</span>
        <span class="quick-copy">
          <strong>{{ item.uname || `房间 ${item.room_id}` }}</strong>
          <span>{{ item.title || "暂无直播标题" }}</span>
        </span>
        <span class="room-number">#{{ item.room_id }}</span>
      </button>
    </div>
    <button v-else class="quick-empty" type="button" @click="openConfiguration">
      <span>＋</span>
      <strong>还没有快捷房间</strong>
      <small>添加后即可从这里一键连接</small>
    </button>
    <p v-if="quickRooms.error" class="error" role="alert">{{ quickRooms.error }}</p>
  </section>

  <section
    v-else-if="room.status === 'connected'"
    class="current-room-shortcut"
    data-testid="current-room-shortcut"
  >
    <span>
      <strong>常用房间</strong>
      <small>{{ currentAlreadySaved ? "当前房间已在本地快捷配置中" : "将当前直播间保存为快捷入口" }}</small>
    </span>
    <el-button
      type="primary"
      plain
      :loading="quickRooms.saving"
      :disabled="currentAlreadySaved"
      data-testid="add-current-room"
      @click="addCurrentRoom"
    >
      {{ currentAlreadySaved ? "已添加" : "一键添加当前房间" }}
    </el-button>
    <span v-if="localMessage" class="success-message">{{ localMessage }}</span>
    <span v-if="quickRooms.error" class="error" role="alert">{{ quickRooms.error }}</span>
  </section>

  <el-dialog
    v-model="dialogVisible"
    title="配置快捷房间"
    width="min(560px, calc(100vw - 32px))"
    class="quick-room-dialog"
    data-testid="quick-room-dialog"
    append-to-body
  >
    <div class="dialog-copy">
      <span class="eyebrow">ROOM VERIFICATION</span>
      <p>输入短号或正常房间号，先验证主播信息，再保存为快捷入口。</p>
    </div>

    <div class="verify-row">
      <div class="verify-field" data-testid="quick-room-input">
        <el-input
          v-model="inputText"
          inputmode="numeric"
          placeholder="例如：1601605"
          aria-label="快捷房间号"
          size="large"
          @keyup.enter="verifyRoom"
        />
      </div>
      <el-button
        size="large"
        :loading="quickRooms.verifying"
        data-testid="verify-quick-room"
        @click="verifyRoom"
      >
        一键测试
      </el-button>
    </div>

    <article v-if="verified" class="verified-card" data-testid="verified-room">
      <div class="verified-header">
        <span class="verified-check" aria-hidden="true">✓</span>
        <span>
          <small>房间号验证成功</small>
          <strong>{{ verified.uname || "未知主播" }}</strong>
        </span>
        <em :class="{ live: verified.live_status === 1 }">{{ liveLabel(verified.live_status) }}</em>
      </div>
      <dl>
        <div><dt>真实房间号</dt><dd>{{ verified.room_id }}</dd></div>
        <div v-if="verified.short_id && verified.short_id !== verified.room_id"><dt>短号</dt><dd>{{ verified.short_id }}</dd></div>
        <div class="title-row"><dt>直播标题</dt><dd>{{ verified.title || "暂无直播标题" }}</dd></div>
      </dl>
    </article>

    <p v-if="quickRooms.error" class="dialog-error error" role="alert">{{ quickRooms.error }}</p>
    <p v-if="localMessage" class="dialog-success">{{ localMessage }}</p>

    <div v-if="quickRooms.rooms.length" class="configured-list">
      <span>已配置 {{ quickRooms.rooms.length }} 个快捷房间</span>
      <small>初版不支持页面删除；如需删除，请关闭程序后编辑 config/quick_rooms.json。</small>
    </div>

    <template #footer>
      <el-button @click="dialogVisible = false">完成</el-button>
      <el-button
        type="primary"
        :disabled="!canSaveVerified"
        :loading="quickRooms.saving"
        data-testid="save-quick-room"
        @click="saveVerified"
      >
        添加快捷入口
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.quick-room-panel,
.current-room-shortcut {
  border: 1px solid var(--cc-border);
  border-radius: var(--cc-radius-panel);
  background: var(--cc-card-background);
  box-shadow: var(--cc-soft-shadow);
}
.quick-room-panel {
  padding: 14px 16px 16px;
}
.quick-heading,
.quick-heading > div,
.current-room-shortcut,
.current-room-shortcut > span:first-child {
  display: flex;
  align-items: center;
}
.quick-heading {
  justify-content: space-between;
  gap: 16px;
}
.quick-heading > div {
  min-width: 0;
  gap: 9px;
}
.eyebrow {
  color: var(--cc-primary);
  font-size: 9px;
  font-weight: 760;
  letter-spacing: 1.5px;
}
.quick-heading strong {
  color: var(--cc-text);
  font-size: 14px;
}
.quick-description {
  color: var(--cc-text-muted);
  font-size: 11px;
}
.quick-grid {
  display: grid;
  margin-top: 12px;
  grid-template-columns: repeat(auto-fill, minmax(245px, 1fr));
  gap: 8px;
}
.quick-card {
  display: flex;
  min-width: 0;
  padding: 11px;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--cc-border);
  border-radius: 12px;
  color: inherit;
  background: var(--cc-fill-faint);
  cursor: pointer;
  text-align: left;
  transition: border-color 160ms ease, transform 160ms ease, background 160ms ease;
}
.quick-card:hover {
  border-color: var(--cc-primary);
  background: var(--cc-primary-soft);
  transform: translateY(-1px);
}
.quick-avatar {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  place-items: center;
  border-radius: 10px;
  color: var(--cc-primary-emphasis);
  background: var(--cc-primary-soft);
  font-size: 13px;
  font-weight: 750;
}
.quick-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 3px;
}
.quick-copy strong,
.quick-copy span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.quick-copy strong {
  color: var(--cc-text);
  font-size: 12px;
}
.quick-copy span,
.room-number {
  color: var(--cc-text-muted);
  font-size: 10px;
}
.room-number {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
}
.quick-empty {
  display: flex;
  width: 100%;
  margin-top: 12px;
  padding: 15px;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: 1px dashed var(--cc-border-strong);
  border-radius: 12px;
  color: var(--cc-text-secondary);
  background: var(--cc-fill-faint);
  cursor: pointer;
}
.quick-empty span {
  color: var(--cc-primary);
  font-size: 18px;
}
.quick-empty small {
  color: var(--cc-text-muted);
}
.quick-state {
  padding: 18px;
  color: var(--cc-text-muted);
  text-align: center;
  font-size: 12px;
}
.current-room-shortcut {
  min-height: 52px;
  padding: 9px 12px;
  justify-content: flex-end;
  gap: 10px;
}
.current-room-shortcut > span:first-child {
  margin-right: auto;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}
.current-room-shortcut strong {
  color: var(--cc-text);
  font-size: 12px;
}
.current-room-shortcut small {
  color: var(--cc-text-muted);
  font-size: 10px;
}
.success-message,
.dialog-success {
  color: var(--cc-success);
  font-size: 11px;
}
.error {
  margin: 8px 0 0;
  color: var(--cc-danger);
  font-size: 11px;
}
.dialog-copy p {
  margin: 6px 0 16px;
  color: var(--cc-text-secondary);
  font-size: 12px;
}
.verify-row {
  display: flex;
  gap: 8px;
}
.verify-field {
  flex: 1;
}
.verified-card {
  margin-top: 14px;
  padding: 14px;
  border: 1px solid rgb(66 211 146 / 24%);
  border-radius: 13px;
  background: var(--cc-success-soft);
}
.verified-header {
  display: flex;
  align-items: center;
  gap: 10px;
}
.verified-check {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: 50%;
  color: white;
  background: var(--cc-success);
  font-weight: 800;
}
.verified-header > span:nth-child(2) {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
}
.verified-header small,
.configured-list small {
  color: var(--cc-text-muted);
  font-size: 10px;
}
.verified-header strong {
  color: var(--cc-text);
  font-size: 14px;
}
.verified-header em {
  padding: 4px 7px;
  border-radius: 999px;
  color: var(--cc-text-muted);
  background: var(--cc-fill-subtle);
  font-size: 10px;
  font-style: normal;
}
.verified-header em.live {
  color: var(--cc-success-emphasis);
  background: rgb(66 211 146 / 16%);
}
.verified-card dl {
  display: grid;
  margin: 13px 0 0;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.verified-card dl > div {
  padding: 8px 9px;
  border: 1px solid var(--cc-verified-detail-border);
  border-radius: 9px;
  background: var(--cc-verified-detail-bg);
}
.verified-card .title-row {
  grid-column: 1 / -1;
}
.verified-card dt {
  color: var(--cc-verified-detail-label);
  font-size: 9px;
}
.verified-card dd {
  overflow: hidden;
  margin: 3px 0 0;
  color: var(--cc-verified-detail-text);
  font-size: 11px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.configured-list {
  display: flex;
  margin-top: 16px;
  padding-top: 12px;
  flex-direction: column;
  gap: 3px;
  border-top: 1px solid var(--cc-border);
  color: var(--cc-text-secondary);
  font-size: 11px;
}
@media (max-width: 640px) {
  .quick-heading,
  .quick-heading > div,
  .current-room-shortcut {
    align-items: stretch;
    flex-direction: column;
  }
  .quick-heading > div {
    gap: 4px;
  }
  .quick-description {
    margin-bottom: 4px;
  }
  .current-room-shortcut > span:first-child {
    margin-right: 0;
  }
  .verify-row {
    flex-direction: column;
  }
}
</style>
