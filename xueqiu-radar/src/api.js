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

// 拉取一页线索（分页 + 服务端筛选）
// params: { stock, q, min, order, limit, offset }
// 返回: { clues, total, has_more, limit, offset }
export async function fetchCluesPage(params = {}) {
  const p = new URLSearchParams();
  if (params.stock) p.set("stock", params.stock);
  if (params.q) p.set("q", params.q);
  if (params.min != null) p.set("min", String(params.min));
  if (params.order) p.set("order", params.order);
  if (params.limit != null) p.set("limit", String(params.limit));
  if (params.offset != null) p.set("offset", String(params.offset));
  const data = await getJson("/api/clues?" + p.toString());
  return {
    clues: data.clues || [],
    total: data.total || 0,
    has_more: !!data.has_more,
    limit: data.limit || 50,
    offset: data.offset || 0,
  };
}
