import { defineStore } from "pinia";
import { ref } from "vue";
import { httpClient } from "../api/client";

export type AuthStatus =
  | "loading"
  | "needs_login"
  | "authenticated"
  | "expired";

export type QrPollStatus =
  | "scanning"
  | "confirmed"
  | "expired"
  | "success"
  | null;

export interface UserInfo {
  uname: string;
  mid: number;
}

export const AUTH_EXPIRED_MESSAGE = "B站登录已过期，请重新扫码登录";

export const useAuthStore = defineStore("auth", () => {
  const status = ref<AuthStatus>("loading");
  const token = ref<string>("");
  const userInfo = ref<UserInfo | null>(null);
  const qrcodeUrl = ref<string>("");
  const qrKey = ref<string>("");
  const qrPollStatus = ref<QrPollStatus>(null);
  const expiredMessage = ref<string | null>(null);

  let pollHandle: ReturnType<typeof setInterval> | null = null;

  function setToken(value: string): void {
    token.value = value;
  }

  function stopPolling(): void {
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  function markExpired(message: string = AUTH_EXPIRED_MESSAGE): void {
    // Several in-flight room/moderation requests can all observe the same
    // expiry. Only the first transition owns cleanup; a later 401 must not
    // stop the fresh QR polling that has already started on the login page.
    if (status.value === "expired") return;
    stopPolling();
    status.value = "expired";
    userInfo.value = null;
    expiredMessage.value = message;
    qrcodeUrl.value = "";
    qrKey.value = "";
    qrPollStatus.value = null;
  }

  async function fetchStatus(): Promise<void> {
    const response = await httpClient.get<{ state: string }>("/auth/status");
    const next = response.data.state;
    if (next === "authenticated") {
      status.value = "authenticated";
      expiredMessage.value = null;
    } else if (next === "needs_login") {
      status.value = "needs_login";
      userInfo.value = null;
      expiredMessage.value = null;
    } else if (next === "expired") {
      markExpired();
    } else {
      status.value = "needs_login";
      userInfo.value = null;
      expiredMessage.value = null;
    }
  }

  async function pollOnce(): Promise<void> {
    try {
      const response = await httpClient.get<{ status: QrPollStatus }>(
        "/auth/qr/poll",
        { params: { qrcode_key: qrKey.value } },
      );
      const next = response.data.status;
      qrPollStatus.value = next;
      if (next === "expired") {
        stopPolling();
      } else if (next === "success") {
        stopPolling();
        await fetchStatus();
      }
    } catch {
      // Poll errors are non-fatal: the next interval tick will retry.
      // Catching here keeps the long-term interval callback from
      // leaking unhandled promise rejections and keeps startQr's
      // fire-and-forget first poll safe.
    }
  }

  async function startQr(): Promise<void> {
    stopPolling();
    const response = await httpClient.post<{
      qrcode_url: string;
      qrcode_key: string;
    }>("/auth/qr/start");
    qrcodeUrl.value = response.data.qrcode_url;
    qrKey.value = response.data.qrcode_key;
    qrPollStatus.value = "scanning";
    pollHandle = setInterval(() => {
      void pollOnce();
    }, 2000);
    // pollOnce swallows its own errors, so this initial poll can never
    // reject — the QR image is rendered even on a transient poll blip,
    // and the setInterval above keeps retrying.
    await pollOnce();
  }

  async function loginManual(
    sessdata: string,
    bili_jct: string,
    buvid3: string | null,
  ): Promise<void> {
    const response = await httpClient.post<{ uname: string; mid: number }>(
      "/auth/manual",
      { sessdata, bili_jct, buvid3 },
    );
    userInfo.value = {
      uname: response.data.uname,
      mid: response.data.mid,
    };
    await fetchStatus();
  }

  // Populate userInfo for every authenticated entry path (manual login,
  // QR login, and page reload). The backend's
  // /auth/me returns {uname, mid} when a session is active and 401
  // otherwise; we forward rejections so the caller (App.vue) can
  // distinguish "logged out" from "user not yet known".
  async function fetchMe(): Promise<void> {
    const response = await httpClient.get<{ uname: string; mid: number }>(
      "/auth/me",
    );
    userInfo.value = {
      uname: response.data.uname,
      mid: response.data.mid,
    };
  }

  return {
    status,
    token,
    userInfo,
    qrcodeUrl,
    qrKey,
    qrPollStatus,
    expiredMessage,
    setToken,
    markExpired,
    fetchStatus,
    fetchMe,
    startQr,
    loginManual,
  };
});
