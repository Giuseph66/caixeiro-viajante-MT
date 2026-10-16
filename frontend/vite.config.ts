import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const projectRoot = fileURLToPath(new URL("../", import.meta.url));

export default defineConfig(({ mode }) => {
  const { VITE_API_PROXY_TARGET } = loadEnv(mode, projectRoot, "VITE_");
  return {
    plugins: [react()],
    server: {
      proxy: {
        "/api": VITE_API_PROXY_TARGET || "http://127.0.0.1:8000",
      },
    },
  };
});
