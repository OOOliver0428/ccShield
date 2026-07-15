import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { ElMessage } from "element-plus";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useRoomStore } from "../stores/room";
import BanControls from "./BanControls.vue";

/**
 * BanControls.vue tests (T19).
 *
 * The ban duration map lives inside the component (it's a UI
 * affordance, not transport shape), so we read it back via the
 * label of each option. The ElMessageBox confirm step is mocked
 * via ``vi.spyOn(ElementPlus, 'ElMessageBox')`` — we just need to
 * assert it was called BEFORE the POST fires (二次确认 contract).
 */
describe("BanControls.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the six supported duration options including permanent", () => {
    const wrapper = mount(BanControls, { props: { uid: 42, uname: "bob" } });
    const options = wrapper.findAll('[data-testid="duration-option"]');
    expect(options).toHaveLength(6);
    const labels = options.map((o) => o.text());
    expect(labels).toEqual(
      ["本场", "2小时", "4小时", "24小时", "7天", "永久"],
    );
    expect(labels).toContain("永久");
  });

  it("clicking 禁言 with default duration POSTs hour=2", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    let postedBody: unknown = null;
    server.use(
      http.post("*/api/ban", async ({ request }) => {
        postedBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );

    // Stub confirm → accept.
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = mount(BanControls, { props: { uid: 42, uname: "bob" } });
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    expect(postedBody).toEqual({
      room_id: 12345,
      uid: 42,
      hour: 2,
      uname: "bob",
    });
  });

  it("selects 永久 → hour=-1 and requires confirmation", async () => {
    const room = useRoomStore();
    room.currentRoomId = 9999;

    let postedBody: unknown = null;
    server.use(
      http.post("*/api/ban", async ({ request }) => {
        postedBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );

    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = mount(BanControls, { props: { uid: 7, uname: "x" } });
    const options = wrapper.findAll('[data-testid="duration-option"]');
    const permanent = options.find((o) => o.text() === "永久");
    expect(permanent).toBeDefined();
    await permanent!.trigger("click");
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    expect(confirm.mock.calls[0]?.[0]).toContain("期限：永久");
    expect((postedBody as { hour: number }).hour).toBe(-1);
  });

  it("selects 本场 → hour=0 in the POST body", async () => {
    const room = useRoomStore();
    room.currentRoomId = 9999;

    let postedBody: unknown = null;
    server.use(
      http.post("*/api/ban", async ({ request }) => {
        postedBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );

    vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = mount(BanControls, { props: { uid: 7, uname: "x" } });
    const options = wrapper.findAll('[data-testid="duration-option"]');
    const local = options.find((o) => o.text() === "本场");
    expect(local).toBeDefined();
    await local!.trigger("click");
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    expect((postedBody as { hour: number }).hour).toBe(0);
  });

  it("二次确认: confirm() is called before POST /api/ban", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    const callOrder: string[] = [];
    vi.spyOn(window, "confirm").mockImplementation(() => {
      callOrder.push("confirm");
      return true;
    });
    server.use(
      http.post("*/api/ban", async () => {
        callOrder.push("post");
        return HttpResponse.json({ ok: true });
      }),
    );

    const wrapper = mount(BanControls, { props: { uid: 42, uname: "bob" } });
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    expect(callOrder).toEqual(["confirm", "post"]);
  });

  it("二次确认 cancel → no POST is issued", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    let postCalls = 0;
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    server.use(
      http.post("*/api/ban", async () => {
        postCalls += 1;
        return HttpResponse.json({ ok: true });
      }),
    );

    const wrapper = mount(BanControls, {
      props: { uid: 42, uname: "bob", message: "原始弹幕" },
    });
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    expect(postCalls).toBe(0);
    expect(confirm.mock.calls[0]?.[0]).toContain("bob (uid:42)");
    expect(confirm.mock.calls[0]?.[0]).toContain("发言：原始弹幕");
  });

  it("on 200 success → emits 'success' event with the banned uid", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    server.use(
      http.post("*/api/ban", () => HttpResponse.json({ ok: true })),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const successToast = vi
      .spyOn(ElMessage, "success")
      .mockReturnValue({ close: vi.fn() });

    const wrapper = mount(BanControls, { props: { uid: 42, uname: "bob" } });
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    const events = wrapper.emitted("success");
    expect(events).toBeTruthy();
    expect(events?.[0]?.[0]).toEqual({ uid: 42 });
    expect(successToast).toHaveBeenCalledWith(
      expect.objectContaining({
        message: "已成功禁言 bob（2小时）",
      }),
    );
  });

  it("on 200 success → banStore receives the new ban via addBan (optimistic)", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    server.use(
      http.post("*/api/ban", () =>
        HttpResponse.json({ ok: true, block_id: "b-77" }),
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    // We can't easily reach the store from here without exporting it,
    // but the optimistic add is verified through the store import:
    const { useBanStore } = await import("../stores/ban");
    const banStore = useBanStore();

    const wrapper = mount(BanControls, { props: { uid: 77, uname: "carol" } });
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    expect(banStore.banList.some((b) => b.uid === 77)).toBe(true);
  });

  it("on backend error → does NOT emit success and surfaces error", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    server.use(
      http.post("*/api/ban", () =>
        HttpResponse.json({ detail: "ban failed" }, { status: 400 }),
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = mount(BanControls, { props: { uid: 42, uname: "bob" } });
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    const events = wrapper.emitted("success");
    expect(events).toBeFalsy();
    expect(wrapper.find('[data-testid="ban-error"]').exists()).toBe(true);
  });

  it("reason input is optional — empty reason sends undefined", async () => {
    const room = useRoomStore();
    room.currentRoomId = 12345;

    let postedBody: unknown = null;
    server.use(
      http.post("*/api/ban", async ({ request }) => {
        postedBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = mount(BanControls, { props: { uid: 42, uname: "bob" } });
    // Inner-class selector — el-input data-testid forwarding is brittle.
    const reasonInput = wrapper.find("input.el-input__inner");
    expect(reasonInput.exists()).toBe(true);
    await wrapper.find('[data-testid="ban-btn"]').trigger("click");
    await flushPromises();

    const body = postedBody as { reason?: string };
    expect(body.reason === "" || body.reason === undefined).toBe(true);
  });
});
