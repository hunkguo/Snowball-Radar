/**
 * 雪球雷达 · 线索台  Worker 后端
 *
 * 职责：
 *   1) POST /api/ingest   —— xueqiu.exe 定时上传 { meta, candidates, comments }，鉴权后写入 D1
 *   2) GET  /api/rounds   —— 轮次列表（倒序，支持分页）
 *   3) GET  /api/clues    —— 线索（按 ts 时间排序，支持分页 + 标的/关键词/分数筛选）
 *   4) POST /api/cleanup  —— 手动触发清理（token 鉴权），删 10 天前数据
 *   5) 每日 Cron 自动清理 10 天前数据
 *   6) 其余路径            —— 交给 ASSETS 托管 Vue 前台（SPA）
 *
 * 绑定：env.DB (D1)、env.INGEST_TOKEN (secret)、env.ASSETS
 */

const BATCH_LIMIT = 100; // D1 单次 batch 上限
const KEEP_DAYS = 10;    // 保留天数，超过则自动清理
const MAX_PAGE = 200;    // 单页上限

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
  const candidates = Array.isArray(body && body.candidates) ? body.candidates : [];
  const comments = Array.isArray(body && body.comments) ? body.comments : [];

  const source = meta.source || "unknown";
  const title = meta.title || "";
  const hashtag = meta.hashtag || "";
  const generatedAt = meta.generated_at || "";
  const candidateCount = candidates.length;
  const totalComments = meta.total_comments != null ? meta.total_comments : comments.length;
  const createdAt = Date.now();
  const yearHint = (generatedAt || roundId).slice(0, 4);
  const gen = parseGeneratedAt(generatedAt); // 评论时间缺失时兜底用上传时间

  const stmts = [];

  // 1) 轮次（覆盖写）
  stmts.push(
    env.DB.prepare(
      `INSERT OR REPLACE INTO rounds
         (round_id, source, title, hashtag, generated_at, candidate_count, total_comments, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    ).bind(roundId, source, title, hashtag, generatedAt, candidateCount, totalComments, createdAt)
  );

  // 2) 候选线索（按 round_id + clue_id 覆盖），ts 用于排序，date 为真实发布日期（北京时间）
  for (const c of candidates) {
    const p = parseTs(c.time_str, yearHint);
    const ts = p.ts || gen.ts;        // 时间串解析不出则用上传时间兜底
    const date = p.date || gen.date;  // 避免出现未知日期
    stmts.push(
      env.DB.prepare(
        `INSERT OR REPLACE INTO clues
           (round_id, clue_id, user_name, time_str, like_count, reply_count, text, score, tags, stocks, section, ts, date)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
      ).bind(
        roundId,
        String(c.id != null ? c.id : ""),
        c.user_name || "",
        c.time_str || "",
        Number(c.like_count) || 0,
        Number(c.reply_count) || 0,
        c.text || "",
        Number(c.score) || 0,
        JSON.stringify(c.tags || []),
        JSON.stringify(c.stocks || []),
        c.section || "",
        ts,
        date
      )
    );
  }

  // 注：原始评论不再单独落库（raw_comments 已删除）。
  // comments 仅用于本次统计 total_comments，候选线索已写入 clues。

  // 分批写入（D1 单次 batch 上限 100）
  for (let i = 0; i < stmts.length; i += BATCH_LIMIT) {
    await env.DB.batch(stmts.slice(i, i + BATCH_LIMIT));
  }

  return { ok: true, round_id: roundId, clues: candidates.length, comments: comments.length };
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
  return { limit, offset, order, stock, q, minScore };
}

// 组装 WHERE 与绑定参数
function buildWhere({ stock, q, minScore, round }) {
  const where = [];
  const binds = [];
  if (round) { where.push("round_id = ?"); binds.push(round); }
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
    `SELECT round_id, source, title, hashtag, generated_at, candidate_count, total_comments, created_at
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
  const { limit, offset, order, stock, q, minScore } = parseQuery(url);
  const round = url.searchParams.get("round") || "";
  const { clause, binds } = buildWhere({ stock, q, minScore, round });

  const { results: t } = await env.DB.prepare(
    `SELECT COUNT(*) AS n FROM clues ${clause}`
  ).bind(...binds).all();
  const total = (t && t[0] && t[0].n) || 0;

  const { results } = await env.DB.prepare(
    `SELECT * FROM clues ${clause} ORDER BY ts ${order}, score DESC LIMIT ? OFFSET ?`
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

// 清理 KEEP_DAYS 天前的数据（ts/created_at 早于 cutoff 的）
async function cleanup(env) {
  const cutoff = Date.now() - KEEP_DAYS * 24 * 3600 * 1000;
  const delClues = await env.DB.prepare("DELETE FROM clues WHERE ts > 0 AND ts < ?").bind(cutoff).run();
  const delRounds = await env.DB.prepare("DELETE FROM rounds WHERE created_at > 0 AND created_at < ?").bind(cutoff).run();
  return {
    ok: true,
    keep_days: KEEP_DAYS,
    cutoff: new Date(cutoff).toISOString(),
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
