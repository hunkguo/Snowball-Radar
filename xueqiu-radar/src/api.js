// 与 Worker 后端交互的轻量封装

async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`请求失败 ${r.status}: ${await r.text()}`);
  return r.json();
}

export async function fetchRounds() {
  const data = await getJson("/api/rounds?limit=200");
  return data.rounds || [];
}

// 拉取一页线索（分页 + 服务端筛选 + 排序）
// params: { stock, q, min, order, sort, section, limit, offset }
//   sort: "ts"(默认，按时间) | "jev"(按 Jev 价值分降序)
//   section: ""(全部) | "recommend"(推荐) | "hot"(热门) | "follow"(关注) | "topic"(热点话题)
//   unread:  true = 只看未读（服务端按 reads 表过滤，跨设备同步）
// 返回: { clues, total, has_more, limit, offset }
export async function fetchCluesPage(params = {}) {
  const p = new URLSearchParams();
  if (params.stock) p.set("stock", params.stock);
  if (params.q) p.set("q", params.q);
  if (params.min != null) p.set("min", String(params.min));
  if (params.order) p.set("order", params.order);
  if (params.sort) p.set("sort", params.sort);
  if (params.section) p.set("section", params.section);
  if (params.unread) p.set("unread", "1");
  if (params.round) p.set("round", params.round);
  if (params.limit != null) p.set("limit", String(params.limit));
  if (params.offset != null) p.set("offset", String(params.offset));
  const data = await getJson("/api/clues?" + p.toString());
  return {
    clues: data.clues || [],
    total: data.total || 0,
    read_total: data.read_total || 0,
    has_more: !!data.has_more,
    limit: data.limit || 50,
    offset: data.offset || 0,
  };
}

// 已读状态（跨设备共享，存服务端）
export async function fetchReadIds() {
  const data = await getJson("/api/reads");
  return { ids: data.ids || [], total: data.total || 0 };
}

// ids: 字符串数组；read=true 标记，false 取消
async function postReads(ids, method) {
  const r = await fetch("/api/reads", {
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ids: ids.map(String) }),
  });
  if (!r.ok) throw new Error(`请求失败 ${r.status}: ${await r.text()}`);
  return r.json();
}

export function markRead(ids) { return postReads(ids, "POST"); }
export function unmarkRead(ids) { return postReads(ids, "DELETE"); }

// 清除全部已读（保留兼容入口）
export async function clearAllReads() {
  const r = await fetch("/api/reads", {
    method: "DELETE",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ all: true }),
  });
  if (!r.ok) throw new Error(`请求失败 ${r.status}: ${await r.text()}`);
  return r.json();
}

// 按资讯日期清除近 days 天的已读标记（恢复为未读），返回 { cleared }
export async function clearReadsSince(days) {
  const r = await fetch("/api/reads", {
    method: "DELETE",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ days }),
  });
  if (!r.ok) throw new Error(`请求失败 ${r.status}: ${await r.text()}`);
  return r.json();
}
