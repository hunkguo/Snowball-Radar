<script setup>
import { ref, onMounted, onUnmounted, computed } from "vue";
import { fetchRounds, fetchCluesPage } from "./api.js";

const rounds = ref([]);
const clues = ref([]);
const loading = ref(false);
const loadingMore = ref(false);
const error = ref("");

// 分页状态
const total = ref(0);
const hasMore = ref(false);
const offset = ref(0);
const PAGE = 50;

// 筛选（全部在服务端执行）
const stockFilter = ref("");
const minScore = ref(0);
const search = ref("");
const asc = ref(false); // false = 最新在前（倒序时间线）
const sort = ref("ts"); // "ts"=按时间(默认) | "jev"=按 Jev 价值分降序
// 板块筛选：""=全部 | "recommend"=推荐/关注 | "hot"=热门 | "topic"=热点话题
const sectionFilter = ref("");
const SECTION_TABS = [
  { key: "", label: "全部" },
  { key: "recommend", label: "推荐/关注" },
  { key: "hot", label: "热门" },
  { key: "topic", label: "热点话题" },
];

onMounted(() => {
  loadInitial();
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
});
onUnmounted(() => window.removeEventListener("scroll", onScroll));

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
  limit: PAGE,
}));

async function loadInitial() {
  loading.value = true;
  error.value = "";
  try {
    const [r, page] = await Promise.all([
      fetchRounds(),
      fetchCluesPage({ ...currentParams.value, offset: 0 }),
    ]);
    rounds.value = r || [];
    clues.value = page.clues || [];
    total.value = page.total || 0;
    hasMore.value = !!page.has_more;
    offset.value = page.clues.length || 0;
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}

async function loadMore() {
  if (loadingMore.value || !hasMore.value) return;
  loadingMore.value = true;
  try {
    const page = await fetchCluesPage({ ...currentParams.value, offset: offset.value });
    const seen = new Set(clues.value.map((c) => c.round_id + "_" + c.clue_id));
    for (const c of page.clues) {
      const k = c.round_id + "_" + c.clue_id;
      if (!seen.has(k)) clues.value.push(c);
    }
    hasMore.value = !!page.has_more;
    offset.value += page.clues.length;
  } catch (e) {
    error.value = e.message;
  } finally {
    loadingMore.value = false;
  }
}

// 筛选条件变化时，回到第一页重新拉取
function onFilterChange() {
  loadInitial();
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

// 卡片时间标签：刚刚 / X分钟前 / X小时前 / 今天 / 昨天 / 日期
function relTime(c) {
  const d = dateStr(c);
  if (c.ts) {
    const diff = Math.floor(Date.now() / 1000) - c.ts;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
    // 仅当与今天同属一天才显示「X小时前」，避免跨天被误判为几小时前
    if (d === todayStr() && diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  }
  if (!d) return "（时间未知）";
  if (d === todayStr()) return "今天";
  if (d === yesterdayStr()) return "昨天";
  return d;
}

// Jev 投资参考价值标签：仅当已由 Jev 评估(jev_ok)且分>0 时显示「价值 N%」
function jevLabel(c) {
  const v = c.jev_value;
  if (typeof v === "number" && v > 0 && c.jev_ok) return Math.round(v * 100) + "%";
  return "";
}

// 板块归属：推荐(section=recommend) / 热门(section=hot) / 热点话题(无 section，来自 hashtag 引擎)
function sectionOf(c) {
  if (c.section === "recommend") return "recommend";
  if (c.section === "hot") return "hot";
  return "topic";
}
function sectionLabel(c) {
  const s = sectionOf(c);
  return s === "recommend" ? "推荐" : s === "hot" ? "热门" : "话题";
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
</script>

<template>
  <div>
    <div class="topbar">
      <div>
        <h1>雪球雷达 · 评论时间线</h1>
        <div class="sub">
          按时间线浏览 xueqiu.exe 上传的雪球评论内容
          <template v-if="total">
            · 共 {{ total }} 条（已加载 {{ clues.length }}）
          </template>
        </div>
      </div>
      <div class="topbar-actions">
        <button @click="sort = (sort === 'jev' ? 'ts' : 'jev'); onFilterChange()">
          {{ sort === "jev" ? "按时间 ↓" : "按价值 ↓" }}
        </button>
        <button @click="asc = !asc; onFilterChange()">{{ asc ? "正序 ↑" : "倒序 ↓" }}</button>
        <button @click="loadInitial">刷新</button>
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

    <div class="filters">
      <input v-model="search" @input="onFilterChange" placeholder="搜索正文 / 作者" />
      <input v-model="stockFilter" @input="onFilterChange" placeholder="按标的筛选（如 澜起科技）" />
      <select v-model.number="minScore" @change="onFilterChange">
        <option :value="0">分数 ≥ 0</option>
        <option :value="7">分数 ≥ 7</option>
        <option :value="12">分数 ≥ 12</option>
        <option :value="15">分数 ≥ 15</option>
      </select>
      <span class="count">共 {{ total }} 条</span>
    </div>

    <div v-if="loading" class="loading">加载中…</div>
    <div v-else-if="error" class="error">错误：{{ error }}</div>
    <div v-else-if="!total" class="empty">
      <template v-if="hasActiveFilter">当前筛选条件下暂无数据，试试切换板块或清空筛选条件。</template>
      <template v-else>暂无数据。请先让 xueqiu.exe 上传一轮线索（加 --upload 与 Worker 地址）。</template>
    </div>
    <div v-else>
      <div v-for="[day, items] in timeline" :key="day">
        <div class="day-divider">{{ dayLabel(day) }}</div>
        <div v-for="c in items" :key="c.round_id + '_' + c.clue_id" class="clue">
          <div class="head">
            <span class="time" :title="c.time_str">{{ relTime(c) }}</span>
            <span class="who">{{ c.user_name || "（匿名）" }}</span>
            <span
              class="src"
              :class="sectionOf(c)"
              :title="'来源：' + ((roundMap[c.round_id] && roundMap[c.round_id].source) || '-')"
            >{{ sectionLabel(c) }}</span>
          </div>

          <div class="text">{{ c.text }}</div>

          <div class="meta">
            <span :class="['score', scoreClass(c.score)]">分 {{ c.score || 0 }}</span>
            <span v-if="jevLabel(c)" class="jev" :title="`Jev 投资参考价值 ${jevLabel(c)}`">价值 {{ jevLabel(c) }}</span>
            <span>赞 {{ c.like_count || 0 }}</span>
            <span v-if="c.reply_count">· 回 {{ c.reply_count }}</span>
            <span v-for="(s, i) in (c.stocks || [])" :key="i" class="stock">{{ s }}</span>
          </div>

          <div class="tags" v-if="c.tags && c.tags.length">
            <span v-for="(t, i) in c.tags" :key="i" class="tag">{{ t }}</span>
          </div>
        </div>
      </div>

      <div class="more">
        <button v-if="hasMore" :disabled="loadingMore" @click="loadMore">
          {{ loadingMore ? "加载中…" : "加载更多" }}
        </button>
        <span v-else class="end">— 没有更多了 —</span>
      </div>
    </div>

    <button class="to-top" v-show="showToTop" @click="scrollToTop" aria-label="返回顶部" title="返回顶部">↑</button>
  </div>
</template>
