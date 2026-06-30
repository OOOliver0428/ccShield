import { describe, it, expect, beforeEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useAuthStore } from "../stores/auth";
import { bootstrap } from "./client";

describe("api/client.bootstrap", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("GETs /api/auth/bootstrap and stores the returned token in authStore.token", async () => {
    server.use(
      http.get("*/api/auth/bootstrap", () =>
        HttpResponse.json({ token: "abc-123-xyz" }),
      ),
    );

    const store = useAuthStore();
    expect(store.token).toBe("");

    await bootstrap();

    expect(store.token).toBe("abc-123-xyz");
  });
});