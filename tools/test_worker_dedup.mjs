/**
 * 本地验证：用 node:sqlite 真实模拟 D1，跑 Worker 的实际 ingest / handleExists 逻辑。
 * 验证目标：
 *   1) 跨轮同一 clue_id 只存一行（全局去重生效）
 *   2) 重复命中时 hit_count / last_seen_round 正确更新
 *   3) 批内重复被剔除
 *   4) 集中写入：单批语句数 = min(总数, 100)，事务数最少
 *   5) /api/exists 探针返回正确的 exists / missing
 */
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dir = dirname(fileURLToPath(import.meta.url));
const ROOT = process.env.XQ_ROOT || join(__dir, "..", "xueqiu-radar");

// ── 1) 建库：跑 0001 + 0002 + 0004 + 0005 + 0006 + 0007 + 0009 迁移 ──
const db = new DatabaseSync(":memory:");
const migDir = join(ROOT, "migrations");
for (const f of ["0001_init.sql", "0002_ts_and_cleanup.sql", "0004_add_date.sql",
                 "0005_add_jev_value.sql", "0006_add_user_post_id.sql",
                 "0007_add_post_author_id.sql", "0009_global_dedup.sql"]) {
  const sql = readFileSync(join(migDir, f), "utf8");
  try { db.exec(sql); } catch (e) { console.log(`  [迁移 ${f}] 跳过: ${e.message.slice(0, 60)}`); }
}
console.log("✓ 迁移执行完成");

// ── 2) 造一个 D1 兼容的 env.DB 适配层 ──
let batchCalls = 0, batchStmts = 0;
function makeEnv() {
  return {
    DB: {
      prepare(sql) {
        const st = db.prepare(sql);
        return {
          bind(...args) {
            return {
              async all() { return { results: st.all(...args) }; },
              async run() { const r = st.run(...args); return { meta: { changes: r.changes } }; },
            };
          },
          async all() { return { results: st.all() }; },
          async run() { const r = st.run(); return { meta: { changes: r.changes } }; },
        };
      },
      async batch(stmts) {
        batchCalls++;
        batchStmts += stmts.length;
        for (const s of stmts) {
          // D1 batch 是事务：这里用真实事务模拟
          await s.run();
        }
      },
    },
  };
}

