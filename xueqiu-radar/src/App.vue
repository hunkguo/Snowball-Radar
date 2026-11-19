<script setup>
import { ref, onMounted, onUnmounted, computed, nextTick } from "vue";
import {
  fetchRounds, fetchCluesPage, markRead, unmarkRead, clearAllReads, clearReadsSince,
} from "./api.js";

const rounds = ref([]);
const clues = ref([]);
const loading = ref(false);
const error = ref("");

// ── 已读进度（跨设备：状态存服务端 D1，按 clue_id 共享）──────────
// 两个开关是「本机阅读偏好」，存 localStorage；已读本身不存本地。
const PREFS_KEY = "xr3_read_prefs";
function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch { return {}; }
}
const savedPrefs = loadPrefs();
const hideRead = ref(savedPrefs.hideRead !== false);   // 默认隐藏已读
const autoMark = ref(savedPrefs.autoMark !== false);   // 默认滚动自动已读
const readTotal = ref(0);                              // 服务端已读总数
const CLEAR_DAYS = [1, 3, 5, 7];
const clearDays = ref(CLEAR_DAYS.includes(savedPrefs.clearDays) ? savedPrefs.clearDays : 3); // 清除范围（近 N 天资讯）
function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      hideRead: hideRead.value, autoMark: autoMark.value, clearDays: clearDays.value,
    }));
  } catch { /* 隐私模式下忽略 */ }
}

// 分页状态（整页替换模式：页码 + 上一页/下一页 + 首页/末页 + 跳页）
const total = ref(0);
const page = ref(1);
const PAGE = 50;
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE)));
// 页码序列：以当前页为中心取 7 个连续页码（贴边时自动补齐，始终满 7 个），
// 首末页常驻，缺口用 … 省略。68 页时首页显示 1 2 3 4 5 6 7 … 68，一眼可选。
const PAGE_WINDOW = 7;
const pageList = computed(() => {
  const tp = totalPages.value;
  const cur = Math.min(Math.max(1, page.value), tp);
  const half = Math.floor(PAGE_WINDOW / 2);
  let end = Math.min(tp, Math.max(1, cur - half) + PAGE_WINDOW - 1);
  let start = Math.max(1, end - PAGE_WINDOW + 1);
  end = Math.min(tp, start + PAGE_WINDOW - 1);
  const out = [];
  if (start > 1) {
    out.push(1);
    if (start > 2) out.push("…");
  }
  for (let p = start; p <= end; p++) out.push(p);
  if (end < tp) {
    if (end < tp - 1) out.push("…");
    out.push(tp);
  }
  return out;
});
// 跳页输入框
const jumpTo = ref("");

// 筛选（全部在服务端执行）
const stockFilter = ref("");
const minScore = ref(0);
const search = ref("");
const asc = ref(false); // false = 最新在前（倒序时间线）
const sort = ref("ts"); // "ts"=按时间(默认) | "jev"=按 Jev 价值分降序
// 板块筛选：""=全部 | "recommend"=推荐 | "hot"=热门 | "follow"=关注 | "topic"=热点话题
const sectionFilter = ref("");
const SECTION_TABS = [
  { key: "", label: "全部" },
  { key: "recommend", label: "推荐" },
  { key: "hot", label: "热门" },
  { key: "follow", label: "关注" },
  { key: "topic", label: "热点话题" },
];

// ── URL 状态（刷新/分享/前进后退都能定位到同一页）──────────────
// 所有视图状态写进 query string：?page=3&section=hot&q=xx&stock=xx&min=7&sort=jev&order=asc
// 为什么不用「/p/3」这种伪静态路径：本站是 SPA + 静态资源，路径要靠 fallback 兜底，
// assets 路由一变就 404；query 参数零依赖，也不会被 CDN 当静态文件缓存。
const VALID_SECTIONS = ["", "recommend", "hot", "follow", "topic"];
const VALID_MINS = [0, 7, 12, 15];

