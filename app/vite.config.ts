import { defineConfig } from "vite";

export default defineConfig({
  clearScreen: false,
  server: { port: 1420, strictPort: true },
  build: {
    target: "es2022",
    outDir: "dist",
    emptyOutDir: true,
    // Two pages: the JARVIS window and the Board window.
    rollupOptions: { input: { main: "index.html", board: "board.html" } },
  },
});
