import { describe, it, expect, beforeEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { http, HttpResponse } from "msw";
import { server } from "../__tests__/setup";
import { useAuthStore } from "../stores/auth";
import { bootstrap, httpClient } from "./client";

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

  it("globally transitions to expired on the stable B站 auth error", async () => {
    server.use(
      http.get("*/api/protected-expired", () =>
        HttpResponse.json(
          {
            detail: {
              code: "BILI_AUTH_EXPIRED",
              message: "登录凭据已失效",
            },
          },
          { status: 401 },
        ),
      ),
    );
    const store = useAuthStore();
    store.status = "authenticated";
    store.userInfo = { uname: "alice", mid: 1 };

    await expect(httpClient.get("/protected-expired")).rejects.toBeTruthy();

    expect(store.status).toBe("expired");
    expect(store.userInfo).toBeNull();
    expect(store.expiredMessage).toBe("登录凭据已失效");
  });

  it("does not confuse a local-token 401 with B站 Cookie expiry", async () => {
    server.use(
      http.get("*/api/local-token-rejected", () =>
        HttpResponse.json({ detail: "invalid local token" }, { status: 401 }),
      ),
    );
    const store = useAuthStore();
    store.status = "authenticated";

    await expect(httpClient.get("/local-token-rejected")).rejects.toBeTruthy();

    expect(store.status).toBe("authenticated");
    expect(store.expiredMessage).toBeNull();
  });
});
