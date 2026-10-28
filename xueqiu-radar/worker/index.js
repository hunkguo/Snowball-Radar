/**
 * 雪球雷达 · 线索台  Worker 后端
 *
 * 职责：
 *   1) POST /api/ingest   —— 油猴脚本上传 { meta, candidates, comments }，鉴权后写入 D1
 *   2) GET  /api/rounds   —— 轮次列表（倒序，支持分页）
 *   3) GET  /api/clues    —— 线索（按 ts 时间排序，支持分页 + 标的/关键词/分数/板块筛选）
 *                            板块筛选：section=recommend(推荐/关注) | hot(热门) | topic(热点话题)
 *   4) POST /api/cleanup  —— 手动触发清理（token 鉴权），删 10 天前数据
 *   5) 每日 Cron 自动清理 10 天前数据
 *   6) 其余路径            —— 交给 ASSETS 托管 Vue 前台（SPA）
 *
 * 绑定：env.DB (D1)、env.INGEST_TOKEN (secret)、env.ASSETS
 */

const BATCH_LIMIT = 100;   // D1 单次 batch 的语句数上限
const KEEP_DAYS = 10;       // 保留天数，超过则自动清理
const MAX_PAGE = 200;       // 单页上限
const MAX_CANDIDATES = 2000; // 单次 ingest 候选上限（防滥用）

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function safeJson(s, fallback) {
  if (s == null) return fallback;
  try {
    return JSON.parse(s);
  } catch {
    return fallback;
  }
}

function makeRoundId(meta) {
  const g = (meta && meta.generated_at ? meta.generated_at : new Date().toISOString())
    .replace(/[: ]/g, "-");
  const slug = (meta && (meta.hashtag || meta.title)) || "x";
  return `${g}__${(slug || "").slice(0, 24)}`;
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

// 把「年/月/日/时/分」按北京时间（GMT+8）转成真实 unix 秒，并返回 YYYY-MM-DD 日期串。
// 雪球时间均为北京时间，故用 Date.UTC 构造后再减 8 小时，得到与绝对时刻一致的 epoch，
// 这样前端「X小时前」的相对计算才准确（不受 Worker 运行时所在时区影响）。
function toDateTs(y, mo, d, h, mi) {
  const date = `${y}-${pad2(mo)}-${pad2(d)}`;
  const dt = new Date(Date.UTC(y, mo - 1, d, h - 8, mi));
  const t = Math.floor(dt.getTime() / 1000);
  return { ts: isNaN(t) ? 0 : t, date };
}

// 解析评论时间字符串，返回 { ts, date }：
//   支持 "YYYY-MM-DD HH:MM" 或 "MM-DD HH:MM"（缺年份时用 yearHint 兜底）
function parseTs(timeStr, yearHint) {
  const s = timeStr || "";
  let m = s.match(/(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})/);
  if (m) return toDateTs(Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4]), Number(m[5]));
  m = s.match(/(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})/);
  if (m) {
    const y = Number(yearHint) || new Date().getUTCFullYear();
    return toDateTs(y, Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4]));
  }
  return { ts: 0, date: "" };
}

// 解析轮次 generated_at（"YYYY-MM-DD HH:MM:SS"，抓取/上传时间），作为评论时间缺失时的兜底
function parseGeneratedAt(g) {
  const m = (g || "").match(/(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})/);
  if (!m) return { ts: 0, date: "" };
  return toDateTs(Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4]), Number(m[5]));
}

