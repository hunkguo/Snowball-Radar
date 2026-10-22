// ==UserScript==
// @name         雪球雷达 · 保号抓取版
// @namespace    https://snowball-radar.local/userscript
// @version      0.2.0-safe
// @description  在你自己的 Chrome 里抓雪球评论并上传 Worker。被动模式（DOM 监听，0 请求）+ 主动低频模式（手动触发，小批量）。针对账号封禁做了全面加固。
// @author       hunk / WorkBuddy
// @match        https://xueqiu.com/*
// @match        https://*.xueqiu.com/*
// @connect      *
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @grant        GM_addStyle
// @run-at       document-idle
// @noframes
// ==/UserScript==

/* global GM_xmlhttpRequest, GM_setValue, GM_getValue, GM_registerMenuCommand, GM_notification, GM_addStyle */

(function () {
  "use strict";
  if (window.top !== window.self) return; // 只在主帧跑，避免 iframe 重复注入

  // ══════════════════════════════════════════════════
  // 配置
  // ══════════════════════════════════════════════════
  const DEFAULTS = {
    url: "",                    // Worker /api/ingest 完整地址
    token: "",                  // 与 Worker INGEST_TOKEN 一致
    threshold: 5,               // 价值打分阈值
    mode: "passive",            // passive=被动DOM监听  active=主动低频
    activePosts: 5,             // 主动模式：每板块抓几帖（硬上限 8）
    activePages: 2,             // 主动模式：每帖几页评论（硬上限 3）
    intervalMin: 180,           // 主动模式：自动间隔（硬下限 120）
    auto: false,                // 默认关闭自动
    passiveAutoUpload: true,    // 被动模式攒够一批就自动上传
  };
  const K = {
    url: "xr2_upload_url", token: "xr2_upload_token", threshold: "xr2_threshold",
    mode: "xr2_mode", activePosts: "xr2_active_posts", activePages: "xr2_active_pages",
    intervalMin: "xr2_interval_min", auto: "xr2_auto", passiveAutoUpload: "xr2_passive_auto",
    batch: "xr2_batch", lastActive: "xr2_last_active", seen: "xr2_seen", stat: "xr2_stat",
  };
  const CFG = {};
  for (const k in DEFAULTS) CFG[k] = GM_getValue(K[k] ?? ("xr2_" + k), DEFAULTS[k]);

  // ══════════════════════════════════════════════════
  // 保号硬约束（这些是脚本不可绕过的自��律）
  // ══════════════════════════════════════════════════
  const LIMIT = {
    maxActivePosts: 8,          // 每板块帖子上限
    maxActivePages: 3,          // 每帖评论页上限
    minIntervalMin: 120,        // 自动模式最小间隔
    nightStart: 23,             // 夜间停抓
    nightEnd: 7,
    minDelayMs: 2500,           // 帖子间最小间隔
    maxDelayMs: 6500,           // 帖子间最大间隔
    pageDelayMs: 1800,          // 翻页间隔
    batchUploadSize: 40,        // 被动模式攒够多少条上传一次
  };

  // ══════════════════════════════════════════════════
  // 工具
  // ══════════════════════════════════════════════════
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rand = (a, b) => a + Math.random() * (b - a);
  const pad2 = (n) => String(n).padStart(2, "0");

  function epochToBeijing(sec, withSec) {
    if (!sec || sec <= 0) return "";
    if (sec > 1e12) sec /= 1000;
    if (sec < 1.48e9) return "";
    const d = new Date(sec * 1000 + 8 * 3600 * 1000);
    const p = (n) => pad2(n);
    const base = `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
    return withSec ? base + ":" + p(d.getUTCSeconds()) : base;
  }
  function isNight() {
    const h = new Date().getHours();
    return h >= LIMIT.nightStart || h < LIMIT.nightEnd;
  }

  const ENT = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'", "&nbsp;": " ", "&#39;": "'", "&hellip;": "…" };
  function cleanText(t) {
    if (!t) return "";
    return String(t).replace(/<[^>]*>/g, " ").replace(/&[a-z]+;|&#\d+;/gi, (m) => ENT[m.toLowerCase()] || m).replace(/\s+/g, " ").trim();
  }

  // ══════════════════════════════════════════════════
  // 价值打分（沿用项目规则）
  // ══════════════════════════════════════════════════
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
  const STOCK_GUARD = { "美的": "(?<!小而)(?<!大而)(?<!完)(?<!物)(美的)(?!好)(?!元)" };
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

  const _stockRe = (function () {
    const aliases = Object.keys(STOCK_NAMES).sort((a, b) => b.length - a.length);
    return new RegExp(aliases.map((a) => STOCK_GUARD[a] || "(" + esc(a) + ")").join("|"), "g");
  })();
  const _eventRe = new RegExp(Object.keys(EVENT_KEYWORDS).map((k) => "(" + esc(k) + ")").join("|"), "g");

  function isMeaningless(text) {
    if (!text) return true;
    const t = text.trim();
    if (!t) return true;
    const stripped = t.replace(/[^\p{L}\p{N}]/gu, "");
    if (!stripped || stripped.length < 2) return true;
    if (new Set(stripped).size === 1 && stripped.length >= 2) return true;
    if (stripped.length <= 3 && /^\d+$/.test(stripped)) return true;
    const MEANINGLESS = new Set(["","顶","沙发","板凳","地板","前排","插眼","马克","留名","mark","m","mm",
      "dddd","好的","嗯","哦","啊","哈","哈哈","666","6","111","。。","...","。。。","？","!","支持","赞",
      "好","对","是","否","路过","围观","签到","收藏","关注","已阅","呵呵","啊啊","学习了","看不懂","不明觉厉"]);
    return MEANINGLESS.has(t.toLowerCase());
  }

  function scoreComment(text, like, reply) {
    like = like || 0; reply = reply || 0;
    if (isMeaningless(text)) return { score: -100, tags: [], stocks: [] };
    let s = 0; const tags = []; const stocks = [];
    const allNums = Array.from(new Set(text.match(SIX_DIGIT_RE) || []));
    const codes = allNums.filter((c) => VALID_CODE_RE.test(c) && new Set(c).size > 1);
    if (codes.length) { s += 3; tags.push("code:" + codes.join(",")); }
    const bad = allNums.filter((c) => !codes.includes(c));
    if (bad.length) tags.push("num:" + bad.join(","));
    _stockRe.lastIndex = 0; let m;
    while ((m = _stockRe.exec(text)) !== null) {
      const v = STOCK_NAMES[m[1] || m[0]];
      if (v && !stocks.includes(v)) stocks.push(v);
    }
    if (stocks.length) { s += 2 * stocks.length; tags.push("stock:" + stocks.join(",")); }
    _eventRe.lastIndex = 0; const ev = [];
    while ((m = _eventRe.exec(text)) !== null) {
      const k = m[1] || m[0];
      if (!ev.includes(k)) { ev.push(k); s += EVENT_KEYWORDS[k]; }
    }
    if (ev.length) tags.push("event:" + ev.join(","));
    if (text.length >= 30) s += 1;
    if (/\d/.test(text) || text.includes("%")) s += 1;
    if (like >= 50) s += 2; else if (like >= 10) s += 1;
    if (reply >= 5) { s += 2; tags.push("interact:replies(" + reply + ")"); }
    else if (reply >= 1) { s += 1; tags.push("interact:replies(" + reply + ")"); }
    return { score: s, tags, stocks };
  }

  // ══════════════════════════════════════════════════
  // 登录态检测（关键：没登录就不发任何请求）
  // ══════════════════════════════════════════════════
  function loginState() {
    const c = document.cookie || "";
    return { hasToken: /xq_a_token=/.test(c), hasRToken: /xq_r_token=/.test(c) };
  }
  function ensureLoggedIn() {
    const s = loginState();
    if (s.hasToken) return true;
    log("✗ 未检测到登录态（缺 xq_a_token），本次不发起任何请求");
    log("  → 请先在浏览器正常登录雪球（扫码即可），再回来点抓取");
    setStatus("未登录 · 已跳过");
    return false;
  }

  // ══════════════════════════════════════════════════
  // WAF / 风控识别
  // ══════════════════════════════════════════════════
  const WAF_MARKERS = ["安全策略拦截", "投资是一场马拉松", "request has been blocked",
    "Sorry, your request has been blocked", "页面已升级需要验证", "aliyun_waf_aa",
    "captcha", "验证码", "滑动验证", "账号异常", "已被封禁", "无权访问", "登录已失效",
    "抱歉，您的访问过于频繁"];

  async function xqJson(url) {
    let resp, txt;
    try {
      resp = await fetch(url, {
        credentials: "include",
        headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*" },
      });
      txt = await resp.text();
    } catch (e) {
      return { ok: false, waf: false, blocked: false, data: null };
    }
    // WAF 挑战页特征：返回了 HTML 而不是 JSON
    const looksHtml = /^\s*(<!doctype|<html|<\?xml)/i.test(txt) || /aliyun_waf_aa|_waf_/.test(txt);
    if (looksHtml) return { ok: false, waf: true, blocked: false, data: null };
    if (!resp.ok) return { ok: false, waf: false, blocked: resp.status === 403 || resp.status === 429, data: null };
    try {
      const data = JSON.parse(txt);
      // 业务层封号：返回了 JSON 但带错误码
      if (data && (data.error_code || data.error_description)) {
        const msg = String(data.error_description || data.error_code || "");
        const hit = WAF_MARKERS.some((k) => msg.includes(k));
        return { ok: false, waf: false, blocked: hit, data: null, msg };
      }
      if (data && typeof data === "object") return { ok: true, waf: false, blocked: false, data };
      return { ok: false, waf: false, blocked: false, data: null };
    } catch (e) {
      return { ok: false, waf: false, blocked: false, data: null };
    }
  }

  function normalizePost(item) {
    let p = item;
    if (item && typeof item.data === "string") {
      try { p = JSON.parse(item.data); } catch (e) { p = item; }
      if (item.original_status && item.original_status.id) p = item.original_status;
    } else if (item && item.original_status && item.original_status.id) {
      p = item.original_status;
    }
    if (!p || !p.id) return null;
    if (!(p.description || p.title)) return null;
    const u = p.user || {};
    return {
      id: String(p.id),
      title: cleanText(p.title || ""),
      screen_name: u.screen_name || p.user_screen_name || "",
      created_at: p.created_at || 0,
    };
  }

  async function getPosts(section) {
    const url = section === "recommend"
      ? "https://xueqiu.com/statuses/fundx/public/list.json?source=fund_public&page=1"
      : "https://xueqiu.com/statuses/hot/listV2.json?since=-1&max_id=-1&size=15";
    const r = await xqJson(url);
    if (!r.ok) return { posts: [], waf: r.waf, blocked: r.blocked, msg: r.msg };
    const items = section === "recommend" ? ((r.data && r.data.list) || []) : ((r.data && r.data.items) || []);
    const posts = [];
    for (const it of items) { const p = normalizePost(it); if (p) posts.push(p); }
    const cap = Math.min(CFG.activePosts, LIMIT.maxActivePosts);
    return { posts: posts.slice(0, cap), waf: false, blocked: false };
  }

  async function getComments(postId) {
    const out = []; const seen = new Set();
    const maxPages = Math.min(CFG.activePages, LIMIT.maxActivePages);
    for (let pg = 1; pg <= maxPages; pg++) {
      const url = `https://xueqiu.com/statuses/comments.json?id=${postId}&page=${pg}&count=20`;
      const r = await xqJson(url);
      if (!r.ok) return { comments: out, waf: r.waf, blocked: r.blocked, msg: r.msg };
      const list = (r.data && (r.data.comments || r.data.list)) || [];
      if (!list.length) break;
      for (const c of list) {
        const cid = String(c.id);
        if (seen.has(cid)) continue;
        seen.add(cid);
        const cu = c.user || {};
        out.push({
          id: cid,
          text: cleanText(c.text || c.description || ""),
          created_at: c.created_at || 0,
          like_count: c.like_count || 0,
          reply_count: c.reply_count || 0,
          user_name: cu.screen_name || "",
        });
      }
      if (list.length < 20) break;
      await sleep(LIMIT.pageDelayMs + rand(0, 900));
    }
    return { comments: out, waf: false, blocked: false };
  }

  // ══════════════════════════════════════════════════
  // 筛选 + payload
  // ══════════════════════════════════════════════════
  function extractClues(comments, threshold) {
    const cands = []; const seen = new Set();
    for (const c of comments) {
      const { score, tags, stocks } = scoreComment(c.text, c.like_count, c.reply_count);
      if (score < threshold) continue;
      if (seen.has(c.id)) continue;
      seen.add(c.id);
      cands.push({
        id: c.id, user_name: c.user_name || "", post_author: c.post_author || "",
        time_str: epochToBeijing(c.created_at, false), created_at: c.created_at,
        like_count: c.like_count, reply_count: c.reply_count, text: c.text,
        score, tags, stocks, section: c.section || "", post_id: c.post_id || "",
        hashtag: c.hashtag || "", jev_value: 0, jev_ok: false,
      });
    }
    cands.sort((a, b) => (b.score - a.score) || (b.like_count - a.like_count));
    return cands.slice(0, 80);
  }

  function buildPayload(source, candidates, totalComments) {
    const ga = epochToBeijing(Math.floor(Date.now() / 1000), true);
    return {
      round_id: ga.replace(/[: ]/g, "-") + "__xr2_" + source,
      meta: { source, title: "雪球雷达·油猴抓取 v2", hashtag: "", generated_at: ga, total_comments: totalComments },
      candidates,
      comments: [],
    };
  }

  // ══════════════════════════════════════════════════
  // 上传（复用原分片 + 退避逻辑）
  // ══════════════════════════════════════════════════
  const UPLOAD_CHUNK = 200, UPLOAD_RETRIES = 4, UPLOAD_BACKOFF = 1500;

  function postOnce(payload) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "POST", url: CFG.url,
        headers: { "Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer " + CFG.token, "Accept": "application/json, text/plain, */*" },
        data: JSON.stringify(payload),
        onload: (res) => resolve({ code: res.status, body: res.responseText, net: false }),
        onerror: () => resolve({ code: 0, body: "", net: true }),
        ontimeout: () => resolve({ code: 0, body: "", net: true }),
      });
    });
  }

  async function upload(payload) {
    if (!CFG.url || !CFG.token) return { ok: false, msg: "未配置上传地址或 token" };
    const cands = payload.candidates || [];
    const chunks = [];
    for (let i = 0; i < cands.length; i += UPLOAD_CHUNK) chunks.push(cands.slice(i, i + UPLOAD_CHUNK));
    if (!chunks.length) chunks.push([]);
    let lastCode = 0, lastBody = "", okCount = 0;
    for (let ci = 0; ci < chunks.length; ci++) {
      const cp = Object.assign({}, payload); cp.candidates = chunks[ci];
      let done = false;
      for (let a = 0; a <= UPLOAD_RETRIES && !done; a++) {
        const r = await postOnce(cp);
        lastCode = r.code; lastBody = r.body;
        if (!r.net && r.code === 200) { done = true; okCount++; break; }
        if (!r.net && r.code === 401) return { ok: false, code: 401, msg: "token 鉴权失败" };
        if (!r.net && r.code >= 400 && r.code < 500) return { ok: false, code: r.code, msg: r.body || ("HTTP " + r.code) };
        if (a < UPLOAD_RETRIES) await sleep(UPLOAD_BACKOFF * Math.pow(2, a));
      }
      if (!done) return { ok: false, code: lastCode, msg: `分片 ${ci + 1}/${chunks.length} 重试耗尽：${lastBody || "网络错误"}` };
      if (ci < chunks.length - 1) await sleep(300);
    }
    return { ok: true, code: lastCode, chunks: chunks.length };
  }

  // ══════════════════════════════════════════════════
  // 主动低频模式
  // ══════════════════════════════════════════════════
  const state = { running: false, mode: CFG.mode };

  async function runActive() {
    if (state.running) return;
    if (!CFG.url || !CFG.token) { log("✗ 未配置上传地址/token，请点「设置」"); return; }
    if (!ensureLoggedIn()) return;

    // 保号纪律：最小间隔检查 + 夜间停抓
    const last = GM_getValue(K.lastActive, 0);
    const minGap = LIMIT.minIntervalMin * 60 * 1000;
    if (Date.now() - last < minGap) {
      const waitMin = Math.ceil((minGap - (Date.now() - last)) / 60000);
      log(`⚠ 距上次主动抓取不足 ${LIMIT.minIntervalMin} 分钟，需再等 ${waitMin} 分钟`);
      setStatus("冷却中 · " + waitMin + " 分钟后可抓");
      return;
    }
    if (isNight()) {
      log(`⚠ 夜间停抓时段（${LIMIT.nightStart}:00-${LIMIT.nightEnd}:00），本次跳过`);
      setStatus("夜间停抓 · 已跳过");
      return;
    }

    state.running = true;
    GM_setValue(K.lastActive, Date.now());
    setStatus("主动抓取中…");
    try {
      const all = [];
      let stopped = "";
      for (const section of ["recommend", "hot"]) {
        log("→ 抓 " + (section === "recommend" ? "推荐" : "热门"));
        const pr = await getPosts(section);
        if (pr.waf) { stopped = "命中阿里云 WAF 挑战，已停止（本轮不再请求）"; break; }
        if (pr.blocked) { stopped = "接口返回受限：'" + (pr.msg || "") + "'，请立刻停止并检查账号状态"; break; }
        log(`  帖子 ${pr.posts.length} 条`);
        if (!pr.posts.length && section === "recommend") log("  ⚠ 帖子为 0，可能登录态已失效");
        for (const p of pr.posts) {
          const cr = await getComments(p.id);
          if (cr.waf) { stopped = "评论命中 WAF，已停止"; break; }
          if (cr.blocked) { stopped = "评论接口受限：'" + (cr.msg || "") + "'，请停止操作"; break; }
          for (const c of cr.comments) {
            c.section = section; c.post_id = p.id; c.post_author = p.screen_name;
            all.push(c);
          }
          log(`  · ${p.screen_name || p.id} → ${cr.comments.length} 评论`);
          if (cr.comments.length === 0) log("    ⚠ 该帖 0 评论");
          // 随机长延迟，模拟真人逐帖翻看
          await sleep(rand(LIMIT.minDelayMs, LIMIT.maxDelayMs));
        }
        if (stopped) break;
        await sleep(rand(3000, 6000));
      }
      if (stopped) { log("✗ " + stopped); setStatus(stopped); bumpStat("stopped"); return; }

      log(`共 ${all.length} 条评论，打分中…`);
      const cands = extractClues(all, CFG.threshold);
      log(`▲ 候选 ${cands.length} 条（阈值 ${CFG.threshold}）`);
      const up = await upload(buildPayload("active_" + (CFG.activePosts > 4 ? "big" : "small"), cands, all.length));
      if (up.ok) {
        log(`✓ 上传成功 · 候选 ${cands.length} 条`);
        setStatus("完成 · 候选 " + cands.length + " · " + new Date().toLocaleTimeString("zh-CN"));
        bumpStat("active");
      } else {
        log("✗ 上传失败：" + (up.msg || up.code || "未知"));
        setStatus("上传失败：" + (up.msg || up.code || ""));
      }
    } catch (e) {
      log("✗ 异常：" + (e && e.message ? e.message : e));
      setStatus("异常：" + (e && e.message ? e.message : e));
    } finally {
      state.running = false;
    }
  }

  // ══════════════════════════════════════════════════
  // 被动模式：监听 DOM，0 请求
  // ══════════════════════════════════════════════════
  // 雪球评论区 DOM 结构（多选择器兼容不同版本）
  const SEL = {
    postItem: "article.timeline__item, .timeline__item",
    postLink: "a[href*='/u/'], h1 a, .timeline__item a[href*='/']",
    commentItem: ".comment__item, .comment-item, article.comment, .comment",
    commentText: ".comment__content, .comment-item__content, .comment__main, .detail",
    author: ".name a, .comment__name a, .user-name, .name",
    like: ".like, .like__count, .toolbar__like",
    reply: ".reply__btn, .reply-count, .toolbar__reply",
    time: ".time, .comment__time, .date",
  };

  const seenIds = new Set((GM_getValue(K.seen, []) || []).slice(-4000));
  let batch = (GM_getValue(K.batch, []) || []);
  let flushTimer = null;

  function saveSeen() { GM_setValue(K.seen, Array.from(seenIds).slice(-4000)); }

  function postIdFromPage() {
    const m = location.pathname.match(/\/(\d{10,})\/?/);
    if (m) return m[1];
    const a = document.querySelector("a[href*='xueqiu.com/']");
    return null;
  }
  function currentPostAuthor() {
    const el = document.querySelector(".timeline__item .name a, .detail__author a, h1 a");
    return el ? cleanText(el.textContent) : "";
  }

  function harvestComments(root) {
    if (state.mode !== "passive") return;
    const nodes = (root || document).querySelectorAll(SEL.commentItem);
    if (!nodes.length) return;
    const author = currentPostAuthor();
    const postId = postIdFromPage() || "";
    let added = 0;

    for (const n of nodes) {
      const textEl = n.querySelector(SEL.commentText);
      if (!textEl) continue;
      const text = cleanText(textEl.textContent);
      if (!text || isMeaningless(text)) continue;

      // 用文本+作者+时间的组合做指纹去重（被动模式下拿不到 comment id）
      const auEl = n.querySelector(SEL.author);
      const tmEl = n.querySelector(SEL.time);
      const lkEl = n.querySelector(SEL.like);
      const rpEl = n.querySelector(SEL.reply);
      const fp = [cleanText(auEl ? auEl.textContent : ""), text.slice(0, 60), cleanText(tmEl ? tmEl.textContent : "")].join("|");
      const fid = "d" + hash(fp);
      if (seenIds.has(fid)) continue;

      seenIds.add(fid);
      batch.push({
        id: fid,
        text,
        user_name: cleanText(auEl ? auEl.textContent : ""),
        post_author: author,
        post_id: postId,
        section: "passive",
        like_count: parseInt(cleanText(lkEl ? lkEl.textContent : "0").replace(/\D/g, ""), 10) || 0,
        reply_count: parseInt(cleanText(rpEl ? rpEl.textContent : "0").replace(/\D/g, ""), 10) || 0,
        created_at: 0,
      });
      added++;
    }
    if (added) {
      log(`◎ 被动采集 +${added}（缓冲区 ${batch.length}/${LIMIT.batchUploadSize}）`);
      saveSeen();
      scheduleFlush();
    }
  }

  function hash(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return Math.abs(h).toString(36);
  }

  function scheduleFlush() {
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = setTimeout(() => { flushBatch(); }, 1500);
  }

  let flushing = false;
  async function flushBatch(force) {
    if (flushing) return;
    if (!batch.length) return;
    if (!force && batch.length < LIMIT.batchUploadSize) return;
    if (!CFG.url || !CFG.token) return;
    flushing = true;
    const items = batch.slice();
    try {
      const cands = extractClues(items, CFG.threshold);
      if (!cands.length) { batch = []; GM_setValue(K.batch, batch); log("· 缓冲区已清理（本批无达标候选）"); return; }
      const up = await upload(buildPayload("passive", cands, items.length));
      if (up.ok) {
        // 只清掉已上传成功的
        const sentIds = new Set(cands.map((c) => c.id));
        batch = batch.filter((b) => !sentIds.has(b.id));
        GM_setValue(K.batch, batch);
        log(`✓ 被动上传成功 · 候选 ${cands.length} 条`);
        bumpStat("passive");
        if (batch.length) scheduleFlush();
      } else {
        log("✗ 被动上传失败：" + (up.msg || up.code || "") + "（数据已保留）");
      }
    } catch (e) {
      log("✗ 被动上传异常：" + (e && e.message ? e.message : e));
    } finally {
      flushing = false;
    }
  }

  function installPassiveHooks() {
    if (state.mode !== "passive") return;
    harvestComments(document);
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.matches && n.matches(SEL.commentItem)) harvestComments(n.parentElement || document);
          else harvestComments(n);
        }
      }
    });
    mo.observe(document.body, { childList: true, subtree: true });
    log("◎ 被动监听已开启：你正常翻帖子即可，评论会自动收集");
    scheduleFlush();
  }

  // ══════════════════════════════════════════════════
  // 统计
  // ══════════════════════════════════════════════════
  function bumpStat(k) {
    const s = GM_getValue(K.stat, {}) || {};
    s[k] = (s[k] || 0) + 1;
    s.last = new Date().toLocaleString("zh-CN");
    GM_setValue(K.stat, s);
  }
  function readStat() { return GM_getValue(K.stat, {}) || {}; }

  // ══════════════════════════════════════════════════
  // UI
  // ══════════════════════════════════════════════════
  let panel, statusEl, logEl;

  GM_addStyle(`
    #xr2{position:fixed;left:12px;bottom:12px;z-index:2147483647;width:312px;
      background:#0f172a;color:#e2e8f0;font:13px/1.5 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
      border:1px solid #334155;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.5);overflow:hidden}
    #xr2 .h{display:flex;align-items:center;justify-content:space-between;
      padding:8px 12px;background:#1e293b;border-bottom:1px solid #334155;cursor:pointer}
    #xr2 .h b{color:#38bdf8;font-size:13px}
    #xr2 .bd{padding:10px 12px}
    #xr2 .st{color:#94a3b8;font-size:12px;margin-bottom:8px;min-height:16px}
    #xr2 .bar{height:4px;background:#1e293b;border-radius:3px;margin-bottom:9px;overflow:hidden}
    #xr2 .bar i{display:block;height:100%;background:#38bdf8;width:0;border-radius:3px;transition:width .3s}
    #xr2 .btns{display:flex;gap:6px;flex-wrap:wrap}
    #xr2 button{flex:1;background:#273449;color:#e2e8f0;border:1px solid #334155;
      border-radius:8px;padding:6px 8px;cursor:pointer;font-size:12px;min-width:88px}
    #xr2 button:hover{border-color:#38bdf8}
    #xr2 button.pri{background:#0e7490;border-color:#0e7490}
    #xr2 .hint{color:#64748b;font-size:11px;margin-top:8px;line-height:1.6}
    #xr2 .log{margin-top:8px;max-height:158px;overflow:auto;font-size:11px;
      color:#94a3b8;background:#0b1220;border-radius:8px;padding:6px 8px;white-space:pre-wrap}
    #xr2.min .bd{display:none}
  `);

  function setStatus(t) { if (statusEl) statusEl.textContent = t; }
  function setBar(pct) { const i = panel && panel.querySelector(".bar i"); if (i) i.style.width = Math.max(0, Math.min(100, pct)) + "%"; }
  function log(t) {
    if (!logEl) return;
    const line = "[" + new Date().toLocaleTimeString("zh-CN") + "] " + t;
    logEl.textContent = (logEl.textContent + "\n" + line).split("\n").slice(-30).join("\n");
    logEl.scrollTop = logEl.scrollHeight;
  }

  function buildPanel() {
    if (document.getElementById("xr2")) return;
    panel = document.createElement("div");
    panel.id = "xr2";
    panel.innerHTML = `
      <div class="h"><b>雪球雷达 v2 · 保号版</b><span style="color:#94a3b8;font-size:12px">▾</span></div>
      <div class="bd">
        <div class="st">就绪</div>
        <div class="bar"><i></i></div>
        <div class="btns">
          <button class="pri" data-a="run">主动抓一轮</button>
          <button data-a="flush">立即上传缓冲</button>
          <button data-a="cfg">设置</button>
        </div>
        <div class="hint"></div>
        <div class="log"></div>
      </div>`;
    document.body.appendChild(panel);
    statusEl = panel.querySelector(".st");
    logEl = panel.querySelector(".log");
    panel.querySelector(".h").addEventListener("click", () => panel.classList.toggle("min"));
    panel.querySelector('[data-a="run"]').addEventListener("click", runActive);
    panel.querySelector('[data-a="flush"]').addEventListener("click", () => { flushBatch(true); setBar(100); });
    panel.querySelector('[data-a="cfg"]').addEventListener("click", openConfig);
    updateHint();
  }

  function updateHint() {
    const h = panel && panel.querySelector(".hint");
    if (!h) return;
    const s = readStat();
    const login = loginState().hasToken ? "已登录" : "未登录";
    h.innerHTML = `模式：<b>${state.mode === "passive" ? "被动·DOM监听（0 请求）" : "主动·低频"}</b> · ${login}<br>`
      + `主动上限 ${LIMIT.maxActivePosts} 帖 / ${LIMIT.maxActivePages} 页 · 间隔 ≥${LIMIT.minIntervalMin} 分钟 · 夜间停抓<br>`
      + `缓冲区 ${batch.length} 条 · 累计 主动${s.active || 0} / 被动${s.passive || 0} 次${s.last ? " · " + s.last : ""}`;
  }

  function openConfig() {
    const url = prompt("Worker 上传地址（/api/ingest 完整 URL）：", CFG.url);
    if (url === null) return;
    const token = prompt("上传 Token（与 Worker INGEST_TOKEN 一致）：", CFG.token);
    if (token === null) return;
    const mode = prompt("模式：passive=被动DOM监听（推荐，0请求） / active=主动低频\n当前：" + CFG.mode, CFG.mode);
    if (mode === null) return;
    const thr = prompt("价值打分阈值（默认 5）：", String(CFG.threshold));
    const ap = prompt("主动模式·每板块抓几帖（1-8）：", String(CFG.activePosts));
    const ac = prompt("主动模式·每帖几页评论（1-3）：", String(CFG.activePages));
    const im = prompt("主动模式·自动间隔分钟（≥120，建议 180）：", String(CFG.intervalMin));

    CFG.url = (url || "").trim();
    CFG.token = (token || "").trim();
    CFG.mode = (mode || "passive").trim().toLowerCase() === "active" ? "active" : "passive";
    CFG.threshold = Math.max(0, parseInt(thr, 10) || 5);
    CFG.activePosts = Math.min(LIMIT.maxActivePosts, Math.max(1, parseInt(ap, 10) || 5));
    CFG.activePages = Math.min(LIMIT.maxActivePages, Math.max(1, parseInt(ac, 10) || 2));
    CFG.intervalMin = Math.max(LIMIT.minIntervalMin, parseInt(im, 10) || 180);

    GM_setValue(K.url, CFG.url); GM_setValue(K.token, CFG.token);
    GM_setValue(K.mode, CFG.mode); GM_setValue(K.threshold, CFG.threshold);
    GM_setValue(K.activePosts, CFG.activePosts); GM_setValue(K.activePages, CFG.activePages);
    GM_setValue(K.intervalMin, CFG.intervalMin);

    state.mode = CFG.mode;
    log("配置已保存（当前：" + (CFG.mode === "passive" ? "被动" : "主动") + "模式）");
    if (state.mode === "passive") { installPassiveHooks(); harvestComments(document); }
    updateHint();
    if (CFG.url && CFG.token) log("✓ 地址/token 已配置");
    else log("⚠ 地址或 token 仍为空，无法上传");
  }

  // 自动定时（仅主动模式，且强制最小间隔）
  let autoTimer = null;
  function setupAuto() {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
    if (CFG.auto && CFG.mode === "active" && CFG.intervalMin >= LIMIT.minIntervalMin) {
      autoTimer = setInterval(() => { if (!isNight()) runActive(); else log("· 夜间，自动任务已跳过"); }, CFG.intervalMin * 60 * 1000);
      log(`自动任务已启动：每 ${CFG.intervalMin} 分钟（夜间跳过）`);
    }
  }

  // ══════════════════════════════════════════════════
  // 启动
  // ══════════════════════════════════════════════════
  function boot() {
    state.mode = CFG.mode === "active" ? "active" : "passive";
    buildPanel();
    setBar(state.mode === "passive" ? 100 : 0);

    const ls = loginState();
    if (!ls.hasToken) {
      setStatus("未登录 · 被动监听仍可用");
      log("⚠ 未检测到 xq_a_token");
      log("  被动模式不依赖接口，仍可工作（DOM 监听）");
      log("  如需主动抓取，请先正常登录雪球");
    } else {
      setStatus("已登录 · " + (state.mode === "passive" ? "被动监听中" : "就绪"));
      log("✓ 已加载配置，登录态正常");
    }
    if (!CFG.url || !CFG.token) log("⚠ 未配置上传地址/token → 点「设置」填写（否则只采集不上传）");

    if (state.mode === "passive") installPassiveHooks();
    else log("主动模式：点「主动抓一轮」手动触发（每轮请求数很低）");

    // SPA 路由切换
    let lastUrl = location.href;
    setInterval(() => {
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        log("→ 页面切换，重新扫描评论");
        setTimeout(() => harvestComments(document), 1200);
        setTimeout(() => harvestComments(document), 3500);
      }
    }, 1000);

    setupAuto();
    updateHint();
    window.addEventListener("beforeunload", () => { try { GM_setValue(K.batch, batch); saveSeen(); } catch (e) {} });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  GM_registerMenuCommand("主动抓一轮（低频）", runActive, "r");
  GM_registerMenuCommand("立即上传缓冲区", () => flushBatch(true), "u");
  GM_registerMenuCommand("设置", openConfig, "c");
})();