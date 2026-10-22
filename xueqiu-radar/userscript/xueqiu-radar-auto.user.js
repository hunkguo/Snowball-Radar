// ==UserScript==
// @name         雪球雷达 · 无人值守保号版 v3
// @namespace    https://snowball-radar.local/userscript
// @version      3.0.0
// @description  油猴全自动抓取雪球评论并上传 Worker。打开即后台常驻，无需点击。含实测校准的选择器、账号池轮换、WAF/封号自动退避、跨轮断点续跑。
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
    postsPerRound: 6, commentPages: 2,
    minIntervalMin: 150, maxIntervalMin: 240,
    sections: "recommend,hot",
    autoStart: true, notify: true,
    batch_min: 30, batch_interval: 90,   // 攒批阈值与最长等待
  };
  const LIMIT = {
    MAX_POSTS: 10, MAX_PAGES: 3, MIN_INTERVAL: 90,
    NIGHT_START: 23, NIGHT_END: 7,
    POST_DELAY: [3200, 7800],
    PAGE_DELAY: [2100, 3600],
    SECTION_DELAY: [4000, 8000],
    WAF_BACKOFF: [180, 420],
    BLOCKED_BACKOFF: [720, 1440],
    MAX_EMPTY: 3,
    SESSION_TTL_MIN: 45,
    HEARTBEAT: 60,
  };
  const KEYS = {
    cfg: NS + "cfg", seen: NS + "seen", stat: NS + "stat",
    accounts: NS + "accounts", idx: NS + "acct_idx", nextRun: NS + "next_run",
    pending: NS + "pending", lastFlush: NS + "last_flush",
  };

  function loadCfg() {
    const c = Object.assign({}, DEFAULTS, GM_getValue(KEYS.cfg, {}) || {});
    c.postsPerRound = Math.min(LIMIT.MAX_POSTS, Math.max(1, +c.postsPerRound || 6));
    c.commentPages = Math.min(LIMIT.MAX_PAGES, Math.max(1, +c.commentPages || 2));
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
  function isNight() {
    const h = new Date().getHours();
    return h >= LIMIT.NIGHT_START || h < LIMIT.NIGHT_END;
  }
  function msToNightEnd() {
    const h = new Date().getHours();
    return h >= LIMIT.NIGHT_START ? ((24 - h) + LIMIT.NIGHT_END) * 3600000 : (LIMIT.NIGHT_END - h) * 3600000;
  }
  function fmtDur(ms) {
    if (ms <= 0) return "即将";
    const m = Math.round(ms / 60000);
    if (m < 60) return m + " 分钟";
    return Math.floor(m / 60) + " 小时 " + (m % 60) + " 分";
  }
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
  };
  const STOCK_GUARD = { "美的":"(?<!小而)(?<!大而)(?<!完)(?<!物)(美的)(?!好)(?!元)" };
  const EVENT_KEYWORDS = {
    "利好":3,"利空":3,"传闻":2,"小道消息":2,"消息称":2,"据悉":2,"据传":2,"听说":2,
    "圈内":2,"渠道":2,"内部":2,"独家":2,"合作":2,"建厂":2,"扩产":2,"投产":2,"量产":2,
    "试产":1,"产能":1,"良率":1,"订单":2,"中标":2,"签约":2,"供货":2,"定点":2,
    "认证":1,"客户":1,"并购":2,"重组":2,"收购":1,"业绩":1,"超预期":2,"不及预期":2,
    "预增":1,"预减":1,"扭亏":1,"涨价":2,"降价":1,"提价":2,"获批":2,"临床":1,"研发":1,
    "减持":2,"增持":2,"回购":2,"定增":1,"调研":1,"交流":1,"机构":1,"涨停":2,"跌停":2,
    "异动":1,"突破":1,"上调":2,"下调":2,"辟谣":2,"澄清":1,"否认":2,
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
    if (text.length >= 30) s += 1;
    if (/\d/.test(text) || text.includes("%")) s += 1;
    if (like >= 50) s += 2; else if (like >= 10) s += 1;
    if (reply >= 5) { s += 2; tags.push("interact:replies("+reply+")"); }
    else if (reply >= 1) { s += 1; tags.push("interact:replies("+reply+")"); }
    return { score: s, tags, stocks };
  }

  // ══════════════════════════════════════════════════════════════
  //  持久化
  // ══════════════════════════════════════════════════════════════
  const seenSet = new Set((GM_getValue(KEYS.seen, []) || []).slice(-8000));
  const stat = Object.assign({ rounds:0, comments:0, clues:0, uploads:0, fails:0, empty:0, waf:0, blocked:0, dedup:0, lastOk:"" },
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

  async function xqJson(url) {
    let resp, txt;
    try {
      resp = await fetch(url, {
        credentials: "include",
        headers: { "X-Requested-With":"XMLHttpRequest", "Accept":"application/json, text/plain, */*" },
        cache: "no-store",
      });
      txt = await resp.text();
    } catch (e) { return { ok:false, kind:"network", data:null }; }

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
      if (data && typeof data === "object") return { ok:true, kind:"ok", data };
      return { ok:false, kind:"parse", data:null };
    } catch (e) { return { ok:false, kind:"parse", data:null }; }
  }

  // ══════════════════════════════════════════════════════════════
  //  接口
  // ══════════════════════════════════════════════════════════════
  // 实测：热门列表 items[].id 是序号；真实帖子 id 在 original_status.id 或 data(JSON串)
  function normalizePost(item) {
    let p = item;
    if (item && typeof item.data === "string") {
      try { p = JSON.parse(item.data); } catch (e) { p = item; }
      if (item.original_status && item.original_status.id) p = item.original_status;
    } else if (item && item.original_status && item.original_status.id) {
      p = item.original_status;
    }
    if (!p || !p.id) return null;
    // 必须有真实用户信息，剔除热门榜占位项
    if (!p.user || !p.user.screen_name) return null;
    if (!(p.description || p.title)) return null;
    const u = p.user;
    return { id: String(p.id), title: cleanText(p.title||""), screen_name: u.screen_name, created_at: p.created_at||0 };
  }

  async function getPosts(section) {
    const url = section === "recommend"
      ? "/statuses/fundx/public/list.json?source=fund_public&page=1"
      : "/statuses/hot/listV2.json?since=-1&max_id=-1&size=15";
    const r = await xqJson(url);
    if (!r.ok) return { posts:[], kind:r.kind, msg:r.msg };
    const items = section === "recommend" ? ((r.data&&r.data.list)||[]) : ((r.data&&r.data.items)||[]);
    const posts = [];
    for (const it of items) { const p = normalizePost(it); if (p) posts.push(p); }
    return { posts: posts.slice(0, CFG.postsPerRound), kind:"ok" };
  }

  async function getComments(postId) {
    const out = []; const ids = new Set();
    for (let pg = 1; pg <= CFG.commentPages; pg++) {
      const r = await xqJson(`/statuses/comments.json?id=${postId}&page=${pg}&count=20`);
      if (!r.ok) return { comments: out, kind: r.kind, msg: r.msg };
      const list = (r.data && (r.data.comments || r.data.list)) || [];
      if (!list.length) break;
      for (const c of list) {
        const cid = String(c.id);
        if (ids.has(cid)) continue;
        ids.add(cid);
        const cu = c.user || {};
        out.push({
          id: cid, text: cleanText(c.text || c.description || ""),
          created_at: c.created_at || 0, like_count: c.like_count || 0,
          reply_count: c.reply_count || 0, user_name: cu.screen_name || "",
        });
      }
      if (list.length < 20) break;
      await sleep(rnd(LIMIT.PAGE_DELAY[0], LIMIT.PAGE_DELAY[1]));
    }
    return { comments: out, kind: "ok" };
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
      if (seenSet.has(c.id) || local.has(c.id)) continue;
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
    cands.sort((a,b)=>(b.score-a.score)||(b.like_count-a.like_count));
    return cands.slice(0,100);
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
        bump("uploads");
        return { ok: true, skipped: skipped };
      }

      // 2) 一次性发出（分片仅在极多时才触发）
      //    total_candidates 传原始条数，服务端才能算出真实去重数
      const payload = buildPayload("auto", toSend, originalCount, originalCount);
      const up = await upload(payload);
      if (!up.ok) {
        log("✗ 攒批上传失败：" + (up.msg || up.code));
        bump("fails");
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
      return { ok: true, sent: toSend.length, skipped };
    } finally {
      flushingBatch = false;
    }
  }

  // ══════════════════════════════════════════════════════════════
  //  主调度
  // ══════════════════════════════════════════════════════════════
  const ST = { running:false, stopped:false, timer:null, sessionStart:now(), nextAt:0 };

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
    updatePanel();
  }
  const nextDelay = () => rnd(CFG.minIntervalMin, CFG.maxIntervalMin) * 60000;

  async function loop() {
    if (ST.running || ST.stopped) return;
    ST.running = true;
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
        log(`🌙 夜间停抓（${LIMIT.NIGHT_START}:00-${LIMIT.NIGHT_END}:00），${fmtDur(wait)} 后恢复`);
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
      const sections = (CFG.sections||"recommend,hot").split(",").map(s=>s.trim()).filter(Boolean);
      const all = [];
      let aborted = null;

      for (const section of sections) {
        if (ST.stopped) break;
        log(`→ 抓「${section}」`);
        const pr = await getPosts(section);
        if (pr.kind !== "ok") { aborted = pr; log(`  ✗ 列表失败：${pr.kind}${pr.msg?" ("+pr.msg+")":""}`); break; }
        log(`  有效帖子 ${pr.posts.length} 条`);
        for (const p of pr.posts) {
          if (ST.stopped) break;
          const cr = await getComments(p.id);
          if (cr.kind !== "ok") { aborted = cr; log(`  ✗ 评论失败（${cr.kind}${cr.msg?" "+cr.msg:""}）`); break; }
          for (const c of cr.comments) { c.section=section; c.post_id=p.id; c.post_author=p.screen_name; all.push(c); }
          log(`  · ${p.screen_name} → ${cr.comments.length} 条`);
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

      if (cands.length) {
        // 攒批：候选先入缓冲，跨轮累积到阈值再一次性发出
        // ① 攒够阈值 → 立即发（减少 HTTP 往返与 D1 事务）
        // ② 没攒够 → 保留到下一轮，由 90 分钟超时兜底（防止长期滞留）
        for (const c of cands) pending.push(c);
        log(`▲ 候选 ${cands.length} 条（阈值 ${CFG.threshold}）· 缓冲 ${pending.length} 条`);
        const fr = await flushPending(false);
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
    hintEl.innerHTML =
      `状态 <b>${loginTxt}</b> · 账号 <b>${acct?acct.name:"当前浏览器"}</b>${lst.length>1?"（共"+lst.length+"个轮换）":""}<br>`+
      `轮次 ${stat.rounds||0} · 评论 ${stat.comments||0} · 线索 ${stat.clues||0} · 上传 ${stat.uploads||0}<br>`+
      `缓冲 <b>${pending.length}</b> 条（满 ${BATCH.minToFlush} 即发）· 服务端去重 ${stat.dedup||0} 条<br>`+
      `WAF ${stat.waf||0} · 受限 ${stat.blocked||0} · 失败 ${stat.fails||0}`+(stat.lastOk?" · 末次 "+stat.lastOk:"")+"<br>"+
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
              <div><label>每板块帖数</label><input type="number" id="xr3-pp" min="1" max="10"></div>
              <div><label>每帖页数</label><input type="number" id="xr3-cp" min="1" max="3"></div>
              <div><label>板块</label><input type="text" id="xr3-sec"></div>
              <div><label>最小间隔(分)</label><input type="number" id="xr3-mi" min="90" max="1440"></div>
              <div><label>最大间隔(分)</label><input type="number" id="xr3-ma" min="90" max="1440"></div>
              <div><label>攒批阈值</label><input type="number" id="xr3-bt" min="1" max="200"></div>
              <div><label>攒批等待(分)</label><input type="number" id="xr3-bi" min="5" max="1440"></div>
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
    $("#xr3-pp").value = CFG.postsPerRound;
    $("#xr3-cp").value = CFG.commentPages;
    $("#xr3-sec").value = CFG.sections;
    $("#xr3-mi").value = CFG.minIntervalMin;
    $("#xr3-ma").value = CFG.maxIntervalMin;
    $("#xr3-bt").value = BATCH.minToFlush;
    $("#xr3-bi").value = BATCH.maxIntervalMin;

    const msg = wrap.querySelector(".msg");
    const say = (t, ok) => { msg.textContent = t; msg.style.color = ok === false ? "#f87171" : (ok ? "#4ade80" : "#94a3b8"); };

    // 关闭
    const close = () => wrap.remove();
    wrap.querySelector("[data-x]").addEventListener("click", close);
    wrap.querySelector('[data-t="cancel"]').addEventListener("click", close);
    wrap.querySelector(".mask").addEventListener("click", (e) => { if (e.target === wrap.querySelector(".mask")) close(); });

    // 连通性测试：调用 /api/exists 探针（需要一个真实 clue_id，用不存在的即可）
    wrap.querySelector('[data-t="test"]').addEventListener("click", async () => {
      const u = $("#xr3-url").value.trim();
      const tk = $("#xr3-token").value.trim();
      if (!u || !tk) { say("请先填写地址和 Token", false); return; }
      say("测试中…");
      const probeUrl = u.replace(/\/api\/ingest.*$/, "/api/exists");
      const r = await new Promise((resolve) => {
        GM_xmlhttpRequest({
          method: "POST", url: probeUrl,
          headers: { "Content-Type": "application/json", "Authorization": "Bearer " + tk },
          data: JSON.stringify({ clue_ids: ["__ping__"] }),
          timeout: 15000,
          onload: (res) => resolve({ code: res.status, body: res.responseText }),
          onerror: () => resolve({ code: 0, body: "" }),
          ontimeout: () => resolve({ code: 0, body: "" }),
        });
      });
      if (r.code === 200) {
        let n = "";
        try { n = (JSON.parse(r.body).total != null) ? ("· 库中匹配 " + JSON.parse(r.body).total + " 条") : ""; } catch (e) {}
        say("✓ 连通正常，地址与 Token 有效" + n, true);
      } else if (r.code === 401) {
        say("✗ Token 不正确（401）", false);
      } else if (r.code === 404) {
        say("✗ 接口不存在（404）· Worker 可能未部署新版", false);
      } else if (r.code === 0) {
        say("✗ 网络不通（检查地址/代理/域名）", false);
      } else {
        say("✗ HTTP " + r.code + " " + (r.body || "").slice(0, 60), false);
      }
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
    wrap.querySelector('[data-t="save"]').addEventListener("click", () => {
      CFG.url = $("#xr3-url").value.trim();
      CFG.token = $("#xr3-token").value.trim();
      CFG.threshold = Math.max(0, parseInt($("#xr3-thr").value, 10) || 5);
      CFG.postsPerRound = Math.min(LIMIT.MAX_POSTS, Math.max(1, parseInt($("#xr3-pp").value, 10) || 6));
      CFG.commentPages = Math.min(LIMIT.MAX_PAGES, Math.max(1, parseInt($("#xr3-cp").value, 10) || 2));
      CFG.sections = ($("#xr3-sec").value || "recommend,hot").trim();
      CFG.minIntervalMin = Math.max(LIMIT.MIN_INTERVAL, parseInt($("#xr3-mi").value, 10) || 150);
      CFG.maxIntervalMin = Math.max(CFG.minIntervalMin, parseInt($("#xr3-ma").value, 10) || 240);
      BATCH.minToFlush = Math.min(200, Math.max(1, parseInt($("#xr3-bt").value, 10) || 30));
      BATCH.maxIntervalMin = Math.min(1440, Math.max(5, parseInt($("#xr3-bi").value, 10) || 90));
      BATCH.maxBuffer = Math.max(BATCH.minToFlush * 2, 300);
      CFG.batch_min = BATCH.minToFlush;
      CFG.batch_interval = BATCH.maxIntervalMin;
      GM_setValue(KEYS.cfg, CFG);
      persist();
      log(CFG.url && CFG.token ? "✓ 配置已保存" : "⚠ 地址或 token 为空 → 无法上传");
      if (CFG.url && CFG.token) {
        // 只显示主机名，完整路径不写进日志（避免截图分享时泄露完整地址）
        try { log("  上传目标：" + new URL(CFG.url).host); } catch (e) { log("  上传目标：已配置"); }
      }
      close();
      updatePanel();
    });
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
      const age = (now()-ST.sessionStart)/60000;
      if (age > LIMIT.SESSION_TTL_MIN && !ST.running) {
        log("🔄 会话运行 "+Math.round(age)+" 分钟，主动刷新续命");
        ST.sessionStart = now();
        setTimeout(()=>{ try { location.reload(); } catch(e){} }, 3000);
      }
      updatePanel();
    }, LIMIT.HEARTBEAT*1000);
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
    // 页面关闭前尽量把缓冲发出去（用 sendBeacon 兜底，fetch 可能来不及）
    if (pending.length && CFG.url && CFG.token) {
      try {
        const payload = buildPayload("auto", pending, pending.length);
        navigator.sendBeacon(CFG.url, new Blob([JSON.stringify(payload)], { type: "application/json" }));
      } catch (e) {}
    }
  });

  // 多标签页协调：只让一个主控，避免重复抓取
  const TAB_ID = NS+"tab_"+Math.random().toString(36).slice(2,8);
  function claimTab() {
    const cur = GM_getValue(NS+"leader","");
    const ts = GM_getValue(NS+"leader_ts",0);
    if (!cur || cur === TAB_ID || now()-ts > 120000) { GM_setValue(NS+"leader",TAB_ID); GM_setValue(NS+"leader_ts",now()); return true; }
    return false;
  }
  setInterval(()=>{ if (GM_getValue(NS+"leader","")===TAB_ID) GM_setValue(NS+"leader_ts",now()); }, 30000);

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
      log("ℹ 其他标签页已在运行，本页仅观察（避免重复抓取）");
      setStatus("观察模式");
    }
    updatePanel();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  GM_registerMenuCommand("立即跑一轮", ()=>{ if (ST.timer) clearTimeout(ST.timer); loop(); }, "r");
  GM_registerMenuCommand("设置", openConfig, "c");
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
      "\n失败 "+(stat.fails||0)+"\n\n末次成功："+(stat.lastOk||"无"));
  }, "t");
})();
