import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import ThemeToggle from "./ThemeToggle.vue";

describe("ThemeToggle.vue", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.dataset.theme = "dark";
    document.documentElement.style.colorScheme = "";

    const meta = document.createElement("meta");
    meta.name = "theme-color";
    meta.content = "#090c12";
    document.head.appendChild(meta);
  });

  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.dataset.theme = "dark";
    document.documentElement.style.colorScheme = "";
    document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => meta.remove());
  });

  it("switches to light mode and persists the choice", async () => {
    const wrapper = mount(ThemeToggle);
    const button = wrapper.get('[data-testid="theme-toggle"]');

    expect(button.text()).toContain("浅色");
    expect(button.attributes("aria-label")).toBe("切换到浅色主题");

    await button.trigger("click");

    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.style.colorScheme).toBe("light");
    expect(window.localStorage.getItem("ccshield-theme")).toBe("light");
    expect(document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.content).toBe(
      "#f2f5fa",
    );
    expect(button.text()).toContain("深色");
  });

  it("initializes from the theme already applied before Vue mounts", async () => {
    document.documentElement.dataset.theme = "light";
    const wrapper = mount(ThemeToggle);
    const button = wrapper.get('[data-testid="theme-toggle"]');

    expect(button.text()).toContain("深色");
    await button.trigger("click");

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem("ccshield-theme")).toBe("dark");
  });
});
