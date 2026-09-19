# 雪球爬虫 (Xueqiu Spider)

> 自动抓取[雪球网](https://xueqiu.com)推荐/热门板块与指定话题的帖子和评论，从中提取有价值的线索，输出**适合人工直接阅读的价值线索 md**（按标的分组、每条评论仅列一次、保留时间/作者/互动数据）。如需交给大模型深度分析，可加 `--prompt` 在文末附上提示词区块。支持持续运行、SQLite 去重存储，可打包为独立 EXE。

## 功能特性

- **双板块抓取**：推荐 (fundx API)、热门 (hot API)
- **评论抓取**：自动抓取每篇帖子的评论，支持翻页
- **持续运行**：登录后自动循环抓取，每 30-45 分钟一轮，`Ctrl+C` 优雅退出
- **SQLite 存储**：所有数据存入本地数据库，按帖子/评论 ID 去重 (Upsert)
- **定时任务**：每 6 小时整理一轮（原始 JSON 导出默认关闭，可用 `--json` 开启）
- **价值线索提取（Layer 1）**：每 6 小时自动扫描全部评论，规则打分剔除灌水，按标的分组提取有价值线索（小道消息/产业链/业绩订单/多空方向），生成**适合人工直接阅读的 `clues_*.md`**（每条评论仅列一次，保留时间/作者/点赞/回复数；`--prompt` 可附 AI 提示词）
- **反自动化检测**：stealth JS 隐藏 webdriver、模拟插件列表、模拟人类鼠标行为
- **模拟鼠标点击**：`_human_click()` 实现移动→停顿→点击，带随机偏移
- **慢速模式**：页间间隔 5-10 秒，评论间间隔 3-6 秒，降低被封风险
- **EXE 打包**：支持 PyInstaller 打包为独立可执行文件，自带自定义图标
- **话题评论抓取**：`hashtag_comments.py` 针对指定雪球话题页，提取帖子并抓取全部评论（小道消息/有价值信息主要集中在此）
- **持续运行 (话题版)**：每 45-60 分钟随机抓取一轮，每轮生成独立 md 线索文件（仅新评论，不重复），`Ctrl+C` 退出
- **独立增量文件**：每轮导出带时间戳的 md，数据库按评论 ID 去重，内容不重复
- **默认只产 md**：每轮只输出「整理内容（按标的分组、每条评论仅列一次、保留时间/作者）」的 `.md`，不落地 JSON；需要结构化数据或 AI 提示词时分别加 `--json` / `--prompt`

## 项目结构

```
xueqiu-spider/
├── xueqiu.py           # ★ 统一入口：参数驱动，整合下面两个抓取引擎
├── scraper.py          # 引擎A：推荐/热门 板块帖子+评论抓取 (v7) + Layer 1 价值线索提取
├── hashtag_comments.py # 引擎B：话题页评论抓取器（小道消息主力来源）
├── clue_extractor.py   # 通用价值线索打分 + 成品产出（两个引擎与统一入口共用，单一来源）
├── insight_extractor.py# 话题评论价值提取（复用 clue_extractor）
├── build_exe.py        # PyInstaller 打包脚本（打包统一入口 xueqiu.py）
├── requirements.txt    # Python 依赖
├── favicon.ico         # EXE 图标
├── favicon.png         # 图标源文件
├── .gitignore
├── README.md
└── data/               # 运行时自动生成（已 gitignore）
    ├── xueqiu.db                      # SQLite 数据库（推荐/热门）
    ├── hashtag_comments.db            # SQLite 数据库（话题）
    ├── seen_recommend_comments.json   # 增量分析：推荐/热门 已分析评论 id 记录
    ├── seen_hashtag_comments.json     # 增量分析：话题 已分析评论 id 记录
    ├── chrome_profile/                # 持久化 Chrome Profile
    ├── exports/                       # 导出目录（默认只产 md）
    │   ├── clues_YYYYMMDD_HHMMSS.md   # 价值线索（推荐/热门，按标的分组，每条评论仅列一次，保留时间/作者）
    │   ├── insight_<标识>_YYYYMMDD_HHMMSS.md   # 价值线索（话题，同上；`--prompt` 可附 AI 提示词区块）
    │   ├── xueqiu_export_YYYYMMDD_HHMMSS.json      # 原始抓取数据（--json 才产出）
    │   ├── hashtag_comments_<标识>_YYYYMMDD_HHMMSS.json  # 话题原始数据（--json 才产出）
    │   └── clues_/insight_ 同名 .json                # 结构化候选（--json 才产出）
    └── logs/                          # 运行日志
        └── scrape_YYYYMMDD_HHMMSS.log
```

## 环境要求

- **Python** 3.10+
- **Google Chrome** (系统安装版，爬虫使用 `channel="chrome"` 调用系统 Chrome)
- **操作系统**：Windows (主要测试)、macOS、Linux

## 快速开始

### 1. 安装依赖

```bash
# 克隆项目
git clone https://github.com/your-username/xueqiu-spider.git
cd xueqiu-spider

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器驱动
playwright install chromium
```

### 2. 运行爬虫

```bash
python scraper.py
```

首次运行时，程序会：
1. 复制系统 Chrome 的关键文件 (Cookies, Preferences 等) 到 `data/chrome_profile/`
2. 打开 Chrome 窗口并导航到雪球登录页
3. **等待用户手动登录**（最多 300 秒 / 5 分钟）
4. 登录成功后自动开始持续抓取

> 登录态保存在持久化 Profile 中，后续运行无需重复登录（cookie 有效期内）。

### 3. 退出程序

按 `Ctrl+C` 退出，程序会：
- 生成最终的价值线索 md
- 关闭数据库连接
- 关闭浏览器

## 统一入口 (xueqiu.py) ★

把「推荐/热门 板块」与「指定话题」两套抓取整合到**一个程序**，运行时用 `--mode` 区分，**默认两者都抓**。无论哪种模式，策略完全一致：抓到的评论都经过 Layer 1 规则打分、剔除灌水，输出**价值线索 md（按标的分组、每条评论仅列一次、保留时间/作者，适合人工直接阅读）**。

> 默认**只产出 `.md`**，不落地任何 JSON。需要结构化数据时加 `--json`；需要交给大模型深度分析时加 `--prompt`（在文末附上提示词区块）。

```bash
# 默认：推荐/热门 + 话题 都抓（持续运行）
python xueqiu.py

# 仅抓 推荐/热门
python xueqiu.py --mode recommend

# 仅抓 话题
python xueqiu.py --mode hashtag

# 两者都抓（每轮顺序执行：先推荐/热门，再话题）—— 与默认等价
python xueqiu.py --mode all

# 两者各抓一轮后立即退出（不进入持续循环）
python xueqiu.py --once

# 仅基于已抓评论提取价值线索（不启动浏览器/不抓取）——最快验证产出
python xueqiu.py --clues

# 额外导出结构化 JSON（默认只产 md）
python xueqiu.py --json

# 话题模式可临时覆盖目标话题
python xueqiu.py --mode hashtag --url "https://xueqiu.com/hashtag/..." --name "话题标题" --short "short_id"

# 调整抓取间隔（分钟）
python xueqiu.py --interval-min 30 --interval-max 45
```

参数一览：

| 参数 | 说明 |
|------|------|
| `--mode` | `recommend`(推荐/热门) / `hashtag`(话题) / `all`(两者都抓)，**默认 `all`** |
| `--once` | 只抓一轮即退出（不持续循环） |
| `--clues` | 仅提取价值线索，不抓取（基于已抓评论） |
| `--no-clues` | 跳过每轮的价值线索生成 |
| `--json` | 额外导出结构化 JSON（**默认关闭，只产出 md**） |
| `--prompt` | 在文末追加「发给大模型的提示词」区块（**默认关闭**，适合自己直接阅读） |
| `--headless` / `--no-headless` | 话题抓取是否无头（推荐模式始终需要可见窗口登录） |
| `--url` / `--name` / `--short` | 覆盖默认话题页 URL / 标题 / 文件名短标识 |
| `--interval-min` / `--interval-max` | 抓取间隔上下限（分钟） |
| `--full` | 全量分析（**默认增量**：只分析新出现的评论，分析过的不再重复） |
| `--reset-seen` | 清空「已分析评论」记录，下次运行重新全量分析一遍后再恢复增量 |

> `--mode all` 下两个引擎**顺序**执行（各自独立打开/关闭 Chrome，持久化 Profile 保存登录态），因此不会因共享同一用户目录而冲突。

### 增量分析（默认开启，解决「每小时观点雷同」）

默认即**增量分析**：每一轮只把「上次之后新出现的评论」送去做价值提取，已经分析输出过的评论（含昨天留下的、命中或未命中阈值的）会被记录在 `data/seen_recommend_comments.json` / `data/seen_hashtag_comments.json` 中，下轮直接跳过，**不再重复输出**。

- 第一次开启增量时，会把当前库内所有评论作为基线分析一遍并标记（仅此一次），之后每轮只出真正新增的评论。
- 想重新全量分析一次（例如改了打分口径后想刷新一遍）：加 `--reset-seen`（会先清空记录、本次强制全量，之后自动恢复增量）。
- 只想偶尔全量、平时保持增量：加 `--full`（仅当次全量，不影响记录）。
- 两套引擎（推荐/热门、话题）各有独立的「已分析」记录，互不干扰。

## 使用 EXE (无需 Python 环境)

`build_exe.py` 打包**统一入口**（`xueqiu.py`）：

### 打包

```bash
pip install pyinstaller
python build_exe.py
```

输出：`dist/xueqiu/xueqiu.exe`

### 运行

1. 将 `dist/xueqiu/` 整个文件夹复制到目标机器
2. 双击 `xueqiu.exe`（默认即抓 推荐/热门 + 话题；加 `--mode recommend|hashtag` 可只抓其一）
3. 程序持续抓取评论，每轮生成**价值线索 md**（含大模型提示词），`Ctrl+C` 退出
4. 结果保存在 EXE 同目录的 `data/`（数据库 + 每轮线索 md）

## 话题评论抓取 (hashtag_comments.py)

针对**单个雪球话题页**深度抓取评论（推荐/热门板块的帖子评论较水，而有价值的小道消息往往集中在特定话题的评论区）。

### 持续运行模式（默认）

程序启动后**持续运行**：每 45-60 分钟（随机）抓取一轮，每轮生成独立的线索 md，数据库按评论 ID 去重，内容不重复。`Ctrl+C` 优雅退出。

```bash
python hashtag_comments.py
```

行为：
- 打开配置的话题页（默认 `#沃什：加息25基点至4%，通胀难降但就业不伤#`）
- 滚动加载帖子，提取所有 `article.timeline__item` 中的帖子 ID
- 逐条调用 `/statuses/comments.json` 抓取评论（支持翻页，最多 15 页/帖）
- SQLite 去重存储到 `data/hashtag_comments.db`
- **每轮生成 Layer1 价值线索** `data/exports/insight_<标识>_<时间戳>.md`（默认只产 md）
- 原始评论 JSON `hashtag_comments_<标识>_<时间戳>.json` 默认**不产出**（将 `EXPORT_JSON` 改为 `True` 开启）
- 每轮结束打印**下次执行时间**，等待 45-60 分钟后自动开始下一轮

### 单次模式

如需只抓一次就退出，将文件顶部 `CONTINUOUS = True` 改为 `False`（或直接用统一入口 `python xueqiu.py --mode hashtag --once`）。

> 话题页与评论 API 均为公开接口，默认无头模式即可抓取，无需登录。
> 打包为独立 EXE 见上文「[使用 EXE](#使用-exe-无需-python-环境)」一节（`build_exe.py` 打包的是统一入口 `xueqiu.py`）。

修改 `hashtag_comments.py` 顶部的 `HASHTAG_URL` / `HASHTAG_NAME` / `HASHTAG_SHORT` 即可抓取其他话题。

## 价值提取 (insight_extractor.py, Layer 1)

从 `data/hashtag_comments.db` 中**筛选有投资参考价值的评论**，整理成可直接发给大模型分析的文档。

```bash
python insight_extractor.py
```

工作流程：
1. 读取已抓取的评论，逐条**规则打分**（股票代码 +3、已知标的名 +2/个、方向/事件关键词加权、信息密度、点赞加权、互动回复数加权）
2. **剔除灌水**（顶/沙发/666/纯表情/纯重复字符等）
3. 按分数阈值（默认 ≥5）筛选候选池，按**标的**分组（每条评论仅归入首个标的组、跨标的以行首标签标注，避免重复罗列）
4. 生成产出：
   - `data/exports/insight_<标识>_<时间戳>.md` —— **按标的分组、每条评论仅列一次、保留时间/作者/点赞/回复数**，适合人工直接阅读（默认产出）
   - `data/exports/insight_<标识>_<时间戳>.json` —— 结构化候选数据（需 `main(write_json=True)` 或 `--json`）

> 默认输出面向人工阅读，不含 AI 提示词。若想交给大模型做深度分析，加 `--prompt`（统一入口）或在调用 `main()` / `generate_clue_files()` 时传 `include_prompt=True`，会在文末附上「发给大模型的提示词」区块（内置话题背景与任务指令，输出要求为「直接输出 markdown 日报正文，不要返回 JSON」）。

## 价值线索提取（scraper.py 集成, Layer 1）

主爬虫 `scraper.py` 现已**内置**同一套 Layer 1 规则打分（与话题版共用 `clue_extractor.py`），无需再单独跑脚本：每 6 小时 JSON 导出时会**自动扫描全部已抓评论**，筛选出有价值线索并保存。

产出（位于 `data/exports/`）：

- `clues_<时间戳>.md` —— 按标的分组的候选评论（每条仅列一次，保留时间/作者/点赞/回复数，适合人工阅读；`--prompt` 可附 AI 提示词）
- `clues_<时间戳>.json` —— 结构化候选（分数、标签、命中标的；需 `EXPORT_JSON=True` 或 `--json`）

### 按需单独提取（不启动浏览器/不抓取）

如果只想基于**已抓取**的评论立即生成价值线索（例如刚跑完一轮后想马上看结果）：

```bash
python scraper.py --clues
```

### 跳过生成

持续运行时若想关闭线索文件生成：

```bash
python scraper.py --no-clues
```

### 打分口径（clue_extractor.py，单一来源）

| 信号 | 加分 | 说明 |
|------|------|------|
| 命中 6 位股票代码 | +3 | 如 `688008` |
| 命中已知标的名 | +2/个 | 澜起科技、英特尔、中芯国际、寒武纪、宁德时代…（可在 `clue_extractor.py` 顶部 `STOCK_NAMES` 扩充） |
| 方向/事件/传闻关键词 | 加权 | 利好/利空 +3，合作/建厂/订单/中标/据传/内部 +2，业绩/良率/回购 +1… |
| 信息密度 | +1~2 | 文本 ≥30 字 +1；含数字或 `%` +1 |
| 社区认可 | +1~2 | 点赞 ≥10 +1；≥50 +2 |
| 互动讨论 | +2~3 | 该评论收到的回复数 `reply_count` ≥1 +2；≥5 +3（代表引发了讨论，是社区二次认可信号，与点赞互补） |

阈值 `≥5` 进入候选池；同时 `is_meaningless()` 会剔除灌水（顶/沙发/666/纯表情/纯重复字符等）。互动维度只作**加分项**——纯灌水即使回复数很高也会被 `is_meaningless()` 先行过滤，不会借互动翻盘。

> 评论正文在入库前已用 `norm_comment()` 优化：去 HTML、剥离「回复 @某人：」前缀、压缩空白，使正文更聚焦实质内容。

### 配置参数（scraper.py 顶部）

```python
GEN_CLUES = True           # 每轮同时生成价值线索文件
CLUE_THRESHOLD = 5         # 进入候选池的最低分
CLUE_MAX_CANDIDATES = 80   # 发给大模型的候选上限（按分数截取）
EXPORT_JSON = False        # 是否导出原始/结构化 JSON（默认 False，只产出 md）
```

> 价值线索的成品文件（默认只有适合人工阅读的 `.md`）由 `clue_extractor.generate_clue_files()` **统一产出**，三个入口（`scraper.py`、`insight_extractor.py`、`xueqiu.py`）都走同一函数，保证格式与口径完全一致；传 `write_json=True`（或统一入口 `--json`）才会额外写 JSON，传 `include_prompt=True`（或统一入口 `--prompt`）才会在文末附上 AI 提示词区块。

## 数据说明

### SQLite 数据库 (`data/xueqiu.db`)

三张表，按 ID 去重 (INSERT ... ON CONFLICT DO UPDATE)：

| 表名 | 说明 | 主要字段 |
|------|------|----------|
| `posts` | 帖子 | id, section, title, description, text, created_at, like_count, reply_count, user_id, user_screen_name, ... |
| `comments` | 评论 | id, post_id, text, created_at, like_count, user_id, user_screen_name, ... |
| `scrape_runs` | 运行记录 | run_id, start_time, end_time, recommend_count, hot_count, comment_count, new_posts, new_comments |

### JSON 导出 (`data/exports/xueqiu_export_YYYYMMDD_HHMMSS.json`)

每 6 小时自动导出。首次导出为全量，后续为增量（只导出上次导出后新增的帖子和评论）：

```json
{
  "export_time": "2026-08-04 15:00:00",
  "platform": "xueqiu",
  "export_type": "incremental",
  "since": "2026-08-04T09:00:00",
  "exported_posts": 12,
  "exported_comments": 45,
  "db_total_posts": 156,
  "db_total_comments": 810,
  "sections": {
    "recommend": [ { "id": "...", "title": "...", "comments": [...] } ],
    "hot": [ ... ]
  },
  "recent_runs": [ ... ]
}
```

### 运行日志 (`data/logs/scrape_YYYYMMDD_HHMMSS.json`)

每次运行生成一个日志文件，记录抓取过程、API 调用、统计信息等。

## API 端点

| 板块 | API | 鉴权 | 说明 |
|------|-----|------|------|
| 推荐 | `/statuses/fundx/public/list.json?source=fund_public&page=N` | 公开 | 每页 10 条，支持翻页 |
| 热门 | `/statuses/hot/listV2.json?since=-1&max_id=-1&size=15` | 公开 | 一次性返回约 15 条 |
| 评论 | `/statuses/comments.json?id=<post_id>&page=N&count=20` | 公开 | 每页 20 条，支持翻页 |

API 调用方式：通过 `page.evaluate()` 在浏览器上下文中执行 `fetch()`，携带浏览器 cookie。

## 配置参数

在 `scraper.py` 中可调整：

```python
# 抓取间隔
SCRAPE_INTERVAL_MIN = 30 * 60   # 最小间隔 30 分钟
SCRAPE_INTERVAL_MAX = 45 * 60   # 最大间隔 45 分钟
JSON_EXPORT_INTERVAL = 6 * 3600 # JSON 导出间隔 6 小时

# 抓取参数
XueqiuScraper(
    max_pages=3,           # 推荐 API 翻页数
    max_comment_pages=2,   # 每篇帖子评论翻页数
    max_comment_posts=15,  # 每轮抓取评论的帖子数
    login_wait=300,        # 登录等待时间（秒）
)
```

## 反检测策略

| 策略 | 实现 |
|------|------|
| 隐藏 webdriver | `navigator.webdriver` → `undefined` |
| 模拟插件 | 注入 5 个 PDF Viewer 插件 |
| 模拟语言 | `navigator.languages` → `['zh-CN', 'zh', 'en-US', 'en']` |
| 权限查询 | 拦截 `permissions.query` 返回真实 Notification 状态 |
| 鼠标模拟 | `_human_click()`: 移动→停顿→点击，随机偏移 ±5px |
| 浏览模拟 | `_simulate_browsing()`: 随机滚动、鼠标移动、偶尔回滚 |
| 慢速请求 | 页间 5-10s，评论间 3-6s，板块间 5-10s |
| 启动参数 | `--disable-blink-features=AutomationControlled` |

## 持久化登录方案

Chrome 127+ 引入了 App-Bound Encryption (v20 cookie)，导致复制的 Profile 中 cookie 无法被解密。本项目采用以下方案：

1. **首次运行**：复制系统 Chrome 的关键文件到 `data/chrome_profile/`
2. **手动登录**：在 Playwright 打开的 Chrome 窗口中手动登录雪球
3. **持久化保存**：登录态写入 `data/chrome_profile/`，后续运行直接复用
4. **Cookie 路径检测**：兼容 Chrome 115+ 的 `Default/Network/Cookies` 路径

> 推荐和热门板块使用公开 API，不需要登录。

## 已知限制

- **Chrome v20 加密**：无法通过复制 cookie 绕过登录，必须手动登录首次
- **推荐 API**：每页 10 条，翻页深度有限
- **热门 API**：一次性返回约 15 条，无翻页
- **反爬风险**：尽管采用了多种反检测策略，高频请求仍可能触发风控

## 技术栈

- **Python 3.10+**
- **Playwright** — 浏览器自动化 (使用系统 Chrome)
- **SQLite** — 内置数据库，无需额外安装
- **PyInstaller** — EXE 打包

## 免责声明

本项目仅供学习和研究使用。使用本项目抓取的数据时请遵守雪球网的相关服务条款。使用者需自行承担因使用本工具而产生的一切法律责任。

## License

MIT
