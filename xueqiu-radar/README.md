# 雪球雷达 · 线索台（xueqiu-radar）

接收本地 `xueqiu.exe` 定时上传的雪球评论线索，提供 Web 前台按标的聚合展示。

- **后端**：单个 Cloudflare Worker（接收上传 + 读取 API + 托管静态前台）
- **存储**：Cloudflare D1（Serverless SQLite）
- **前台**：Vue 3 + Vite 构建的 SPA，随 Worker 一起部署

---

## 一、本地开发与联调

```bash
cd xueqiu-radar
npm install
npm run dev          # 本地 Vite 预览前台（默认 http://localhost:5173）
```

本地联调 Worker（需先建本地 D1）：

```bash
wrangler d1 create xueqiu-radar-db        # 拿到 database_id，填进 wrangler.toml
npm run db:local                          # 建表（本地 .wrangler 目录）
wrangler dev                              # 本地跑 Worker + 前台（含 /api/*）
```

本地上传测试（替代 exe，验证 ingest）：

```bash
curl -X POST http://127.0.0.1:8787/api/ingest \
  -H "Authorization: Bearer <你的token>" \
  -H "Content-Type: application/json" \
  -d '{"round_id":"test","meta":{"source":"hashtag","title":"测试","generated_at":"2026-09-19 10:00:00"},"candidates":[{"id":"1","user_name":"张三","time_str":"09-19 10:00","like_count":10,"reply_count":2,"text":"澜起科技订单超预期","score":15,"tags":["stock:澜起科技"],"stocks":["澜起科技"]}],"comments":[{"id":"1","user_name":"张三","text":"澜起科技订单超预期","like_count":10,"reply_count":2,"time_str":"09-19 10:00"}]}'
```

---

## 二、部署到 Cloudflare

```bash
# 1) 建线上库
wrangler d1 create xueqiu-radar-db
#   拿到 database_id，填进 wrangler.toml（已填好占位值，替换即可）

# 2) 建表（按序执行 0001 + 0002 迁移；0002 负责加 ts 列与清理索引）
wrangler d1 migrations apply xueqiu-radar-db --remote
#   本地用：wrangler d1 migrations apply xueqiu-radar-db --local

# 3) 设置上传鉴权 token（务必用 secret，别写进仓库 / 别提交）
wrangler secret put INGEST_TOKEN
#   输入的内容要与 xueqiu.exe 的 --worker-token 完全一致

# 4) 构建前台并部署 Worker（含每日自动清理的 Cron Trigger）
npm run deploy
```

> 早期版本是 `wrangler d1 execute ... --file=./migrations/0001_init.sql` 单文件建表；
> 现在改用 `migrations apply` 会按序跑完所有迁移（含 0002）。已存在的库重复跑 0001（IF NOT EXISTS）是空操作，安全。

部署后会得到 `*.<你的子域>.workers.dev` 地址（大陆打不开，见第三节）。

---

## 三、大陆访问：自定义域名 + 优选 IP（重要）

`*.workers.dev` 在大陆已被墙，默认自定义域名也会绕道美国。推荐使用**自定义域名 + 灰云直连 CF 优选节点**：

