/**
 * Vitest setup — boots MSW's Node-side request interceptor and resets it
 * between tests. Per-test mocks register via `server.use(...)` from
 * inside the test body; the default handlers here are deliberately
 * sparse so an accidental network call surfaces as a clean test
 * failure rather than a silent 404.
 */
import { afterAll, afterEach, beforeAll } from "vitest";
import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";

export const handlers = [
  // 501 by default for any /api/* call a test forgot to mock.
  http.get("*/api/*", () =>
    HttpResponse.json({ detail: "not mocked" }, { status: 501 }),
  ),
  http.post("*/api/*", () =>
    HttpResponse.json({ detail: "not mocked" }, { status: 501 }),
  ),
];

export const server = setupServer(...handlers);

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});

afterEach(() => {
  server.resetHandlers();
});

afterAll(() => {
  server.close();
});