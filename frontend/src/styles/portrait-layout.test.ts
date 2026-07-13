import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const DESKTOP_PORTRAIT_QUERY =
  "@media (orientation: portrait) and (min-width: 680px) and (min-height: 900px) and (hover: hover) and (pointer: fine)";

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

describe("desktop portrait layout", () => {
  it("keeps the moderation workspace in a full-height two-column layout", () => {
    const app = source("../App.vue");

    expect(app).toContain(DESKTOP_PORTRAIT_QUERY);
    expect(app).toContain("height: max(720px, calc(100dvh - 339px));");
    expect(app).toContain(
      "grid-template-columns: minmax(0, 1fr) clamp(288px, 30vw, 332px);",
    );
  });

  it("overrides narrow-screen stacking only for fine-pointer portrait desktops", () => {
    const roomInput = source("../components/RoomInput.vue");
    const danmakuList = source("../components/DanmakuList.vue");
    const banList = source("../components/BanList.vue");

    for (const component of [roomInput, danmakuList, banList]) {
      expect(component).toContain(DESKTOP_PORTRAIT_QUERY);
    }

    expect(roomInput).toContain("padding-right: 110px;");
    expect(danmakuList).toMatch(/\.danmaku-list \{\r?\n\s+height: 100%;/);
    expect(banList).toMatch(/\.ban-list \{\r?\n\s+height: 100%;/);
  });
});