function readUrlState() {
  const sp = new URLSearchParams(location.search);
  const sec = sp.get("section") || "";
  sectionFilter.value = VALID_SECTIONS.includes(sec) ? sec : "";
  search.value = sp.get("q") || "";
  stockFilter.value = sp.get("stock") || "";
  const min = parseInt(sp.get("min") || "", 10);
  minScore.value = VALID_MINS.includes(min) ? min : 0;
  sort.value = sp.get("sort") === "jev" ? "jev" : "ts";
  asc.value = sp.get("order") === "asc";
  const p = parseInt(sp.get("page") || "", 10);
  page.value = Number.isFinite(p) && p > 0 ? p : 1;
}

function writeUrlState(mode = "replace") {
  const sp = new URLSearchParams();
  if (page.value > 1) sp.set("page", String(page.value));
  if (sectionFilter.value) sp.set("section", sectionFilter.value);
  if (search.value.trim()) sp.set("q", search.value.trim());
  if (stockFilter.value.trim()) sp.set("stock", stockFilter.value.trim());
  if (minScore.value) sp.set("min", String(minScore.value));
  if (sort.value !== "ts") sp.set("sort", sort.value);
  if (asc.value) sp.set("order", "asc");
  const qs = sp.toString();
  const next = location.pathname + (qs ? "?" + qs : "");
  if (next === location.pathname + location.search) return;
  // 翻页用 push（可后退），改筛选/输入用 replace（不刷屏历史记录）
  try { history[mode + "State"]({ page: page.value }, "", next); } catch (e) { /* file:// 等场景忽略 */ }
}

function onPopState() {
  readUrlState();
  loadPage(true);
}

onMounted(async () => {
  readUrlState();
  await loadPage(true);
  await clampPage();
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("popstate", onPopState);
  onScroll();
});
onUnmounted(() => {
  window.removeEventListener("scroll", onScroll);
  window.removeEventListener("popstate", onPopState);
  clearTimeout(searchTimer);
  teardownObserver();
  flushPendingReads(); // 离开前把还没提交的已读补发掉
});

