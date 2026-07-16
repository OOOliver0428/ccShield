import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ProjectFooter from "./ProjectFooter.vue";

describe("ProjectFooter", () => {
  it("renders the MIT attribution and safe GitHub links", () => {
    const wrapper = mount(ProjectFooter);

    expect(wrapper.text()).toContain(
      "ccShield © 2026 OOOliver0428 and contributors",
    );
    expect(wrapper.text()).toContain("MIT License");
    expect(wrapper.text()).toContain("GitHub");

    const links = wrapper.findAll("a");
    expect(links.map((link) => link.attributes("href"))).toEqual([
      "https://github.com/OOOliver0428",
      "https://github.com/OOOliver0428/ccShield/blob/main/LICENSE",
      "https://github.com/OOOliver0428/ccShield",
    ]);
    for (const link of links) {
      expect(link.attributes("target")).toBe("_blank");
      expect(link.attributes("rel")).toBe("noopener noreferrer");
    }
  });
});
