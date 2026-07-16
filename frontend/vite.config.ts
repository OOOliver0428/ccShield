import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";
import path from "node:path";

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      resolvers: [ElementPlusResolver()],
    }),
    Components({
      resolvers: [ElementPlusResolver()],
    }),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    // Direct `vite` / `npm run dev` usage automatically tries 5174, 5175,
    // ... when the preferred port is already occupied. The cross-platform
    // launcher selects the same way and passes its result explicitly.
    strictPort: false,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // BridgeWS and BanlistWS use /api/ws/* during development. Vite's
        // HTTP proxy does not forward WebSocket upgrade requests
        // unless `ws: true` is set, so all danmaku flows were silently
        // dead. Verified via code inspection of the proxy config.
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/__tests__/setup.ts"],
    css: false,
    server: {
      deps: {
        inline: [/element-plus/],
      },
    },
  },
});
