import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";

// The dev server is local and has no remote content. The Tauri
// production build serves the same assets from `dist` over the custom IPC
// protocol; no localhost listener exists in the packaged application.
export default defineConfig({
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/src-tauri/target", "**/src-tauri/target/**"] },
  },
  build: {
    target: "es2022",
    sourcemap: false,
    minify: "esbuild",
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        dreamingOrb: fileURLToPath(new URL("./dreaming-orb.html", import.meta.url)),
      },
    },
  },
});