1. 准备一个已托管到 Cloudflare 的域名（阿里云/腾讯云购买，NS 改到 Cloudflare）。
2. Cloudflare 控制台 → **Workers 路由**，为该域名添加路由 `你的域名/*` → 选择本 Worker。
3. 给该域名添加一条 **A 记录**（如 `xueqiu.你的域名.com`），指向一个**对大陆延迟最低的 Cloudflare 优选 IP**（用 [CloudflareSpeedTest](https://github.com/XIU2/CloudflareSpeedTest) 测速挑选）。
4. **关键**：这条 A 记录保持**灰云（DNS only，不代理）**，不要用橙云代理——否则又会绕道美国。
5. 用该自定义域名访问前台即可（如 `https://xueqiu.你的域名.com`）。

> 优选 IP 会随时间变化，若某天变慢可重新测速更换。若仍不满意，可再加一台日本/境外小 VPS 做 Nginx 反代中继（更稳，但需一台常驻服务器）。

---

## 四、配置 xueqiu.exe 定时上传

在 `xueqiu.py` 所在目录运行（或已打包的 `xueqiu.exe`）：

```bash
# 默认每轮自动上传（复用现有抓取周期）
python xueqiu.py --upload --worker-url https://xueqiu.你的域名.com/api/ingest --worker-token <你的token>

# 也支持环境变量（避免每次敲命令行）：
#   WORKER_URL=https://xueqiu.你的域名.com/api/ingest
#   WORKER_TOKEN=<你的token>
# 然后直接：python xueqiu.py --upload
```

- `--upload` 默认**关闭**，需显式开启（没部署 Worker 时空跑也不会报错，只是跳过上传）。
- 上传 payload：`{ round_id, meta, candidates, comments }`，其中：
  - `candidates` = Layer1 打分后的候选线索（前台**时间线**展示的就是它，按评论发布时间排序）
  - `comments`   = 本轮实际分析的原始评论（仅用于统计总数，不再单独落库；候选线索已写入 clues）
- 上传失败会被**静默吞掉并打印日志**，不影响本地抓取与落盘。
- 重新打包 EXE 后，EXE 同样支持 `--upload` 等参数。

> 前台展示策略（2026-09-19 调整）：以**评论内容为主**、**按时间线排序**（以评论真实发布时间为准，而非上传时间）、**不展示标的之间的关联关系**。每条评论独立成卡片（时间 / 作者 / 正文 / 赞·回·分 / 标的标签），按日期分段；支持按标的、分数阈值、关键词筛选，以及正序/倒序切换。前台采用**分页加载**（默认每页 50 条，点「加载更多」追加），避免一次性拉取全部。

---

## 五、API 与数据清理

### 读取接口（均支持分页）

- `GET /api/rounds?limit=50&offset=0`
  返回 `{ rounds, total, has_more }`，按 `created_at` 倒序。
- `GET /api/clues?limit=50&offset=0&order=desc&stock=&q=&min=`
  返回 `{ clues, total, has_more }`，按 `ts`（评论真实发布时间）排序。
  查询参数：
  - `round` ：只看某轮（与 stock/q/min 可叠加）
  - `stock` ：按标的名模糊匹配（如 `澜起科技`）
  - `q`     ：正文 / 作者关键词
  - `min`   ：分数下限（如 `min=7`）
  - `order` ：`desc`（默认，最新在前）/ `asc`
  - `limit` ：单页条数（1–200，默认 50）
  - `offset`：偏移，用于翻页

### 自动清理（保留 10 天）

- **每日 Cron 自动清理**：`wrangler.toml` 里配了 `crons = ["0 16 * * *"]`（UTC 16:00 = 北京时间 00:00），Worker 的 `scheduled` 每天删除 `ts`（评论时间）/`created_at`（轮次时间）早于 **10 天前** 的数据（`KEEP_DAYS = 10`，改这个值即可调整保留天数）。
  - 注：免费版 Cloudflare 也支持 Cron Trigger，但触发器数量有限；若免费版不可用，用下面的手动接口照样能清理。
- **手动触发清理**：`POST /api/cleanup`（需 `Authorization: Bearer <token>`），立即删除 10 天前数据，返回删除条数：
  ```bash
  curl -X POST https://xueqiu.你的域名.com/api/cleanup \
    -H "Authorization: Bearer <你的token>"
  # 返回示例：{"ok":true,"keep_days":10,"deleted":{"clues":12,"rounds":2}}
  ```
- 只删除有真实时间（`ts>0`/`created_at>0`）且早于 cutoff 的记录；无时间字段的遗留行不会被误删。

### Jev 价值打分（opt-in，默认关闭）

用 [TypeSafe Jev](https://docs.typesafe.ai)（System One 模型）对**规则高分候选**逐条打「A股投资参考价值」分（0~1），作为排序辅助信号。它**只出分数、不替你改写/生成内容**，与你"自己读评论"的初衷不冲突。

- **前置**：从 [console.typesafe.ai/keys](https://console.typesafe.ai/keys) 拿 API Key（目前 early access / waitlist）。价格约 `$0.042 / 1M input tokens`，历史全量回填约 `$0.002`，日常可忽略。
- **启用方式**（二选一）：
  ```bash
  # 1) 命令行（token 直接作为 --jev-token 参数传入；--jev-key 仍兼容）
  python xueqiu.py --upload --jev --jev-token <你的JevToken>
  # 或回填历史：
  python backfill_d1.py --worker-url https://xueqiu.cn24.org/api/ingest --worker-token <token> --jev --jev-token <你的JevToken>

  # 2) 环境变量（免每次传参）
  export JEV_API_KEY=<你的JevKey>     # 也可用 TYPESAFE_API_KEY
  python xueqiu.py --upload --jev
  ```
- **行为**：仅当 `--jev` 且能拿到 key 时才联网；否则完全离线。失败自动降级（`jev_value=0`，不中断抓取/上传）。
- **网络**：`api.typesafe.ai` 在大陆可能不稳/被墙，脚本已内置读 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量；需要时先 `export HTTPS_PROXY=...` 再跑。
- **结果落库**：`clues.jev_value`（REAL）。前台点「按价值 ↓」即按该分降序浏览；卡片显示「价值 N%」标签（仅已评估且分>0 时）。
- **调用范围**：只对在 `clue_extractor.extract_clues` 里**已过规则阈值**（≥ threshold）的候选打分，聚焦高价值、控量。

---

## 六、目录结构

```
xueqiu-radar/
├─ wrangler.toml          # Worker + D1 + 静态资源 + Cron 配置
├─ package.json
├─ vite.config.js
├─ index.html
├─ migrations/
│  ├─ 0001_init.sql         # D1 建表（rounds / clues）
│  ├─ 0002_ts_and_cleanup.sql  # 增量：加 ts 列 + 清理索引
│  ├─ 0003_drop_raw_comments.sql  # 删除只写不读的 raw_comments 表
│  └─ 0004_add_date.sql      # 增量：clues 加 date 列（真实发布日期，北京时间），前台显示今天/昨天/X小时前
│  └─ 0005_add_jev_value.sql # 增量：clues 加 jev_value 列（Jev 投资参考价值分，0~1，opt-in）
├─ worker/
│  └─ index.js             # Worker 后端（ingest / rounds / clues / cleanup / 静态托管）
└─ src/                    # Vue 前台
   ├─ main.js
   ├─ App.vue
   ├─ api.js
   └─ style.css
```
