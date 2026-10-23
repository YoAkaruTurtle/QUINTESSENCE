import { defineConfig } from "vite";

// The dev server is local, single-page and has no remote content.  The Tauri
// production build serves the same assets from `dist` over the custom IPC
// protocol; no localhost listener exists in the packaged application.
export default defineConfig({
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  build: {
    target: "es2022",
    sourcemap: false,
    minify: "esbuild",
  },
});
