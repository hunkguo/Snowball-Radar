# 雪球雷达 · Snowball-Radar

抓取雪球评论、提炼投资线索的自动化工具。

> **当前架构：油猴脚本（采集） + Cloudflare Worker（去重入库） + Vue 前台（展示）**
>
> 早期版本是 Python + Playwright 打包成 `xueqiu.exe` 在本地跑。该方案因自动化特征明显，
> 账号被雪球风控封禁，已于 2026-10-22 移除。现在采集环节改为**跑在你自己浏览器里的油猴脚本**——
> 零自动化指纹，登录态天然复用。

---

## 一、架构

```
┌──────────────────────────────────────────────────────────────┐
│  你的 Chrome（正常登录雪球）                                  │
│  └─ 油猴脚本 xueqiu-radar-auto.user.js                       │
│       · 后台定时抓取推荐/热门评论（低频，夜间停抓）           │
│       · 规则打分 → 攒批 → 上传                               │
└──────────────────────┬───────────────────────────────────────┘
                       │ HTTPS  POST /api/ingest
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Cloudflare Worker（xueqiu-radar）                            │
│  · 全局去重（clue_id 唯一索引 + UPSERT）                      │
│  · 集中写入（合并 DB.batch 事务）                            │
│  · /api/exists  去重探针                                     │
│  · /api/hits    命中上报                                     │
└──────────────────────┬───────────────────────────────────────┘
                       ▼
              Cloudflare D1（线索库）
                       ▲
                       │ GET /api/clues
┌──────────────────────────────────────────────────────────────┐
│  Vue 3 前台（radar.cn24.org）                                 │
│  按时间/标的/分数/板块筛选展示线索                            │
└──────────────────────────────────────────────────────────────┘
```

---

## 二、目录结构

```
Snowball-Radar/
├── 部署指南-去重升级.md          ← 升级步骤（必读）
│
├── xueqiu-radar/                  ← 后端 + 前台（Cloudflare）
│   ├── worker/index.js            Worker：去重入库 / 读取 API / 托管前台
│   ├── src/                       Vue 3 前台
│   │   ├── App.vue
│   │   ├── api.js
│   │   └── style.css
│   ├── migrations/                D1 数据库迁移
│   │   ├── 0001_init.sql … 0008_add_post_id_index.sql
│   │   ├── 0009_global_dedup.sql  ← 全局去重（唯一索引）
│   │   └── 部署去重升级.bat
│   ├── userscript/                油猴脚本
│   │   ├── xueqiu-radar-auto.user.js  ★ 无人值守版（主用，v3.2.1）
│   │   ├── 启动雪球雷达.bat           开机自启
│   │   └── README-无人值守版.md       完整文档
│   ├── wrangler.toml              Worker / D1 / 域名配置
│   └── package.json
│
└── tools/                         去重与集中写入的验证脚本
    ├── test_worker_dedup.mjs      Worker 单元测试
    ├── test_e2e_dedup.mjs          端到端去重
    ├── test_server_fallback.py    服务端兜底 + 鉴权
    └── mock_worker.py              本地 mock Worker
```

---

## 三、快速开始

### 1. 部署后端

```bash
cd xueqiu-radar
npx wrangler login                # 一次即可
npm install --legacy-peer-deps    # 必须带此参数，见部署指南
npm run db:remote                 # 应用 D1 迁移
npm run deploy                    # 构建前台 + 部署 Worker
```

详见 [`部署指南-去重升级.md`](部署指南-去重升级.md)。

### 2. 安装油猴脚本