// 返回顶部浮动按钮：滚动超过阈值才显示
const showToTop = ref(false);
function onScroll() {
  showToTop.value = (window.scrollY || window.pageYOffset || 0) > 400;
}
function scrollToTop() {
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const currentParams = computed(() => ({
  stock: stockFilter.value.trim(),
  q: search.value.trim(),
  min: minScore.value || 0,
  order: asc.value ? "asc" : "desc",
  sort: sort.value,
  section: sectionFilter.value,
  unread: hideRead.value,   // 隐藏已读 → 服务端过滤，分页计数也跟着变
  limit: PAGE,
}));

// 拉取当前页（默认不重复拉 rounds；首屏/刷新时才需要）
async function loadPage(withRounds = false) {
  loading.value = true;
  error.value = "";
  try {
    const params = { ...currentParams.value, offset: (page.value - 1) * PAGE };
    if (withRounds) {
      const [r, pg] = await Promise.all([fetchRounds(), fetchCluesPage(params)]);
      rounds.value = r || [];
      clues.value = pg.clues || [];
      total.value = pg.total || 0;
      readTotal.value = pg.read_total || readTotal.value;
    } else {
      const pg = await fetchCluesPage(params);
      clues.value = pg.clues || [];
      total.value = pg.total || 0;
      readTotal.value = pg.read_total || readTotal.value;
    }
    // 每次换页后重建滚动观察器：新卡片进入视野停留 2 秒即自动标记已读
    await nextTick();
    setupReadObserver();
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}

// ── 滚动自动已读 ──────────────────────────────────────────────
// 关键点：标记只改前端样式 + 攒批发服务端，绝不重新拉列表 ——
// 否则滚动过程中列表突然缩短会让页面跳动、甚至找不到刚才看到的位置。
let readObserver = null;
const readTimers = new Map();     // clue_id -> setTimeout（停留计时）
const pendingReads = new Set();   // 攒批待提交
let flushTimer = null;
const DWELL_MS = 2000;            // 停留 2 秒才算读过（快速划过不算）

function setupReadObserver() {
  if (readObserver) { readObserver.disconnect(); readObserver = null; }
  for (const t of readTimers.values()) clearTimeout(t);
  readTimers.clear();
  if (!autoMark.value) return;
  if (typeof IntersectionObserver === "undefined") return;
  readObserver = new IntersectionObserver((entries) => {
    for (const en of entries) {
      const el = en.target;
      const cid = el.getAttribute("data-cid");
      if (!cid || el.dataset.read === "1") continue;
      if (en.isIntersecting) {
        if (readTimers.has(cid)) continue;
        readTimers.set(cid, setTimeout(() => {
          readTimers.delete(cid);
          markOneLocally(cid);
        }, DWELL_MS));
      } else {
        const t = readTimers.get(cid);
        if (t) { clearTimeout(t); readTimers.delete(cid); }
      }
    }
  }, { threshold: 0.55 });
  document.querySelectorAll(".clue[data-cid]").forEach((el) => readObserver.observe(el));
}
function teardownObserver() {
  if (readObserver) { readObserver.disconnect(); readObserver = null; }
  for (const t of readTimers.values()) clearTimeout(t);
  readTimers.clear();
}

// 前端立即灰显 + 攒批，900ms 内的多次标记合并成一次请求
function markOneLocally(cid) {
  const c = clues.value.find((x) => String(x.clue_id) === cid);
  if (!c || c.is_read) return;
  c.is_read = 1;
  const el = document.querySelector(`.clue[data-cid="${cid}"]`);
  if (el) el.dataset.read = "1";
  pendingReads.add(cid);
  readTotal.value++;
  if (!flushTimer) flushTimer = setTimeout(flushPendingReads, 900);
}
async function flushPendingReads() {
  if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
  if (!pendingReads.size) return;
  const ids = [...pendingReads];
  pendingReads.clear();
  try {
    await markRead(ids);
  } catch (e) {
    // 标记失败不打断阅读；下次加载会从服务端重新取回真实状态
    console.warn("[已读] 批量标记失败", e);
  }
}

// 单条：点「已读 / 未读」按钮
async function toggleOne(c) {
  const cid = String(c.clue_id);
  const toRead = !c.is_read;
  c.is_read = toRead ? 1 : 0;
  const el = document.querySelector(`.clue[data-cid="${cid}"]`);
  if (el) el.dataset.read = toRead ? "1" : "0";
  readTotal.value += toRead ? 1 : -1;
  try {
    if (toRead) await markRead([cid]);
    else await unmarkRead([cid]);
  } catch (e) {
    console.warn("[已读] 单条切换失败", e);
  }
}

// 整页：把当前页全部标已读。隐藏模式下随后重拉 → 直接进入下一批未读
async function markPageRead() {
  const ids = clues.value.filter((c) => !c.is_read).map((c) => String(c.clue_id));
  if (!ids.length) return;
  const fresh = ids.filter((id) => !pendingReads.has(id));
  for (const c of clues.value) c.is_read = 1;
  pendingReads.clear();
  if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
  readTotal.value += fresh.length;
  try {
    await markRead(ids);
    if (hideRead.value) await loadPage();
    else setupReadObserver();
  } catch (e) {
    console.warn("[已读] 整页标记失败", e);
  }
}

// 清除已读：默认按「资讯日期」清近 N 天（只重读这几天的），全部清除作为次级选项。
// 先 flush 掉本机还没提交的标记，避免清完又被这些待提交项写回去。
async function clearReadRange() {
  const days = Number(clearDays.value);
  if (!window.confirm(
    `确定把「近 ${days} 天资讯」的已读标记清掉吗？\n` +
    `这些天读过的评论会重新出现在列表里（所有设备同步）。\n\n` +
    `只想清得更少/更多，可先在下拉框改天数。`
  )) return;
  await doClear(() => clearReadsSince(days), `近 ${days} 天`);
}
async function clearAllRead() {
  if (!window.confirm("确定清空【所有】已读吗？近 10 天的阅读进度会全部重置，量较大，建议优先用「近 N 天」。")) return;
  await doClear(clearAllReads, "全部");
}
async function doClear(fn, label) {
  try {
    if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
    if (pendingReads.size) await flushPendingReads();
    const r = await fn();
    const n = (r && r.cleared) || 0;
    // loadPage 会用服务端的 read_total 刷新计数（按范围清时不能简单归零）
    pendingReads.clear();
    await loadPage();
    window.alert(`已清除${label}已读标记：${n} 条恢复为未读。`);
  } catch (e) {
    console.warn("[已读] 清除失败", e);
    window.alert("清除失败，请稍后重试：" + e.message);
  }
}

function onReadPrefChange() {
  savePrefs();
  flushPendingReads();
  loadPage();
}
function onAutoMarkChange() {
  savePrefs();
  setupReadObserver();
}

// 深链页码越界（比如数据被清理后 ?page=50 已不存在）→ 自动落到最后一页
async function clampPage() {
  if (total.value > 0 && page.value > totalPages.value) {
    page.value = totalPages.value;
    await loadPage();
    writeUrlState("replace");
  }
}

async function goPage(p) {
  const tp = totalPages.value;
  if (p < 1 || p > tp || p === page.value) return;
  page.value = p;
  jumpTo.value = "";
  writeUrlState("push");
  await loadPage();
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function prevPage() { goPage(page.value - 1); }
function nextPage() { goPage(page.value + 1); }
function firstPage() { goPage(1); }
function lastPage() { goPage(totalPages.value); }
function doJump() {
  const n = parseInt(jumpTo.value, 10);
  if (!Number.isFinite(n)) { jumpTo.value = ""; return; }
  const p = Math.min(totalPages.value, Math.max(1, n));
  if (p === page.value) { jumpTo.value = ""; return; }
  goPage(p);
}

// 筛选条件变化时，回到第一页重新拉取
function onFilterChange() {
  page.value = 1;
  writeUrlState("replace");
  loadPage();
}

// 文本输入防抖：每敲一个字就发一次请求会把 D1 打爆
let searchTimer = null;
function onFilterInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(onFilterChange, 350);
}

// 刷新按钮：保留当前页与筛选，只重新拉数据
function reload() {
  writeUrlState("replace");
  loadPage(true);
}

// round_id -> { title, source }，用于每条评论标注来源
const roundMap = computed(() => {
  const m = {};
  for (const r of rounds.value) m[r.round_id] = r;
  return m;
});

function pad2(n) {
  return String(n).padStart(2, "0");
}
// 今天 / 昨天的 YYYY-MM-DD（按浏览器本地日期；用户为北京时间，与评论日期口径一致）
function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}
function yesterdayStr() {
  const d = new Date(Date.now() - 86400000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

// 评论真实日期：优先用服务端存的 c.date（北京时间，YYYY-MM-DD）；
// 旧行无 date 时从 time_str 解析（兼容 "YYYY-MM-DD ..." 与 "MM-DD ..." 两种格式）
function dateStr(c) {
  if (c.date) return c.date;
  const t = c.time_str || "";
  let m = t.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return `${m[1]}-${pad2(+m[2])}-${pad2(+m[3])}`;
  m = t.match(/(\d{1,2})-(\d{1,2})/);
  if (m) return `${new Date().getFullYear()}-${pad2(+m[1])}-${pad2(+m[2])}`;
  return "";
}

// 日期分隔标题：今天 / 昨天 / YYYY-MM-DD（date 永远有值，理论上不再出现「未知日期」）
function dayLabel(d) {
  if (!d) return "未知日期";
  if (d === todayStr()) return "今天";
  if (d === yesterdayStr()) return "昨天";
  return d;
}

// 从 time_str 提取时刻 HH:MM（脚本上报格式 YYYY-MM-DD HH:MM，北京时间）
function clockOf(c) {
  const m = (c.time_str || "").match(/(\d{1,2}):(\d{2})/);
  return m ? `${pad2(+m[1])}:${m[2]}` : "";
}

// 卡片时间标签：一律带具体时刻，避免一屏「昨天」分不清先后。
// 刚刚 / X分钟前 保留相对显示；其余 → 今天 HH:MM / 昨天 HH:MM / MM-DD HH:MM
function relTime(c) {
  const d = dateStr(c);
  const clock = clockOf(c);
  if (c.ts) {
    const diff = Math.floor(Date.now() / 1000) - c.ts;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  }
  if (!d) return clock || "（时间未知）";
  const at = (label) => (clock ? `${label} ${clock}` : label);
  if (d === todayStr()) return at("今天");
  if (d === yesterdayStr()) return at("昨天");
  // 同年只显示 MM-DD，跨年补年份；都带时刻
  const sameYear = d.slice(0, 4) === String(new Date().getFullYear());
  return at(sameYear ? d.slice(5) : d);
}

// Jev 投资参考价值标签：仅当已由 Jev 评估(jev_ok)且分>0 时显示「价值 N%」
function jevLabel(c) {
  const v = c.jev_value;
  if (typeof v === "number" && v > 0 && c.jev_ok) return Math.round(v * 100) + "%";
  return "";
}

// 板块归属：推荐(section=recommend) / 热门(section=hot) / 关注(section=follow) / 热点话题(无 section，来自 hashtag 引擎)
function sectionOf(c) {
  if (c.section === "recommend") return "recommend";
  if (c.section === "hot") return "hot";
  if (c.section === "follow") return "follow";
  return "topic";
}
function sectionLabel(c) {
  const s = sectionOf(c);
  return s === "recommend" ? "推荐" : s === "follow" ? "关注" : s === "hot" ? "热门" : "话题";
}

// 重复命中标记：该线索被多轮抓取到（全局去重后只存一行，但可看出热度）
// hit_count > 1 表示它在热门帖里反复出现
function hitLabel(c) {
  const n = Number(c.hit_count) || 0;
  return n > 1 ? `×${n}` : "";
}

// 是否有任何生效中的筛选（用于区分「无数据」与「筛选后无数据」两种空态）
const hasActiveFilter = computed(() =>
  !!(sectionFilter.value || search.value.trim() || stockFilter.value.trim() || minScore.value)
);

// 按「日期」分段（时间线视觉分隔，不做任何关联推算）
const timeline = computed(() => {
  const map = {};
  for (const c of clues.value) {
    const day = dateStr(c) || "未知日期";
    (map[day] = map[day] || []).push(c);
  }
  return Object.entries(map).sort((a, b) =>
    asc.value ? a[0].localeCompare(b[0]) : b[0].localeCompare(a[0])
  );
});

function scoreClass(s) {
  if (s >= 12) return "score-hi";
  if (s >= 7) return "score-mid";
  return "score-lo";
}

// 点击用户名 → 来源站用户主页（便于关注）
function userUrl(c) {
  return c.user_id ? "https://xueqiu.com/u/" + encodeURIComponent(c.user_id) : "";
}
// 「原文」按钮 → 来源站帖子页（地址格式 /<帖子作者ID>/<帖子ID>）
// 注意：不能用「评论者ID + 评论ID(clue_id)」，该形式在来源站返回 404；
// 必须用帖子作者的数字 ID（post_author_id）+ 帖子 ID（post_id）才能正确打开。
function origUrl(c) {
  if (!c.post_author_id || !c.post_id) return "";
  return "https://xueqiu.com/" + encodeURIComponent(c.post_author_id) + "/" + encodeURIComponent(c.post_id);
}
</script>

<template>
  <div>
    <div class="topbar">
      <div>
        <h1>股票评论雷达 · 评论时间线</h1>
        <div class="sub">
          按时间线浏览自动抓取的高价值股票社区评论
          <template v-if="total">
            · 近 10 天共 {{ total }} 条 · 第 {{ page }} / {{ totalPages }} 页
          </template>
        </div>
      </div>
      <div class="topbar-actions">
        <button @click="sort = (sort === 'jev' ? 'ts' : 'jev'); onFilterChange()">
          {{ sort === "jev" ? "按时间 ↓" : "按价值 ↓" }}
        </button>
        <button
          @click="asc = !asc; onFilterChange()"
          :title="asc ? '当前旧在前（时间升序），点击改为新在前' : '当前新在前（时间倒序），点击改为旧在前'"
        >{{ asc ? "旧在前 ↑" : "新在前 ↓" }}</button>
        <button @click="reload">刷新</button>
      </div>
    </div>

    <!-- 板块筛选：推荐/关注 · 热门 · 热点话题（服务端按 section 过滤） -->
    <div class="section-tabs">
      <button
        v-for="t in SECTION_TABS"
        :key="t.key"
        :class="['tab', { active: sectionFilter === t.key }]"
        @click="sectionFilter = t.key; onFilterChange()"
      >{{ t.label }}</button>
    </div>

    <!-- 阅读进度条：已读跨设备同步（存服务端），支持滚动自动标记 / 整页 / 单条 -->
    <div class="readbar">
      <label class="rchk" title="勾选后已读资讯从列表消失（服务端过滤，翻页计数同步）；换设备也生效">
        <input type="checkbox" v-model="hideRead" @change="onReadPrefChange"> 隐藏已读
      </label>
      <label class="rchk" title="卡片在视野中停留 2 秒即自动标记已读，快速划过不算">
        <input type="checkbox" v-model="autoMark" @change="onAutoMarkChange"> 滚动自动已读
      </label>
      <span class="rstat">已读 <b>{{ readTotal }}</b> 条</span>
      <button class="rbtn" @click="markPageRead" :disabled="loading || !clues.length" title="把当前页全部标记为已读">本页全标已读</button>
      <span class="rclear" title="按资讯日期恢复近 N 天的已读，方便重读这几天的评论">
        清除
        <select v-model.number="clearDays" @change="savePrefs">
          <option v-for="d in CLEAR_DAYS" :key="d" :value="d">近 {{ d }} 天</option>
        </select>
        <button class="rbtn" @click="clearReadRange" :disabled="loading || !readTotal">恢复未读</button>
        <button class="rbtn rbtn-minor" @click="clearAllRead" :disabled="loading || !readTotal" title="重置全部已读（量大，慎用）">全部</button>
      </span>
    </div>

    <div class="filters">
      <input v-model="search" @input="onFilterInput" placeholder="搜索正文 / 作者" />
      <input v-model="stockFilter" @input="onFilterInput" placeholder="按标的筛选（如 澜起科技）" />
      <select v-model.number="minScore" @change="onFilterChange">
        <option :value="0">分数 ≥ 0</option>
        <option :value="7">分数 ≥ 7</option>
        <option :value="12">分数 ≥ 12</option>
        <option :value="15">分数 ≥ 15</option>
      </select>
      <span class="count" title="只保留最近 10 天入库的线索；旧数据每天 00:00 自动清理，所以总数是滚动的、不是历史累计">近 10 天 {{ total }} 条</span>
    </div>

    <div v-if="loading" class="loading">加载中…</div>
    <div v-else-if="error" class="error">错误：{{ error }}</div>
    <div v-else-if="!total" class="empty">
      <template v-if="hideRead && !hasActiveFilter">
        近 10 天的资讯都已读完 ✓（已标记 {{ readTotal }} 条）。<br>
        取消勾选「隐藏已读」可回看，或用「清除 → 近 N 天 → 恢复未读」重读某几天。
      </template>
      <template v-else-if="hasActiveFilter">当前筛选条件下暂无数据，试试切换板块或清空筛选条件。</template>
      <template v-else>暂无数据。请先让上传工具运行一轮并开启上传（--upload 指定 Worker 地址）。</template>
    </div>
    <div v-else>
      <div v-for="[day, items] in timeline" :key="day">
        <div class="day-divider">{{ dayLabel(day) }}</div>
        <div
          v-for="c in items"
          :key="c.round_id + '_' + c.clue_id"
          class="clue"
          :class="{ read: !!c.is_read }"
          :data-cid="String(c.clue_id)"
          :data-read="c.is_read ? '1' : '0'"
        >
          <div class="head">
            <span class="time" :title="c.time_str">{{ relTime(c) }}</span>
            <span class="who">
              <a v-if="c.user_id" class="who-link" :href="userUrl(c)" target="_blank" rel="noopener noreferrer">{{ c.user_name || "（匿名）" }}</a>
              <template v-else>{{ c.user_name || "（匿名）" }}</template>
            </span>
            <span
              class="src"
              :class="sectionOf(c)"
              :title="'来源：' + ((roundMap[c.round_id] && roundMap[c.round_id].source) || '-')"
            >{{ sectionLabel(c) }}</span>
            <span
              v-if="hitLabel(c)"
              class="hit"
              :title="'该线索被 ' + c.hit_count + ' 轮抓取到（全局去重后仅存一份）'"
            >{{ hitLabel(c) }}</span>
            <button
              class="read-toggle"
              :class="{ done: !!c.is_read }"
              @click.stop="toggleOne(c)"
              :title="c.is_read ? '取消已读' : '标记为已读'"
            >{{ c.is_read ? "✓ 已读" : "○ 已读" }}</button>
          </div>

          <div class="text">{{ c.text }}</div>

          <div class="meta">
            <span :class="['score', scoreClass(c.score)]">分 {{ c.score || 0 }}</span>
            <span v-if="jevLabel(c)" class="jev" :title="`Jev 投资参考价值 ${jevLabel(c)}`">价值 {{ jevLabel(c) }}</span>
            <span>赞 {{ c.like_count || 0 }}</span>
            <span v-if="c.reply_count">· 回 {{ c.reply_count }}</span>
            <span v-for="(s, i) in (c.stocks || [])" :key="i" class="stock">{{ s }}</span>
            <a v-if="origUrl(c)" class="orig" :href="origUrl(c)" target="_blank" rel="noopener noreferrer" title="在新标签打开原文（含上下文）">原文 ↗</a>
          </div>

          <div class="tags" v-if="c.tags && c.tags.length">
            <span v-for="(t, i) in c.tags" :key="i" class="tag">{{ t }}</span>
          </div>
        </div>
      </div>

      <div class="pager" v-if="total > 0">
        <button class="pg-btn nav" :disabled="page <= 1 || loading" @click="firstPage" title="回到第一页">« 首页</button>
        <button class="pg-btn nav" :disabled="page <= 1 || loading" @click="prevPage">‹ 上一页</button>
        <span class="pg-nums">
          <button
            v-for="(p, i) in pageList"
            :key="i"
            :class="['pg-btn', 'num', { active: p === page, ellipsis: p === '…' }]"
            :disabled="p === '…' || loading"
            @click="p !== '…' && goPage(p)"
          >{{ p }}</button>
        </span>
        <button class="pg-btn nav" :disabled="page >= totalPages || loading" @click="nextPage">下一页 ›</button>
        <button class="pg-btn nav" :disabled="page >= totalPages || loading" @click="lastPage" title="跳到最后一页">末页 »</button>
        <span class="pg-jump">
          跳至
          <input
            type="number" min="1" :max="totalPages" inputmode="numeric"
            v-model="jumpTo" @keyup.enter="doJump" :disabled="loading"
            :placeholder="String(page)" aria-label="跳转到指定页码"
          >
          页
          <button class="pg-btn" :disabled="loading" @click="doJump">Go</button>
        </span>
      </div>
      <div class="pg-caption" v-if="total > 0">第 {{ page }} / {{ totalPages }} 页 · 每页 {{ PAGE }} 条 · 共 {{ total }} 条</div>
    </div>

    <button class="to-top" v-show="showToTop" @click="scrollToTop" aria-label="返回顶部" title="返回顶部">↑</button>
  </div>
</template>
