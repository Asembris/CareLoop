/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// The dev proxy keeps the frontend same-origin against the FastAPI backend.
// Every request the real adapter makes is a relative /api path, so nothing in the
// app knows where the backend lives and backend CORS is never involved.
//
// The target is `CARELOOP_API_TARGET` rather than `VITE_API_BASE_URL`, and the
// distinction is load-bearing now that the adapter is real. Vite exposes every
// `VITE_`-prefixed variable to the *client*, where `services/api.ts` reads it as
// the base for every request — so setting it to point the proxy at a different
// port would also make the browser call that port directly, cross-origin, against
// a backend that mounts no CORS middleware. One variable moved the backend; the
// other moves the proxy. Only the second is what a developer on a busy port wants.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") }
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.CARELOOP_API_TARGET || "http://127.0.0.1:8000",
        changeOrigin: true
      }
    }
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true
  }
});