1. Chrome 装 [Tampermonkey](https://www.tampermonkey.net/)
2. 新建脚本，粘贴 `xueqiu-radar/userscript/xueqiu-radar-auto.user.js` 全文，保存
3. 打开任意雪球页面 → 左下角出现「雪球雷达 v3」面板
4. 点「设置」，填入：
   - 上传地址：`https://radar.cn24.org/api/ingest`
   - 上传 Token：与 `wrangler secret put INGEST_TOKEN` 设的一致

配好即自动运行，**无需任何点击**。开机自启用 `userscript/启动雪球雷达.bat`。

### 3. 打开前台

访问 `https://radar.cn24.org` 查看线索。

---

## 四、核心设计

### 4.1 为什么用油猴脚本而不是 EXE

| | Playwright EXE（已移除） | 油猴脚本（当前） |
|---|---|---|
| 自动化指纹 | 需注入 stealth 伪装，仍有残留 | **零**——跑在真实浏览器里 |
| 登录态 | 需复制 Chrome Profile，受 App-Bound Encryption 限制 | **天然复用**当前浏览器登录态 |
| 封号风险 | 高（指纹 + 定时轮询 + 连续翻页特征） | 低（行为接近真人） |
| 维护成本 | 需跟随雪球前端改版 | 同左，但失败面小得多 |
| 过 WAF | 需对抗阿里云 WAF JS 挑战 | 浏览器天然执行 JS，天然通过 |

实测确认（2026-10-22）：雪球 Web 端用阿里云 WAF，裸 HTTP 请求必吃 110KB JS 挑战页，
**浏览器执行 JS 后自动获得 `acw_tc` 通行证**。因此任何 `python-requests` 方案都过不去，
真实浏览器方案则天然通过。

> Android 模拟器方案已评估并否决：雪球 App 端有五层反爬（设备指纹、SSL Pinning、
> AES+RSA 请求体加密、行为埋点反模拟、服务端实时风控），模拟器会命中「反模拟」层，
> 平台可能返回虚假数据且无法分辨。

### 4.2 保号约束（脚本内强制，UI 改不动）

```
每板块帖子 ≤ 10    每帖评论页 ≤ 3    轮次间隔 90–240 分钟随机
帖子间随机延迟 3.2–7.8s    翻页延迟 2.1–3.6s    夜间 23:00–07:00 零请求
连续 3 轮 0 评论 → 主动降频    命中 WAF → 退避 3–7 小时
账号受限 → 退避 12–24 小时并自动切换账号
```

### 4.3 三层去重

| 层 | 位置 | 作用 |
|---|---|---|
| ① 客户端 | `seenSet`（`GM_setValue` 持久化） | 已上传的 `clue_id` 不重传 |
| ② 客户端探针 | `POST /api/exists` | 上传前问服务端「哪些已在库」，只发新的 |
| ③ 服务端兜底 | `clue_id` UNIQUE 索引 + UPSERT | **无论客户端怎么做，库里保证唯一** |

第 ③ 层是关键。雪球热门帖会在多轮抓取中反复出现，而原表主键是 `(round_id, clue_id)`
——只保证「轮内幂等」，跨轮同一评论会插多行。

重复线索不丢弃，走 UPSERT 只更新计数：

- `round_id` 保留**首现轮次**（前台来源标签不变）
- 记录 `first_seen_round` / `last_seen_round` / `hit_count`
- 点赞/回复数取 `MAX`（这类数本来就会增长）
- 前台卡片显示 `×N` 标记，一眼看出「这条在热门帖里反复出现」

### 4.4 集中写入

- **服务端**：一轮所有 INSERT/UPDATE 合并成尽量少的 `DB.batch` 事务（250 条候选：251 次 → 3 次）
- **客户端**：候选先进持久化缓冲，攒够 30 条或超 90 分钟才发；缓冲持久化，崩溃不丢

---

## 五、实测要点（2026-10-22，Chrome 140 / 雪球现行前端）

写代码前请先了解这些，否则会踩坑：

1. **`xq_a_token` 是 HttpOnly**，`document.cookie` 读不到 → 判断登录态必须用接口探针：
   `GET /statuses/comments.json?id=<真实帖id>&page=1&count=1`
   游客返回 `error_code 10022`，登录返回 200 + comments 数组
2. **`/statuses/comments.json` 必须登录**（网上说「公开接口」是错的）
3. **`/v4/statuses/comments.json` 已 404**——网上流传的 v4 路径已失效
4. **前端上了 CSS Modules**，类名带随机哈希（`style_timeline__item__main_lHD`）
   → 固定选择器 `.timeline__item` 已失效，须用 `[class*="..."]` 子串匹配
5. **未登录访问 `/<post_id>` 会被重定向回首页**
6. **热门列表 `items[].id` 是序号**（如 973959），真实帖 id 在 `original_status.id`
7. **裸 curl 必吃 WAF 挑战页**，一个 cookie 都不下发 → python-requests 永远过不了

---

## 六、API

| 接口 | 方法 | 用途 | 鉴权 |
|---|---|---|---|
| `/api/ingest` | POST | 上传线索（去重入库） | Bearer Token |
| `/api/exists` | POST | 查哪些 clue_id 已在库 | Bearer Token |
| `/api/hits` | POST | 上报重复命中（只更新计数） | Bearer Token |
| `/api/clues` | GET | 读线索（分页 + 筛选 + 排序） | 无 |
| `/api/rounds` | GET | 读轮次列表 | 无 |
| `/api/cleanup` | POST | 手动清理 10 天前数据 | Bearer Token |

筛选参数：`stock` `q` `min` `order` `sort=jev` `section=recommend|hot|topic` `round` `limit` `offset`

---

## 七、验证

```bash
cd tools
node test_worker_dedup.mjs      # Worker 单元：跨轮去重 / 批内去重 / 幂等 / 批次切分
node test_e2e_dedup.mjs          # 端到端：探针 + 上报 + 集中写入
python test_server_fallback.py   # 服务端兜底 + 鉴权
```

实测结果：

```
clues 表：总行 30，唯一 30  ✓ 无重复行
命中分布：1 次→10 条，2 次→5 条，3 次→15 条
行写入减少 54%    DB.batch 事务减少 99%
第 2 次上传同内容 → inserted=0, updated=10 ✓ 未新增行
错误 token → 401 ✓
```

---

## 八、风险提示

- 雪球服务协议明确禁止爬虫与绕过安全机制。**低频、低量、自用**是相对安全的区间，但仍有封号可能。
- 脚本已把频率压到接近真人水平，但不能保证 100% 不被风控。
- 建议先用新号跑几天观察，确认稳定再上主力号。
- 命中 WAF / 限制时脚本会自动退避，**不要手动点「立即跑一轮」硬刚**。
- 多账号请用**不同的 Chrome 配置文件**（脚本里的「账号池」只是备注标识，不切换实际登录态）。

---

## 九、License

MIT
