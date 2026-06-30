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
    server.use(
      http.post("*/api/auth/qr/start", () =>
        HttpResponse.json({
          qrcode_url: "data:image/png;base64,FAKEQR",
          qrcode_key: "k1",
        }),
      ),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "scanning" }),
      ),
    );

    const wrapper = mount(QrLogin);
    const store = useAuthStore();
    await store.startQr();
    await flushPromises();

    const img = wrapper.find("img");
    expect(img.exists()).toBe(true);
    expect(img.attributes("src")).toBe("data:image/png;base64,FAKEQR");
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
          qrcode_url: `data:image/png;base64,QR${startCalls}`,
          qrcode_key: `k${startCalls}`,
        });
      }),
      http.get("*/api/auth/qr/poll", () =>
        HttpResponse.json({ status: "expired" }),
      ),
    );

    const wrapper = mount(QrLogin);
    const store = useAuthStore();
    await store.startQr();
    await flushPromises();

    expect(startCalls).toBe(1);

    const buttons = wrapper.findAll("button");
    const regen = buttons.find((b) => b.text().includes("重新生成"));
    expect(regen).toBeDefined();
    await regen!.trigger("click");
    await flushPromises();

    expect(startCalls).toBe(2);
    const img = wrapper.find("img");
    expect(img.attributes("src")).toBe("data:image/png;base64,QR2");
  });
});