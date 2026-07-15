import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useAuthStore } from "../stores/auth";
import QrLogin from "./QrLogin.vue";

describe("QrLogin.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the QR image when qrcodeUrl is set", async () => {
    // B站 returns a scan-link string for `qrcode_url` (NOT an image URL).
    // QrLogin must render it via QRCode.toDataURL into a PNG data URL.
    const scanLink =
      "https://passport.bilibili.com/x/passport-login/web/qrcode/confirm?qrcode_key=k1";
    server.use(
      http.post("*/api/auth/qr/start", () =>
        HttpResponse.json({
          qrcode_url: scanLink,
          qrcode_key: "k1",
        }),
      ),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "scanning" }),
      ),
    );

    const wrapper = mount(QrLogin);
    await flushPromises();
    await flushPromises();
    await flushPromises();
    await vi.waitFor(
      () => expect(wrapper.find('[data-testid="qr-image"]').exists()).toBe(true),
      { timeout: 2000 },
    );

    const img = wrapper.get('[data-testid="qr-image"]');
    const src = img.attributes("src");
    expect(src).toMatch(/^data:image\/png;base64,/);
    // Crucially, the raw scan-link string MUST NOT be passed through as src.
    expect(src).not.toBe(scanLink);
  });

  it("renders a '重新生成' button when qrPollStatus === 'expired'", async () => {
    server.use(
      http.post("*/api/auth/qr/start", () =>
        HttpResponse.json({
          qrcode_url: "data:image/png;base64,FAKEQR",
          qrcode_key: "k2",
        }),
      ),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "expired" }),
      ),
    );

    const wrapper = mount(QrLogin);
    const store = useAuthStore();
    await store.startQr();
    await flushPromises();

    expect(wrapper.text()).toContain("重新生成");
  });

  it("clicking '重新生成' triggers a fresh startQr", async () => {
    let startCalls = 0;
    server.use(
      http.post("*/api/auth/qr/start", () => {
        startCalls += 1;
        return HttpResponse.json({
          qrcode_url: `https://passport.bilibili.com/x/passport-login/web/qrcode/confirm?qrcode_key=k${startCalls}`,
          qrcode_key: `k${startCalls}`,
        });
      }),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "expired" }),
      ),
    );

    const wrapper = mount(QrLogin);
    await flushPromises();
    await flushPromises();
    await flushPromises();
    expect(startCalls).toBe(1);

    const buttons = wrapper.findAll("button");
    const regen = buttons.find((b) => b.text().includes("重新生成"));
    expect(regen).toBeDefined();
    await regen!.trigger("click");
    await flushPromises();
    await flushPromises();
    await flushPromises();
    await vi.waitFor(
      () => expect(wrapper.find('[data-testid="qr-image"]').exists()).toBe(true),
      { timeout: 2000 },
    );

    expect(startCalls).toBe(2);
    const img = wrapper.get('[data-testid="qr-image"]');
    const src = img.attributes("src");
    expect(src).toMatch(/^data:image\/png;base64,/);
  });

  // ---------------------------------------------------------------------------
  // F3 manual-QA regression: the page used to sit forever on
  // "正在生成二维码…" because (a) the browser never called /api/auth/qr/start
  // (no onMounted wired startQr) and (b) the SPA tried to load the B站
  // scan-link as if it were an image src. These tests pin both halves.
  // ---------------------------------------------------------------------------

  it("calls auth.startQr() automatically on mount (H1: F3 regression)", async () => {
    server.use(
      http.post("*/api/auth/qr/start", () =>
        HttpResponse.json({
          qrcode_url: "https://passport.bilibili.com/x/passport-login/web/qrcode/confirm?qrcode_key=auto",
          qrcode_key: "auto-key",
        }),
      ),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "scanning" }),
      ),
    );

    const wrapper = mount(QrLogin);
    await flushPromises();
    await flushPromises();
    await flushPromises();
    void wrapper;

    const store = useAuthStore();
    expect(store.qrcodeUrl).toBe(
      "https://passport.bilibili.com/x/passport-login/web/qrcode/confirm?qrcode_key=auto",
    );
    expect(store.qrKey).toBe("auto-key");
    expect(store.qrPollStatus).toBe("scanning");
  });

  it("renders the scan-link string as a QR <img> (H2: scan-link is NOT an image URL)", async () => {
    server.use(
      http.post("*/api/auth/qr/start", () =>
        HttpResponse.json({
          qrcode_url:
            "https://passport.bilibili.com/x/passport-login/web/qrcode/confirm?qrcode_key=scanlink",
          qrcode_key: "scanlink",
        }),
      ),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "scanning" }),
      ),
    );

    const wrapper = mount(QrLogin);
    await flushPromises();
    await flushPromises();
    await flushPromises();
    // QR generation uses a worker-backed PNG pipeline. Wait for the rendered
    // image rather than assuming a fixed number of promise drains is enough
    // on every CI runner.
    await vi.waitFor(
      () => expect(wrapper.find('[data-testid="qr-image"]').exists()).toBe(true),
      { timeout: 2000 },
    );

    const img = wrapper.get('[data-testid="qr-image"]');
    const src = img.attributes("src");
    expect(src).toBeTruthy();
    // The src must be a rendered QR data URL (data:image/...) — NOT the raw
    // scan-link string itself, which <el-image> would try to fetch as an
    // image and fail.
    expect(src).not.toBe(
      "https://passport.bilibili.com/x/passport-login/web/qrcode/confirm?qrcode_key=scanlink",
    );
    expect(src).toMatch(/^data:image\/png;base64,/);
  });

  it("shows an error message + retry button when startQr rejects (no silent 'generating')", async () => {
    server.use(
      http.post("*/api/auth/qr/start", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    const wrapper = mount(QrLogin);
    await flushPromises();
    await flushPromises();
    await flushPromises();

    const text = wrapper.text();
    // Must NOT stay on the "正在生成二维码…" default branch.
    expect(text).not.toContain("正在生成二维码…");
    // Must surface the failure with a retry affordance.
    expect(text).toMatch(/失败|重试/);

    const retryBtn = wrapper
      .findAll("button")
      .find((b) => b.text().includes("重试"));
    expect(retryBtn).toBeDefined();
  });
});