async function ingest(body, env) {
  const meta = body && body.meta ? body.meta : {};
  const roundId = body && body.round_id ? String(body.round_id) : makeRoundId(meta);
  const rawCandidates = Array.isArray(body && body.candidates) ? body.candidates : [];
  const comments = Array.isArray(body && body.comments) ? body.comments : [];

  // 客户端传来的"已去重线索"清单（可选，用于省一次 SELECT）
  const clientSeen = Array.isArray(body && body.seen_clue_ids) ? body.seen_clue_ids.map(String) : null;

  // 客户端传来的"本轮候选总数"（含被客户端探针跳过的重复）
  // 用于在 rounds 表如实记录「本轮候选 N 条，其中 M 条是重复」
  const clientTotal = meta.total_candidates != null ? Number(meta.total_candidates) : null;

  const source = meta.source || "unknown";
  const title = meta.title || "";
  const hashtag = meta.hashtag || "";
  const generatedAt = meta.generated_at || "";
  const totalComments = meta.total_comments != null ? meta.total_comments : comments.length;
  const createdAt = Date.now();
  const yearHint = (generatedAt || roundId).slice(0, 4);
  const gen = parseGeneratedAt(generatedAt); // 评论时间缺失时兜底用上传时间

  // ────────────────────────────────────────────────────────────
  // 第 1 步：全局去重
  //   数据库主键是 (round_id, clue_id)，只能保证「轮内幂等」。
  //   雪球热门帖会反复出现在多轮抓取里，同一条评论（clue_id 相同）
  //   在不同 round_id 下会插多行 → 前端出现重复条目 + 重复消耗写额度。
  //
  //   做法：先查出本批 clue_id 中哪些已在库（全库维度），
  //   已存在的走 UPSERT（只更新 last_seen_round / hit_count / 互动数），
  //   不存在的走 INSERT。这样无论客户端是否去重，服务端都保证唯一。
  // ────────────────────────────────────────────────────────────
  const candidates = rawCandidates.slice(0, MAX_CANDIDATES);

  // 批内先去重：同一批 payload 里出现两次的 clue_id，只处理一次
  const batchSeen = new Set();
  const uniqCandidates = [];
  let inBatchDupes = 0;
  for (const c of candidates) {
    const cid = String(c.id != null ? c.id : "");
    if (!cid) continue;
    if (batchSeen.has(cid)) { inBatchDupes++; continue; }
    batchSeen.add(cid);
    uniqCandidates.push(c);
  }

  // 查全库已存在的 clue_id（分批 IN 查询，避开 SQLite 变量数上限）
  const existing = new Set();
  const ids = Array.from(batchSeen);
  const IN_CHUNK = 100;
  for (let i = 0; i < ids.length; i += IN_CHUNK) {
    const part = ids.slice(i, i + IN_CHUNK);
    const ph = part.map(() => "?").join(",");
    const q = await env.DB.prepare(
      `SELECT clue_id FROM clues WHERE clue_id IN (${ph})`
    ).bind(...part).all();
    for (const row of (q.results || [])) existing.add(String(row.clue_id));
  }

  const stmts = [];
  let inserted = 0;
  let updated = 0;

  for (const c of uniqCandidates) {
    const cid = String(c.id != null ? c.id : "");
    const p = parseTs(c.time_str, yearHint);
    const ts = p.ts || gen.ts;        // 时间串解析不出则用上传时间兜底
    const date = p.date || gen.date;  // 避免出现未知日期

    if (existing.has(cid)) {
      // 已存在 → UPSERT：保留原始 round_id（首次出现的那轮），
      // 只更新「最近命中轮次 / 命中次数 / 互动数（点赞回复会增长）」。
      // 时间与文本不变（评论内容不可变，点赞数用较大值兜底）。
      updated++;
      stmts.push(
        env.DB.prepare(
          `UPDATE clues SET
             last_seen_round = ?,
             hit_count = COALESCE(hit_count, 0) + 1,
             like_count = MAX(COALESCE(like_count, 0), ?),
             reply_count = MAX(COALESCE(reply_count, 0), ?)
           WHERE clue_id = ?`
        ).bind(roundId, Number(c.like_count) || 0, Number(c.reply_count) || 0, cid)
      );
      continue;
    }

    inserted++;
    stmts.push(
      env.DB.prepare(
        `INSERT INTO clues
           (round_id, clue_id, first_seen_round, last_seen_round, hit_count,
            user_name, user_id, post_id, post_author_id, time_str,
            like_count, reply_count, text, score, tags, stocks, section, ts, date, jev_value,
            ingested_at)
         VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(clue_id) DO UPDATE SET
           last_seen_round = excluded.last_seen_round,
           hit_count = COALESCE(clues.hit_count, 0) + 1,
           like_count = MAX(COALESCE(clues.like_count, 0), excluded.like_count),
           reply_count = MAX(COALESCE(clues.reply_count, 0), excluded.reply_count)`
      ).bind(
        roundId,
        cid,
        roundId,      // first_seen_round
        roundId,      // last_seen_round
        c.user_name || "",
        String(c.user_id || ""),
        String(c.post_id || ""),
        String(c.post_author_id || ""),
        c.time_str || "",
        Number(c.like_count) || 0,
        Number(c.reply_count) || 0,
        c.text || "",
        Number(c.score) || 0,
        JSON.stringify(c.tags || []),
        JSON.stringify(c.stocks || []),
        c.section || "",
        ts,
        date,
        Number(c.jev_value) || 0,
        createdAt
      )
    );
  }

  // 轮次元信息（覆盖写）—— 放在最后。
  // candidate_count：本轮候选总数（若客户端告知则用其值，含被跳过的重复）
  // deduped_count：其中判定为重复而跳过的条数
  const dedupedCount = updated + inBatchDupes + (clientTotal != null
    ? Math.max(0, clientTotal - uniqCandidates.length - updated)
    : 0);
  const candidateTotal = clientTotal != null ? clientTotal : candidates.length;
  stmts.push(
    env.DB.prepare(
      `INSERT OR REPLACE INTO rounds
         (round_id, source, title, hashtag, generated_at, candidate_count, total_comments, deduped_count, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).bind(roundId, source, title, hashtag, generatedAt, candidateTotal, totalComments, dedupedCount, createdAt)
  );

  // ────────────────────────────────────────────────────────────
  // 第 2 步：集中写入
  //   把所有 INSERT/UPDATE 合并成尽量少的 DB.batch 往返。
  //   D1 每个 batch 是一个事务，一次往返 = 一个事务，
  //   比"每条一个 batch"减少 90%+ 的网络往返与事务开销。
  // ────────────────────────────────────────────────────────────
  let batches = 0;
  for (let i = 0; i < stmts.length; i += BATCH_LIMIT) {
    await env.DB.batch(stmts.slice(i, i + BATCH_LIMIT));
    batches++;
  }

  return {
    ok: true,
    round_id: roundId,
    clues: uniqCandidates.length,
    inserted,
    updated,
    in_batch_dupes: inBatchDupes,
    deduped: dedupedCount,
    comments: comments.length,
    batches,
  };
}

// 去重探针：POST { clue_ids: [...] } → { exists: [...], missing: [...] }
// 客户端在上传前先问一次，只上传 missing 的部分。
// 作用：① 缩小请求体 ② 避免无谓的 UPSERT 写入 ③ 减少 D1 写额度消耗
async function handleExists(env, request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }
  const ids = Array.isArray(body && body.clue_ids)
    ? body.clue_ids.map(String).filter(Boolean).slice(0, 2000)
    : [];
  if (!ids.length) return json({ exists: [], missing: [], total: 0 });

  const exists = [];
  const IN_CHUNK = 100;
  for (let i = 0; i < ids.length; i += IN_CHUNK) {
    const part = ids.slice(i, i + IN_CHUNK);
    const ph = part.map(() => "?").join(",");
    const q = await env.DB.prepare(
      `SELECT clue_id FROM clues WHERE clue_id IN (${ph})`
    ).bind(...part).all();
    for (const row of (q.results || [])) exists.push(String(row.clue_id));
  }
  const existSet = new Set(exists);
  const missing = ids.filter((x) => !existSet.has(x));
  return json({ exists, missing, total: ids.length });
}

// 命中上报：客户端告知"这批 clue_id 我又抓到了"（已存在于库）
// 只做计数与最近轮次更新，不写正文行 → 不产生重复、几乎不占写额度
async function handleHits(env, request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }
  const ids = Array.isArray(body && body.clue_ids)
    ? body.clue_ids.map(String).filter(Boolean).slice(0, 2000)
    : [];
  const roundId = String((body && body.round_id) || "");
  if (!ids.length) return json({ ok: true, updated: 0 });

  const stmts = [];
  const IN_CHUNK = 100;
  for (let i = 0; i < ids.length; i += IN_CHUNK) {
    const part = ids.slice(i, i + IN_CHUNK);
    const ph = part.map(() => "?").join(",");
    stmts.push(
      env.DB.prepare(
        `UPDATE clues SET
           last_seen_round = ?,
           hit_count = COALESCE(hit_count, 0) + 1
         WHERE clue_id IN (${ph})`
      ).bind(roundId, ...part)
    );
  }
  let updated = 0;
  for (let i = 0; i < stmts.length; i += BATCH_LIMIT) {
    const r = await env.DB.batch(stmts.slice(i, i + BATCH_LIMIT));
    updated += (r && r.length) || 0;
  }
  return json({ ok: true, updated });
}

// 解析分页 / 筛选参数
function parseQuery(url) {
  const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") || "50", 10), 1), MAX_PAGE);
  const offset = Math.max(parseInt(url.searchParams.get("offset") || "0", 10), 0);
  const order = url.searchParams.get("order") === "asc" ? "ASC" : "DESC";
  const stock = (url.searchParams.get("stock") || "").trim();
  const q = (url.searchParams.get("q") || "").trim();
  const min = url.searchParams.get("min");
  const minScore = min != null && min !== "" ? Math.max(parseInt(min, 10) || 0, 0) : null;
  const sort = url.searchParams.get("sort") === "jev" ? "jev" : "ts";
  // section 筛选："" = 全部 | "recommend" = 推荐 | "hot" = 热门 | "follow" = 关注 | "topic" = 热点话题(无 section)
  const rawSection = (url.searchParams.get("section") || "").trim();
  const section = ["recommend", "hot", "follow", "topic"].includes(rawSection) ? rawSection : "";
  return { limit, offset, order, stock, q, minScore, sort, section };
}

// 组装 WHERE 与绑定参数
function buildWhere({ stock, q, minScore, round, section }) {
  const where = [];
  const binds = [];
  if (round) { where.push("round_id = ?"); binds.push(round); }
  if (section === "topic") {
    // 热点话题来源（hashtag 引擎）不写 section，统一归为「话题」
    where.push("(section IS NULL OR section = '')");
  } else if (section) {
    where.push("section = ?"); binds.push(section);
  }
  if (stock) { where.push("stocks LIKE ?"); binds.push("%" + stock + "%"); }
  if (q) { where.push("(text LIKE ? OR user_name LIKE ?)"); binds.push("%" + q + "%", "%" + q + "%"); }
  if (minScore != null) { where.push("score >= ?"); binds.push(minScore); }
  const clause = where.length ? "WHERE " + where.join(" AND ") : "";
  return { clause, binds };
}

async function handleRounds(env, url) {
  const { limit, offset } = parseQuery(url);
  const { results: t } = await env.DB.prepare("SELECT COUNT(*) AS n FROM rounds").all();
  const total = (t && t[0] && t[0].n) || 0;
  const { results } = await env.DB.prepare(
    `SELECT round_id, source, title, hashtag, generated_at, candidate_count, total_comments, deduped_count, created_at
       FROM rounds ORDER BY created_at DESC LIMIT ? OFFSET ?`
  ).bind(limit, offset).all();
  return json({
    rounds: results || [],
    total,
    limit,
    offset,
    has_more: offset + limit < total,
  });
}

async function handleClues(env, url) {
  const { limit, offset, order, stock, q, minScore, sort, section } = parseQuery(url);
  const round = url.searchParams.get("round") || "";
  const { clause, binds } = buildWhere({ stock, q, minScore, round, section });

  const { results: t } = await env.DB.prepare(
    `SELECT COUNT(*) AS n FROM clues ${clause}`
  ).bind(...binds).all();
  const total = (t && t[0] && t[0].n) || 0;

  // 排序：默认按时间(ts)；sort=jev 时按 Jev 价值分降序（价值优先浏览）
  const orderBy = sort === "jev"
    ? "jev_value DESC, ts DESC"
    : `ts ${order}, score DESC`;

  const { results } = await env.DB.prepare(
    `SELECT * FROM clues ${clause} ORDER BY ${orderBy} LIMIT ? OFFSET ?`
  ).bind(...binds, limit, offset).all();
  const clues = (results || []).map((r) => ({
    ...r,
    tags: safeJson(r.tags, []),
    stocks: safeJson(r.stocks, []),
  }));
  return json({
    clues,
    total,
    limit,
    offset,
    has_more: offset + limit < total,
  });
}

// 清理 KEEP_DAYS 天前「入库」的数据
// ⚠️ 两个历史坑，别再踩：
// 1) 单位陷阱：clues.ts 存的是「秒」，ingested_at / rounds.created_at 是「毫秒」。
//    曾拿毫秒级 cutoff 去比较秒级 ts，导致所有 clues 被误删（每天 00:00 清空的元凶）。
// 2) 口径陷阱（2026-10-28 修）：以前按 ts（评论自身时间）判过期，
//    于是「今天才抓到的、10 天前的老评论」刚入库就被追溯删除 →
//    前台「共 N 条」忽高忽低，今天是 4000+ 明天变 3000+。
//    现改为按 ingested_at（首次入库时间）判断，与 rounds.created_at 同一把尺子，
//    一条线索与它的首现轮次同生同灭。
//    迁移前入库的老行若 ingested_at=0，回退用 ts*1000；两者都无（时间未知）则不删。
async function cleanup(env) {
  const cutoffMs = Date.now() - KEEP_DAYS * 24 * 3600 * 1000;
  const timeExpr = "CASE WHEN ingested_at > 0 THEN ingested_at WHEN ts > 0 THEN ts * 1000 ELSE NULL END";
  const delClues = await env.DB.prepare(
    `DELETE FROM clues WHERE ${timeExpr} < ?`
  ).bind(cutoffMs).run();
  const delRounds = await env.DB.prepare("DELETE FROM rounds WHERE created_at > 0 AND created_at < ?").bind(cutoffMs).run();
  return {
    ok: true,
    keep_days: KEEP_DAYS,
    basis: "ingested_at",
    cutoff: new Date(cutoffMs).toISOString(),
    deleted: {
      clues: (delClues && delClues.meta && delClues.meta.changes) || 0,
      rounds: (delRounds && delRounds.meta && delRounds.meta.changes) || 0,
    },
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    try {
      if (path === "/api/ingest") {
        if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
        const auth = request.headers.get("authorization") || "";
        if (!env.INGEST_TOKEN || auth !== "Bearer " + env.INGEST_TOKEN) {
          return json({ error: "unauthorized" }, 401);
        }
        let body;
        try {
          body = await request.json();
        } catch {
          return json({ error: "invalid json" }, 400);
        }
        const res = await ingest(body, env);
        return json(res, res.ok ? 200 : 400);
      }

      if (path === "/api/cleanup") {
        if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
        const auth = request.headers.get("authorization") || "";
        if (!env.INGEST_TOKEN || auth !== "Bearer " + env.INGEST_TOKEN) {
          return json({ error: "unauthorized" }, 401);
        }
        const res = await cleanup(env);
        return json(res);
      }

      if (path === "/api/rounds") return await handleRounds(env, url);
      if (path === "/api/clues") return await handleClues(env, url);
      // 去重探针：客户端批量问「这批 clue_id 里哪些已在库」，
      // 拿到后只上传新线索 → 大幅缩小请求体与 D1 写入量
      if (path === "/api/exists") {
        if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
        const auth = request.headers.get("authorization") || "";
        if (!env.INGEST_TOKEN || auth !== "Bearer " + env.INGEST_TOKEN) {
          return json({ error: "unauthorized" }, 401);
        }
        return await handleExists(env, request);
      }
      // 命中上报：重复线索只更新计数，不写正文行
      if (path === "/api/hits") {
        if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
        const auth = request.headers.get("authorization") || "";
        if (!env.INGEST_TOKEN || auth !== "Bearer " + env.INGEST_TOKEN) {
          return json({ error: "unauthorized" }, 401);
        }
        return await handleHits(env, request);
      }

      // 其余一律交由静态资源（SPA 路由）
      return env.ASSETS.fetch(request);
    } catch (e) {
      return json({ error: String((e && e.message) || e) }, 500);
    }
  },

  // Cloudflare Cron Trigger：每日自动清理 10 天前数据
  async scheduled(event, env, ctx) {
    try {
      const res = await cleanup(env);
      console.log("[cron] cleanup", JSON.stringify(res));
    } catch (e) {
      console.error("[cron] cleanup failed", e);
    }
  },
};
