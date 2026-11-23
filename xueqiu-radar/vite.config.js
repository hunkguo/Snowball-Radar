import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { execSync } from "node:child_process";

// 构建时把 git commit 与构建时间烧进前端，页面底部显示，
// 方便一眼判断线上是不是最新代码（每次部署 hash 都会变）。
function buildInfo() {
  let hash = "unknown";
  try {
    hash = execSync("git rev-parse --short HEAD", { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch { /* 非 git 环境降级为 unknown */ }
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const time = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return { hash, time };
}
const info = buildInfo();

// base 用相对路径，保证部署到 Worker 任意路径都能正确加载静态资源
export default defineConfig({
  plugins: [vue()],
  base: "./",
  define: {
    __BUILD_HASH__: JSON.stringify(info.hash),
    __BUILD_TIME__: JSON.stringify(info.time),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
