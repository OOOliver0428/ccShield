import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import GuardBadge from "./GuardBadge.vue";

describe("GuardBadge.vue", () => {
  it("renders nothing when level is 0", () => {
    const wrapper = mount(GuardBadge, { props: { level: 0 } });
    expect(wrapper.find('[data-testid="guard-badge"]').exists()).toBe(false);
    expect(wrapper.text()).toBe("");
  });

  it("renders nothing when level is unknown (e.g. 4)", () => {
    const wrapper = mount(GuardBadge, { props: { level: 4 } });
    expect(wrapper.find('[data-testid="guard-badge"]').exists()).toBe(false);
  });

  it("renders 舰长 for level=1", () => {
    const wrapper = mount(GuardBadge, { props: { level: 1 } });
    const badge = wrapper.find('[data-testid="guard-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe("舰长");
  });

  it("renders 提督 for level=2", () => {
    const wrapper = mount(GuardBadge, { props: { level: 2 } });
    const badge = wrapper.find('[data-testid="guard-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe("提督");
  });

  it("renders 总督 for level=3", () => {
    const wrapper = mount(GuardBadge, { props: { level: 3 } });
    const badge = wrapper.find('[data-testid="guard-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe("总督");
  });
});