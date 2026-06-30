import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import type { BridgeScEvent } from "../api/ws";
import SuperChatItem from "./SuperChatItem.vue";

function makeSc(overrides: Partial<BridgeScEvent> = {}): BridgeScEvent {
  return {
    type: "sc",
    uid: 7,
    uname: "bob",
    text: "hi",
    price: 30,
    ts: 1_700_000_000,
    ...overrides,
  };
}

describe("SuperChatItem.vue", () => {
  it("renders uname, text, and formatted price", () => {
    const wrapper = mount(SuperChatItem, {
      props: { sc: makeSc({ uname: "bob", text: "hi", price: 30 }) },
    });

    expect(wrapper.find('[data-testid="sc-row"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="sc-uname"]').text()).toBe("bob");
    expect(wrapper.find('[data-testid="sc-text"]').text()).toBe("hi");
    expect(wrapper.find('[data-testid="sc-price"]').text()).toBe("¥30");
  });

  it("formats large prices with no decimals", () => {
    const wrapper = mount(SuperChatItem, {
      props: { sc: makeSc({ price: 1234 }) },
    });
    expect(wrapper.find('[data-testid="sc-price"]').text()).toBe("¥1234");
  });

  it("renders timestamp formatted as HH:MM:SS", () => {
    // 1_700_000_000 = 2023-11-14T22:13:20Z
    const wrapper = mount(SuperChatItem, {
      props: { sc: makeSc({ ts: 1_700_000_000 }) },
    });
    const ts = wrapper.find('[data-testid="sc-ts"]');
    expect(ts.exists()).toBe(true);
    expect(ts.text()).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});