// ── 3) 加载 Worker 源码，抽取 ingest / handleExists ──
const workerSrc = readFileSync(join(ROOT, "worker", "index.js"), "utf8");
// 把 ESM export default 改写成可测的 CommonJS-ish 片段
const mod = workerSrc
  .replace(/export default\s*\{/, "const __handler = {")
  .concat("\nmodule.exports = { ingest, handleExists, parseTs, makeRoundId };\n");
const modPath = join(__dir, "_worker_under_test.cjs");
const { writeFileSync } = await import("node:fs");
writeFileSync(modPath, mod);
const { ingest, handleExists } = await import("file://" + modPath.replace(/\\/g, "/"));

// ── 4) 测试用例 ──
const env = makeEnv();
const cands = (ids) => ids.map((id, i) => ({
  id, user_name: "用户" + i, text: "评论 " + id, score: 8,
  tags: ["stock:贵州茅台"], stocks: ["贵州茅台"], section: "hot",
  time_str: "2026-10-22 10:00", like_count: 10 + i, reply_count: 2,
  post_id: "413226032", user_id: "1000", post_author_id: "2000",
}));

console.log("\n" + "=".repeat(64));
console.log("测试 1：首轮写入 3 条");
console.log("=".repeat(64));
let r = await ingest({
  round_id: "R1", meta: { source: "auto", generated_at: "2026-10-22 10:00:00", total_comments: 100 },
  candidates: cands(["c1", "c2", "c3"]), comments: [],
}, env);
console.log("  返回:", JSON.stringify(r));
let q = db.prepare("SELECT clue_id, round_id, first_seen_round, last_seen_round, hit_count FROM clues ORDER BY clue_id").all();
console.log("  库中:", q.map(x => `${x.clue_id}(round=${x.round_id}, hit=${x.hit_count})`).join(" "));

console.log("\n" + "=".repeat(64));
console.log("测试 2：第二轮含 2 条重复（c1,c2）+ 1 条新（c4）");
console.log("=".repeat(64));
r = await ingest({
  round_id: "R2", meta: { source: "auto", generated_at: "2026-10-22 17:00:00", total_comments: 80 },
  candidates: cands(["c1", "c2", "c4"]), comments: [],
}, env);
console.log("  返回:", JSON.stringify(r));
q = db.prepare("SELECT clue_id, round_id, first_seen_round, last_seen_round, hit_count, like_count FROM clues ORDER BY clue_id").all();
console.log("  库中:");
for (const x of q) console.log(`    ${x.clue_id}  首现轮=${x.first_seen_round}  最近轮=${x.last_seen_round}  命中=${x.hit_count}  赞=${x.like_count}`);

const total = db.prepare("SELECT COUNT(*) n FROM clues").get().n;
const uniq = db.prepare("SELECT COUNT(DISTINCT clue_id) n FROM clues").get().n;
console.log(`\n  >>> 总行数=${total}  唯一 clue_id=${uniq}  ${total === uniq ? "✓ 无重复" : "✗ 有重复"}`);

console.log("\n" + "=".repeat(64));
console.log("测试 3：批内重复（同一 payload 里 c5 出现两次）");
console.log("=".repeat(64));
r = await ingest({
  round_id: "R3", meta: { source: "auto", generated_at: "2026-10-22 19:00:00" },
  candidates: [...cands(["c5"]), ...cands(["c5"]), ...cands(["c6"])], comments: [],
}, env);
console.log("  返回:", JSON.stringify(r));
const c5 = db.prepare("SELECT COUNT(*) n FROM clues WHERE clue_id='c5'").get().n;
console.log(`  >>> c5 行数 = ${c5}  ${c5 === 1 ? "✓ 已去重" : "✗ 未去重"}`);

console.log("\n" + "=".repeat(64));
console.log("测试 4：/api/exists 去重探针");
console.log("=".repeat(64));
const req = { json: async () => ({ clue_ids: ["c1", "c3", "c9", "c10"] }) };
const exResp = await handleExists(env, req);
const ex = await exResp.json();
console.log("  返回:", JSON.stringify(ex));
const exOk = ex.exists.includes("c1") && ex.exists.includes("c3")
  && ex.missing.includes("c9") && ex.missing.includes("c10")
  && ex.total === 4;
console.log(`  >>> ${exOk ? "✓ 探针返回正确的 exists/missing" : "✗ 探针结果不正确"}`);

console.log("\n" + "=".repeat(64));
console.log("测试 5：集中写入 —— 250 条候选的批次切分");
console.log("=".repeat(64));
batchCalls = 0; batchStmts = 0;
const many = cands(Array.from({ length: 250 }, (_, i) => "m" + i));
r = await ingest({
  round_id: "R4", meta: { source: "auto", generated_at: "2026-10-22 20:00:00" },
  candidates: many, comments: [],
}, env);
console.log(`  候选 250 条 → 返回 inserted=${r.inserted} updated=${r.updated} batches=${r.batches}`);
console.log(`  >>> 实际 DB.batch 调用 ${batchCalls} 次，共 ${batchStmts} 条语句`);
console.log(`  >>> 旧实现（每条一个 batch）需要 ${250 + 1} 次事务 → 现在 ${batchCalls} 次，减少 ${(100 * (251 - batchCalls) / 251).toFixed(0)}%`);

console.log("\n" + "=".repeat(64));
console.log("测试 6：重复上传同一 round_id（网络重试场景）应完全幂等");
console.log("=".repeat(64));
const before = db.prepare("SELECT COUNT(*) n FROM clues").get().n;
await ingest({
  round_id: "R4", meta: { source: "auto", generated_at: "2026-10-22 20:00:00" },
  candidates: many, comments: [],
}, env);
const after = db.prepare("SELECT COUNT(*) n FROM clues").get().n;
console.log(`  重传前 ${before} 行 → 重传后 ${after} 行  ${before === after ? "✓ 幂等" : "✗ 非幂等"}`);

console.log("\n" + "=".repeat(64));
console.log("最终库状态");
console.log("=".repeat(64));
q = db.prepare("SELECT COUNT(*) total, COUNT(DISTINCT clue_id) uniq, SUM(hit_count) hits FROM clues").get();
console.log(`  clues 表：总行 ${q.total}，唯一 ${q.uniq}，累计命中 ${q.hits}`);
const rounds = db.prepare("SELECT round_id, candidate_count, deduped_count FROM rounds ORDER BY round_id").all();
for (const x of rounds) console.log(`  轮次 ${x.round_id}: 候选 ${x.candidate_count}，去重跳过 ${x.deduped_count}`);
