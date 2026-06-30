import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

const apiPort = process.env.ORCHO_DEMO_PORT || "8000";
const webPort = Number(process.env.ORCHO_DEMO_WEB_PORT || "5173");

export default defineConfig({
  plugins: [vue()],
  server: {
    port: webPort,
    strictPort: true,
    proxy: {
      "/api": `http://127.0.0.1:${apiPort}`
    }
  }
});
