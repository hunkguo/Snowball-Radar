# 雪球爬虫 (Xueqiu Spider)

> 自动抓取[雪球网](https://xueqiu.com)推荐和热门两个板块的帖子和评论，支持持续运行、SQLite 去重存储、定时 JSON 导出，可打包为独立 EXE。

## 功能特性

- **双板块抓取**：推荐 (fundx API)、热门 (hot API)
- **评论抓取**：自动抓取每篇帖子的评论，支持翻页
- **持续运行**：登录后自动循环抓取，每 30-45 分钟一轮，`Ctrl+C` 优雅退出
- **SQLite 存储**：所有数据存入本地数据库，按帖子/评论 ID 去重 (Upsert)
- **定时 JSON 导出**：每 6 小时增量导出（首次全量，后续只导出新数据），文件控制在 1MB 以内
- **反自动化检测**：stealth JS 隐藏 webdriver、模拟插件列表、模拟人类鼠标行为
- **模拟鼠标点击**：`_human_click()` 实现移动→停顿→点击，带随机偏移
- **慢速模式**：页间间隔 5-10 秒，评论间间隔 3-6 秒，降低被封风险
- **EXE 打包**：支持 PyInstaller 打包为独立可执行文件，自带自定义图标

## 项目结构

```
xueqiu-spider/
├── scraper.py          # 主爬虫脚本 (v7)
├── build_exe.py        # PyInstaller 打包脚本
├── requirements.txt    # Python 依赖
├── favicon.ico         # EXE 图标
├── favicon.png         # 图标源文件
├── .gitignore
├── README.md
└── data/               # 运行时自动生成（已 gitignore）
    ├── xueqiu.db                      # SQLite 数据库
    ├── chrome_profile/                # 持久化 Chrome Profile
    ├── exports/                       # JSON 导出目录
    │   └── xueqiu_export_YYYYMMDD_HHMMSS.json
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
- 自动导出最终 JSON 文件
- 关闭数据库连接
- 关闭浏览器

## 使用 EXE (无需 Python 环境)

### 打包

```bash
pip install pyinstaller
python build_exe.py
```

输出：`dist/xueqiu_scraper/xueqiu_scraper.exe`

### 运行

1. 将 `dist/xueqiu_scraper/` 整个文件夹复制到目标机器
2. 双击 `xueqiu_scraper.exe`
3. 首次运行在弹出的 Chrome 窗口中手动登录雪球
4. 程序自动持续抓取，`Ctrl+C` 退出

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
