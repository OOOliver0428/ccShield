<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import QRCode from "qrcode";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();

const showManual = ref(false);
const sessdata = ref("");
const biliJct = ref("");
const buvid3 = ref("");
const manualSubmitting = ref(false);
const manualError = ref<string | null>(null);

// F3 manual-QA regression (see QrLogin.test.ts):
// the QR card used to sit on "正在生成二维码…" forever because (a) the
// browser never called /api/auth/qr/start — no onMounted wired startQr
// — and (b) the SPA tried to load the B站 scan-link as if it were an
// image src. Track the request-side failure distinctly from the
// generation-text branch so the user gets a real retry affordance
// instead of a silent infinite spinner.
const startError = ref<string | null>(null);
const isStarting = ref(false);
const qrDataUrl = ref<string>("");
const qrRenderError = ref<string | null>(null);

const statusText = computed(() => {
  if (startError.value !== null) {
    return `生成二维码失败:${startError.value}`;
  }
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
const showRetry = computed(
  () => startError.value !== null && !isStarting.value,
);

async function startQrWithCatch(): Promise<void> {
  isStarting.value = true;
  startError.value = null;
  try {
    await auth.startQr();
  } catch (err) {
    startError.value = (err as Error).message || String(err);
  } finally {
    isStarting.value = false;
  }
}

async function regenerate(): Promise<void> {
  await startQrWithCatch();
}

async function retryStart(): Promise<void> {
  await startQrWithCatch();
}

// Render the B站 scan-link string (`auth.qrcodeUrl`) into a PNG data URL
// via the `qrcode` package. el-image cannot do this — the scan-link is
// a URL the user must scan with the B站 mobile app, not an <img src>.
watch(
  () => auth.qrcodeUrl,
  async (url) => {
    qrRenderError.value = null;
    if (!url) {
      qrDataUrl.value = "";
      return;
    }
    try {
      qrDataUrl.value = await QRCode.toDataURL(url, {
        width: 240,
        margin: 1,
        errorCorrectionLevel: "M",
      });
    } catch (err) {
      qrDataUrl.value = "";
      qrRenderError.value = (err as Error).message || String(err);
    }
  },
  { immediate: true },
);

onMounted(() => {
  void startQrWithCatch();
});

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
        <img
          v-if="qrDataUrl"
          :src="qrDataUrl"
          alt="login QR code"
          class="qr-image"
          data-testid="qr-image"
        />
        <div v-else-if="qrRenderError" class="qr-placeholder qr-placeholder--err">
          二维码渲染失败
        </div>
        <div v-else class="qr-placeholder" data-testid="qr-placeholder">加载中…</div>
      </div>
      <p class="status-text" data-testid="status-text">{{ statusText }}</p>
      <p
        v-if="startError"
        class="error"
        data-testid="qr-start-error"
      >
        {{ startError }}
      </p>
      <div class="qr-actions">
        <el-button
          v-if="showRegenerate"
          type="primary"
          @click="regenerate"
          data-testid="regenerate"
        >
          重新生成
        </el-button>
        <el-button
          v-if="showRetry"
          type="primary"
          @click="retryStart"
          data-testid="qr-retry"
        >
          重试
        </el-button>
      </div>
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
  display: block;
}
.qr-placeholder {
  color: #999;
}
.qr-placeholder--err {
  color: var(--el-color-danger);
}
.status-text {
  margin: 0;
  color: #555;
  font-size: 14px;
}
.qr-actions {
  display: flex;
  gap: 8px;
  align-items: center;
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