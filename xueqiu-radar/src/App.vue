<script setup>
import { ref, onMounted, computed } from "vue";
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

onMounted(loadInitial);

const currentParams = computed(() => ({
  stock: stockFilter.value.trim(),
  q: search.value.trim(),
  min: minScore.value || 0,
  order: asc.value ? "asc" : "desc",
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

function pad(n) {
  return String(n).padStart(2, "0");
}
// ts（unix 秒）-> YYYY-MM-DD；缺 ts 归入「未知日期」
function dayOf(c) {
  if (!c.ts) return "未知日期";
  const d = new Date(c.ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// 按「日期」分段（时间线视觉分隔，不做任何关联推算）
const timeline = computed(() => {
  const map = {};
  for (const c of clues.value) {
    const day = dayOf(c);
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
        <button @click="asc = !asc; onFilterChange()">{{ asc ? "正序 ↑" : "倒序 ↓" }}</button>
        <button @click="loadInitial">刷新</button>
      </div>
    </div>

    <div v-if="loading" class="loading">加载中…</div>
    <div v-else-if="error" class="error">错误：{{ error }}</div>
    <div v-else-if="!total" class="empty">
      暂无数据。请先让 xueqiu.exe 上传一轮线索（加 --upload 与 Worker 地址）。
    </div>
    <div v-else>
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

      <div v-for="[day, items] in timeline" :key="day">
        <div class="day-divider">{{ day }}</div>
        <div v-for="c in items" :key="c.round_id + '_' + c.clue_id" class="clue">
          <div class="head">
            <span class="time">{{ c.time_str || "（时间未知）" }}</span>
            <span class="who">{{ c.user_name || "（匿名）" }}</span>
            <span
              v-if="roundMap[c.round_id] && roundMap[c.round_id].source"
              class="src"
              :class="roundMap[c.round_id].source"
            >{{ roundMap[c.round_id].source }}</span>
          </div>

          <div class="text">{{ c.text }}</div>

          <div class="meta">
            <span :class="['score', scoreClass(c.score)]">分 {{ c.score || 0 }}</span>
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
  </div>
</template>
