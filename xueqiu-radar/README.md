# 雪球雷达 · 线索台（xueqiu-radar）

接收油猴脚本定时上传的雪球评论线索，提供 Web 前台按标的聚合展示。

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

本地上传测试（验证 ingest）：

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
#   交互式输入：自己定一串足够长的随机串，记住它 —— 油猴脚本里要填一样的
#   生成随机 token：openssl rand -base64 32
#   查看已设的 secret 名字（看不到值）：wrangler secret list
#
# ⚠️ secret 存进去后**读不出来**。忘了就重新 put 一个新的：
#      wrangler secret put INGEST_TOKEN   （会覆盖旧的）
#      然后把新值同步到油猴脚本的「设置」里

# 4) 构建前台并部署 Worker（含每日自动清理的 Cron Trigger）
npm run deploy
```

> 早期版本是 `wrangler d1 execute ... --file=./migrations/0001_init.sql` 单文件建表；
> 现在改用 `migrations apply` 会按序跑完所有迁移（含 0009）。已存在的库重复跑 0001（IF NOT EXISTS）是空操作，安全。

部署后会得到 `*.<你的子域>.workers.dev` 地址（大陆打不开，见第三节）。

### 部署后自检

```bash
# 读接口无需鉴权，应返回线索数
curl "https://你的域名/api/clues?limit=1"

# 写接口无 token 应 401（有 token 才是 200）
curl -o /dev/null -w "%{http_code}\n" -X POST https://你的域名/api/exists \
  -H "Content-Type: application/json" -d '{"clue_ids":["1"]}'
```

两项都正常（返回数据 + 401）说明部署成功。然后去油猴脚本「设置」里填地址和 token，
点「测试连通性」确认。

---

## 二·五、去重与集中写入升级（2026-10-22）

### 问题

`clues` 表主键是 `(round_id, clue_id)`，**只能保证「轮内幂等」**。而 `round_id` 每轮都变——
雪球热门帖会在多轮抓取中反复出现，同一条评论（`clue_id` 相同）在不同 `round_id` 下会插入多行。

实测复现：

```
2 轮各含 c1 → clues 总行数 5，唯一 clue_id 4，冗余 1 行
```

后果：前台列表出现重复条目，且每轮重复消耗 D1 写入额度（免费版每日 10 万行写）。

### 三层去重

| 层 | 位置 | 作用 |
|---|---|---|
| ① 客户端 | `seenSet`（`GM_setValue` 持久化） | 已上传的 `clue_id` 不重传 |
| ② 客户端探针 | `POST /api/exists` | 上传前问服务端"哪些已在库"，只发新的 |
| ③ **服务端兜底** | `clues.clue_id` UNIQUE 索引 + UPSERT | **无论客户端怎么做，库里保证唯一** |

第 ③ 层是关键：即使客户端 `seenSet` 被清空、换设备、或多个浏览器并行运行，数据库也不会出现重复行。

### 重复线索的处理（不丢弃，走 UPSERT）

```sql
UPDATE clues SET
  last_seen_round = ?,
  hit_count = COALESCE(hit_count, 0) + 1,
  like_count  = MAX(COALESCE(like_count, 0),  ?),
  reply_count = MAX(COALESCE(reply_count, 0), ?)
