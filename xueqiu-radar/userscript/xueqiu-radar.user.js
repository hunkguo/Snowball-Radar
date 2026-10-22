// ==UserScript==
// @name         雪球雷达 · 抓取上传（油猴版）
// @namespace    https://snowball-radar.local/userscript
// @version      0.1.0
// @description  在雪球页面内直接抓取「推荐/热门」评论，JS 版价值打分后上传到你的 Cloudflare Worker（地址与 token 可自定义）。
// @author       hunk / WorkBuddy
// @match        https://xueqiu.com/*
// @match        https://*.xueqiu.com/*
// @connect      *
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_addStyle
// @grant        GM_notification
// @run-at       document-idle
// ==/UserScript==

/* global GM_xmlhttpRequest, GM_setValue, GM_getValue, GM_registerMenuCommand, GM_addStyle, GM_notification */

(function () {
  "use strict";

  // ──────────────────────────────────────────────
  // 配置（GM_setValue 持久化，可在面板/菜单里改）
  // ──────────────────────────────────────────────
  const CFG = {
    url: GM_getValue("xr_upload_url", ""),
    token: GM_getValue("xr_upload_token", ""),
    intervalMin: GM_getValue("xr_interval_min", 0), // 0 = 不自动
    threshold: GM_getValue("xr_threshold", 5),
    maxPosts: GM_getValue("xr_max_posts", 20), // 每个板块最多抓多少帖
    maxCommentPages: GM_getValue("xr_max_comment_pages", 3),
    auto: GM_getValue("xr_auto", false),
  };

  // ──────────────────────────────────────────────
  // 时间工具：epoch 秒 → 北京时间 'YYYY-MM-DD HH:MM:SS'
  // ──────────────────────────────────────────────
  function pad2(n) { return String(n).padStart(2, "0"); }
  function epochToBeijing(sec, withSec) {
    if (!sec || sec <= 0) return "";
    if (sec > 1e12) sec /= 1000; // 毫秒兜底
    if (sec < 1.48e9) return ""; // 早于 2017 视为无效
    const bj = new Date(sec * 1000 + 8 * 3600 * 1000); // +8h 后用 UTC 取值
    const Y = bj.getUTCFullYear();
    const M = pad2(bj.getUTCMonth() + 1);
    const D = pad2(bj.getUTCDate());
    const h = pad2(bj.getUTCHours());
    const mi = pad2(bj.getUTCMinutes());
    const s = pad2(bj.getUTCSeconds());
    return withSec ? `${Y}-${M}-${D} ${h}:${mi}:${s}` : `${Y}-${M}-${D} ${h}:${mi}`;
  }

  // ──────────────────────────────────────────────
  // 文本清洗：去 HTML 标签 + 常见实体 + 折叠空白
  // ──────────────────────────────────────────────
  const ENT = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'", "&nbsp;": " ", "&#39;": "'", "&hellip;": "…" };
  function cleanText(t) {
    if (!t) return "";
    t = String(t).replace(/<[^>]*>/g, " ");
    t = t.replace(/&[a-z]+;|&#\d+;/gi, (m) => ENT[m.toLowerCase()] || m);
    t = t.replace(/\s+/g, " ").trim();
    return t;
  }

  // ──────────────────────────────────────────────
  // 股票名 / 事件词 表
  // ──────────────────────────────────────────────
  const STOCK_NAMES = {
    "澜起科技": "澜起科技", "澜起": "澜起科技",
    "英特尔": "英特尔", "intel": "英特尔", "Intel": "英特尔",
    "海力士": "SK海力士", "SK海力士": "SK海力士",
    "中芯国际": "中芯国际", "中芯": "中芯国际",
    "韦尔股份": "韦尔股份", "韦尔": "韦尔股份",
    "兆易创新": "兆易创新", "兆易": "兆易创新",
    "北方华创": "北方华创", "卓胜微": "卓胜微",
    "圣邦股份": "圣邦股份", "圣邦": "圣邦股份",
    "长电科技": "长电科技", "长电": "长电科技",
    "通富微电": "通富微电", "寒武纪": "寒武纪",
    "沪硅产业": "沪硅产业", "中微公司": "中微公司", "中微": "中微公司",
    "拓荆科技": "拓荆科技", "芯源微": "芯源微", "盛美上海": "盛美上海",
    "华海清科": "华海清科", "长川科技": "长川科技", "安集科技": "安集科技",
    "鼎龙股份": "鼎龙股份", "江丰电子": "江丰电子", "华峰测控": "华峰测控",
    "江波龙": "江波龙", "佰维存储": "佰维存储", "德明利": "德明利",
    "香农芯创": "香农芯创", "雅克科技": "雅克科技",
    "工业富联": "工业富联", "中际旭创": "中际旭创", "旭创": "中际旭创",
    "新易盛": "新易盛", "天孚通信": "天孚通信", "光迅科技": "光迅科技",
    "源杰科技": "源杰科技", "仕佳光子": "仕佳光子", "剑桥科技": "剑桥科技",
    "太辰光": "太辰光", "联特科技": "联特科技", "华工科技": "华工科技",
    "光库科技": "光库科技", "腾景科技": "腾景科技",
    "立讯精密": "立讯精密", "立讯": "立讯精密",
    "歌尔股份": "歌尔股份", "歌尔": "歌尔股份",
    "蓝思科技": "蓝思科技", "传音控股": "传音控股", "安克创新": "安克创新",
    "领益智造": "领益智造", "东山精密": "东山精密", "鹏鼎控股": "鹏鼎控股",
    "京东方": "京东方A", "TCL科技": "TCL科技", "深天马": "深天马A",
    "金山办公": "金山办公", "科大讯飞": "科大讯飞", "讯飞": "科大讯飞",
    "海康威视": "海康威视", "海康": "海康威视", "大华股份": "大华股份",
    "中科曙光": "中科曙光", "曙光": "中科曙光", "浪潮信息": "浪潮信息",
    "紫光股份": "紫光股份", "神州数码": "神州数码", "深信服": "深信服",
    "中兴通讯": "中兴通讯", "紫光国微": "紫光国微", "复旦微电": "复旦微电",
    "中航沈飞": "中航沈飞", "航发动力": "航发动力", "中国船舶": "中国船舶",
    "中航西飞": "中航西飞", "菲利华": "菲利华", "西部超导": "西部超导",
    "比亚迪": "比亚迪", "宁德时代": "宁德时代",
    "亿纬锂能": "亿纬锂能", "国轩高科": "国轩高科", "欣旺达": "欣旺达",
    "恩捷股份": "恩捷股份", "天赐材料": "天赐材料", "璞泰来": "璞泰来",
    "容百科技": "容百科技", "当升科技": "当升科技", "中伟股份": "中伟股份",
    "格林美": "格林美", "华友钴业": "华友钴业", "天齐锂业": "天齐锂业",
    "赣锋锂业": "赣锋锂业", "盐湖股份": "盐湖股份", "永兴材料": "永兴材料",
    "长城汽车": "长城汽车", "长安汽车": "长安汽车", "上汽集团": "上汽集团",
    "广汽集团": "广汽集团", "赛力斯": "赛力斯", "北汽蓝谷": "北汽蓝谷",
    "江淮汽车": "江淮汽车", "宇通客车": "宇通客车",
    "拓普集团": "拓普集团", "三花智控": "三花智控", "伯特利": "伯特利",
    "保隆科技": "保隆科技", "双环传动": "双环传动",
    "隆基绿能": "隆基绿能", "隆基": "隆基绿能",
    "通威股份": "通威股份", "特变电工": "特变电工", "晶澳科技": "晶澳科技",
    "天合光能": "天合光能", "晶科能源": "晶科能源", "钧达股份": "钧达股份",
    "爱旭股份": "爱旭股份", "福斯特": "福斯特", "福莱特": "福莱特",
    "捷佳伟创": "捷佳伟创", "迈为股份": "迈为股份",
    "金风科技": "金风科技", "明阳智能": "明阳智能", "天顺风能": "天顺风能",
    "阳光电源": "阳光电源", "德业股份": "德业股份", "固德威": "固德威",
    "锦浪科技": "锦浪科技", "思源电气": "思源电气", "国电南瑞": "国电南瑞",
    "许继电气": "许继电气",
    "贵州茅台": "贵州茅台", "茅台": "贵州茅台", "五粮液": "五粮液",
    "泸州老窖": "泸州老窖", "老窖": "泸州老窖",
    "山西汾酒": "山西汾酒", "汾酒": "山西汾酒",
    "洋河股份": "洋河股份", "洋河": "洋河股份",
    "今世缘": "今世缘", "古井贡酒": "古井贡酒", "迎驾贡酒": "迎驾贡酒",
    "口子窖": "口子窖", "舍得酒业": "舍得酒业", "酒鬼酒": "酒鬼酒",
    "水井坊": "水井坊", "古越龙山": "古越龙山", "皇台酒业": "皇台酒业",
    "伊利股份": "伊利股份", "海天味业": "海天味业", "千禾味业": "千禾味业",
    "涪陵榨菜": "涪陵榨菜", "双汇发展": "双汇发展", "安琪酵母": "安琪酵母",
    "金龙鱼": "金龙鱼",
    "恒瑞医药": "恒瑞医药", "百济神州": "百济神州",
    "药明康德": "药明康德", "药明": "药明康德", "凯莱英": "凯莱英",
    "泰格医药": "泰格医药", "昭衍新药": "昭衍新药", "康龙化成": "康龙化成",
    "爱尔眼科": "爱尔眼科", "通策医疗": "通策医疗", "迈瑞医疗": "迈瑞医疗",
    "联影医疗": "联影医疗", "片仔癀": "片仔癀", "云南白药": "云南白药",
    "同仁堂": "同仁堂", "东阿阿胶": "东阿阿胶", "义翘神州": "义翘神州",
    "中信证券": "中信证券", "华泰证券": "华泰证券", "国泰海通": "国泰海通",
    "招商证券": "招商证券", "中金公司": "中金公司",
    "东方财富": "东方财富", "同花顺": "同花顺",
    "中国平安": "中国平安", "中国人寿": "中国人寿", "新华保险": "新华保险",
    "招商银行": "招商银行", "兴业银行": "兴业银行", "平安银行": "平安银行",
    "宁波银行": "宁波银行", "杭州银行": "杭州银行", "江苏银行": "江苏银行",
    "成都银行": "成都银行", "工商银行": "工商银行", "建设银行": "建设银行",
    "农业银行": "农业银行",
    "万科A": "万科A", "保利发展": "保利发展", "招商蛇口": "招商蛇口",
    "金地集团": "金地集团", "我爱我家": "我爱我家", "滨江集团": "滨江集团",
    "华发股份": "华发股份",
    "三一重工": "三一重工", "三一": "三一重工",
    "恒立液压": "恒立液压", "徐工机械": "徐工机械", "中联重科": "中联重科",
    "浙江鼎力": "浙江鼎力",
    "中国建筑": "中国建筑", "海螺水泥": "海螺水泥", "海螺": "海螺水泥",
    "东方雨虹": "东方雨虹", "北新建材": "北新建材",
    "万华化学": "万华化学", "华鲁恒升": "华鲁恒升", "宝丰能源": "宝丰能源",
    "荣盛石化": "荣盛石化", "恒力石化": "恒力石化", "卫星化学": "卫星化学",
    "紫金矿业": "紫金矿业", "紫金": "紫金矿业", "洛阳钼业": "洛阳钼业",
    "江西铜业": "江西铜业", "云南铜业": "云南铜业", "中国铝业": "中国铝业",
    "神火股份": "神火股份", "锡业股份": "锡业股份", "厦门钨业": "厦门钨业",
    "北方稀土": "北方稀土", "金力永磁": "金力永磁",
    "中国神华": "中国神华", "神华": "中国神华",
    "陕西煤业": "陕西煤业", "陕煤": "陕西煤业",
    "兖矿能源": "兖矿能源", "山西焦煤": "山西焦煤", "潞安环能": "潞安环能",
    "大有能源": "大有能源", "新赛股份": "新赛股份",
    "牧原股份": "牧原股份", "牧原": "牧原股份",
    "温氏股份": "温氏股份", "温氏": "温氏股份",
    "新希望": "新希望", "海大集团": "海大集团", "大北农": "大北农",
    "美的集团": "美的集团", "美的": "美的集团",
    "格力电器": "格力电器", "格力": "格力电器",
    "海尔智家": "海尔智家", "海尔": "海尔智家",
    "老板电器": "老板电器", "公牛集团": "公牛集团", "苏泊尔": "苏泊尔",
    "九阳股份": "九阳股份",
    "分众传媒": "分众传媒", "三七互娱": "三七互娱", "吉比特": "吉比特",
    "恺英网络": "恺英网络", "巨人网络": "巨人网络",
    "英伟达": "英伟达", "nvidia": "英伟达", "NVIDIA": "英伟达",
    "台积电": "台积电", "tsmc": "台积电", "TSMC": "台积电",
    "苹果": "苹果", "apple": "苹果", "Apple": "苹果", "华为": "华为",
  };

  // 短名误报防护（带前后文排除）：仅「美的」需要
  const STOCK_GUARD = {
    "美的": "(?<!小而)(?<!大而)(?<!完)(?<!物)(美的)(?!好)(?!元)",
  };

  const EVENT_KEYWORDS = {
    "利好": 3, "利空": 3, "传闻": 2, "小道消息": 2, "消息称": 2, "据悉": 2,
    "据传": 2, "听说": 2, "圈内": 2, "渠道": 2, "内部": 2, "独家": 2,
    "合作": 2, "建厂": 2, "扩产": 2, "投产": 2, "量产": 2, "试产": 1,
    "产能": 1, "良率": 1, "订单": 2, "中标": 2, "签约": 2, "供货": 2,
    "定点": 2, "认证": 1, "客户": 1, "并购": 2, "重组": 2, "收购": 1,
    "业绩": 1, "超预期": 2, "不及预期": 2, "预增": 1, "预减": 1, "扭亏": 1,
    "涨价": 2, "降价": 1, "提价": 2, "获批": 2, "临床": 1, "研发": 1,
    "减持": 2, "增持": 2, "回购": 2, "定增": 1, "调研": 1, "交流": 1,
    "机构": 1, "涨停": 2, "跌停": 2, "异动": 1, "突破": 1, "上调": 2, "下调": 2,
    "辟谣": 2, "澄清": 1, "否认": 1,
  };

  // 6 位数字代码白名单
  const SIX_DIGIT_RE = /(?<!\d)\d{6}(?!\d)/g;
  const VALID_CODE_RE = /^(?:60[0135]|68[89]|00[0-3]|30[012]|43|8[2347]|92|399|5[12568]|1[56])/;

  function isMeaningless(text) {
    if (!text) return true;
    const t = text.trim();
    if (!t) return true;
    // 注意：JS 的 \W 不含 Unicode 字母，会误删中文；用 \p{L}\p{N} 保留中英文与数字
    const stripped = t.replace(/[^\p{L}\p{N}]/gu, "");
    if (!stripped) return true;
    if (stripped.length < 2) return true;
    if (new Set(stripped).size === 1 && stripped.length >= 2) return true; // 纯重复（哈哈哈/。。。。）
    if (stripped.length <= 3 && /^\d+$/.test(stripped)) return true;
    const low = t.toLowerCase();
    const MEANINGLESS = new Set([
      "", "顶", "沙发", "板凳", "地板", "前排", "插眼", "马克", "留名",
      "mark", "m", "mm", "dddd", "好的", "嗯", "哦", "啊", "哈", "哈哈",
      "666", "6", "111", "。。", "...", "。。。", "？", "!",
      "支持", "赞", "好", "对", "是", "否", "路过", "围观", "签到",
      "收藏", "关注", "已阅", "呵呵", "啊啊", "学习了", "看不懂", "不明觉厉",
    ]);
    if (MEANINGLESS.has(low)) return true;
    return false;
  }

  // 预编译匹配器（长名优先，避免短名截胡；短名误报用 guard）
  function escapeRegex(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  const _stockRe = (function () {
    const aliases = Object.keys(STOCK_NAMES).sort((a, b) => b.length - a.length);
    const alt = aliases.map((a) => STOCK_GUARD[a] || "(" + escapeRegex(a) + ")");
    return new RegExp(alt.join("|"), "g");
  })();
  const _eventRe = (function () {
    const alt = Object.keys(EVENT_KEYWORDS).map((k) => "(" + escapeRegex(k) + ")");
    return new RegExp(alt.join("|"), "g");
  })();

  function scoreComment(text, like, reply) {
    like = like || 0; reply = reply || 0;
    if (isMeaningless(text)) return { score: -100, tags: [], stocks: [] };
    let s = 0;
    const tags = [];
    const stocks = [];

    // 1) 6 位数字代码
    const allNums = Array.from(new Set(text.match(SIX_DIGIT_RE) || []));
    const codes = allNums.filter((c) => VALID_CODE_RE.test(c) && new Set(c).size > 1);
    if (codes.length) { s += 3; tags.push("code:" + codes.join(",")); }
    const bad = allNums.filter((c) => !codes.includes(c));
    if (bad.length) tags.push("num:" + bad.join(","));

    // 2) 已知标的
    _stockRe.lastIndex = 0;
    let m;
    while ((m = _stockRe.exec(text)) !== null) {
      const tok = m[1] || m[0];
      const v = STOCK_NAMES[tok];
      if (v && !stocks.includes(v)) stocks.push(v);
    }
    if (stocks.length) { s += 2 * stocks.length; tags.push("stock:" + stocks.join(",")); }

    // 3) 事件 / 方向关键词
    _eventRe.lastIndex = 0;
    const ev = [];
    while ((m = _eventRe.exec(text)) !== null) {
      const tok = m[1] || m[0];
      if (!ev.includes(tok)) { ev.push(tok); s += EVENT_KEYWORDS[tok]; }
    }
    if (ev.length) tags.push("event:" + ev.join(","));

    // 4) 信息密度
    if (text.length >= 30) s += 1;
    if (/\d/.test(text) || text.includes("%")) s += 1;

    // 5) 点赞
    if (like >= 50) s += 2; else if (like >= 10) s += 1;

    // 6) 互动（回复）
    if (reply >= 5) { s += 2; tags.push("interact:replies(" + reply + ")"); }
    else if (reply >= 1) { s += 1; tags.push("interact:replies(" + reply + ")"); }

    return { score: s, tags, stocks };
  }

  // ──────────────────────────────────────────────
  // 雪球 API 抓取（同域 fetch，自动带登录 cookie）
  // ──────────────────────────────────────────────
  const WAF_MARKERS = ["安全策略拦截", "投资是一场马拉松", "request has been blocked", "Sorry, your request has been blocked", "页面已升级需要验证"];

  async function xqJson(url) {
    const resp = await fetch(url, {
      credentials: "include",
      headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*" },
    });
    const txt = await resp.text();
    if (!resp.ok) {
      if (WAF_MARKERS.some((k) => txt.includes(k))) return { ok: false, waf: true, data: null };
      return { ok: false, waf: false, data: null };
    }
    try {
      const data = JSON.parse(txt);
      if (data && typeof data === "object") return { ok: true, waf: false, data };
      if (WAF_MARKERS.some((k) => txt.includes(k))) return { ok: false, waf: true, data: null };
      return { ok: false, waf: false, data: null };
    } catch (e) {
      if (WAF_MARKERS.some((k) => txt.includes(k))) return { ok: false, waf: true, data: null };
      return { ok: false, waf: false, data: null };
    }
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function normalizePost(item) {
    // 兼容推荐(list 直接是 post) 与热门(items: data 是 JSON 字符串 / original_status)
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
      description: cleanText(p.description || p.text || ""),
      screen_name: (u.screen_name || p.user_screen_name || ""),
      created_at: p.created_at || 0,
    };
  }

  async function getPosts(section) {
    let url, raw;
    if (section === "recommend") {
      url = "https://xueqiu.com/statuses/fundx/public/list.json?source=fund_public&page=1";
    } else {
      url = "https://xueqiu.com/statuses/hot/listV2.json?since=-1&max_id=-1&size=15";
    }
    const r = await xqJson(url);
    if (!r.ok) return { posts: [], waf: r.waf };
    let items = [];
    if (section === "recommend") items = (r.data && r.data.list) || [];
    else items = (r.data && r.data.items) || [];
    const posts = [];
    for (const it of items) {
      const p = normalizePost(it);
      if (p) posts.push(p);
    }
    return { posts: posts.slice(0, CFG.maxPosts), waf: false };
  }

  async function getComments(postId) {
    const out = [];
    const seen = new Set();
    for (let pg = 1; pg <= CFG.maxCommentPages; pg++) {
      const url = `https://xueqiu.com/statuses/comments.json?id=${postId}&page=${pg}&count=20`;
      const r = await xqJson(url);
      if (!r.ok) { if (r.waf) return { comments: out, waf: true }; break; }
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
      // 该页不足 20 条 → 没有更多
      if (list.length < 20) break;
      await sleep(900);
    }
    return { comments: out, waf: false };
  }

  // ──────────────────────────────────────────────
  // 筛选 + 组装 payload（对齐 Worker /api/ingest 契约）
  // ──────────────────────────────────────────────
  function extractClues(comments, threshold) {
    const cands = [];
    const seen = new Set();
    for (const c of comments) {
      const { score, tags, stocks } = scoreComment(c.text, c.like_count, c.reply_count);
      if (score < threshold) continue;
      if (seen.has(c.id)) continue;
      seen.add(c.id);
      cands.push({
        id: c.id,
        user_name: c.user_name,
        post_author: c.post_author || "",
        time_str: epochToBeijing(c.created_at, false),
        created_at: c.created_at,
        like_count: c.like_count,
        reply_count: c.reply_count,
        text: c.text,
        score,
        tags,
        stocks,
        section: c.section || "",
        post_id: c.post_id || "",
        hashtag: c.hashtag || "",
        jev_value: 0,
        jev_ok: false,
      });
    }
    cands.sort((a, b) => (b.score - a.score) || (b.like_count - a.like_count));
    return cands.slice(0, 80);
  }

  function buildPayload(meta, candidates, totalComments) {
    const ga = epochToBeijing(Math.floor(Date.now() / 1000), true);
    const roundId = ga.replace(/[: ]/g, "-") + "__xr_" + meta.source;
    return {
      round_id: roundId,
      meta: {
        source: meta.source,
        title: "雪球雷达·油猴抓取",
        hashtag: "",
        generated_at: ga,
        total_comments: totalComments,
      },
      candidates,
      comments: [], // Worker 已不再落库原始评论，仅用 total_comments 统计
    };
  }

  // 单请求最多携带候选数（拆分上传，降低单请求 D1 负载 / 规避 Worker CPU 上限）
  const UPLOAD_CHUNK = 200;
  const UPLOAD_RETRIES = 4;
  const UPLOAD_BACKOFF = 1500; // ms，退避基数，实际 = base * 2**attempt

  function _postOnce(payload) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: CFG.url,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Authorization": "Bearer " + CFG.token,
          "Accept": "application/json, text/plain, */*",
        },
        data: JSON.stringify(payload),
        onload: (res) => resolve({ code: res.status, body: res.responseText, net: false }),
        onerror: () => resolve({ code: 0, body: "", net: true }),
        ontimeout: () => resolve({ code: 0, body: "", net: true }),
      });
    });
  }

  // 分片上传：同一 round_id 多次 POST，服务端 INSERT OR REPLACE 幂等累积；
  // 网络错误 / 5xx 指数退避重试；401 直接失败。
  async function upload(payload) {
    if (!CFG.url || !CFG.token) return { ok: false, msg: "未配置上传地址或 token" };
    const candidates = payload.candidates || [];
    const chunks = [];
    for (let i = 0; i < candidates.length; i += UPLOAD_CHUNK) chunks.push(candidates.slice(i, i + UPLOAD_CHUNK));
    if (chunks.length === 0) chunks.push([]);
    let lastCode = 0, lastBody = "", okCount = 0;
    for (let ci = 0; ci < chunks.length; ci++) {
      const cp = Object.assign({}, payload);
      cp.candidates = chunks[ci];
      let done = false;
      for (let attempt = 0; attempt <= UPLOAD_RETRIES && !done; attempt++) {
        const r = await _postOnce(cp);
        lastCode = r.code; lastBody = r.body;
        if (!r.net && r.code === 200) { done = true; okCount++; break; }
        if (!r.net && r.code === 401) return { ok: false, code: 401, msg: "token 鉴权失败" };
        if (!r.net && r.code >= 400 && r.code < 500) return { ok: false, code: r.code, msg: r.body || ("HTTP " + r.code) };
        // 网络错误 / 5xx：退避重试
        if (attempt < UPLOAD_RETRIES) await sleep(UPLOAD_BACKOFF * Math.pow(2, attempt));
      }
      if (!done) return { ok: false, code: lastCode, msg: "分片 " + (ci + 1) + "/" + chunks.length + " 重试耗尽：" + (lastBody || "网络错误") };
      if (ci < chunks.length - 1) await sleep(300);
    }
    return { ok: true, code: lastCode, body: lastBody, chunks: chunks.length };
  }

  // ──────────────────────────────────────────────
  // 一轮抓取主流程
  // ──────────────────────────────────────────────
  async function runOnce() {
    if (state.running) return;
    if (!CFG.url || !CFG.token) {
      log("✗ 未配置上传地址/token，请点「设置」");
      GM_notification({ title: "雪球雷达", text: "请先配置上传地址和 token（面板→设置）", timeout: 5000 });
      return;
    }
    state.running = true;
    setStatus("抓取中…");
    try {
      const allComments = [];
      let waf = false;
      for (const section of ["recommend", "hot"]) {
        log("→ 抓 " + (section === "recommend" ? "推荐" : "热门"));
        const pr = await getPosts(section);
        if (pr.waf) { waf = true; log("  ✗ 命中风控，停止"); break; }
        log("  帖子 " + pr.posts.length + " 条");
        for (const p of pr.posts) {
          const cr = await getComments(p.id);
          if (cr.waf) { waf = true; log("  ✗ 评论命中风控，停止"); break; }
          for (const c of cr.comments) {
            c.section = section;
            c.post_id = p.id;
            c.post_author = p.screen_name;
            allComments.push(c);
          }
          await sleep(1200);
        }
        if (waf) break;
        await sleep(1500);
      }
      if (waf) { setStatus("被风控拦截，稍后再试"); return; }

      log("共 " + allComments.length + " 条评论，打分中…");
      const cands = extractClues(allComments, CFG.threshold);
      log("▲ 候选 " + cands.length + " 条（阈值 " + CFG.threshold + "）");

      const payload = buildPayload({ source: "recommend+hot" }, cands, allComments.length);
      const up = await upload(payload);
      if (up.ok) {
        log("✓ 上传成功（" + (up.code || "") + "）候选 " + cands.length + " 条");
        setStatus("完成 · 候选 " + cands.length + " · " + new Date().toLocaleTimeString("zh-CN"));
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

  // ──────────────────────────────────────────────
  // 浮动面板 UI
  // ──────────────────────────────────────────────
  let state = { running: false };
  let panel, statusEl, logEl;

  GM_addStyle(`
    #xr-panel{position:fixed;left:12px;bottom:12px;z-index:999999;width:300px;
      background:#0f172a;color:#e2e8f0;font:13px/1.5 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
      border:1px solid #334155;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.5);overflow:hidden}
    #xr-panel .xr-h{display:flex;align-items:center;justify-content:space-between;
      padding:8px 12px;background:#1e293b;border-bottom:1px solid #334155;cursor:pointer}
    #xr-panel .xr-h b{color:#38bdf8;font-size:13px}
    #xr-panel .xr-b{padding:10px 12px}
    #xr-panel .xr-status{color:#94a3b8;font-size:12px;margin-bottom:8px;min-height:16px}
    #xr-panel .xr-btns{display:flex;gap:6px;flex-wrap:wrap}
    #xr-panel button{flex:1;background:#273449;color:#e2e8f0;border:1px solid #334155;
      border-radius:8px;padding:6px 8px;cursor:pointer;font-size:12px}
    #xr-panel button:hover{border-color:#38bdf8}
    #xr-panel .xr-log{margin-top:8px;max-height:150px;overflow:auto;font-size:11px;
      color:#94a3b8;background:#0b1220;border-radius:8px;padding:6px 8px;white-space:pre-wrap}
    #xr-panel.xr-min .xr-b{display:none}
  `);

  function buildPanel() {
    if (document.getElementById("xr-panel")) return;
    panel = document.createElement("div");
    panel.id = "xr-panel";
    panel.innerHTML = `
      <div class="xr-h"><b>雪球雷达</b><span style="color:#94a3b8;font-size:12px">▾</span></div>
      <div class="xr-b">
        <div class="xr-status">就绪</div>
        <div class="xr-btns">
          <button data-act="run">抓取并上传</button>
          <button data-act="cfg">设置</button>
          <button data-act="auto">自动：关</button>
        </div>
        <div class="xr-log"></div>
      </div>`;
    document.body.appendChild(panel);
    statusEl = panel.querySelector(".xr-status");
    logEl = panel.querySelector(".xr-log");
    panel.querySelector(".xr-h").addEventListener("click", () => panel.classList.toggle("xr-min"));
    panel.querySelector('[data-act="run"]').addEventListener("click", runOnce);
    panel.querySelector('[data-act="cfg"]').addEventListener("click", openConfig);
    const autoBtn = panel.querySelector('[data-act="auto"]');
    autoBtn.textContent = "自动：" + (CFG.auto ? "开" : "关");
    autoBtn.addEventListener("click", () => {
      CFG.auto = !CFG.auto;
      GM_setValue("xr_auto", CFG.auto);
      autoBtn.textContent = "自动：" + (CFG.auto ? "开" : "关");
      setupAuto();
      log("自动抓取：" + (CFG.auto ? "开（每 " + CFG.intervalMin + " 分钟）" : "关"));
    });
  }

  function setStatus(t) { if (statusEl) statusEl.textContent = t; }
  function log(t) {
    if (!logEl) return;
    const line = "[" + new Date().toLocaleTimeString("zh-CN") + "] " + t;
    logEl.textContent = (logEl.textContent + "\n" + line).split("\n").slice(-20).join("\n");
    logEl.scrollTop = logEl.scrollHeight;
  }

  function openConfig() {
    const url = prompt("Worker 上传地址（/api/ingest 完整 URL）：", CFG.url);
    if (url === null) return;
    const token = prompt("上传 Token（与 Worker INGEST_TOKEN 一致）：", CFG.token);
    if (token === null) return;
    const interval = prompt("自动抓取间隔（分钟，0 = 关闭自动）：", String(CFG.intervalMin));
    const thr = prompt("价值打分阈值（默认 5）：", String(CFG.threshold));
    const mp = prompt("每板块最多抓帖数（默认 20）：", String(CFG.maxPosts));
    const mcp = prompt("每帖最多评论页数（默认 3）：", String(CFG.maxCommentPages));
    CFG.url = (url || "").trim();
    CFG.token = (token || "").trim();
    CFG.intervalMin = Math.max(0, parseInt(interval, 10) || 0);
    CFG.threshold = Math.max(0, parseInt(thr, 10) || 5);
    CFG.maxPosts = Math.max(1, parseInt(mp, 10) || 20);
    CFG.maxCommentPages = Math.max(1, parseInt(mcp, 10) || 3);
    GM_setValue("xr_upload_url", CFG.url);
    GM_setValue("xr_upload_token", CFG.token);
    GM_setValue("xr_interval_min", CFG.intervalMin);
    GM_setValue("xr_threshold", CFG.threshold);
    GM_setValue("xr_max_posts", CFG.maxPosts);
    GM_setValue("xr_max_comment_pages", CFG.maxCommentPages);
    log("配置已保存");
    if (CFG.url && CFG.token) log("地址/Token 已设置，可点「抓取并上传」");
    else log("⚠ 地址或 Token 仍为空");
  }

  // ──────────────────────────────────────────────
  // 自动抓取定时器（每页加载只挂一个，避免堆积）
  // ──────────────────────────────────────────────
  let autoTimer = null;
  function setupAuto() {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
    if (CFG.auto && CFG.intervalMin > 0) {
      autoTimer = setInterval(runOnce, CFG.intervalMin * 60 * 1000);
    }
  }

  // ──────────────────────────────────────────────
  // 启动
  // ──────────────────────────────────────────────
  function boot() {
    buildPanel();
    if (!CFG.url || !CFG.token) {
      setStatus("请先点「设置」填地址/token");
      log("首次使用：点「设置」填写 Worker 上传地址与 Token");
    } else {
      log("已加载配置，点「抓取并上传」开始");
    }
    setupAuto();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // 菜单命令（油猴扩展菜单里也能触发）
  GM_registerMenuCommand("雪球雷达：立即抓取并上传", runOnce, "r");
  GM_registerMenuCommand("雪球雷达：设置", openConfig, "c");
})();
