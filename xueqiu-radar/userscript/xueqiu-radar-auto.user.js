// ==UserScript==
// @name         雪球雷达 · 无人值守保号版 v3
// @namespace    https://snowball-radar.local/userscript
// @version      3.5.1
// @description  油猴全自动抓取雪球评论（推荐 / 热门 / 关注 三源）并上传 Worker。打开即后台常驻，无需点击。含实测校准的选择器、账号池轮换、WAF/封号自动退避、跨轮断点续跑。
// @author       hunk / WorkBuddy
// @match        https://xueqiu.com/*
// @match        https://*.xueqiu.com/*
// @connect      *
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_listValues
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @grant        GM_addStyle
// @grant        GM_setClipboard
// @run-at       document-idle
// @noframes
// ==/UserScript==

/* global GM_xmlhttpRequest, GM_setValue, GM_getValue, GM_deleteValue, GM_listValues, GM_registerMenuCommand, GM_notification, GM_addStyle, GM_setClipboard */

(function () {
  "use strict";
  if (window.top !== window.self) return;
  // 幂等保护：防止同一页面重复注入导致多实例并发抓取（会重复上传 + 加速风控）
  if (window.__XUEQIU_RADAR_V3__) {
    console.warn("[雪球雷达 v3] 脚本已在本页面运行，跳过重复注入");
    return;
  }
  window.__XUEQIU_RADAR_V3__ = true;

  // ══════════════════════════════════════════════════════════════
  //  实测结论（2026-10-22，Chrome 140 / 雪球现行前端）
  //  1. /statuses/comments.json  必须登录，游客返回 error_code 10022
  //  2. /v4/statuses/comments.json 已 404，不要用网上流传的 v4 路径
  //  3. 前端已上 CSS Modules，类名带随机哈希后缀（style_timeline__item__main_lHD）
  //     → 必须用 [class*="..."] 子串匹配，实测有效
  //  4. 未登录访问 /<post_id> 会被重定向回首页
  //  5. 热门列表 items[].id 是序号（973959），真实帖子 id 在 original_status.id
  // ══════════════════════════════════════════════════════════════

  const NS = "xr3_";
  const DEFAULTS = {
    url: "", token: "", threshold: 5,
    postsPerRound: 6, commentPages: 2,   // postsPerRound 仅用于旧配置迁移（=每板块帖数）
    totalPostsPerRound: 12,              // 每轮总帖数预算，按启用的板块均分（新增板块不增加请求量）
    minIntervalMin: 150, maxIntervalMin: 240,
    sections: "recommend,hot,follow",   // 2026-10-23 默认启用「关注」；取不到会自动跳过
    autoStart: true, notify: true,
    batch_min: 30, batch_interval: 90,   // 攒批阈值与最长等待
    // 夜间静默时段（24 小时制整点）。开始 = 结束 表示**全天运行、不静默**。
    nightStart: 23, nightEnd: 7,
    // 无人值守保活（锁屏 / 远程桌面 / 标签页不在前台时仍要跑）
    keepAwake: true,     // 播放极低音量音频，让 Chrome 判定「正在播放音频」→ 豁免冻结
    autoLead: true,      // 主控标签若长时间无心跳，本页自动接管（原来只能刷新页面才重试）
  };
  const LIMIT = {
    MAX_POSTS: 10, MAX_PAGES: 3, MAX_TOTAL_POSTS: 30, MAX_CANDS: 200,
    // 2026-10-23 由 90 下调至 30（用户要求允许更频繁）。
    // 注意：<90 分钟会明显增加风控/封号概率，保存时会给出警告；这里仍留 30 作为防呆下限。
    MIN_INTERVAL: 30,
    // 静默时段的出厂默认（用户可在「设置」里改，存进 CFG.nightStart / CFG.nightEnd）
    NIGHT_START: 23, NIGHT_END: 7,
    POST_DELAY: [3200, 7800],
    PAGE_DELAY: [2100, 3600],
    SECTION_DELAY: [4000, 8000],
    WAF_BACKOFF: [180, 420],
    BLOCKED_BACKOFF: [720, 1440],
    MAX_EMPTY: 3,
    SESSION_TTL_MIN: 45,
    HEARTBEAT: 60,
    // 主控锁：原来 TTL 只有 120s，而后台标签页的定时器会被 Chrome 节流到 ≥60s/次，
    // 一旦再被拖慢就会「误判主控已死」→ 本页永久停在观察模式。TTL 必须远大于节流后的心跳间隔。
    LEADER_TTL: 6 * 60000,   // 6 分钟无心跳才允许别的标签接管
    LEADER_BEAT: 60000,      // 心跳写入周期
    WATCHDOG: 20000,         // 补跑轮询：长定时器被推迟时靠它兜底
    THROTTLE_ALERT: 3 * 60000, // 心跳间隔超过这个值 → 判定被节流/冻结并告警
  };
  const KEYS = {
    cfg: NS + "cfg", seen: NS + "seen", stat: NS + "stat",
    accounts: NS + "accounts", idx: NS + "acct_idx", nextRun: NS + "next_run",
    pending: NS + "pending", lastFlush: NS + "last_flush",
    migFollow: NS + "mig_follow_v1", maxPage: NS + "maxpage",
  };

  // 整点小时（0~23）归一化：非法值回落到默认
  const normHour = (v, def) => {
    const n = parseInt(v, 10);
    return (Number.isFinite(n) && n >= 0 && n <= 23) ? n : def;
  };

  function loadCfg() {
    const raw = GM_getValue(KEYS.cfg, {}) || {};
    const c = Object.assign({}, DEFAULTS, raw);
    c.postsPerRound = Math.min(LIMIT.MAX_POSTS, Math.max(1, +c.postsPerRound || 6));
    c.commentPages = Math.min(LIMIT.MAX_PAGES, Math.max(1, +c.commentPages || 2));
    // 总帖数预算：旧配置只有「每板块帖数」，换算成总量，保证升级后实际抓取量不变。
    // 注意：这里必须用**旧的**板块数算（下面会补 follow），否则总量会被放大 50%。
    if (raw.totalPostsPerRound == null && raw.postsPerRound != null) {
      const n = String(raw.sections || "recommend,hot")
        .split(",").map((s) => s.trim()).filter(Boolean).length || 2;
      c.totalPostsPerRound = Math.max(1, (+raw.postsPerRound || 6) * n);
    }
    c.totalPostsPerRound = Math.min(LIMIT.MAX_TOTAL_POSTS,
      Math.max(1, +c.totalPostsPerRound || 12));
    // 静默时段：老配置没有这两个字段 → 走出厂默认 23:00-07:00；
    // 非法值（空/非数字/越界）一律回落默认，避免「undefined 点」静默判断失效。
    c.nightStart = normHour(c.nightStart, LIMIT.NIGHT_START);
    c.nightEnd = normHour(c.nightEnd, LIMIT.NIGHT_END);
    // 一次性迁移：把「关注」补进板块（只补一次；之后用户手动删掉不会再被加回来）
    if (!GM_getValue(KEYS.migFollow, false)) {
      const arr = String(c.sections || DEFAULTS.sections)
        .split(",").map((s) => s.trim()).filter(Boolean);
      if (!arr.includes("follow")) {
        arr.push("follow");
        c.sections = arr.join(",");
        GM_setValue(KEYS.cfg, c);   // 持久化，否则下次加载又回到旧板块
      }
      GM_setValue(KEYS.migFollow, true);
    }
    c.minIntervalMin = Math.max(LIMIT.MIN_INTERVAL, +c.minIntervalMin || 150);
    c.maxIntervalMin = Math.max(c.minIntervalMin, +c.maxIntervalMin || 240);
    return c;
  }
  let CFG = loadCfg();

  // ══════════════════════════════════════════════════════════════
  //  工具
  // ══════════════════════════════════════════════════════════════
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rnd = (a, b) => a + Math.random() * (b - a);
  const pad2 = (n) => String(n).padStart(2, "0");
  const now = () => Date.now();
  const sleepUntil = (ts) => sleep(Math.max(0, ts - now()));

  function epochToBeijing(sec, withSec) {
    if (!sec || sec <= 0) return "";
    if (sec > 1e12) sec /= 1000;
    if (sec < 1.48e9) return "";
    const d = new Date(sec * 1000 + 8 * 3600 * 1000), p = pad2;
    const b = `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
    return withSec ? b + ":" + p(d.getUTCSeconds()) : b;
  }
  // 静默时段由用户在「设置」里改（CFG.nightStart / CFG.nightEnd，整点）。
  // 三种形态都要支持：
  //   跨天     23→7    h>=23 或 h<7
  //   不跨天    9→17   h>=9 且 h<17
  //   开始=结束        → 视为「全天运行」，不做任何静默（否则 h>=start 恒真会永久停抓）
  function nightRange() {
    const s = normHour(CFG && CFG.nightStart, LIMIT.NIGHT_START);
    const e = normHour(CFG && CFG.nightEnd, LIMIT.NIGHT_END);
    return { s, e, on: s !== e };
  }
  function isNight() {
    const { s, e, on } = nightRange();
    if (!on) return false;
    const h = new Date().getHours();
    return s < e ? (h >= s && h < e) : (h >= s || h < e);
  }
  // 距离静默结束还有多久：走到结束整点即可（跨天/不跨天通用）
  function msToNightEnd() {
    const { e } = nightRange();
    const h = new Date().getHours();
    let d = e - h;
    if (d <= 0) d += 24;
    return d * 3600000;
  }
  const nightLabel = () => {
    const { s, e, on } = nightRange();
    return on ? `${s}:00-${e}:00` : "全天运行（已关闭静默）";
  };
  function fmtDur(ms) {
    if (ms <= 0) return "即将";
    const m = Math.round(ms / 60000);
    if (m < 60) return m + " 分钟";
    return Math.floor(m / 60) + " 小时 " + (m % 60) + " 分";
  }
  const clip = (s, n) => (String(s || "").length > n ? String(s).slice(0, n) + "…" : String(s || ""));
  const ENT = { "&amp;":"&","&lt;":"<","&gt;":">","&quot;":'"',"&apos;":"'","&nbsp;":" ","&#39;":"'","&hellip;":"…" };
  function cleanText(t) {
    if (!t) return "";
    return String(t).replace(/<[^>]*>/g," ").replace(/&[a-z]+;|&#\d+;/gi,(m)=>ENT[m.toLowerCase()]||m).replace(/\s+/g," ").trim();
  }

  // ══════════════════════════════════════════════════════════════
  //  价值打分（与 Worker 端规则保持一致）
  // ══════════════════════════════════════════════════════════════
  const STOCK_NAMES = {
    "澜起科技":"澜起科技","澜起":"澜起科技","英特尔":"英特尔","intel":"英特尔","Intel":"英特尔",
    "海力士":"SK海力士","SK海力士":"SK海力士","中芯国际":"中芯国际","中芯":"中芯国际",
    "韦尔股份":"韦尔股份","韦尔":"韦尔股份","兆易创新":"兆易创新","兆易":"兆易创新",
    "北方华创":"北方华创","卓胜微":"卓胜微","圣邦股份":"圣邦股份","圣邦":"圣邦股份",
    "长电科技":"长电科技","长电":"长电科技","通富微电":"通富微电","寒武纪":"寒武纪",
    "沪硅产业":"沪硅产业","中微公司":"中微公司","中微":"中微公司","拓荆科技":"拓荆科技",
    "芯源微":"芯源微","盛美上海":"盛美上海","华海清科":"华海清科","长川科技":"长川科技",
    "安集科技":"安集科技","鼎龙股份":"鼎龙股份","江丰电子":"江丰电子","华峰测控":"华峰测控",
    "江波龙":"江波龙","佰维存储":"佰维存储","德明利":"德明利","香农芯创":"香农芯创",
    "雅克科技":"雅克科技","工业富联":"工业富联","中际旭创":"中际旭创","旭创":"中际旭创",
    "新易盛":"新易盛","天孚通信":"天孚通信","光迅科技":"光迅科技","源杰科技":"源杰科技",
    "仕佳光子":"仕佳光子","剑桥科技":"剑桥科技","太辰光":"太辰光","联特科技":"联特科技",
    "华工科技":"华工科技","光库科技":"光库科技","腾景科技":"腾景科技","立讯精密":"立讯精密",
    "立讯":"立讯精密","歌尔股份":"歌尔股份","歌尔":"歌尔股份","蓝思科技":"蓝思科技",
    "传音控股":"传音控股","安克创新":"安克创新","领益智造":"领益智造","东山精密":"东山精密",
    "鹏鼎控股":"鹏鼎控股","京东方":"京东方A","TCL科技":"TCL科技","深天马":"深天马A",
    "金山办公":"金山办公","科大讯飞":"科大讯飞","讯飞":"科大讯飞","海康威视":"海康威视",
    "海康":"海康威视","大华股份":"大华股份","中科曙光":"中科曙光","曙光":"中科曙光",
    "浪潮信息":"浪潮信息","紫光股份":"紫光股份","神州数码":"神州数码","深信服":"深信服",
    "中兴通讯":"中兴通讯","紫光国微":"紫光国微","复旦微电":"复旦微电","中航沈飞":"中航沈飞",
    "航发动力":"航发动力","中国船舶":"中国船舶","中航西飞":"中航西飞","菲利华":"菲利华",
    "西部超导":"西部超导","比亚迪":"比亚迪","宁德时代":"宁德时代","亿纬锂能":"亿纬锂能",
    "国轩高科":"国轩高科","欣旺达":"欣旺达","恩捷股份":"恩捷股份","天赐材料":"天赐材料",
    "璞泰来":"璞泰来","容百科技":"容百科技","当升科技":"当升科技","中伟股份":"中伟股份",
    "格林美":"格林美","华友钴业":"华友钴业","天齐锂业":"天齐锂业","赣锋锂业":"赣锋锂业",
    "盐湖股份":"盐湖股份","永兴材料":"永兴材料","长城汽车":"长城汽车","长安汽车":"长安汽车",
    "上汽集团":"上汽集团","广汽集团":"广汽集团","赛力斯":"赛力斯","北汽蓝谷":"北汽蓝谷",
    "江淮汽车":"江淮汽车","宇通客车":"宇通客车","拓普集团":"拓普集团","三花智控":"三花智控",
    "伯特利":"伯特利","保隆科技":"保隆科技","双环传动":"双环传动","隆基绿能":"隆基绿能",
    "隆基":"隆基绿能","通威股份":"通威股份","特变电工":"特变电工","晶澳科技":"晶澳科技",
    "天合光能":"天合光能","晶科能源":"晶科能源","钧达股份":"钧达股份","爱旭股份":"爱旭股份",
    "福斯特":"福斯特","福莱特":"福莱特","捷佳伟创":"捷佳伟创","迈为股份":"迈为股份",
    "金风科技":"金风科技","明阳智能":"明阳智能","天顺风能":"天顺风能","阳光电源":"阳光电源",
    "德业股份":"德业股份","固德威":"固德威","锦浪科技":"锦浪科技","思源电气":"思源电气",
    "国电南瑞":"国电南瑞","许继电气":"许继电气","贵州茅台":"贵州茅台","茅台":"贵州茅台",
    "五粮液":"五粮液","泸州老窖":"泸州老窖","老窖":"泸州老窖","山西汾酒":"山西汾酒",
    "汾酒":"山西汾酒","洋河股份":"洋河股份","洋河":"洋河股份","今世缘":"今世缘",
    "古井贡酒":"古井贡酒","迎驾贡酒":"迎驾贡酒","口子窖":"口子窖","舍得酒业":"舍得酒业",
    "酒鬼酒":"酒鬼酒","水井坊":"水井坊","古越龙山":"古越龙山","皇台酒业":"皇台酒业",
    "伊利股份":"伊利股份","海天味业":"海天味业","千禾味业":"千禾味业","涪陵榨菜":"涪陵榨菜",
    "双汇发展":"双汇发展","安琪酵母":"安琪酵母","金龙鱼":"金龙鱼","恒瑞医药":"恒瑞医药",
    "百济神州":"百济神州","药明康德":"药明康德","药明":"药明康德","凯莱英":"凯莱英",
    "泰格医药":"泰格医药","昭衍新药":"昭衍新药","康龙化成":"康龙化成","爱尔眼科":"爱尔眼科",
    "通策医疗":"通策医疗","迈瑞医疗":"迈瑞医疗","联影医疗":"联影医疗","片仔癀":"片仔癀",
    "云南白药":"云南白药","同仁堂":"同仁堂","东阿阿胶":"东阿阿胶","义翘神州":"义翘神州",
    "中信证券":"中信证券","华泰证券":"华泰证券","国泰海通":"国泰海通","招商证券":"招商证券",
    "中金公司":"中金公司","东方财富":"东方财富","同花顺":"同花顺","中国平安":"中国平安",
    "中国人寿":"中国人寿","新华保险":"新华保险","招商银行":"招商银行","兴业银行":"兴业银行",
    "平安银行":"平安银行","宁波银行":"宁波银行","杭州银行":"杭州银行","江苏银行":"江苏银行",
    "成都银行":"成都银行","工商银行":"工商银行","建设银行":"建设银行","农业银行":"农业银行",
    "万科A":"万科A","保利发展":"保利发展","招商蛇口":"招商蛇口","金地集团":"金地集团",
    "我爱我家":"我爱我家","滨江集团":"滨江集团","华发股份":"华发股份","三一重工":"三一重工",
    "三一":"三一重工","恒立液压":"恒立液压","徐工机械":"徐工机械","中联重科":"中联重科",
    "浙江鼎力":"浙江鼎力","中国建筑":"中国建筑","海螺水泥":"海螺水泥","海螺":"海螺水泥",
    "东方雨虹":"东方雨虹","北新建材":"北新建材","万华化学":"万华化学","华鲁恒升":"华鲁恒升",
    "宝丰能源":"宝丰能源","荣盛石化":"荣盛石化","恒力石化":"恒力石化","卫星化学":"卫星化学",
    "紫金矿业":"紫金矿业","紫金":"紫金矿业","洛阳钼业":"洛阳钼业","江西铜业":"江西铜业",
    "云南铜业":"云南铜业","中国铝业":"中国铝业","神火股份":"神火股份","锡业股份":"锡业股份",
    "厦门钨业":"厦门钨业","北方稀土":"北方稀土","金力永磁":"金力永磁","中国神华":"中国神华",
    "神华":"中国神华","陕西煤业":"陕西煤业","陕煤":"陕西煤业","兖矿能源":"兖矿能源",
    "山西焦煤":"山西焦煤","潞安环能":"潞安环能","大有能源":"大有能源","新赛股份":"新赛股份",
    "牧原股份":"牧原股份","牧原":"牧原股份","温氏股份":"温氏股份","温氏":"温氏股份",
    "新希望":"新希望","海大集团":"海大集团","大北农":"大北农","美的集团":"美的集团",
    "格力电器":"格力电器","格力":"格力电器","海尔智家":"海尔智家","海尔":"海尔智家",
    "老板电器":"老板电器","公牛集团":"公牛集团","苏泊尔":"苏泊尔","九阳股份":"九阳股份",
    "分众传媒":"分众传媒","三七互娱":"三七互娱","吉比特":"吉比特","恺英网络":"恺英网络",
    "巨人网络":"巨人网络","英伟达":"英伟达","nvidia":"英伟达","NVIDIA":"英伟达",
    "台积电":"台积电","tsmc":"台积电","TSMC":"台积电","苹果":"苹果","apple":"苹果",
    "Apple":"苹果","华为":"华为",

    // ── 2026-10-23 扩充：常用简称 / 产业链 / 指数 / 港中概 ──
    // 起因：实测 951 条真实评论仅 5.9% 过阈值；近失误样本里大量主流股因简称未收录拿不到 +2
    //   （三九/人福/江中/海控/阿胶/招行/东财/宁王/洛钼/江铜/山金…）
    // 取舍原则：**只收「股票语境下高概率指该公司」的简称**。
    //   日常高频词一律不收别名（字节/完美/值得/三七/巨人/光线/光明/长城/长安/红旗/小熊/灯泡…），
    //   否则一条散文评论就能刷出好几个 +2，噪音比漏识别的代价更大。
    // 医药 / 消费
    "华润三九":"华润三九","三九":"华润三九","人福医药":"人福医药","人福":"人福医药",
    "江中药业":"江中药业","江中":"江中药业","招商积余":"招商积余","积余":"招商积余",
    "华润双鹤":"华润双鹤","华东医药":"华东医药","丽珠集团":"丽珠集团","丽珠":"丽珠集团",
    "信立泰":"信立泰","恩华药业":"恩华药业","京新药业":"京新药业","贝达药业":"贝达药业",
    "艾力斯":"艾力斯","荣昌生物":"荣昌生物","科伦药业":"科伦药业","百利天恒":"百利天恒",
    "白云山":"白云山","马应龙":"马应龙","羚锐制药":"羚锐制药","桂林三金":"桂林三金",
    "济川药业":"济川药业","康缘药业":"康缘药业","天士力":"天士力","以岭药业":"以岭药业",
    "阿胶":"东阿阿胶","大参林":"大参林",
    "老百姓":"老百姓","益丰药房":"益丰药房","一心堂":"一心堂",
    // 金融 / 科技
    "招行":"招商银行","中信":"中信证券","华泰":"华泰证券","国泰":"国泰海通","东财":"东方财富",
    "指南针":"指南针","大智慧":"大智慧","恒生电子":"恒生电子","财富趋势":"财富趋势",
    "顶点软件":"顶点软件","金证股份":"金证股份","长亮科技":"长亮科技","宇信科技":"宇信科技",
    "京北方":"京北方","神州信息":"神州信息","中科金财":"中科金财","用友网络":"用友网络",
    "广联达":"广联达","卫宁健康":"卫宁健康","创业慧康":"创业慧康","久远银海":"久远银海",
    "东华软件":"东华软件","太极股份":"太极股份","中国软件":"中国软件","中科软":"中科软",
    "启明星辰":"启明星辰","绿盟科技":"绿盟科技","安恒信息":"安恒信息","奇安信":"奇安信",
    "三六零":"三六零","中科创达":"中科创达","软通动力":"软通动力","润和软件":"润和软件",
    "常山北明":"常山北明",
    // 新能源车 / 电池
    "宁德":"宁德时代","宁王":"宁德时代","小鹏汽车":"小鹏汽车","蔚来":"蔚来","问界":"问界",
    "亿纬":"亿纬锂能","国轩":"国轩高科","孚能科技":"孚能科技",
    "鹏辉能源":"鹏辉能源","南都电源":"南都电源","派能科技":"派能科技","天齐":"天齐锂业",
    "赣锋":"赣锋锂业","盛新锂能":"盛新锂能","融捷股份":"融捷股份","藏格矿业":"藏格矿业",
    "恩捷":"恩捷股份","星源材质":"星源材质","中材科技":"中材科技","杉杉股份":"杉杉股份",
    "中科电气":"中科电气","尚太科技":"尚太科技","新宙邦":"新宙邦","多氟多":"多氟多",
    "天际股份":"天际股份","永太科技":"永太科技","华友":"华友钴业","中伟":"中伟股份",
    // 光伏 / 风电 / 电网
    "通威":"通威股份","协鑫科技":"协鑫科技","TCL中环":"TCL中环","中环":"TCL中环",
    "双良节能":"双良节能","晶盛机电":"晶盛机电","弘元绿能":"弘元绿能","美畅股份":"美畅股份",
    "高测股份":"高测股份","宇晶股份":"宇晶股份","东方日升":"东方日升","横店东磁":"横店东磁",
    "亚玛顿":"亚玛顿","赛伍技术":"赛伍技术","海优新材":"海优新材","金辰股份":"金辰股份",
    "帝尔激光":"帝尔激光","罗博特科":"罗博特科","禾望电气":"禾望电气","盛弘股份":"盛弘股份",
    "大金重工":"大金重工","日月股份":"日月股份","东方电缆":"东方电缆","东缆":"东方电缆",
    "中天科技":"中天科技","亨通光电":"亨通光电","长飞光纤":"长飞光纤","起帆电缆":"起帆电缆",
    "特变":"特变电工","南瑞":"国电南瑞","平高电气":"平高电气","正泰电器":"正泰电器",
    "宏发股份":"宏发股份","三星医疗":"三星医疗","威胜信息":"威胜信息","海兴电力":"海兴电力",
    "林洋能源":"林洋能源",
    // 工程机械 / 建材 / 基建
    "恒立":"恒立液压","徐工":"徐工机械","中联":"中联重科","伟星新材":"伟星新材",
    "三棵树":"三棵树","科顺股份":"科顺股份","中国中铁":"中国中铁","中国铁建":"中国铁建",
    "中国交建":"中国交建","中国电建":"中国电建","中国能建":"中国能建","中国中冶":"中国中冶",
    "上海建工":"上海建工","隧道股份":"隧道股份",
    // 化工 / 资源
    "华鲁":"华鲁恒升","宝丰":"宝丰能源","荣盛":"荣盛石化","恒力":"恒力石化",
    "桐昆股份":"桐昆股份","新凤鸣":"新凤鸣","恒逸石化":"恒逸石化","东方盛虹":"东方盛虹",
    "齐翔腾达":"齐翔腾达","鲁西化工":"鲁西化工","扬农化工":"扬农化工","利尔化学":"利尔化学",
    "润丰股份":"润丰股份","龙佰集团":"龙佰集团","巨化股份":"巨化股份","三美股份":"三美股份",
    "昊华科技":"昊华科技","云天化":"云天化","兴发集团":"兴发集团","川恒股份":"川恒股份",
    "远兴能源":"远兴能源","和邦生物":"和邦生物","新洋丰":"新洋丰","云图控股":"云图控股",
    "史丹利":"史丹利","亚钾国际":"亚钾国际","洛钼":"洛阳钼业","江铜":"江西铜业",
    "云铜":"云南铜业","盛达资源":"盛达资源","山金国际":"山金国际","山东黄金":"山东黄金",
    "中金黄金":"中金黄金","赤峰黄金":"赤峰黄金","中铝":"中国铝业","云铝股份":"云铝股份",
    "明泰铝业":"明泰铝业","包钢股份":"包钢股份","盛和资源":"盛和资源","广晟有色":"广晟有色",
    "厦钨":"厦门钨业","中钨高新":"中钨高新","章源钨业":"章源钨业","中煤能源":"中煤能源",
    "兖矿":"兖矿能源","平煤股份":"平煤股份","淮北矿业":"淮北矿业","冀中能源":"冀中能源",
    "山煤国际":"山煤国际","广汇能源":"广汇能源","新奥股份":"新奥股份","九丰能源":"九丰能源",
    // 农业
    "隆平高科":"隆平高科","登海种业":"登海种业","荃银高科":"荃银高科","天邦食品":"天邦食品",
    "华统股份":"华统股份","京基智农":"京基智农","东瑞股份":"东瑞股份","立华股份":"立华股份",
    "仙坛股份":"仙坛股份","晓鸣股份":"晓鸣股份","秋乐种业":"秋乐种业","康农种业":"康农种业",
    "苏垦农发":"苏垦农发","北大荒":"北大荒","亚盛集团":"亚盛集团","新农开发":"新农开发",
    // 家电 / 消费
    "海信家电":"海信家电","海信":"海信家电","长虹美菱":"长虹美菱","美菱":"长虹美菱",
    "新宝股份":"新宝股份","德昌股份":"德昌股份","新乳业":"新乳业","妙可蓝多":"妙可蓝多",
    "天润乳业":"天润乳业","中炬高新":"中炬高新","恒顺醋业":"恒顺醋业","克明食品":"克明食品",
    "东鹏饮料":"东鹏饮料","养元饮品":"养元饮品","李子园":"李子园","盐津铺子":"盐津铺子",
    "劲仔食品":"劲仔食品","甘源食品":"甘源食品","洽洽食品":"洽洽食品","三只松鼠":"三只松鼠",
    "来伊份":"来伊份","中国中免":"中国中免","中免":"中国中免","王府井":"王府井",
    "重庆百货":"重庆百货","永辉超市":"永辉超市","家家悦":"家家悦","小商品城":"小商品城",
    "首旅酒店":"首旅酒店","君亭酒店":"君亭酒店","广州酒家":"广州酒家","全聚德":"全聚德",
    "同庆楼":"同庆楼","珀莱雅":"珀莱雅","上海家化":"上海家化","水羊股份":"水羊股份",
    "福瑞达":"福瑞达","贝泰妮":"贝泰妮","华熙生物":"华熙生物","爱美客":"爱美客",
    "巨子生物":"巨子生物","敷尔佳":"敷尔佳",
    // TMT / 互联网（只收公司专名，日常词一律不收）
    "腾讯":"腾讯","阿里巴巴":"阿里巴巴","京东":"京东","拼多多":"拼多多","美团":"美团",
    "快手":"快手","哔哩哔哩":"哔哩哔哩","B站":"哔哩哔哩","网易":"网易","百度":"百度",
    "小米":"小米","抖音":"抖音","滴滴":"滴滴","贝壳":"贝壳","微博":"微博","知乎":"知乎",
    "阅文集团":"阅文集团","腾讯音乐":"腾讯音乐","爱奇艺":"爱奇艺","虎牙":"虎牙","斗鱼":"斗鱼",
    "天下秀":"天下秀","上海钢联":"上海钢联","万国数据":"万国数据","世纪互联":"世纪互联",
    "秦淮数据":"秦淮数据","数据港":"数据港","奥飞数据":"奥飞数据","宝信软件":"宝信软件",
    "润泽科技":"润泽科技","优刻得":"优刻得","首都在线":"首都在线","恒为科技":"恒为科技",
    "中贝通信":"中贝通信","润建股份":"润建股份","宜通世纪":"宜通世纪","世纪华通":"世纪华通",
    "电魂网络":"电魂网络","姚记科技":"姚记科技","神州泰岳":"神州泰岳",
    "天娱数科":"天娱数科","华策影视":"华策影视","万达电影":"万达电影","横店影视":"横店影视",
    "金逸影视":"金逸影视","上海电影":"上海电影","中国电影":"中国电影","蓝色光标":"蓝色光标",
    "蓝标":"蓝色光标","易点天下":"易点天下","三人行":"三人行","利欧股份":"利欧股份",
    "中信出版":"中信出版","中南传媒":"中南传媒","凤凰传媒":"凤凰传媒","南方传媒":"南方传媒",
    "皖新传媒":"皖新传媒","新华文轩":"新华文轩","读客文化":"读客文化","果麦文化":"果麦文化",
    "城市传媒":"城市传媒","中国科传":"中国科传",
    // 军工 / 机械
    "沈飞":"中航沈飞","航发":"航发动力","西飞":"中航西飞","中航光电":"中航光电",
    "航天电器":"航天电器","中航重机":"中航重机","航发控制":"航发控制","洪都航空":"洪都航空",
    "航天彩虹":"航天彩虹","内蒙一机":"内蒙一机","北方导航":"北方导航","长城军工":"长城军工",
    "抚顺特钢":"抚顺特钢","图南股份":"图南股份","钢研高纳":"钢研高纳","应流股份":"应流股份",
    "中国重工":"中国重工","中船防务":"中船防务","海兰信":"海兰信",
    "汇川技术":"汇川技术","埃斯顿":"埃斯顿","中大力德":"中大力德","鸣志电器":"鸣志电器",
    "信捷电气":"信捷电气","禾川科技":"禾川科技",
    // 汽车 / 汽零 / 航运
    "上汽":"上汽集团","广汽":"广汽集团","宇通":"宇通客车",
    "福耀玻璃":"福耀玻璃","华域汽车":"华域汽车","赛轮轮胎":"赛轮轮胎","森麒麟":"森麒麟",
    "均胜电子":"均胜电子","继峰股份":"继峰股份","科博达":"科博达","爱柯迪":"爱柯迪",
    "旭升集团":"旭升集团","文灿股份":"文灿股份","广东鸿图":"广东鸿图","中石化":"中国石化",
    "中石油":"中国石油","中海油":"中国海油","中远海控":"中远海控","海控":"中远海控",
    "中远海能":"中远海能","招商轮船":"招商轮船","中谷物流":"中谷物流","安通控股":"安通控股",
    "招商南油":"招商南油","宁波海运":"宁波海运","唐山港":"唐山港","青岛港":"青岛港",
    "日照港":"日照港","北部湾港":"北部湾港",
    // 指数 / ETF / 基金（评论区极高频，且天然是"有价值资讯"信号）
    "上证指数":"上证指数","上证":"上证指数","沪指":"上证指数","深证成指":"深证成指",
    "创业板指":"创业板指","科创50":"科创50","沪深300":"沪深300","中证500":"中证500",
    "中证1000":"中证1000","中证A500":"中证A500","北证50":"北证50","恒生指数":"恒生指数",
    "恒生科技":"恒生科技","恒指":"恒生指数","国企指数":"国企指数","纳斯达克":"纳斯达克",
    "纳指":"纳斯达克","标普500":"标普500","道琼斯":"道琼斯","富时A50":"富时A50",
    "A50":"富时A50","中证红利":"中证红利","沪深300ETF":"沪深300ETF","科创50ETF":"科创50ETF",
    "创业板ETF":"创业板ETF","纳指ETF":"纳指ETF","恒生科技ETF":"恒生科技ETF",
    "中概互联":"中概互联","中概":"中概互联","QQQ":"QQQ",
  };
  const STOCK_GUARD = {
    "美的":"(?<!小而)(?<!大而)(?<!完)(?<!物)(美的)(?!好)(?!元)",
    // 「ST」绝不做裸关键词：会命中 STOCK/STRATEGY/FIRST/COST 等英文词 → 只用「ST股」「摘帽」
  };
  // 事件词表 · 三档权重（2026-10-23 重构）
  //  3 = 强信息：可验证的公告 / 政策 / 订单 / 价格变动，命中就值得看
  //  2 = 中等：  产业动向、资金行为、传闻线索
  //  1 = 辅助：  描述与前瞻词，单独不成线索，配合标的或数字才有意义
  // 设计目标：让「标的 + 一个强事件词」或「强事件词 + 具体数字」能自然过阈值，
  //           同时不让散文式评论靠堆 1 分词混进来。
  const EVENT_KEYWORDS = {
    // ── 3 分：公告与可验证事件 ──
    "公告":3,"财报":3,"年报":3,"季报":3,"业绩预告":3,"业绩快报":3,"招股":3,"IPO":3,
    "中标":3,"签约":3,"合同":3,"定增":3,"可转债":3,"配股":3,"举牌":3,"要约":3,
    "控制权":3,"股权激励":3,"员工持股":3,"回购":3,"注销":3,"立案":3,"问询":3,
    "关注函":3,"退市":3,"停牌":3,"复牌":3,"分拆":3,"借壳":3,"混改":3,"摘帽":3,
    "集采":3,"医保":3,"关税":3,"反倾销":3,"补贴":3,"退税":3,"双反":3,
    "降息":3,"加息":3,"降准":3,"LPR":3,"社融":3,"印花税":3,"非农":3,
    "获批":3,"临床":3,"上市申请":3,"通关":3,"流片":3,"量产":3,"投产":3,"扩产":3,
    "缺货":3,"断供":3,"涨价":3,"提价":3,"解禁":3,"减持":3,"增持":3,"澄清":3,
    "辟谣":3,"超预期":3,"不及预期":3,"扭亏":3,"ST股":3,"处罚":3,
    // ── 2 分：产业与资金动向 ──
    "利好":2,"利空":2,"传闻":2,"据传":2,"消息称":2,"据悉":2,"听说":2,"网传":2,
    "疑似":2,"圈内":2,"渠道":2,"内部":2,"独家":2,"订单":2,"供货":2,"定点":2,
    "送样":2,"认证":2,"客户":2,"合作":2,"建厂":2,"试产":2,"良率":2,"爬坡":2,
    "交期":2,"稼动率":2,"开工率":2,"排产":2,"库存":2,"去化":2,"累库":2,
    "并购":2,"收购":2,"重组":2,"调研":2,"机构":2,"北向":2,"南向":2,"两融":2,
    "融资":2,"大宗":2,"溢价":2,"折价":2,"分红":2,"派息":2,"送股":2,"转增":2,
    "涨停":2,"跌停":2,"异动":2,"上调":2,"下调":2,"目标价":2,"诉讼":2,"仲裁":2,
    "担保":2,"质押":2,"平仓":2,"爆仓":2,"首付款":2,"里程碑":2,"对外授权":2,
    "CPI":2,"PPI":2,"PMI":2,"美联储":2,"衰退":2,"通胀":2,"汇率":2,
    // ── 1 分：辅助与前瞻 ──
    "业绩":1,"预增":1,"预减":1,"扭亏为盈":1,"环比":1,"同比":1,"创新高":1,"创新低":1,
    "预计":1,"有望":1,"计划":1,"即将":1,"将于":1,"正在":1,
    "产能":1,"研发":1,"投入":1,"布局":1,"规划":1,"项目":1,"基地":1,"工厂":1,
    "出货":1,"交付":1,"安装":1,"招标":1,"投标":1,"采购":1,"供应":1,
    "上行":1,"下行":1,"反弹":1,"回调":1,"修复":1,"反转":1,"补涨":1,"滞涨":1,
    "新高":1,"新低":1,"突破":1,"支撑":1,"压力":1,"估值":1,"市盈率":1,"股息":1,
    "收益率":1,"浮盈":1,"浮亏":1,"翻倍":1,"腰斩":1,
    "看好":1,"看空":1,"看多":1,"性价比":1,"确定性":1,"预期差":1,"龙头":1,
    "满仓":1,"空仓":1,"加仓":1,"减仓":1,"补仓":1,"清仓":1,"抄底":1,"逃顶":1,
    "割肉":1,"套牢":1,"解套":1,"止盈":1,"止损":1,"建仓":1,"重仓":1,"轻仓":1,
    "买入":1,"卖出":1,"持有":1,"梭哈":1,
  };
  const SIX_DIGIT_RE = /(?<!\d)\d{6}(?!\d)/g;
  const VALID_CODE_RE = /^(?:60[0135]|68[89]|00[0-3]|30[012]|43|8[2347]|92|399|5[12568]|1[56])/;
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const _stockRe = new RegExp(Object.keys(STOCK_NAMES).sort((a,b)=>b.length-a.length)
    .map((a)=>STOCK_GUARD[a]||("("+esc(a)+")")).join("|"), "g");
  const _eventRe = new RegExp(Object.keys(EVENT_KEYWORDS).map((k)=>("("+esc(k)+")")).join("|"), "g");

  function isMeaningless(text) {
    if (!text) return true;
    const t = text.trim();
    if (!t) return true;
    const st = t.replace(/[^\p{L}\p{N}]/gu, "");
    if (!st || st.length < 2) return true;
    if (new Set(st).size === 1 && st.length >= 2) return true;
    if (st.length <= 3 && /^\d+$/.test(st)) return true;
    const M = new Set(["","顶","沙发","板凳","地板","前排","插眼","马克","留名","mark","m","mm",
      "dddd","好的","嗯","哦","啊","哈","哈哈","666","6","111","。。","...","。。。","？","!","支持","赞",
      "好","对","是","否","路过","围观","签到","收藏","关注","已阅","呵呵","啊啊","学习了","看不懂","不明觉厉"]);
    return M.has(t.toLowerCase());
  }

  function scoreComment(text, like, reply) {
    like = like || 0; reply = reply || 0;
    if (isMeaningless(text)) return { score: -100, tags: [], stocks: [] };
    let s = 0; const tags = []; const stocks = [];
    const nums = Array.from(new Set(text.match(SIX_DIGIT_RE) || []));
    const codes = nums.filter((c)=>VALID_CODE_RE.test(c) && new Set(c).size > 1);
    if (codes.length) { s += 3; tags.push("code:"+codes.join(",")); }
    const bad = nums.filter((c)=>!codes.includes(c));
    if (bad.length) tags.push("num:"+bad.join(","));
    _stockRe.lastIndex = 0; let m;
    while ((m=_stockRe.exec(text))!==null) { const v=STOCK_NAMES[m[1]||m[0]]; if (v && !stocks.includes(v)) stocks.push(v); }
    if (stocks.length) { s += 2*stocks.length; tags.push("stock:"+stocks.join(",")); }
    _eventRe.lastIndex = 0; const ev = [];
    while ((m=_eventRe.exec(text))!==null) { const k=m[1]||m[0]; if (!ev.includes(k)) { ev.push(k); s += EVENT_KEYWORDS[k]; } }
    if (ev.length) tags.push("event:"+ev.join(","));
    // 价值密度：带**具体数字**的资讯远比空口观点可信（"营收增30%" vs "业绩很好"）
    // 三类各 +1，替代原来笼统的"含数字或%"
    if (/\d+(?:\.\d+)?\s*(?:亿|千万|百万|万|美元|欧元|港元|元)/.test(text)) { s += 1; tags.push("density:amount"); }
    if (/\d+(?:\.\d+)?\s*(?:%|个百分点)/.test(text)) { s += 1; tags.push("density:pct"); }
    if (/(?:20\d{2}\s*年|Q[1-4]|上半年|下半年|去年同期|本月|本周|上月|上周)/.test(text)) { s += 1; tags.push("density:period"); }
    // 组合奖励：标的名 + 事件词 = 最有价值的结构（"XX公司中标YY亿订单"）
    if (stocks.length && ev.length) { s += 1; tags.push("combo:stock+event"); }
    if (text.length >= 30) s += 1;
    if (like >= 50) s += 2; else if (like >= 10) s += 1;
    if (reply >= 5) { s += 2; tags.push("interact:replies("+reply+")"); }
    else if (reply >= 1) { s += 1; tags.push("interact:replies("+reply+")"); }
    return { score: s, tags, stocks };
  }

  // ══════════════════════════════════════════════════════════════
  //  持久化
  // ══════════════════════════════════════════════════════════════
  const seenSet = new Set((GM_getValue(KEYS.seen, []) || []).slice(-8000));
  const stat = Object.assign({ rounds:0, comments:0, clues:0, uploads:0, fails:0, empty:0, waf:0, blocked:0, dedup:0, lastOk:"", throttled:0 },
                             GM_getValue(KEYS.stat, {}) || {});
  // 待上传缓冲（持久化 → 浏览器意外关闭也不丢）
  let pending = GM_getValue(KEYS.pending, []) || [];
  let lastFlushAt = GM_getValue(KEYS.lastFlush, 0) || 0;
  const bootAt = now();   // 脚本启动时刻（lastFlushAt=0 时用于起算超时）

  function persist() {
    GM_setValue(KEYS.seen, Array.from(seenSet).slice(-8000));
    GM_setValue(KEYS.stat, stat);
    GM_setValue(KEYS.pending, pending.slice(-500));
    GM_setValue(KEYS.lastFlush, lastFlushAt);
    mpFlush();
  }

  // ── 帖子页数缓存（maxPage）──
  // comments.json 的 maxPage 在同一帖子上短期内不变（推荐/热门是小滚动池，帖子会反复出现）。
  // 缓存后可直接抓尾部页，省掉 page=1 的探测请求 —— 把这次请求用在"多抓一页最新评论"上，
  // 等于**不增加请求量的前提下少漏评论**。
  let _mpCache = (GM_getValue(KEYS.maxPage, {}) || {});
  if (typeof _mpCache !== "object" || _mpCache === null) _mpCache = {};
  let _mpDirty = false;
  const MAXPAGE_TTL = 12 * 3600 * 1000;   // 12 小时
  const MAXPAGE_CAP = 3000;

  function mpGet(pid) {
    const v = _mpCache[pid];
    if (!v) return 0;
    if (now() - (+v[1] || 0) > MAXPAGE_TTL) { delete _mpCache[pid]; _mpDirty = true; return 0; }
    return +v[0] || 0;
  }
  function mpSet(pid, mp) {
    if (!pid || !mp || mp < 1) return;
    _mpCache[pid] = [mp, now()];
    _mpDirty = true;
    if (Object.keys(_mpCache).length > MAXPAGE_CAP) {
      const entries = Object.entries(_mpCache).sort((a, b) => (+a[1][1]) - (+b[1][1]));
      for (let i = 0; i < entries.length / 2; i++) delete _mpCache[entries[i][0]];
    }
  }
  function mpFlush() {
    if (!_mpDirty) return;
    GM_setValue(KEYS.maxPage, _mpCache);
    _mpDirty = false;
  }
  function bump(k, n) { stat[k] = (stat[k]||0) + (n||1); persist(); }

  // ══════════════════════════════════════════════════════════════
  //  账号池
  // ══════════════════════════════════════════════════════════════
  const accounts = () => GM_getValue(KEYS.accounts, []) || [];
  function currentAccount() {
    const l = accounts();
    if (!l.length) return null;
    return l[GM_getValue(KEYS.idx, 0) % l.length];
  }
  function rotateAccount(reason) {
    const l = accounts();
    if (l.length < 2) return false;
    const cur = GM_getValue(KEYS.idx, 0) % l.length;
    const next = (cur + 1) % l.length;
    GM_setValue(KEYS.idx, next);
    log(`🔄 切换账号：${l[cur].name} → ${l[next].name}（${reason}）`);
    return true;
  }

  // ══════════════════════════════════════════════════════════════
  //  登录态 / WAF / 封禁识别
  // ══════════════════════════════════════════════════════════════
  //  实测关键点：xq_a_token 是 HttpOnly，document.cookie 读不到！
  //  → 绝不能用 cookie 字符串判断登录态（网上多数教程在这里翻车）
  //  → 改用「需要登录的接口」做探针：/statuses/comments.json?id=<任一真实帖>
  //     游客返回 error_code 10022「用户未登录」，登录后返回 200 + comments 数组
  //  另有可用探针：/statuses/show.json?id=<id>（公开，恒 200，仅作存活检查）
  const PROBE_POST = "413226134";   // 热门榜真实帖子 id，实测稳定存在
  const AUTH_PROBE = "/statuses/comments.json?id=" + PROBE_POST + "&page=1&count=1";
  const ALIVE_PROBE = "/statuses/show.json?id=" + PROBE_POST;

  let _loginCache = { at: 0, ok: false };

  // 实测：登录态 = comments 接口返回 200 且含 comments/list 数组（游客是 error_code 10022）
  // 若固定探针帖被删导致 404，退化为「只要不是 10022 就视为已登录」
  async function probeAuth() {
    if (now() - _loginCache.at < 60000) return _loginCache.ok;
    const r = await xqJson(AUTH_PROBE);
    let ok = false;
    if (r.ok) {
      ok = !!(r.data && Array.isArray(r.data.comments || r.data.list));
    } else if (r.kind === "notlogin") {
      ok = false;
    } else if (r.kind === "http" && /404/.test(r.msg || "")) {
      // 探针帖不存在了：换动态探针（从热门列表现取一个 id）
      ok = await probeAuthDynamic();
    } else {
      // 其他错误（WAF/网络）→ 不下结论，保守当作未登录，避免误判后仍发请求
      ok = false;
    }
    _loginCache = { at: now(), ok };
    return ok;
  }

  async function probeAuthDynamic() {
    try {
      const r = await xqJson("/statuses/hot/listV2.json?since=-1&max_id=-1&size=5");
      if (!r.ok) return false;
      const items = (r.data && r.data.items) || [];
      for (const it of items) {
        const p = normalizePost(it);
        if (!p) continue;
        const rr = await xqJson(`/statuses/comments.json?id=${p.id}&page=1&count=1`);
        if (rr.ok) return !!(rr.data && Array.isArray(rr.data.comments || rr.data.list));
        if (rr.kind === "notlogin") return false;
      }
    } catch (e) {}
    return false;
  }

  async function probeAlive() {
    const r = await xqJson(ALIVE_PROBE);
    return r.ok;
  }

  // 实测：阿里云 WAF 挑战页特征串
  const WAF_HTML = /aliyun_waf_aa|_waf_bd8|安全策略拦截|request has been blocked|页面已升级需要验证|滑动验证/;
  // 实测：游客态 comments.json 返回 error_code 10022 / "用户未登录"
  const NOT_LOGIN = /10022|用户未登录/;
  const BLOCK_MSG = /账号异常|已被封|封禁|冻结|无权访问|登录失效|访问受限|过于频繁|请稍后再试/;

  // fetch 兜底通道：页面 CSP / 某些扩展会拦掉 fetch，但拦不住油猴的 GM_xmlhttpRequest
  function gmFetch(url) {
    return new Promise((resolve) => {
      try {
        GM_xmlhttpRequest({
          method: "GET", url,
          // 实测：部分接口（尤其 home_timeline）缺 Referer 会被直接拒（HTTP 400 / error_code 400016）
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.xueqiu.com/",
          },
          onload: (res) => resolve({
            resp: { ok: res.status >= 200 && res.status < 300, status: res.status },
            txt: res.responseText || "",
          }),
          onerror: () => resolve(null),
          ontimeout: () => resolve(null),
        });
      } catch (e) { resolve(null); }
    });
  }

  async function xqJson(url) {
    let resp, txt;
    try {
      resp = await fetch(url, {
        credentials: "include",
        headers: { "X-Requested-With":"XMLHttpRequest", "Accept":"application/json, text/plain, */*" },
        cache: "no-store",
      });
      txt = await resp.text();
    } catch (e) {
      const fb = await gmFetch(url);
      if (fb === null) return { ok:false, kind:"network", data:null };
      resp = fb.resp; txt = fb.txt;
    }

    if (WAF_HTML.test(txt.slice(0, 3000))) return { ok:false, kind:"waf", data:null };
    if (!resp.ok) {
      if (resp.status === 403 || resp.status === 429) return { ok:false, kind:"blocked", data:null, msg:"HTTP "+resp.status };
      return { ok:false, kind:"http", data:null, msg:"HTTP "+resp.status };
    }
    try {
      const data = JSON.parse(txt);
      const msg = String((data && (data.error_description || data.error_message)) || "");
      const code = String((data && data.error_code) || "");
      if (NOT_LOGIN.test(msg) || code === "10022") return { ok:false, kind:"notlogin", data:null, msg:msg||"用户未登录" };
      if (msg && BLOCK_MSG.test(msg)) return { ok:false, kind:"blocked", data:null, msg };
      // ⚠ 关键：接口业务错误（如 HTTP 200 + error_code 400016「请重新登录」）以前会被当成
      //   「正常 JSON」，下游 pickList 取不到数组就报「返回 0 条可用帖子」，把真正的报错掩盖掉。
      //   这里显式识别：error_code 非 0 一律算失败，文案含登录字样归 notlogin，其余归 apierr。
      if (data && typeof data === "object" && data.error_code != null && code !== "" && code !== "0") {
        const hint = /重新登录|未登录|登录失效|登录状态/.test(msg) ? "notlogin" : "apierr";
        return { ok:false, kind: hint, data:null, msg: msg || ("error_code " + code) };
      }
      if (data && typeof data === "object") return { ok:true, kind:"ok", data };
      return { ok:false, kind:"parse", data:null };
    } catch (e) { return { ok:false, kind:"parse", data:null }; }
  }

  // ══════════════════════════════════════════════════════════════
  //  接口
  // ══════════════════════════════════════════════════════════════
  // 实测：热门列表 items[].id 是序号；真实帖子 id 在 original_status.id 或 data(JSON串)
  // drop: 可选计数器 {noId,noUser,noText}，用于把「因字段缺失被丢弃」变得可见
  // 帖子的“外壳”有很多种：转发壳（original_status / retweeted_status）、
  // 卡片壳（item_data / status / target）、以及 data 字段（旧版是 JSON 字符串，v4 可能是对象）。
  // 逐层剥壳后再取 id/user/正文，避免「接口明明有数据却 0 条可用」。
  const UNWRAP_KEYS = ["original_status", "retweeted_status", "status", "item_data", "target", "quote", "source_status"];
  function unwrapPost(item) {
    let p = item;
    for (let i = 0; i < 4; i++) {
      if (!p || typeof p !== "object") return null;
      if (typeof p.data === "string") {
        let parsed = null;
        try { parsed = JSON.parse(p.data); } catch (e) { parsed = null; }
        if (parsed && typeof parsed === "object") {
          const inner = unwrapPost(parsed);
          if (inner && inner.id) return inner;
          p = parsed; continue;
        }
      }
      let nx = null;
      for (const k of UNWRAP_KEYS) {
        const v = p[k];
        if (v && typeof v === "object" && v.id) { nx = v; break; }
      }
      if (nx) { p = nx; continue; }
      if (p.data && typeof p.data === "object" && !Array.isArray(p.data)
          && (p.data.id || p.data.description || p.data.text || p.data.user)) { p = p.data; continue; }
      return p;
    }
    return p;
  }

  function normalizePost(item, drop) {
    const p = unwrapPost(item);
    if (!p || !p.id) { if (drop) drop.noId++; return null; }
    const u = p.user || p.author || {};
    // 有 user.id 但缺昵称 → 用数字 ID 兜底。
    // 以前是整条丢弃，接口一改版就会「有效帖子 0 条」且毫无提示。
    if (!u.screen_name && u.id) u.screen_name = String(u.id);
    if (!u.screen_name) { if (drop) drop.noUser++; return null; }
    // 正文字段也可能是 text（长文/转帖），以前只认 description/title 会误丢
    if (!(p.description || p.title || p.text)) { if (drop) drop.noText++; return null; }
    return { id: String(p.id), title: cleanText(p.title||""),
             screen_name: u.screen_name, user_id: String(u.id || ""),
             created_at: p.created_at||0 };
  }

  // 「关注」时间线：雪球多次改版，路径不保证稳定 → 准备多个候选逐个试，
  // 第一个能返回列表的记住复用；全失败也只跳过该板块，不拖垮整轮。
  const FOLLOW_ENDPOINTS = [
    "/v4/statuses/home_timeline.json?since_id=-1&max_id=-1&count=20",
    "/statuses/home_timeline.json?since_id=-1&max_id=-1&count=20",
    "/v4/statuses/home_timeline.json?page=1&count=20",
  ];
  let followEp = "";
  const FOLLOW_STATE = { ok: false, msg: "未测试", at: 0 };
  // 失败冷却：关注流不可用时不要每轮都试 3 个接口（白多 3 次请求，还会加重风控）
  const FOLLOW_COOLDOWN_MS = 6 * 3600 * 1000;   // 6 小时

  // 只在顶层找 statuses/list/items/data 太脆：v4 常见 { data:{ statuses:[...] } } 这类一层嵌套，
  // 顶层 data 是对象就被跳过 → 直接误判「返回 0 条」。改为三档：顶层键 → 一层容器 → 递归兜底。
  function pickList(d) {
    if (!d || typeof d !== "object") return [];
    for (const k of ["statuses", "list", "items", "data"]) {
      if (Array.isArray(d[k])) return d[k];
    }
    for (const k of ["data", "result", "results", "resp", "body", "page", "content", "module"]) {
      const v = d[k];
      if (v && typeof v === "object" && !Array.isArray(v)) {
        for (const kk of ["statuses", "list", "items", "records", "data"]) {
          if (Array.isArray(v[kk])) return v[kk];
        }
      }
    }
    return deepFindList(d, 0) || [];
  }

  // 递归兜底：找出「像帖子列表」的数组（元素是对象且带帖子特征字段），深度 ≤4
  function looksLikePost(o) {
    return !!(o.id || o.status_id || o.description || o.text || o.title
              || o.user || o.original_status || o.retweeted_status || o.item_data);
  }
  function deepFindList(node, depth) {
    if (!node || typeof node !== "object" || depth > 4) return null;
    if (Array.isArray(node)) {
      return (node.length && node.some((x) => x && typeof x === "object" && looksLikePost(x))) ? node : null;
    }
    for (const k of Object.keys(node)) {
      if (/^(error|count|total|page|next|max|since)/i.test(k)) continue;
      const r = deepFindList(node[k], depth + 1);
      if (r) return r;
    }
    return null;
  }

  // 取不到帖子时把原始响应结构摊开，别再让「0 条」成为黑盒
  function shapeOf(d) {
    try {
      if (d === null || typeof d !== "object") return String(d).slice(0, 80);
      const keys = Array.isArray(d) ? "array(len=" + d.length + ")" : Object.keys(d).slice(0, 12).join(",");
      const first = Array.isArray(d) && d.length && d[0] && typeof d[0] === "object"
        ? Object.keys(d[0]).slice(0, 12).join(",") : "";
      const preview = JSON.stringify(d).slice(0, 160);
      return `keys=[${keys}]` + (first ? ` item0=[${first}]` : "") + ` raw=${preview}`;
    } catch (e) { return "(无法序列化)"; }
  }

  async function getFollowPosts(limit) {
    if (FOLLOW_STATE.at && !FOLLOW_STATE.ok && now() - FOLLOW_STATE.at < FOLLOW_COOLDOWN_MS) {
      return { posts: [], kind: "cooldown",
        msg: `上次失败，冷却中（${fmtDur(FOLLOW_STATE.at + FOLLOW_COOLDOWN_MS - now())}后再试）` };
    }
    const order = followEp
      ? [followEp].concat(FOLLOW_ENDPOINTS.filter((e) => e !== followEp))
      : FOLLOW_ENDPOINTS.slice();
    let last = { kind: "unsupported", msg: "未知原因" };
    const tried = [];
    for (const ep of order) {
      const r = await xqJson(ep);
      if (!r.ok) {
        last = { kind: r.kind, msg: ep + " → " + (r.msg || r.kind) };
        tried.push(last.msg);
        continue;
      }
      const items = pickList(r.data);
      const posts = []; const drop = { noId:0, noUser:0, noText:0 };
      for (const it of items) { const p = normalizePost(it, drop); if (p) posts.push(p); }
      ROUND.fieldDrop += drop.noId + drop.noUser + drop.noText;
      if (posts.length) {
        followEp = ep;
        FOLLOW_STATE.ok = true; FOLLOW_STATE.msg = ep; FOLLOW_STATE.at = now();
        return { posts: posts.slice(0, limit), kind: "ok" };
      }
      // 有响应却取不到帖子 → 把结构摊开写进原因，方便直接定位改版
      const why = `${ep} 返回 0 条可用帖子（原始 ${items.length} 条`
        + ` · 丢弃 noId=${drop.noId} noUser=${drop.noUser} noText=${drop.noText}`
        + ` · ${shapeOf(r.data)}）`;
      last = { kind: "empty", msg: why };
      tried.push(why);
    }
    const detail = tried.length > 1
      ? `${last.msg} ｜ 其余候选：${tried.slice(0, -1).map((s) => s.replace(/\s*·\s*raw=.*$/, "")).join("；")}`
      : last.msg;
    FOLLOW_STATE.ok = false; FOLLOW_STATE.msg = detail; FOLLOW_STATE.at = now();
    return { posts: [], kind: last.kind, msg: detail };
  }

  async function getPosts(section, limit) {
    limit = limit || CFG.postsPerRound;
    if (section === "follow") return await getFollowPosts(limit);

    const url = section === "recommend"
      ? "/statuses/fundx/public/list.json?source=fund_public&page=1"
      : "/statuses/hot/listV2.json?since=-1&max_id=-1&size=15";
    const r = await xqJson(url);
    if (!r.ok) return { posts:[], kind:r.kind, msg:r.msg };
    const items = section === "recommend" ? ((r.data&&r.data.list)||[]) : ((r.data&&r.data.items)||[]);
    const posts = []; const drop = { noId:0, noUser:0, noText:0 };
    for (const it of items) { const p = normalizePost(it, drop); if (p) posts.push(p); }
    ROUND.fieldDrop += drop.noId + drop.noUser + drop.noText;
    if (drop.noId + drop.noUser + drop.noText) {
      log(`  [!] 接口返回 ${items.length} 项，其中 ${drop.noId + drop.noUser + drop.noText} 项因字段缺失被丢弃`
        + `（无id ${drop.noId} / 无用户 ${drop.noUser} / 无正文 ${drop.noText}）`);
    }
    return { posts: posts.slice(0, limit), kind:"ok", raw: items.length };
  }

  async function getComments(postId) {
    const out = []; const ids = new Set();
    const push = (list) => {
      let added = 0;
      for (const c of list) {
        const cid = String(c.id);
        if (!cid || ids.has(cid)) continue;
        ids.add(cid);
        const cu = c.user || {};
        out.push({
          id: cid, text: cleanText(c.text || c.description || ""),
          created_at: c.created_at || 0, like_count: c.like_count || 0,
          reply_count: c.reply_count || 0,
          user_name: cu.screen_name || cu.id || "",
        });
        added++;
      }
      return added;
    };

    let maxPage = mpGet(postId);
    let probe = null;          // 无缓存时，第 1 次探测请求拿到的列表
    let partial = false;       // 中途有页面失败（保留已抓到的，不整帖放弃）

    // 阶段一：不知道总页数 → 用一次请求探测（这 20 条也照收，不浪费）
    if (!maxPage) {
      const r = await xqJson(`/statuses/comments.json?id=${postId}&page=1&count=20`);
      if (!r.ok) return { comments: [], kind: r.kind, msg: r.msg };
      const list = (r.data && (r.data.comments || r.data.list)) || [];
      if (!list.length) return { comments: [], kind: "empty" };
      maxPage = Math.max(1, +(r.data && r.data.maxPage) || 1);
      mpSet(postId, maxPage);
      probe = list;
    }

    // 阶段二：剩余请求预算全部投给【尾部页】。
    // 实测：comments.json 按时间升序返回，尾部页 = 最新评论 = 人在页面上看到的那批。
    const budget = Math.max(0, CFG.commentPages - (probe ? 1 : 0));
    const targets = [];
    const askedPages = new Set(probe ? [1] : []);
    for (let i = 0; i < budget; i++) {
      const t = maxPage - i;
      if (t < 1) break;
      if (askedPages.has(t)) continue;   // 与探测页重复 → 跳过，避免白发一次请求
      askedPages.add(t);
      targets.push(t);
    }

    if (probe) push(probe);
    for (const t of targets) {
      const r = await xqJson(`/statuses/comments.json?id=${postId}&page=${t}&count=20`);
      if (!r.ok) { partial = true; break; }
      const list = (r.data && (r.data.comments || r.data.list)) || [];
      if (!list.length) break;
      push(list);
      if (list.length < 20) break;
      await sleep(rnd(LIMIT.PAGE_DELAY[0], LIMIT.PAGE_DELAY[1]));
    }
    return { comments: out, kind: "ok", tail: targets.length > 0,
             maxPage, partial, cached: !probe };
  }

  // ══════════════════════════════════════════════════════════════
  //  上传（对齐 Worker /api/ingest 契约）
  //  设计要点：
  //  1) 攒批：候选先入内存/持久化缓冲，攒够 BATCH_MIN 或到 FLUSH_INTERVAL
  //     才发一次 → 减少 HTTP 往返与 D1 事务数
  //  2) 服务端去重探针：上传前先问 /api/exists，只传库里没有的
  //  3) 分片仅在候选极多时启用（正常一轮 < 200 条，1 次 POST 搞定）
  // ══════════════════════════════════════════════════════════════
  const UPLOAD_CHUNK = 500, UPLOAD_RETRIES = 4, UPLOAD_BACKOFF = 2000;

  // 攒批参数（pending / lastFlushAt 在上方持久化区声明）
  // 阈值可由配置覆盖（GM_getValue('xr3_cfg').batch_min / batch_interval）
  const BATCH = {
    minToFlush: Math.min(200, Math.max(1, +(CFG.batch_min) || 30)),  // 攒够多少条立即上传
    maxIntervalMin: Math.min(1440, Math.max(5, +(CFG.batch_interval) || 90)), // 最长等待
    maxBuffer: 300,        // 缓冲区上限，超出强制发（防内存膨胀）
  };

  function postOnce(payload) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method:"POST", url:CFG.url,
        headers: { "Content-Type":"application/json; charset=utf-8", "Authorization":"Bearer "+CFG.token, "Accept":"application/json" },
        data: JSON.stringify(payload), timeout: 30000,
        onload: (res)=>resolve({code:res.status, body:res.responseText, net:false}),
        onerror: ()=>resolve({code:0, body:"", net:true}),
        ontimeout: ()=>resolve({code:0, body:"", net:true}),
      });
    });
  }

  // 去重探针：问服务端哪些 clue_id 已在库
  async function queryExists(ids) {
    if (!ids.length) return { exists: [], missing: [] };
    const r = await new Promise((resolve) => {
      GM_xmlhttpRequest({
        method:"POST", url: CFG.url.replace(/\/api\/ingest.*$/, "/api/exists"),
        headers: { "Content-Type":"application/json; charset=utf-8", "Authorization":"Bearer "+CFG.token, "Accept":"application/json" },
        data: JSON.stringify({ clue_ids: ids }),
        timeout: 20000,
        onload: (res)=>resolve({ code:res.status, body:res.responseText, net:false }),
        onerror: ()=>resolve({ code:0, body:"", net:true }),
        ontimeout: ()=>resolve({ code:0, body:"", net:true }),
      });
    });
    if (r.net || r.code !== 200) return null;   // 探针失败 → 走服务端兜底去重
    try {
      const j = JSON.parse(r.body);
      return { exists: j.exists || [], missing: j.missing || [] };
    } catch (e) {
      return null;
    }
  }

  // 命中上报：告诉服务端"这批 clue_id 我又抓到了"（只更新 hit_count / last_seen_round，
  // 不重写正文行）。这样服务端能统计"某条线索被几轮反复命中"，
  // 同时不产生任何重复行、也不增加行写入量。
  async function reportHits(ids, roundId) {
    if (!ids.length) return true;
    const r = await new Promise((resolve) => {
      GM_xmlhttpRequest({
        method:"POST", url: CFG.url.replace(/\/api\/ingest.*$/, "/api/hits"),
        headers: { "Content-Type":"application/json; charset=utf-8", "Authorization":"Bearer "+CFG.token, "Accept":"application/json" },
        data: JSON.stringify({ clue_ids: ids, round_id: roundId }),
        timeout: 20000,
        onload: (res)=>resolve({ code:res.status, net:false }),
        onerror: ()=>resolve({ code:0, net:true }),
        ontimeout: ()=>resolve({ code:0, net:true }),
      });
    });
    return !r.net && r.code === 200;
  }

  async function upload(payload) {
    if (!CFG.url || !CFG.token) return { ok:false, msg:"未配置地址/token" };
    const cands = payload.candidates || [];
    const chunks = [];
    for (let i=0;i<cands.length;i+=UPLOAD_CHUNK) chunks.push(cands.slice(i,i+UPLOAD_CHUNK));
    if (!chunks.length) chunks.push([]);
    let lastCode=0, lastBody="";
    for (let ci=0; ci<chunks.length; ci++) {
      const cp = Object.assign({}, payload); cp.candidates = chunks[ci];
      let done = false;
      for (let a=0; a<=UPLOAD_RETRIES && !done; a++) {
        const r = await postOnce(cp);
        lastCode=r.code; lastBody=r.body;
        if (!r.net && r.code===200) { done=true; break; }
        if (!r.net && r.code===401) return { ok:false, code:401, msg:"token 鉴权失败" };
        if (!r.net && r.code===404) return { ok:false, code:404, msg:"接口不存在（Worker 需部署新版本）" };
        if (!r.net && r.code>=400 && r.code<500) return { ok:false, code:r.code, msg:lastBody||("HTTP "+r.code) };
        if (a<UPLOAD_RETRIES) await sleep(UPLOAD_BACKOFF * Math.pow(2,a));
      }
      if (!done) return { ok:false, code:lastCode, msg:"上传重试耗尽："+(lastBody||"网络错误") };
      if (ci<chunks.length-1) await sleep(400);
    }
    return { ok:true, code:lastCode };
  }

  function extractClues(comments, threshold) {
    const cands = []; const local = new Set();
    for (const c of comments) {
      if (seenSet.has(c.id) || local.has(c.id)) { ROUND.seenSkip++; continue; }
      const { score, tags, stocks } = scoreComment(c.text, c.like_count, c.reply_count);
      if (score < threshold) continue;
      local.add(c.id);
      cands.push({
        id:c.id, user_name:c.user_name||"", post_author:c.post_author||"",
        time_str:epochToBeijing(c.created_at,false), created_at:c.created_at,
        like_count:c.like_count, reply_count:c.reply_count, text:c.text,
        score, tags, stocks, section:c.section||"", post_id:c.post_id||"",
        hashtag:c.hashtag||"", jev_value:0, jev_ok:false,
      });
    }
    // 分数相同 → 点赞高优先 → 时间新优先（尾部页改动后，同分时保留较新的观点）
    cands.sort((a,b)=>(b.score-a.score)||(b.like_count-a.like_count)||(b.created_at-a.created_at));
    LAST_EXTRACT = { total: cands.length, cap: LIMIT.MAX_CANDS,
                     kept: Math.min(cands.length, LIMIT.MAX_CANDS) };
    return cands.slice(0, LIMIT.MAX_CANDS);
  }
  // 生成与 payload 一致的 round_id（用于命中上报，让服务端记 last_seen_round）
  function roundIdOf(source) {
    return epochToBeijing(Math.floor(now()/1000), true).replace(/[: ]/g,"-") + "__xr3_" + source;
  }

  function buildPayload(source, candidates, totalComments, totalCandidates) {
    const ga = epochToBeijing(Math.floor(now()/1000), true);
    return {
      round_id: ga.replace(/[: ]/g,"-")+"__xr3_"+source,
      meta: {
        source, title:"雪球雷达·无人值守 v3", hashtag:"",
        generated_at:ga, total_comments: totalComments,
        // 本轮候选总数（含被探针跳过的重复）→ 服务端据此如实记录去重统计
        total_candidates: totalCandidates != null ? totalCandidates : candidates.length,
      },
      candidates, comments: [],
    };
  }

  // ────────────────────────────────────────────────────────────
  //  攒批上传
  //  候选先进 pending 缓冲，满足条件再一次性发出。
  //  收益：① HTTP 往返数下降 ② D1 事务数下降 ③ 一次写入更多行
  // ────────────────────────────────────────────────────────────
  let flushingBatch = false;

  function shouldFlush(force) {
    if (force) return true;
    if (pending.length >= BATCH.maxBuffer) return true;
    if (pending.length >= BATCH.minToFlush) return true;
    // 超时兜底：距上次上传超过 maxIntervalMin 就发（避免小批量长期滞留）
    // lastFlushAt=0 表示从未上传过 → 从「脚本启动时刻」起算超时
    const base = lastFlushAt > 0 ? lastFlushAt : bootAt;
    if (pending.length > 0 && now() - base >= BATCH.maxIntervalMin * 60000) return true;
    return false;
  }

  async function flushPending(force) {
    if (flushingBatch) return { ok: false, msg: "正在上传中" };
    if (!pending.length) return { ok: true, skipped: "缓冲为空" };
    if (!shouldFlush(force)) {
      return { ok: true, deferred: true, buffered: pending.length };
    }
    flushingBatch = true;
    const items = pending.slice();
    const originalCount = items.length;   // 探针前的原始条数（含将被判重的）
    try {
      // 1) 先问服务端哪些已在库（探针失败则照常传，服务端有兜底去重）
      const ids = items.map((c) => c.id);
      const probe = await queryExists(ids);
      let toSend = items;
      let skipped = 0;
      if (probe) {
        const existSet = new Set(probe.exists);
        toSend = items.filter((c) => !existSet.has(c.id));
        skipped = items.length - toSend.length;
        if (skipped) {
          stat.dedup = (stat.dedup || 0) + skipped;
          // 上报命中（只更新计数，不写正文行）→ 服务端能统计反复命中的热度
          const dupIds = items.filter((c) => existSet.has(c.id)).map((c) => c.id);
          const hitOk = await reportHits(dupIds, roundIdOf("auto"));
          log(`· 服务端去重：${skipped} 条已存在，跳过${hitOk ? "（已上报命中）" : ""}`);
        }
      }

      if (!toSend.length) {
        for (const c of items) seenSet.add(c.id);
        pending.length = 0;
        lastFlushAt = now();
        persist();
        log("· 本批全部为重复线索，无需写入");
        // 注意：不计入 uploads —— 这次并没有真正写入任何行，
        // 计入会让面板误报「上传正常」
        stat.lastOk = new Date().toLocaleString("zh-CN");
        persist();
        setStatus("无新线索（全部重复）");
        updatePanel();
        return { ok: true, skipped: skipped };
      }

      // 2) 一次性发出（分片仅在极多时才触发）
      //    total_candidates 传原始条数，服务端才能算出真实去重数
      const payload = buildPayload("auto", toSend, originalCount, originalCount);
      const up = await upload(payload);
      if (!up.ok) {
        log("✗ 攒批上传失败：" + (up.msg || up.code));
        setStatus("上传失败 · " + (up.msg || up.code));
        updatePanel();
        bump("fails");
        notify("雪球雷达", "上传失败：" + (up.msg || up.code) + "（缓冲已保留，会重试）");
        return { ok: false, msg: up.msg || String(up.code) };
      }
      for (const c of toSend) seenSet.add(c.id);
      pending.length = 0;
      lastFlushAt = now();
      bump("uploads");
      stat.clues += toSend.length;
      stat.lastOk = new Date().toLocaleString("zh-CN");
      persist();
      log(`✓ 上传成功 · 新增 ${toSend.length} 条${skipped ? "（跳过重复 " + skipped + "）" : ""}`);
      setStatus("上传成功 · " + new Date().toLocaleTimeString("zh-CN"));
      updatePanel();
      // 首次上传成功 → 桌面通知（用户可能没盯着面板）
      if (stat.uploads === 1) {
        try { notify("雪球雷达", "上传通道已打通 · 新增 " + toSend.length + " 条线索"); } catch (e) {}
      }
      return { ok: true, sent: toSend.length, skipped };
    } finally {
      flushingBatch = false;
    }
  }

  // ══════════════════════════════════════════════════════════════
  //  主调度
  // ══════════════════════════════════════════════════════════════
  const ST = { running:false, stopped:false, timer:null, sessionStart:now(), nextAt:0, runAfterCurrent:false,
               leader:false, lastTick:now(), watchdog:null, awake:false };

  // 单轮诊断计数：每轮开始重置、结束汇总，用来回答"到底漏了多少"
  const ROUND = { posts:0, comments:0, cands:0, seenSkip:0, fieldDrop:0,
                  tailPages:0, partialPosts:0, cachedPages:0 };
  // 最近一次 extractClues 的截断情况（候选超上限时给出明确提示）
  let LAST_EXTRACT = { total: 0, cap: 0, kept: 0 };
  let LAST_ROUND = null;   // 上轮诊断快照（面板展示用）
  function roundReset() {
    Object.assign(ROUND, { posts:0, comments:0, cands:0, seenSkip:0, fieldDrop:0,
                          tailPages:0, partialPosts:0, cachedPages:0 });
  }

  function log(t) {
    if (logEl) {
      const line = "["+new Date().toLocaleTimeString("zh-CN")+"] "+t;
      logEl.textContent = (logEl.textContent+"\n"+line).split("\n").slice(-40).join("\n");
      logEl.scrollTop = logEl.scrollHeight;
    }
    if (window.console && console.log) console.log("[雪球雷达] "+t);
  }
  function setStatus(t) { if (statusEl) statusEl.textContent = t; }
  function notify(title, text) {
    if (!CFG.notify) return;
    try { GM_notification({ title, text, timeout:8000 }); } catch (e) {}
  }

  function scheduleNext(delayMs) {
    ST.nextAt = now() + delayMs;
    GM_setValue(NS+"next_run", ST.nextAt);
    if (ST.timer) clearTimeout(ST.timer);
    ST.timer = setTimeout(loop, delayMs);
    startWatchdog();   // 长定时器被后台节流推迟时，由看门狗按绝对时间补跑
    updatePanel();
  }
  const nextDelay = () => rnd(CFG.minIntervalMin, CFG.maxIntervalMin) * 60000;

  async function loop() {
    if (ST.running || ST.stopped) return;
    ST.running = true;
    roundReset();
    setStatus("运行中…");
    try {
      // 前置检查
      if (!CFG.url || !CFG.token) {
        log("✗ 未配置上传地址/token → 自动任务暂停（不发起任何抓取请求）");
        setStatus("未配置 · 已暂停");
        scheduleNext(15*60000);
        return;
      }
      if (isNight()) {
        const wait = msToNightEnd() + rnd(0, 1800000);
        log(`🌙 静默时段停抓（${nightLabel()}），${fmtDur(wait)} 后恢复`);
        setStatus("夜间停抓");
        scheduleNext(wait);
        return;
      }
      if (!await probeAuth()) {
        log("✗ 未登录（探针接口返回「用户未登录」）→ 本轮不发起抓取请求");
        _loginCache.ok = false;
        if (rotateAccount("未登录")) { scheduleNext(3000); return; }
        notify("雪球雷达", "雪球未登录。请在雪球页面完成登录，脚本每 15 分钟自动重试");
        setStatus("等待登录");
        scheduleNext(15*60000);
        return;
      }
      _loginCache.ok = true;

      // 抓取
      const acct = currentAccount();
      log(`▶ 新一轮（账号：${acct ? acct.name : "当前浏览器账号"}）`);
      const SEC_LABEL = { recommend: "推荐", hot: "热门", follow: "关注" };
      const ALLOWED = ["recommend", "hot", "follow"];
      const sections = (CFG.sections || "recommend,hot")
        .split(",").map((s) => s.trim()).filter((s) => ALLOWED.includes(s));
      if (!sections.length) sections.push("recommend");
      // 总预算按启用板块均分：新增「关注」不会增加每轮请求量（2 源各 6 → 3 源各 4）
      // 仍受「每板块 ≤ 10 帖」的保号硬约束
      const perSection = Math.min(LIMIT.MAX_POSTS,
        Math.max(1, Math.ceil((CFG.totalPostsPerRound || 12) / sections.length)));
      log(`板块：${sections.map((s) => SEC_LABEL[s] || s).join(" + ")}`
        + `（每板块 ${perSection} 帖 · 总预算 ${CFG.totalPostsPerRound}）`);
      const all = [];
      let aborted = null;

      for (const section of sections) {
        if (ST.stopped) break;
        log(`→ 抓「${SEC_LABEL[section] || section}」`);
        const pr = await getPosts(section, perSection);
        if (pr.kind !== "ok") {
          // 「关注」是新增源，接口路径尚未在真实登录态下完全验证：
          // 失败只跳过它自己，绝不拖垮整轮（推荐/热门照常跑）
          if (section === "follow") {
            if (pr.kind === "cooldown") {
              log(`  · 「关注」${pr.msg} → 跳过`);
            } else {
              log(`  ⚠ 「关注」不可用（${pr.kind}${pr.msg ? " · " + clip(pr.msg, 320) : ""}）→ 跳过该板块，其余照常`);
            }
            continue;
          }
          aborted = pr; log(`  ✗ 列表失败：${pr.kind}${pr.msg?" ("+pr.msg+")":""}`); break;
        }
        log(`  有效帖子 ${pr.posts.length} 条`);
        for (const p of pr.posts) {
          if (ST.stopped) break;
          const cr = await getComments(p.id);
          if (cr.kind !== "ok") { aborted = cr; log(`  ✗ 评论失败（${cr.kind}${cr.msg?" "+cr.msg:""}）`); break; }
          ROUND.posts++;
          ROUND.comments += cr.comments.length;
          for (const c of cr.comments) { c.section=section; c.post_id=p.id; c.post_author=p.screen_name; all.push(c); }
          if (cr.tail) ROUND.tailPages++;
          if (cr.cached) ROUND.cachedPages++;
          if (cr.partial) ROUND.partialPosts++;
          log(`  · ${p.screen_name} → ${cr.comments.length} 条` +
              (cr.tail ? `（最新页，该帖共 ${cr.maxPage} 页${cr.cached ? " · 页码已缓存" : ""}）` : "") +
              (cr.partial ? " ⚠部分页失败" : ""));
          await sleep(rnd(LIMIT.POST_DELAY[0], LIMIT.POST_DELAY[1]));
        }
        if (aborted) break;
        await sleep(rnd(LIMIT.SECTION_DELAY[0], LIMIT.SECTION_DELAY[1]));
      }

      // 异常分支
      if (aborted) {
        bump("fails");
        if (aborted.kind === "waf") {
          bump("waf");
          const wait = rnd(LIMIT.WAF_BACKOFF[0], LIMIT.WAF_BACKOFF[1])*60000;
          log(`⚠ 命中阿里云 WAF → 退避 ${fmtDur(wait)}（期间零请求）`);
          notify("雪球雷达", "命中 WAF 风控，已自动退避 "+fmtDur(wait));
          setStatus("WAF 退避中");
          scheduleNext(wait); return;
        }
        if (aborted.kind === "blocked") {
          bump("blocked");
          const wait = rnd(LIMIT.BLOCKED_BACKOFF[0], LIMIT.BLOCKED_BACKOFF[1])*60000;
          log(`🚫 接口受限：${aborted.msg||""}`);
          if (rotateAccount("账号受限")) {
            log(`  已切换账号，${fmtDur(wait)} 后用新账号重试`);
            setStatus("已切换账号 · 退避中");
            scheduleNext(wait); return;
          }
          notify("雪球雷达", "账号疑似被限制："+(aborted.msg||"接口受限")+"。已退避 "+fmtDur(wait));
          setStatus("账号受限 · 退避中");
          scheduleNext(wait); return;
        }
        if (aborted.kind === "notlogin") {
          log("  登录态在请求中失效");
          if (rotateAccount("请求中掉登录")) { scheduleNext(3000); return; }
          notify("雪球雷达", "登录态失效（"+(aborted.msg||"10022")+"），请重新登录");
          setStatus("等待登录");
          scheduleNext(15*60000); return;
        }
        log("⚠ 本轮异常结束，稍后重试");
        scheduleNext(30*60000);
        return;
      }

      // 打分 & 上传
      bump("rounds");
      stat.comments += all.length;
      persist();
      log(`共 ${all.length} 条评论，打分中…`);
      const cands = extractClues(all, CFG.threshold);
      log(`▲ 候选 ${cands.length} 条（阈值 ${CFG.threshold}）`);
      if (LAST_EXTRACT.total > LAST_EXTRACT.kept) {
        log(`  [!] 共 ${LAST_EXTRACT.total} 条过阈值，超过单轮上限 ${LAST_EXTRACT.cap}，`
          + `按分数截断（漏 ${LAST_EXTRACT.total - LAST_EXTRACT.kept} 条）`);
      }
      ROUND.cands = cands.length;
      log(`诊断：帖子 ${ROUND.posts} · 评论 ${all.length} · 候选 ${cands.length}`
        + ` · seen跳过 ${ROUND.seenSkip}`
        + (ROUND.fieldDrop ? ` · 字段丢弃 ${ROUND.fieldDrop}` : "")
        + ` · 最新页命中 ${ROUND.tailPages}/${ROUND.posts}（页码缓存 ${ROUND.cachedPages}）`
        + (ROUND.partialPosts ? ` · 部分失败帖 ${ROUND.partialPosts}` : ""));
      LAST_ROUND = Object.assign({}, ROUND);

      if (cands.length) {
        // 攒批：候选先入缓冲，跨轮累积到阈值再一次性发出
        // ① 攒够阈值 → 立即发（减少 HTTP 往返与 D1 事务）
        // ② 没攒够 → 保留到下一轮，由 90 分钟超时兜底（防止长期滞留）
        // ③ 例外：首轮（从未成功上传过）强制发一次 —— 否则用户配好配置后
        //    看到的是「攒批中」，仍然不知道上传到底成没成
        for (const c of cands) pending.push(c);
        const firstEver = stat.uploads === 0;
        log(`▲ 候选 ${cands.length} 条（阈值 ${CFG.threshold}）· 缓冲 ${pending.length} 条`);
        const fr = await flushPending(firstEver);
        if (fr.deferred) {
          log(`⏳ 攒批中（攒够 ${BATCH.minToFlush} 条或超 ${BATCH.maxIntervalMin} 分钟再发）`);
        } else if (!fr.ok) {
          notify("雪球雷达", "上传失败："+(fr.msg||"未知"));
        }
        stat.empty = 0;
      } else {
        log("· 无达标候选（评论 ID 已记入去重表）");
      }

      // 连续空 → 降频
      if (all.length === 0) {
        stat.empty = (stat.empty||0)+1;
        if (stat.empty >= LIMIT.MAX_EMPTY) {
          const wait = rnd(LIMIT.WAF_BACKOFF[0], LIMIT.WAF_BACKOFF[1])*60000;
          log(`⚠ 连续 ${stat.empty} 轮 0 评论 → 判定异常，降频 ${fmtDur(wait)}`);
          notify("雪球雷达", "连续多轮 0 评论，已降频 "+fmtDur(wait));
          stat.empty = 0; persist();
          scheduleNext(wait); return;
        }
      } else { stat.empty = 0; persist(); }

      const d = nextDelay();
      log(`⏰ 下一轮 ${fmtDur(d)} 后（${new Date(now()+d).toLocaleTimeString("zh-CN")}）`);
      setStatus("等待下一轮 · "+fmtDur(d));
      scheduleNext(d);
    } catch (e) {
      bump("fails");
      log("✗ 异常："+(e && e.message ? e.message : e));
      setStatus("异常 · 稍后重试");
      scheduleNext(20*60000);
    } finally {
      ST.running = false;
      updatePanel();
      // 配置后置的「立即验证」请求：本轮结束后补跑一次
      // 但如果这一轮已经成功上传了，就不必再补跑（避免多跑一轮）
      if (ST.runAfterCurrent) {
        ST.runAfterCurrent = false;
        if (stat.uploads > 0) {
          log("✓ 本轮已成功上传，无需额外验证");
        } else {
          log("▶ 开始验证完整链路（配置后触发）…");
          setTimeout(() => { if (!ST.running && !ST.stopped) loop(); }, 1200);
        }
      }
    }
  }

  // ══════════════════════════════════════════════════════════════
  //  UI
  // ══════════════════════════════════════════════════════════════
  let panel, statusEl, logEl, hintEl;

  GM_addStyle(`
    #xr3{position:fixed;left:12px;bottom:12px;z-index:2147483647;width:330px;
      background:#0f172a;color:#e2e8f0;font:13px/1.55 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
      border:1px solid #334155;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.5);overflow:hidden}
    #xr3 .h{display:flex;align-items:center;justify-content:space-between;
      padding:8px 12px;background:#1e293b;border-bottom:1px solid #334155;cursor:pointer;user-select:none}
    #xr3 .h b{color:#38bdf8;font-size:13px;font-weight:500}
    #xr3 .h .dot{width:7px;height:7px;border-radius:50%;background:#475569;display:inline-block;margin-right:6px}
    #xr3 .h .dot.on{background:#22c55e}
    #xr3 .h .dot.wait{background:#f59e0b}
    #xr3 .bd{padding:10px 12px}
    #xr3 .st{color:#94a3b8;font-size:12px;margin-bottom:8px;min-height:17px}
    #xr3 .btns{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
    #xr3 button{flex:1;background:#273449;color:#e2e8f0;border:1px solid #334155;
      border-radius:8px;padding:6px 8px;cursor:pointer;font-size:12px;min-width:70px;font-family:inherit}
    #xr3 button:hover{border-color:#38bdf8}
    #xr3 button.pri{background:#0e7490;border-color:#0e7490;color:#fff}
    #xr3 .hint{color:#64748b;font-size:11px;line-height:1.65;margin-bottom:8px}
    #xr3 .hint b{color:#94a3b8;font-weight:500}
    #xr3 .log{max-height:160px;overflow:auto;font-size:11px;color:#94a3b8;background:#0b1220;
      border-radius:8px;padding:7px 8px;white-space:pre-wrap;border:0.5px solid #1e293b}
    #xr3.min .bd{display:none}

    /* 设置对话框 */
    #xr3-cfg{position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;
      background:rgba(2,6,23,.72);font:13px/1.55 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif}
    #xr3-cfg .dlg{width:520px;max-width:94vw;max-height:88vh;overflow:auto;background:#0f172a;color:#e2e8f0;
      border:1px solid #334155;border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
    #xr3-cfg .dh{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;
      background:#1e293b;border-bottom:1px solid #334155;font-size:14px;font-weight:500}
    #xr3-cfg .dh .x{cursor:pointer;color:#64748b;font-size:15px;line-height:1;padding:2px 4px}
    #xr3-cfg .dh .x:hover{color:#e2e8f0}
    #xr3-cfg .db{padding:14px 16px}
    #xr3-cfg label{display:block;font-size:11px;color:#94a3b8;margin:10px 0 4px}
    #xr3-cfg input[type=text],#xr3-cfg input[type=password],#xr3-cfg input[type=number]{
      width:100%;box-sizing:border-box;background:#0b1220;color:#e2e8f0;border:1px solid #334155;
      border-radius:8px;padding:7px 9px;font-size:12px;font-family:inherit}
    #xr3-cfg input:focus{outline:none;border-color:#38bdf8}
    #xr3-cfg .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0 10px}
    #xr3-cfg .grid label{margin-top:8px}
    #xr3-cfg .row{display:flex;gap:6px;margin-top:12px}
    #xr3-cfg .sep{height:1px;background:#1e293b;margin:14px 0 4px}
    #xr3-cfg .df{display:flex;align-items:center;gap:8px;padding:11px 16px;border-top:1px solid #334155;background:#0b1220}
    #xr3-cfg .df .msg{flex:1;font-size:11px;color:#94a3b8;word-break:break-all}
    #xr3-cfg button.sm{background:#273449;color:#e2e8f0;border:1px solid #334155;border-radius:8px;
      padding:6px 12px;cursor:pointer;font-size:12px;font-family:inherit}
    #xr3-cfg button.sm:hover{border-color:#38bdf8}
    #xr3-cfg button.sm.pri{background:#0e7490;border-color:#0e7490;color:#fff}
  `);

  function updatePanel() {
    if (!panel) return;
    const dot = panel.querySelector(".h .dot");
    const nextIn = ST.nextAt ? ST.nextAt - now() : 0;
    const acct = currentAccount();
    const lst = accounts();
    // 登录态用探针结果（xq_a_token 是 HttpOnly，读不到 cookie）
    const loginTxt = _loginCache.ok ? "已登录" : (_loginCache.at ? "未登录" : "未检测");
    // 上传状态行：把「能不能上传」这件事说清楚，不要埋在统计数字里
    let upLine;
    if (!CFG.url || !CFG.token) {
      upLine = `<span style="color:#f87171">未配置上传地址/Token</span> · 点「设置」`;
    } else if (stat.uploads > 0) {
      upLine = `<span style="color:#4ade80">上传正常</span> · 成功 ${stat.uploads} 次` +
               (stat.lastOk ? ` · 末次 ${stat.lastOk}` : "");
    } else if (stat.fails > 0) {
      upLine = `<span style="color:#f87171">上传失败 ${stat.fails} 次</span> · 点「设置」测试连通性`;
    } else {
      upLine = `<span style="color:#fbbf24">已配置 · 尚未上传</span> · 点「立即跑一轮」验证`;
    }
    hintEl.innerHTML =
      `状态 <b>${loginTxt}</b> · 账号 <b>${acct?acct.name:"当前浏览器"}</b>${lst.length>1?"（共"+lst.length+"个轮换）":""}<br>`+
      `上传 ${upLine}<br>`+
      `轮次 ${stat.rounds||0} · 评论 ${stat.comments||0} · 线索 ${stat.clues||0} · 服务端去重 ${stat.dedup||0}<br>`+
      `缓冲 <b>${pending.length}</b> 条（满 ${BATCH.minToFlush} 即发）<br>`+
      (LAST_ROUND ? `上轮 帖 ${LAST_ROUND.posts} · 评 ${LAST_ROUND.comments} · 候选 ${LAST_ROUND.cands}`
        + ` · 最新页 ${LAST_ROUND.tailPages}/${LAST_ROUND.posts}`
        + (LAST_ROUND.seenSkip ? ` · seen跳过 ${LAST_ROUND.seenSkip}` : "")
        + (LAST_ROUND.fieldDrop ? ` · 字段丢弃 ${LAST_ROUND.fieldDrop}` : "") + `<br>` : "")+
      (String(CFG.sections||"").includes("follow")
        ? `关注流 <b style="color:${FOLLOW_STATE.ok?"#4ade80":"#fbbf24"}">${
            !FOLLOW_STATE.at ? "未测试" : (FOLLOW_STATE.ok ? "可用" : "不可用")
          }</b>${FOLLOW_STATE.ok ? "" : "（菜单→测试「关注」流接口）"}<br>`
        : "")+
      `WAF ${stat.waf||0} · 受限 ${stat.blocked||0} · 失败 ${stat.fails||0}`
        + (stat.throttled ? ` · <span style="color:#fbbf24">节流补跑 ${stat.throttled}</span>` : "") + `<br>`+
      // 无人值守排障最关键的三件事：谁是主控、页面是否可见、保活是否生效
      `主控 <b style="color:${ST.leader?"#4ade80":"#fbbf24"}">${ST.leader?"本页":"其他页"}</b>`
        + `${ST.leader?"":"（"+Math.round(leaderAge()/60000)+"分钟无心跳）"}`
        + ` · 页面 ${document.visibilityState === "visible" ? "可见" : "<span style=\"color:#fbbf24\">不可见</span>"}`
        +       ` · 保活 ${ST.awake?"<span style=\"color:#4ade80\">开</span>":"<span style=\"color:#f87171\">关</span>"}<br>`+
      `静默 <b>${nightLabel()}</b>${isNight()?" <span style=\"color:#38bdf8\">（当前静默中）</span>":""}<br>`+
      `下一轮 <b>${ST.stopped?"已停止":(nextIn>0?fmtDur(nextIn)+"后":"即将开始")}</b>`;
    if (dot) dot.className = "dot"+(ST.stopped?"":(ST.running?" on":(nextIn>0?" wait":" on")));
  }

  function buildPanel() {
    if (document.getElementById("xr3")) return;
    panel = document.createElement("div");
    panel.id = "xr3";
    panel.innerHTML = `
      <div class="h"><b><span class="dot"></span>雪球雷达 v3 · 无人值守</b><span style="color:#64748b;font-size:12px">▾</span></div>
      <div class="bd">
        <div class="st">初始化中…</div>
        <div class="btns">
          <button class="pri" data-a="run">立即跑一轮</button>
          <button data-a="flush">刷新缓冲</button>
          <button data-a="stop">停止</button>
          <button data-a="cfg">设置</button>
          <button data-a="acct">账号池</button>
        </div>
        <div class="hint"></div>
        <div class="log"></div>
      </div>`;
    document.body.appendChild(panel);
    statusEl = panel.querySelector(".st");
    logEl = panel.querySelector(".log");
    hintEl = panel.querySelector(".hint");
    panel.querySelector(".h").addEventListener("click", ()=>panel.classList.toggle("min"));
    panel.querySelector('[data-a="run"]').addEventListener("click", ()=>{ if (ST.timer) clearTimeout(ST.timer); loop(); });
    panel.querySelector('[data-a="flush"]').addEventListener("click", async ()=>{
      if (!pending.length) { log("· 缓冲为空"); return; }
      log(`手动刷新缓冲 ${pending.length} 条…`);
      await flushPending(true);
      updatePanel();
    });
    panel.querySelector('[data-a="stop"]').addEventListener("click", ()=>{
      ST.stopped = !ST.stopped;
      if (ST.timer) { clearTimeout(ST.timer); ST.timer = null; }
      if (ST.stopped) { log("⏸ 已停止自动抓取"); setStatus("已停止"); }
      else { log("▶ 已恢复"); scheduleNext(nextDelay()); }
      updatePanel();
    });
    panel.querySelector('[data-a="cfg"]').addEventListener("click", openConfig);
    panel.querySelector('[data-a="acct"]').addEventListener("click", openAccounts);
    updatePanel();
  }

  // ────────────────────────────────────────────────────────────
  //  设置对话框
  //  设计要点：
  //  ① 上传地址与 Token **不写死在脚本里**，首次使用时在此填写
  //     → 脚本可以安全地分享/提交到仓库，不泄露个人 Worker 地址
  //  ② Token 用 password 类型输入，界面上不可见
  //  ③ 提供「连通性测试」，填完立刻能验证对不对
  //  ④ 提供「配置导出/导入」，方便多设备迁移（导出内容可自行决定是否分享）
  // ────────────────────────────────────────────────────────────
  function openConfig() {
    if (document.getElementById("xr3-cfg")) return;
    const wrap = document.createElement("div");
    wrap.id = "xr3-cfg";
    wrap.innerHTML = `
      <div class="mask">
        <div class="dlg">
          <div class="dh">设置<span class="x" data-x>✕</span></div>
          <div class="db">
            <label>上传地址（/api/ingest 完整 URL）</label>
            <input type="text" id="xr3-url" placeholder="https://你的域名/api/ingest" value="">
            <label>上传 Token（与 Worker 的 INGEST_TOKEN 一致）</label>
            <input type="password" id="xr3-token" placeholder="粘贴 token" value="">
            <div class="row">
              <button data-t="test" class="sm">测试连通性</button>
              <button data-t="export" class="sm">导出配置</button>
              <button data-t="import" class="sm">导入配置</button>
            </div>
            <div class="sep"></div>
            <div class="grid">
              <div><label>打分阈值</label><input type="number" id="xr3-thr" min="0" max="30"></div>
              <div><label>每轮总帖数</label><input type="number" id="xr3-pp" min="1" max="30"></div>
              <div><label>每帖页数</label><input type="number" id="xr3-cp" min="1" max="3"></div>
              <div><label>板块</label><input type="text" id="xr3-sec" placeholder="recommend,hot,follow"></div>
              <div style="grid-column:1/-1;font-size:11px;line-height:1.5;color:#94a3b8;margin-top:6px">
                板块可选 <b>recommend</b>（推荐）/ <b>hot</b>（热门）/ <b>follow</b>（关注）。<br>
                总帖数会按板块数<b>均分</b>：例如总 12 帖 + 3 个板块 → 每块 4 帖（不增加请求量）。
              </div>
              <div><label>最小间隔(分)</label><input type="number" id="xr3-mi" min="30" max="1440"></div>
              <div><label>最大间隔(分)</label><input type="number" id="xr3-ma" min="30" max="1440"></div>
              <div style="grid-column:1/-1;font-size:11px;line-height:1.5;color:#f59e0b;margin-top:6px">
                间隔下限 30 分钟（最大须 ≥ 最小）。填 &lt;90 分钟会明显增加风控/封号风险，建议 150~240。
              </div>
              <div><label>攒批阈值</label><input type="number" id="xr3-bt" min="1" max="200"></div>
              <div><label>攒批等待(分)</label><input type="number" id="xr3-bi" min="5" max="1440"></div>
              <div><label>静默开始(整点)</label><input type="number" id="xr3-ns" min="0" max="23"></div>
              <div><label>静默结束(整点)</label><input type="number" id="xr3-ne" min="0" max="23"></div>
              <div style="grid-column:1/-1;font-size:11px;line-height:1.5;color:#94a3b8;margin-top:6px">
                静默时段内不发任何请求，到点自动恢复（默认 23:00-07:00）。
                支持跨天（23→7）与不跨天（9→17）；<b>开始 = 结束</b> 表示<b>全天运行、不静默</b>。
              </div>
            </div>
            <div class="sep"></div>
            <div style="font-size:11px;line-height:1.6;color:#94a3b8">
              <label style="display:flex;align-items:center;gap:7px;margin:6px 0;color:#e2e8f0">
                <input type="checkbox" id="xr3-ka" style="width:auto;margin:0">
                后台保活（播放极低音量音频，避免 Chrome 冻结本标签）
              </label>
              <label style="display:flex;align-items:center;gap:7px;margin:6px 0;color:#e2e8f0">
                <input type="checkbox" id="xr3-al" style="width:auto;margin:0">
                自动接管主控（主控标签长时间无心跳时本页接手）
              </label>
              锁屏 / 远程桌面 / 标签页在另一个虚拟桌面时，Chrome 会节流甚至冻结后台标签。
              上面两项能把影响降到最低，但**根治办法是用带参数的 Chrome 启动**（见 README）。
            </div>
          </div>
          <div class="df">
            <span class="msg"></span>
            <button data-t="cancel" class="sm">取消</button>
            <button data-t="save" class="sm pri">保存</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(wrap);

    const $ = (id) => wrap.querySelector(id);
    $("#xr3-url").value = CFG.url || "";
    $("#xr3-token").value = CFG.token || "";
    $("#xr3-thr").value = CFG.threshold;
    $("#xr3-pp").value = CFG.totalPostsPerRound;
    $("#xr3-cp").value = CFG.commentPages;
    $("#xr3-sec").value = CFG.sections;
    $("#xr3-mi").value = CFG.minIntervalMin;
    $("#xr3-ma").value = CFG.maxIntervalMin;
    $("#xr3-bt").value = BATCH.minToFlush;
    $("#xr3-bi").value = BATCH.maxIntervalMin;
    $("#xr3-ns").value = normHour(CFG.nightStart, LIMIT.NIGHT_START);
    $("#xr3-ne").value = normHour(CFG.nightEnd, LIMIT.NIGHT_END);
    $("#xr3-ka").checked = CFG.keepAwake !== false;
    $("#xr3-al").checked = CFG.autoLead !== false;

    const msg = wrap.querySelector(".msg");
    const say = (t, ok) => { msg.textContent = t; msg.style.color = ok === false ? "#f87171" : (ok ? "#4ade80" : "#94a3b8"); };

    // 关闭
    const close = () => wrap.remove();
    wrap.querySelector("[data-x]").addEventListener("click", close);
    wrap.querySelector('[data-t="cancel"]').addEventListener("click", close);
    wrap.querySelector(".mask").addEventListener("click", (e) => { if (e.target === wrap.querySelector(".mask")) close(); });

    // 连通性测试：复用 verifyUpload（与真实上传同一条鉴权路径）
    wrap.querySelector('[data-t="test"]').addEventListener("click", async () => {
      const u = $("#xr3-url").value.trim();
      const tk = $("#xr3-token").value.trim();
      if (!u || !tk) { say("请先填写地址和 Token", false); return; }
      say("测试中…");
      // 临时用输入框的值测试，不影响已保存配置
      const saved = { url: CFG.url, token: CFG.token };
      CFG.url = u; CFG.token = tk;
      const res = await verifyUpload();
      CFG.url = saved.url; CFG.token = saved.token;
      say((res.ok ? "✓ " : "✗ ") + res.msg, res.ok);
    });

    // 导出配置（把配置写进剪贴板，由用户自行决定分享与否）
    wrap.querySelector('[data-t="export"]').addEventListener("click", () => {
      const payload = JSON.stringify({ url: $("#xr3-url").value.trim(), token: $("#xr3-token").value.trim() });
      try {
        GM_setClipboard(payload, "text");
        say("✓ 配置已复制到剪贴板（含 Token，请勿随意分享）", true);
      } catch (e) {
        prompt("复制下面的配置 JSON：", payload);
      }
    });

    // 导入配置
    wrap.querySelector('[data-t="import"]').addEventListener("click", () => {
      const s = prompt("粘贴配置 JSON（由「导出配置」得到）：");
      if (!s) return;
      try {
        const o = JSON.parse(s);
        if (o.url) $("#xr3-url").value = o.url;
        if (o.token) $("#xr3-token").value = o.token;
        say("✓ 已导入，点「保存」生效", true);
      } catch (e) { say("✗ JSON 格式错误", false); }
    });

    // 保存
    wrap.querySelector('[data-t="save"]').addEventListener("click", async () => {
      CFG.url = $("#xr3-url").value.trim();
      CFG.token = $("#xr3-token").value.trim();
      CFG.threshold = Math.max(0, parseInt($("#xr3-thr").value, 10) || 5);
      // 每轮总帖数预算（按启用板块均分）；postsPerRound 为旧字段，同步成单板块值保持兼容
      CFG.totalPostsPerRound = Math.min(LIMIT.MAX_TOTAL_POSTS,
        Math.max(1, parseInt($("#xr3-pp").value, 10) || 12));
      CFG.commentPages = Math.min(LIMIT.MAX_PAGES, Math.max(1, parseInt($("#xr3-cp").value, 10) || 2));
      // 板块：只接受已实现的三种，未知值丢弃（否则每轮都会发出无效请求）
      const allowedSec = ["recommend", "hot", "follow"];
      const secRaw = ($("#xr3-sec").value || "recommend,hot").split(",").map((s) => s.trim()).filter(Boolean);
      const secIn = secRaw.filter((s) => allowedSec.includes(s));
      const secBad = secRaw.filter((s) => !allowedSec.includes(s));
      CFG.sections = (secIn.length ? secIn : ["recommend", "hot"]).join(",");
      $("#xr3-sec").value = CFG.sections;
      if (secBad.length) log("⚠ 未知板块已忽略：" + secBad.join(",") + "（仅支持 recommend/hot/follow）");
      CFG.postsPerRound = Math.min(LIMIT.MAX_POSTS,
        Math.max(1, Math.ceil(CFG.totalPostsPerRound / Math.max(1, secIn.length || 2))));
      // 间隔：下限 30 分钟，最大须 ≥ 最小。
      // 关键：被钳制时**明确告知**——旧版是静默改写，看起来就像"保存失败"。
      const rawMi = parseInt($("#xr3-mi").value, 10);
      const rawMa = parseInt($("#xr3-ma").value, 10);
      CFG.minIntervalMin = Math.max(LIMIT.MIN_INTERVAL, rawMi || 150);
      CFG.maxIntervalMin = Math.max(CFG.minIntervalMin, rawMa || 240);
      const notes = [];
      if (!rawMi) notes.push(`最小间隔留空，已按默认 ${CFG.minIntervalMin} 分钟保存`);
      else if (rawMi < LIMIT.MIN_INTERVAL) notes.push(`最小间隔最低 ${LIMIT.MIN_INTERVAL} 分钟，已按 ${LIMIT.MIN_INTERVAL} 保存`);
      if (!rawMa) notes.push(`最大间隔留空，已按默认 ${CFG.maxIntervalMin} 分钟保存`);
      else if (rawMa < CFG.minIntervalMin) notes.push(`最大间隔不能小于最小间隔，已按 ${CFG.minIntervalMin} 保存`);
      if (CFG.minIntervalMin < 90) notes.push(`间隔 ${CFG.minIntervalMin} 分钟偏短，风控/封号风险明显升高`);
      // 输入框回填成实际生效值，避免"我填的"和"它存的"不一致
      $("#xr3-mi").value = CFG.minIntervalMin;
      $("#xr3-ma").value = CFG.maxIntervalMin;
      // 静默时段：整点 0~23；开始 = 结束 → 全天运行（不静默）
      const rawNs = parseInt($("#xr3-ns").value, 10);
      const rawNe = parseInt($("#xr3-ne").value, 10);
      const oldNight = nightLabel();
      CFG.nightStart = normHour(rawNs, LIMIT.NIGHT_START);
      CFG.nightEnd = normHour(rawNe, LIMIT.NIGHT_END);
      const badHour = [rawNs, rawNe].some((n) => !Number.isFinite(n) || n < 0 || n > 23);
      if (badHour) notes.push(`静默时段只能填 0~23 的整点，非法值已按默认 ${LIMIT.NIGHT_START}:00-${LIMIT.NIGHT_END}:00 处理`);
      if (CFG.nightStart === CFG.nightEnd) notes.push(`静默开始 = 结束 → 全天运行，不再夜间停抓`);
      $("#xr3-ns").value = CFG.nightStart;
      $("#xr3-ne").value = CFG.nightEnd;
      BATCH.minToFlush = Math.min(200, Math.max(1, parseInt($("#xr3-bt").value, 10) || 30));
      BATCH.maxIntervalMin = Math.min(1440, Math.max(5, parseInt($("#xr3-bi").value, 10) || 90));
      BATCH.maxBuffer = Math.max(BATCH.minToFlush * 2, 300);
      CFG.batch_min = BATCH.minToFlush;
      CFG.batch_interval = BATCH.maxIntervalMin;
      const prevAwake = CFG.keepAwake !== false;
      CFG.keepAwake = !!$("#xr3-ka").checked;
      CFG.autoLead = !!$("#xr3-al").checked;
      if (CFG.keepAwake && !prevAwake) startKeepAwake();
      if (!CFG.keepAwake && prevAwake) log("🔇 后台保活已关闭（不可见标签可能被冻结，建议改用带启动参数的 Chrome）");
      GM_setValue(KEYS.cfg, CFG);
      persist();
      notes.forEach((n) => log("⚠ " + n));

      // 让新间隔立刻生效：已排定的定时器仍按旧间隔在等，这里重排一次，
      // 否则面板"下次运行"不变，会让人以为没保存成功。
      if (!ST.running && !ST.stopped && ST.timer) {
        const nightChanged = oldNight !== nightLabel();
        if (nightChanged && !isNight()) {
          // 原本正卡在静默等待里，改完不该再等 → 尽快开跑（而不是又排 2~4 小时）
          scheduleNext(8000);
          log(`✓ 静默时段 ${oldNight} → ${nightLabel()}，即将开始新一轮`);
        } else {
          scheduleNext(nextDelay());
          log(`✓ 下次运行已按新间隔重排（${CFG.minIntervalMin}~${CFG.maxIntervalMin} 分钟）`);
        }
      } else if (oldNight !== nightLabel()) {
        log(`✓ 静默时段已更新：${oldNight} → ${nightLabel()}`);
      }
      if (notes.length) say(notes.join("；"), false);

      if (!CFG.url || !CFG.token) {
        log("⚠ 地址或 token 为空 → 无法上传");
        close();
        updatePanel();
        return;
      }

      // 立即自检：几秒内给出成败，不必等 1.5~4 小时
      say("正在验证上传通道…");
      const res = await verifyUpload();
      if (res.ok) {
        log("✓ 上传通道验证通过（" + res.msg + "）");
        try { log("  上传目标：" + new URL(CFG.url).host); } catch (e) { log("  上传目标：已配置"); }
        close();
        updatePanel();
        // 首次配置成功 → 立刻验证完整链路，让用户马上看到真实上传结果
        // 注意：可能正有一轮在跑（那轮多半因无配置而失败），
        //      所以用「排一次立即执行」而非直接 loop()，避免与运行中的轮次冲突。
        if (stat.uploads === 0 && !ST.stopped) {
          if (ST.running) {
            log("▶ 首次配置成功，当前轮次结束后立即验证完整链路…");
            // 标记：当前轮次结束后立刻补跑
            ST.runAfterCurrent = true;
          } else {
            log("▶ 首次配置成功，立即跑一轮验证完整链路…");
            if (ST.timer) clearTimeout(ST.timer);
            loop();
          }
        } else {
          setStatus("已配置 · 等待下一轮");
        }
      } else {
        log("✗ 上传通道验证失败：" + res.msg);
        say("✗ " + res.msg + "\n（已保存配置，可点「测试连通性」重试，或修改后再次保存）", false);
        // 不关闭对话框，让用户直接改
      }
    });
  }

  // ────────────────────────────────────────────────────────────
  //  配置自检
  //  保存配置后立即做一次真实的上传探针，几秒内告知成败。
  //  背景：保存后若不主动跑一轮，用户要等 1.5~4 小时才知道能不能上传，
  //        体验上等于「保存后一片空白」。
  //
  //  探针做法：POST /api/exists 带一个不存在的 clue_id
  //  → 走的是与真实上传完全相同的鉴权与网络路径，
  //  → 但不写入任何线索数据（只是查存在性），不会污染数据库
  // ────────────────────────────────────────────────────────────
  async function verifyUpload() {
    if (!CFG.url || !CFG.token) return { ok: false, msg: "未配置地址或 Token" };
    const probeUrl = CFG.url.replace(/\/api\/ingest.*$/, "/api/exists");
    const t0 = now();
    const r = await new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "POST", url: probeUrl,
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + CFG.token },
        data: JSON.stringify({ clue_ids: ["__xr3_probe__"] }),
        timeout: 15000,
        onload: (res) => resolve({ code: res.status, body: res.responseText }),
        onerror: () => resolve({ code: 0, body: "" }),
        ontimeout: () => resolve({ code: 0, body: "" }),
      });
    });
    const ms = now() - t0;
    if (r.code === 200) return { ok: true, msg: `连通正常（${ms}ms）· 地址与 Token 有效`, ms };
    if (r.code === 401) return { ok: false, msg: "Token 不正确（401）· 请检查是否与 INGEST_TOKEN 一致" };
    if (r.code === 404) return { ok: false, msg: "接口不存在（404）· Worker 可能未部署 /api/exists" };
    if (r.code === 403) return { ok: false, msg: "被拒绝（403）· 可能是 WAF 或域名未绑定" };
    if (r.code === 0) return { ok: false, msg: "网络不通 · 检查地址拼写、网络/代理" };
    return { ok: false, msg: `HTTP ${r.code} · ${(r.body || "").slice(0, 80)}` };
  }

  function openAccounts() {
    const l = accounts();
    if (!l.length) {
      const n = prompt("添加账号备注名（登录态仍用当前浏览器；多账号请用不同 Chrome 配置文件）\n如：账号1", "账号1");
      if (n===null || !n.trim()) return;
      GM_setValue(KEYS.accounts, [{name:n.trim(), added:now()}]);
      log("✓ 已添加账号："+n.trim());
      updatePanel(); return;
    }
    const lines = l.map((a,i)=>`${i+1}. ${a.name}`).join("\n");
    const act = prompt("当前账号池：\n"+lines+"\n\n输入序号切换 / +名称 添加 / -序号 删除：", String((GM_getValue(KEYS.idx,0)%l.length)+1));
    if (act===null) return;
    const t = act.trim();
    if (t.startsWith("+")) {
      const n = t.slice(1).trim();
      if (n) { l.push({name:n, added:now()}); GM_setValue(KEYS.accounts,l); log("✓ 已添加："+n); }
    } else if (t.startsWith("-")) {
      const i = parseInt(t.slice(1),10)-1;
      if (i>=0 && i<l.length) { const rm=l.splice(i,1)[0]; GM_setValue(KEYS.accounts,l); GM_setValue(KEYS.idx,0); log("✓ 已删除："+rm.name); }
    } else {
      const i = parseInt(t,10)-1;
      if (i>=0 && i<l.length) { GM_setValue(KEYS.idx,i); log("✓ 当前账号："+l[i].name); }
    }
    updatePanel();
  }

  // ══════════════════════════════════════════════════════════════
  //  后台守护
  // ══════════════════════════════════════════════════════════════
  function startKeepAlive() {
    setInterval(() => {
      // ── 节流/冻结自检：心跳间隔远超设定值，说明 Chrome 在压后台 ──
      const gap = now() - ST.lastTick;
      ST.lastTick = now();
      if (ST.sessionStart && gap > LIMIT.THROTTLE_ALERT) {
        stat.throttled = (stat.throttled || 0) + 1;
        log(`⚠ 检测到浏览器节流/冻结：本次心跳距上次 ${Math.round(gap/60000)} 分钟`
          + `（页面${document.visibilityState === "visible" ? "可见" : "不可见"}）`);
        log("  → 定时器被推迟时会自动补跑；若长期如此，请用带 --disable-background-timer-throttling"
          + " 等参数的 Chrome 启动（见 README「锁屏/远程桌面/后台标签」章节）");
        GM_setValue(KEYS.stat, stat);
      }
      const age = (now()-ST.sessionStart)/60000;
      if (age > LIMIT.SESSION_TTL_MIN && !ST.running) {
        log("🔄 会话运行 "+Math.round(age)+" 分钟，主动刷新续命");
        ST.sessionStart = now();
        setTimeout(()=>{ try { location.reload(); } catch(e){} }, 3000);
      }
      tickLeader();
      updatePanel();
    }, LIMIT.HEARTBEAT*1000);
  }

  // 补跑看门狗：Chrome 会把后台标签的长 setTimeout 推迟（对齐到分钟甚至冻结），
  // 原来的兜底只挂在 visibilitychange 上 —— 但窗口若在另一个虚拟桌面/被远程桌面遮挡，
  // 它**永远不会变回 visible**，兜底就永远不触发。
  // 这里改用短周期轮询 + 绝对时间戳：只要定时器还能跳，就一定能在 1 分钟内发现「超时未跑」。
  function startWatchdog() {
    if (ST.watchdog) return;
    ST.watchdog = setInterval(() => {
      if (ST.stopped || ST.running || !ST.leader || !CFG.autoStart) return;
      if (ST.nextAt && now() >= ST.nextAt + 5000) {
        const late = Math.round((now() - ST.nextAt) / 60000);
        stat.throttled = (stat.throttled || 0) + 1;
        log(`⏱ 定时器被推迟 ${late} 分钟（后台节流），按计划时间补跑`);
        if (ST.timer) clearTimeout(ST.timer);
        loop();
      }
    }, LIMIT.WATCHDOG);
  }

  // 防冻结：Chrome 对「正在播放音频」的标签页豁免 intensive throttling 与 freezing。
  // 播一个 220Hz、增益 0.0008（约 -62dB，听不见）的正弦波，把自己伪装成「有声页面」。
  // 这是页面内唯一能绕过冻结的手段；真正的解法还是浏览器启动参数（见 README）。
  function startKeepAwake() {
    if (!CFG.keepAwake || ST.awake) return;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      const ctx = new AC();
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      g.gain.value = 0.0008;
      osc.frequency.value = 220;
      osc.connect(g); g.connect(ctx.destination); osc.start();
      ST.awake = true;
      const resume = () => {
        if (ctx.state !== "running") {
          ctx.resume().then(() => {
            if (ctx.state === "running" && !ST._awakeLogged) {
              ST._awakeLogged = true;
              log("🔊 后台保活已开启（极低音量音频，用于避免 Chrome 冻结本标签）");
            }
          }).catch(()=>{});
        }
      };
      resume();
      // 自动播放策略：首次用户手势后再尝试一次（多数情况下这次才真正 running）
      const once = () => { resume(); window.removeEventListener("pointerdown", once); window.removeEventListener("keydown", once); };
      window.addEventListener("pointerdown", once);
      window.addEventListener("keydown", once);
    } catch (e) { /* 不支持就算了，不影响主流程 */ }
  }

  // 页面重新可见时的处理
  // 背景：Chrome 会对后台标签页的定时器做节流（甚至冻结），
  //       setTimeout(loop) 可能被推迟很久才触发。
  //       这里用「绝对时间戳 nextAt」判断是否错过了轮次，错过就立即补跑。
  //       —— 这正是无人值守能在「用户切走一整天」后仍继续工作的关键。
  // 互斥：三个触发点（定时器 / visibilitychange / pageshow）都过 loop() 开头的
  //       `if (ST.running) return` 守卫，JS 单线程下不会并发执行。
  function onPageVisible(tag) {
    if (ST.stopped) { updatePanel(); return; }
    if (ST.running) { log(`◉ ${tag}（本轮正在运行中）`); updatePanel(); return; }
    if (ST.nextAt && now() >= ST.nextAt) {
      const late = Math.round((now() - ST.nextAt) / 60000);
      log(`◉ ${tag} · 错过了 ${late} 分钟的轮次，立即补跑`);
      if (ST.timer) clearTimeout(ST.timer);
      loop();
    } else {
      const left = ST.nextAt ? Math.round((ST.nextAt - now()) / 60000) : 0;
      log(`◉ ${tag} · 下一轮还有 ${left} 分钟，正常等待`);
      updatePanel();
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") onPageVisible("页面重新可见");
  });
  window.addEventListener("pageshow", (e) => {
    if (e.persisted) onPageVisible("页面从缓存恢复");
  });
  window.addEventListener("beforeunload", () => {
    persist();
    // 主动交还主控锁：否则别的标签要等 6 分钟 TTL 才敢接管（刷新时尤其明显）
    try { if (leaderNow() === TAB_ID) GM_setValue(NS+"leader", ""); } catch (e) {}
    // 页面关闭前尽量把缓冲发出去（用 sendBeacon 兜底，fetch 可能来不及）
    if (pending.length && CFG.url && CFG.token) {
      try {
        const payload = buildPayload("auto", pending, pending.length);
        navigator.sendBeacon(CFG.url, new Blob([JSON.stringify(payload)], { type: "application/json" }));
      } catch (e) {}
    }
  });

  // 多标签页协调：只让一个主控，避免重复抓取。
  // ⚠ 老实现有两个致命缺陷（锁屏/远程桌面/标签页在后台时会集中爆发）：
  //   ① claimTab() 只在启动时调用一次 —— 一旦当时别的标签是主控，本页就**永远**是观察模式，
  //      哪怕那个主控早就死了，也只能靠手动刷新页面才重试。
  //   ② 锁 TTL 只有 120 秒，而心跳是 setInterval(30s)。后台标签的定时器会被 Chrome 节流到
  //      ≥60 秒一跳（冻结时干脆不跳）→ 主控「心跳超时」被误判成死亡，各标签来回抢锁，
  //      最后谁都抓不上。
  // 修法：TTL 放大到 6 分钟（远大于节流后的心跳间隔），并把抢锁放进心跳里**每轮重试**。
  // TAB_ID 必须**在同一个标签页内跨刷新保持不变**（sessionStorage 正好满足）。
  // 否则 SESSION_TTL 触发的 location.reload() 会让本页变成「全新标签」：
  // 老 TAB_ID 仍占着锁且心跳已停，新页面又认不出那是自己 → 双方僵持到锁过期，白白停 6 分钟。
  let TAB_ID = "";
  try { TAB_ID = sessionStorage.getItem(NS+"tabid") || ""; } catch (e) {}
  if (!TAB_ID) {
    TAB_ID = NS+"tab_"+Math.random().toString(36).slice(2,8);
    try { sessionStorage.setItem(NS+"tabid", TAB_ID); } catch (e) {}
  }
  const leaderNow = () => GM_getValue(NS+"leader","");
  const leaderAge = () => now() - (GM_getValue(NS+"leader_ts",0) || 0);

  function claimTab() {
    const cur = leaderNow(), age = leaderAge();
    if (!cur || cur === TAB_ID || age > LIMIT.LEADER_TTL) {
      if (cur && cur !== TAB_ID) log(`♻ 接管主控（上一个已 ${Math.round(age/60000)} 分钟无心跳）`);
      GM_setValue(NS+"leader",TAB_ID); GM_setValue(NS+"leader_ts",now());
      ST.leader = true;
      return true;
    }
    ST.leader = false;
    return false;
  }

  // 心跳里调用：主控续期；非主控在锁过期后自动接管（解决「启动晚一步就永远观察」）
  function tickLeader() {
    const cur = leaderNow();
    if (cur === TAB_ID) { GM_setValue(NS+"leader_ts",now()); ST.leader = true; return true; }
    // 锁被别的标签拿走了（常见于本页刷新期间被抢，或已另有主控）→ 主动让位，停掉自己的定时器
    if (ST.leader) {
      ST.leader = false;
      if (ST.timer) { clearTimeout(ST.timer); ST.timer = null; }
      log("⏸ 主控已被其他标签接管，本页转为观察模式");
      updatePanel();
      return false;
    }
    if (!CFG.autoLead) return false;
    if (!cur || leaderAge() > LIMIT.LEADER_TTL) {
      if (!claimTab()) return false;
      log("▶ 本页接管主控，开始无人值守运行");
      if (CFG.autoStart && !ST.stopped && !ST.running && !ST.timer) {
        const nr = GM_getValue(NS+"next_run", 0);
        const wait = (nr && nr > now()) ? nr-now() : 8000;
        scheduleNext(wait);
      }
      updatePanel();
      return true;
    }
    ST.leader = false;
    return false;
  }

  // ══════════════════════════════════════════════════════════════
  //  启动
  // ══════════════════════════════════════════════════════════════
  function boot() {
    buildPanel();
    // 登录态必须用接口探针判断（xq_a_token 是 HttpOnly，document.cookie 读不到）
    probeAuth().then((ok) => {
      _loginCache.ok = ok;
      if (!ok) {
        log("⚠ 当前未登录雪球");
        log("  → 请在浏览器里登录雪球（扫码即可），脚本每 15 分钟自动重试，登录后立即恢复");
        setStatus("等待登录");
      } else {
        log("✓ 已登录，探针校验通过");
      }
      updatePanel();
    });
    if (!CFG.url || !CFG.token) log("⚠ 未配置 Worker 地址/token → 点「设置」");

    startKeepAlive();
    startKeepAwake();
    if (CFG.autoLead) startWatchdog();   // 观察模式的标签也挂上看门狗，接管后能立刻补跑

    // 启动时先处理上次残留的缓冲（浏览器上次异常关闭时可能有积压）
    if (pending.length) {
      log(`⏳ 发现上次残留缓冲 ${pending.length} 条，尝试上传`);
      setTimeout(() => flushPending(true), 2500);
    }

    if (claimTab()) {
      log("▶ 本标签页为主控，开始无人值守运行");
      if (CFG.autoStart) {
        const nr = GM_getValue(NS+"next_run", 0);
        const wait = (nr && nr > now()) ? nr-now() : 4000;
        log(wait > 5000 ? "⏰ 距下轮 "+fmtDur(wait) : "⏰ 即将启动首轮");
        setStatus("等待首轮 · "+fmtDur(wait));
        scheduleNext(wait);
      } else {
        log("自动启动已关闭，点「立即跑一轮」手动触发");
        setStatus("就绪");
      }
    } else {
      const mins = Math.round(leaderAge() / 60000);
      log(`ℹ 其他标签页是主控，本页暂为观察模式（避免重复抓取）`);
      log(`  → 该主控若 ${Math.max(1, Math.round((LIMIT.LEADER_TTL - leaderAge())/60000))} 分钟内无心跳，本页会自动接管`
        + `（当前已 ${mins} 分钟无心跳）；想立刻接管：菜单→「强制接管主控」`);
      setStatus("观察模式");
    }
    updatePanel();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  GM_registerMenuCommand("立即跑一轮", ()=>{ if (ST.timer) clearTimeout(ST.timer); loop(); }, "r");
  GM_registerMenuCommand("设置", openConfig, "c");
  // 「关注」流是新增源，接口路径需在真实登录态下验证：这个菜单逐个试候选接口，
  // 打开雪球页面点一下，面板日志里就能看到哪个可用、能取到多少条。
  // 诊断「关注」流：逐个候选接口打印原始结构，一眼看出是接口报错还是字段改版
  async function diagFollow() {
    log("▶ 测试「关注」时间线（逐个候选接口）…");
    let okCount = 0;
    for (const ep of FOLLOW_ENDPOINTS) {
      const r = await xqJson(ep);
      if (!r.ok) { log(`  ✗ ${ep} → ${r.kind}${r.msg ? " · " + r.msg : ""}`); continue; }
      const items = pickList(r.data);
      const drop = { noId:0, noUser:0, noText:0 };
      const posts = items.map((x) => normalizePost(x, drop)).filter(Boolean);
      if (posts.length) okCount++;
      log(`  ${posts.length ? "✓" : "·"} ${ep} → 原始 ${items.length} 条，可用 ${posts.length} 条`
        + (posts.length ? ""
           : `（丢弃 noId=${drop.noId} noUser=${drop.noUser} noText=${drop.noText}）`));
      if (posts.length) log(`     例：${posts[0].screen_name} · ${(posts[0].title || "").slice(0, 30)}`);
      else log(`     结构：${clip(shapeOf(r.data), 400)}`);
    }
    log(okCount ? "  ✓ 关注流可用（自动记住能用的那个接口）"
                : "  ✗ 全部不可用：可能没关注任何人、登录态失效，或接口又改版了");
    return okCount;
  }
  GM_registerMenuCommand("测试「关注」流接口", async () => {
    FOLLOW_STATE.at = 0;                     // 诊断时忽略冷却，强制重测
    const n = await diagFollow();
    if (n) { FOLLOW_STATE.ok = true; FOLLOW_STATE.at = now(); }
  });
  GM_registerMenuCommand("强制接管主控（本页抢锁）", () => {
    GM_setValue(NS+"leader", TAB_ID); GM_setValue(NS+"leader_ts", now());
    ST.leader = true; ST.stopped = false;
    log("♻ 本页已强制接管主控（原先的主控将退为观察模式）");
    if (ST.timer) clearTimeout(ST.timer);
    if (!ST.running) scheduleNext(6000);
    updatePanel();
  }, "L");
  GM_registerMenuCommand("后台保活：开/关", () => {
    CFG.keepAwake = !CFG.keepAwake;
    GM_setValue(KEYS.cfg, CFG);
    if (CFG.keepAwake) { startKeepAwake(); log("🔊 后台保活已开启（极低音量音频，避免 Chrome 冻结本标签）"); }
    else log("🔇 后台保活已关闭（不可见的标签页可能被 Chrome 冻结，建议改用启动参数的 Chrome）");
    updatePanel();
  }, "k");
  GM_registerMenuCommand("重置「关注」冷却", () => {
    FOLLOW_STATE.ok = false; FOLLOW_STATE.at = 0; followEp = "";
    log("♻ 「关注」冷却与已记住的接口已重置，下一轮会重新探测");
  });
  GM_registerMenuCommand("账号池", openAccounts, "a");
  GM_registerMenuCommand("停止/恢复自动抓取", ()=>{
    ST.stopped = !ST.stopped;
    if (ST.timer) { clearTimeout(ST.timer); ST.timer=null; }
    if (ST.stopped) { log("⏸ 已停止"); setStatus("已停止"); }
    else { log("▶ 已恢复"); scheduleNext(nextDelay()); }
    updatePanel();
  }, "s");
  GM_registerMenuCommand("查看统计", ()=>{
    alert("累计统计\n轮次 "+(stat.rounds||0)+"\n评论 "+(stat.comments||0)+"\n候选 "+(stat.clues||0)+
      "\n上传成功 "+(stat.uploads||0)+"\nWAF 命中 "+(stat.waf||0)+"\n账号受限 "+(stat.blocked||0)+
      "\n节流补跑 "+(stat.throttled||0)+"\n失败 "+(stat.fails||0)+"\n\n末次成功："+(stat.lastOk||"无"));
  }, "t");
})();
