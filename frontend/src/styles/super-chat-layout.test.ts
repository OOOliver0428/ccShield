import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

describe("SuperChat horizontal queue layout", () => {
  it("keeps all active cards in one fixed-height horizontally scrollable row", () => {
    const component = source("../components/DanmakuList.vue");

    expect(component).toMatch(/\.sc-cards \{[\s\S]*?display: flex;/);
    expect(component).toMatch(/\.sc-cards \{[\s\S]*?height: 100px;/);
    expect(component).toMatch(/\.sc-cards \{[\s\S]*?overflow-x: auto;/);
    expect(component).toMatch(/\.sc-cards \{[\s\S]*?overflow-y: hidden;/);
    expect(component).not.toContain("grid-template-columns: repeat(2");
  });
});
