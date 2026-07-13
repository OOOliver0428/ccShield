import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

describe("quick room verification theme contrast", () => {
  it("defines dedicated high-contrast detail colors for both themes", () => {
    const tokens = source("./tokens.css");

    expect(tokens.match(/--cc-verified-detail-bg:/g)).toHaveLength(2);
    expect(tokens.match(/--cc-verified-detail-text:/g)).toHaveLength(2);
    expect(tokens).toContain("--cc-verified-detail-text: #f5fff9;");
    expect(tokens).toContain("--cc-verified-detail-text: #17382d;");
  });

  it("uses the contrast tokens for resolved room metadata", () => {
    const component = source("../components/QuickRooms.vue");

    expect(component).toContain("background: var(--cc-verified-detail-bg);");
    expect(component).toContain("color: var(--cc-verified-detail-label);");
    expect(component).toContain("color: var(--cc-verified-detail-text);");
  });
});
