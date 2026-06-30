<script setup lang="ts">
import { computed, ref } from "vue";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();

const showManual = ref(false);
const sessdata = ref("");
const biliJct = ref("");
const buvid3 = ref("");
const manualSubmitting = ref(false);
const manualError = ref<string | null>(null);

const statusText = computed(() => {
  switch (auth.qrPollStatus) {
    case "scanning":
      return "请使用 B站 手机 App 扫码登录";
    case "confirmed":
      return "已扫码,请在手机上点击确认";
    case "expired":
      return "二维码已过期";
    case "success":
      return "登录成功";
    default:
      return "正在生成二维码…";
  }
});

const showRegenerate = computed(() => auth.qrPollStatus === "expired");

async function regenerate(): Promise<void> {
  await auth.startQr();
}

async function submitManual(): Promise<void> {
  if (!sessdata.value || !biliJct.value) {
    manualError.value = "SESSDATA 和 bili_jct 必填";
    return;
  }
  manualError.value = null;
  manualSubmitting.value = true;
  try {
    await auth.loginManual(
      sessdata.value,
      biliJct.value,
      buvid3.value || null,
    );
  } catch (err) {
    manualError.value = (err as Error).message;
  } finally {
    manualSubmitting.value = false;
  }
}
</script>

<template>
  <el-card class="qr-login-card" shadow="hover">
    <template #header>
      <div class="card-header">
        <span class="title">reccshield · 登录</span>
        <el-button
          link
          type="primary"
          @click="showManual = !showManual"
          data-testid="toggle-manual"
        >
          {{ showManual ? "扫码登录" : "手动输入 Cookie" }}
        </el-button>
      </div>
    </template>

    <div v-if="!showManual" class="qr-section" data-testid="qr-section">
      <div class="qr-frame">
        <el-image
          v-if="auth.qrcodeUrl"
          :src="auth.qrcodeUrl"
          fit="contain"
          class="qr-image"
          data-testid="qr-image"
        />
        <div v-else class="qr-placeholder">加载中…</div>
      </div>
      <p class="status-text" data-testid="status-text">{{ statusText }}</p>
      <el-button
        v-if="showRegenerate"
        type="primary"
        @click="regenerate"
        data-testid="regenerate"
      >
        重新生成
      </el-button>
    </div>

    <el-form
      v-else
      class="manual-section"
      label-position="top"
      @submit.prevent="submitManual"
      data-testid="manual-section"
    >
      <el-form-item label="SESSDATA" required>
        <el-input
          v-model="sessdata"
          placeholder="从浏览器 Cookie 复制"
          data-testid="sessdata"
        />
      </el-form-item>
      <el-form-item label="bili_jct" required>
        <el-input
          v-model="biliJct"
          placeholder="从浏览器 Cookie 复制"
          data-testid="bili_jct"
        />
      </el-form-item>
      <el-form-item label="buvid3 (可选)">
        <el-input
          v-model="buvid3"
          placeholder="从浏览器 Cookie 复制 (可选)"
          data-testid="buvid3"
        />
      </el-form-item>
      <p v-if="manualError" class="error" data-testid="manual-error">
        {{ manualError }}
      </p>
      <el-button
        type="primary"
        native-type="submit"
        :loading="manualSubmitting"
        @click="submitManual"
        data-testid="manual-submit"
      >
        登录
      </el-button>
    </el-form>
  </el-card>
</template>

<style scoped>
.qr-login-card {
  width: 360px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.title {
  font-weight: 600;
}
.qr-section {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.qr-frame {
  width: 240px;
  height: 240px;
  background: #f5f5f5;
  border: 1px solid #e4e4e4;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.qr-image {
  width: 100%;
  height: 100%;
}
.qr-placeholder {
  color: #999;
}
.status-text {
  margin: 0;
  color: #555;
  font-size: 14px;
}
.manual-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.error {
  color: var(--el-color-danger);
  font-size: 13px;
  margin: 0;
}
</style>