import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import FanMedal from "./FanMedal.vue";

describe("FanMedal.vue", () => {
  it("renders nothing when medal is null", () => {
    const wrapper = mount(FanMedal, { props: { medal: null } });
    expect(wrapper.find('[data-testid="medal-badge"]').exists()).toBe(false);
    expect(wrapper.text()).toBe("");
  });

  it("renders medal name and level when present", () => {
    const wrapper = mount(FanMedal, {
      props: { medal: { name: "粉丝团", level: 5 } },
    });
    const badge = wrapper.find('[data-testid="medal-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toContain("粉丝团");
    expect(badge.text()).toContain("5");
  });

  it("renders medal with level 25", () => {
    const wrapper = mount(FanMedal, {
      props: { medal: { name: "粉丝牌", level: 25 } },
    });
    const badge = wrapper.find('[data-testid="medal-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toContain("粉丝牌");
    expect(badge.text()).toContain("25");
  });
});