WHERE clue_id = ?
```

- `round_id` 保留**首现轮次** → 前台「来源」标签不变
- 新增 `first_seen_round` / `last_seen_round` / `hit_count` 记录轨迹
- 点赞/回复数取 `MAX`（这类数本来就会增长）
- 前台线索卡片显示 `×N` 标记（`hit_count > 1`），直观看出"这条在热门帖里反复出现"

### 集中写入

**服务端**：一轮的所有 INSERT/UPDATE 合并成尽量少的 `DB.batch` 事务。

```
250 条候选 → 旧实现 251 次事务 → 新实现 3 次（减少 99%）
```

**客户端**（油猴脚本）：候选先进持久化缓冲，攒够 `batch_min` 条（默认 30）或超
`batch_interval` 分钟（默认 90）才发一次。缓冲存 `GM_setValue`，浏览器意外关闭不丢，
下次启动自动补传，页面关闭时用 `sendBeacon` 兜底。

### 部署步骤

```bash
cd xueqiu-radar
npm install
npm run db:remote     # ① 先跑迁移 0009（含历史重复行清理）
npm run deploy        # ② 再构建前台 + 部署 Worker
```

或直接双击 `migrations/部署去重升级.bat`（自动按顺序执行，迁移失败会中止不部署）。

> ⚠️ **顺序不能反**：Worker 新版依赖 `0009` 建出的 UNIQUE 索引。先部署 Worker 再迁移，
> 会因索引缺失而退化为"每条一个事务"。
>
> ⚠️ 迁移 `0009` 会删除历史重复行（保留最早出现的那条）——这是有意的清理，
> 不影响前台已展示的线索内容。

### 新增接口

| 接口 | 用途 | 鉴权 |
|---|---|---|
| `POST /api/exists` | 批量查询哪些 `clue_id` 已在库 | Bearer Token |
| `POST /api/hits` | 上报重复命中（只更新计数，不写正文行） | Bearer Token |

两个接口都要求 token 鉴权。

### 验证

```bash
node ../tools/test_worker_dedup.mjs      # Worker 单元（6 项）
node ../tools/test_e2e_dedup.mjs          # 端到端去重 + 集中写入
python ../tools/test_server_fallback.py   # 服务端兜底 + 鉴权
```

实测结果：

```
clues 表：总行 30，唯一 30  ✓ 无重复行
命中分布：1 次→10 条，2 次→5 条，3 次→15 条
行写入减少 54%    DB.batch 事务减少 99%
第 2 次上传同内容 → inserted=0, updated=10 ✓ 未新增行
```

---

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

## 四、配置油猴脚本上传

采集端已改为**跑在你自己浏览器里的油猴脚本**（早期版本是 Python + Playwright 打包的
`xueqiu.exe`，因自动化特征明显导致账号被风控封禁，已于 2026-10-22 移除）。

安装步骤：

1. Chrome 装 [Tampermonkey](https://www.tampermonkey.net/)
2. 新建脚本，粘贴 `userscript/xueqiu-radar-auto.user.js` 全文，保存
3. 打开任意雪球页面 → 左下角出现「雪球雷达 v3」面板
4. 点「设置」填入：
   - 上传地址：`https://radar.cn24.org/api/ingest`
   - 上传 Token：与 `wrangler secret put INGEST_TOKEN` 设的一致

配好后**无需任何点击**，脚本打开即后台运行（自动抓取、打分、攒批、上传）。
开机自启：把 `userscript/启动雪球雷达.bat` 的快捷方式放进 Windows 启动文件夹。

上传 payload：`{ round_id, meta, candidates, comments }`，其中：

- `candidates` = 打分后的候选线索（前台**时间线**展示的就是它，按评论发布时间排序）
- `comments`   = 本轮实际分析的原始评论（仅用于统计总数，不再单独落库；候选线索已写入 clues）
- `meta.total_candidates` = 本轮候选总数（含被去重跳过的），服务端据此记录去重统计

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

用 [TypeSafe Jev](https://docs.typesafe.ai)（System One 模型）对**规则高分候选**逐条打「A股投资参考价值」分（0~1），作为排序辅助信号。它**只出分数、不替你改写/生成内容**，与你「自己读评论」的初衷不冲突。

- **前置**：从 [console.typesafe.ai/keys](https://console.typesafe.ai/keys) 拿 API Key（目前 early access / waitlist）。价格约 `$0.042 / 1M input tokens`，历史全量回填约 `$0.002`，日常可忽略。
- **数据落库**：`clues.jev_value`（REAL）。前台点「按价值 ↓」即按该分降序浏览；卡片显示「价值 N%」标签（仅已评估且分>0 时）。
- **调用范围**：只对**已过规则阈值**（≥ threshold）的候选打分，聚焦高价值、控量。
- **当前状态**：油猴脚本暂未内置 Jev 调用，上传的候选 `jev_value` 默认为 0（前台不显示价值标签）。
  数据库列与前台的展示能力已就绪，后续接入只需在脚本里补一个打分请求并填进 payload 的 `jev_value` 字段。
- **网络注意**：`api.typesafe.ai` 在大陆可能不稳/被墙，若接入需处理代理。

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
│  ├─ 0004_add_date.sql      # 增量：clues 加 date 列（真实发布日期，北京时间），前台显示今天/昨天/X小时前
│  ├─ 0005_add_jev_value.sql # 增量：clues 加 jev_value 列（Jev 投资参考价值分，0~1，opt-in）
│  ├─ 0006_add_user_post_id.sql   # 增量：clues 加 user_id / post_id
│  ├─ 0007_add_post_author_id.sql # 增量：clues 加 post_author_id
│  ├─ 0008_add_post_id_index.sql  # 增量：post_id 建索引
│  └─ 0009_global_dedup.sql # 增量：clue_id 唯一索引 + hit_count 等追踪列（全局去重）
├─ worker/
│  └─ index.js             # Worker 后端（ingest / exists / hits / rounds / clues / cleanup / 静态托管）
└─ src/                    # Vue 前台
   ├─ main.js
   ├─ App.vue
   ├─ api.js
   └─ style.css
```
