import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// base 用相对路径，保证部署到 Worker 任意路径都能正确加载静态资源
export default defineConfig({
  plugins: [vue()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
