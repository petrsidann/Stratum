import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base: "./" makes built asset URLs relative, so the site works at
// https://<user>.github.io/<repo>/ without extra configuration.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", sourcemap: false },
});
