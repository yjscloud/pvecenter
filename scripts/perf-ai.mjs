#!/usr/bin/env node
/* ==========================================================================
   pvecenter — AI 助手基线压测（S1 / S3 / S6 / S7）

   为什么要有这个脚本：优化前后各说一句「好像快了」不算证据。这里按固定的场景
   矩阵打同一套请求，输出 p50 / p95 —— 单次测量在模型侧波动面前几乎没有意义。

   场景（编号沿用 docs/ai-perf 的矩阵）：

   * ``S1`` 无工具问答：量首字（prompt 体积、上游 TTFB）与整轮；
   * ``S3`` 一次只读工具：量「多一轮模型往返」的代价（工具分组后应显著缩短）；
   * ``S6`` 长会话追问：同一会话先灌 N 轮，再量第 N+1 轮（历史膨胀 / 摘要）；
   * ``S7`` 并发：同时发 N 轮，量排队（服务端 stage=queued 事件给出位置）。

   用法：

     PVECENTER_URL=https://panel.example.com \
     PVECENTER_USER=admin PVECENTER_PASS=*** \
     node scripts/perf-ai.mjs --host <host_id> --runs 10

   也可以用 ``PVECENTER_TOKEN`` 直接给访问令牌（CI / 无密码环境）。
   只依赖 Node 18+ 自带的 fetch，不引第三方库 —— 它要在任何一台机器上能跑。
   ========================================================================== */

const args = parseArgs(process.argv.slice(2));
const BASE = (args.url || process.env.PVECENTER_URL || 'http://127.0.0.1:8080').replace(/\/+$/, '');
const RUNS = Number(args.runs || 10);
const FILL = Number(args.fill || 12);
const SCENARIOS = String(args.scenarios || 'S1,S3')
  .split(',')
  .map((item) => item.trim().toUpperCase())
  .filter(Boolean);
const CONCURRENCY = String(args.concurrency || '')
  .split(',')
  .map((item) => Number(item.trim()))
  .filter((n) => Number.isFinite(n) && n > 0);

/* --------------------------------------------------------------- 基础设施 */

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item.startsWith('--')) continue;
    const key = item.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith('--')) {
      out[key] = 'true';
      continue;
    }
    out[key] = next;
    i += 1;
  }
  return out;
}

function fail(message) {
  console.error(`\n✗ ${message}\n`);
  process.exit(1);
}

/** 分位数：最近秩法，与后端 aiaudit._percentile 用同一套口径。 */
function percentile(values, q) {
  if (!values.length) return 0;
  const ordered = [...values].sort((a, b) => a - b);
  const index = Math.round((q / 100) * (ordered.length - 1));
  return Math.round(ordered[Math.max(0, Math.min(ordered.length - 1, index))]);
}

function ms(value) {
  if (!value) return '—';
  return value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(1)}s`;
}

let token = process.env.PVECENTER_TOKEN || '';

async function api(path, options = {}) {
  const res = await fetch(`${BASE}/api${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${options.method || 'GET'} ${path} → ${res.status} ${body.slice(0, 200)}`);
  }
  return res.json();
}

async function login() {
  if (token) return;
  const user = process.env.PVECENTER_USER || args.user;
  const pass = process.env.PVECENTER_PASS || args.pass;
  if (!user || !pass) {
    fail('需要 PVECENTER_USER / PVECENTER_PASS，或 PVECENTER_TOKEN');
  }
  const data = await api('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username: user, password: pass }),
  });
  if (data.mfa_required) fail('该账号开了两步验证：请用 PVECENTER_TOKEN 跑脚本');
  token = data.access_token;
}

/* ------------------------------------------------------------------ 一轮对话 */

/**
 * 跑一轮对话并计时。
 *
 * SSE 的 ``token`` 是「首字」，``done`` 是整轮结束。体检报告模式没有 token 事件，
 * 用 ``report_progress`` 当首字 —— 否则那一路的首字会被记成 0（看起来像免费）。
 */
async function turn(conversationId, text, mode = 'chat') {
  const started = performance.now();
  const res = await fetch(
    `${BASE}/api/ai/conversations/${encodeURIComponent(conversationId)}/messages/stream`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ text, mode }),
    },
  );
  if (!res.ok || !res.body) {
    const body = await res.text().catch(() => '');
    throw new Error(`stream → ${res.status} ${body.slice(0, 200)}`);
  }

  const result = { ttfbMs: 0, totalMs: 0, queueMs: 0, queuedAhead: 0, steps: 0 };
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf('\n\n');
    while (boundary >= 0) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf('\n\n');
      const line = chunk.split('\n').find((item) => item.startsWith('data:'));
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line.slice(5).trim());
      } catch {
        continue;
      }
      if (!result.ttfbMs && (event.type === 'token' || event.type === 'report_progress')) {
        result.ttfbMs = performance.now() - started;
      }
      if (event.type === 'stage' && event.stage === 'queued') {
        result.queuedAhead = Number(event.ahead || 0);
        result.queueMs = performance.now() - started;
      }
      if (event.type === 'thinking') result.steps = Math.max(result.steps, event.step || 0);
      if (event.type === 'error') throw new Error(event.message || 'stream error');
      if (event.type === 'done' || event.type === 'aborted') {
        result.totalMs = performance.now() - started;
        return result;
      }
    }
  }
  // 流断在没有 done 的情况下：如实记下已经等到的时长（脚本不抛错，交给调用方看 p95）
  result.totalMs = performance.now() - started;
  return result;
}

async function newConversation(hostId) {
  return api('/ai/conversations', {
    method: 'POST',
    body: JSON.stringify({ host_id: hostId }),
  });
}

const PROMPTS = {
  S1: '用一句话说明你现在能做什么，不要调用工具。',
  S3: '只做一件事：查一下这台机器的磁盘使用率和内存情况，各用一句话总结。',
  S6: '继续：用一句话复述这台机器刚才查到的磁盘与内存结论。',
  S7: '用一句话说明你现在能做什么，不要调用工具。',
};

/* -------------------------------------------------------------------- 场景 */

async function runScenario(name, hostId, repeat) {
  const samples = [];
  for (let i = 0; i < repeat; i += 1) {
    const conv = await newConversation(hostId);
    if (name === 'S6') {
      // 长会话：先灌历史。这一步的耗时不计入样本 —— 量的是「第 N+1 轮有多慢」
      for (let n = 0; n < FILL; n += 1) {
        await turn(conv.id, `第 ${n + 1} 轮：用一句话回答 1+1 等于几，不要调用工具。`);
      }
    }
    const result = await turn(conv.id, PROMPTS[name]);
    samples.push(result);
    process.stderr.write(
      `  ${name} #${i + 1}: 首字 ${ms(result.ttfbMs)} / 整轮 ${ms(result.totalMs)}` +
        `${result.steps ? ` / ${result.steps} 步` : ''}\n`,
    );
  }
  return samples;
}

async function runConcurrency(hostId, width, repeat) {
  const samples = [];
  for (let n = 0; n < repeat; n += 1) {
    const results = await Promise.all(
      Array.from({ length: width }, async () => {
        const conv = await newConversation(hostId);
        return turn(conv.id, PROMPTS.S7);
      }),
    );
    const queued = results.filter((item) => item.queuedAhead > 0).length;
    samples.push({
      queueMs: Math.max(...results.map((item) => item.queueMs)),
      totalMs: Math.max(...results.map((item) => item.totalMs)),
      queued,
    });
    process.stderr.write(
      `  S7(${width}) 第 ${n + 1} 批: ${queued}/${width} 排队, 最长排队 ${ms(
        Math.max(...results.map((item) => item.queueMs)),
      )}\n`,
    );
  }
  return samples;
}

/* -------------------------------------------------------------------- 主流程 */

async function main() {
  await login();
  const targets = await api('/ai/targets');
  const hosts = targets.hosts || [];
  if (!hosts.length) fail('这台面板没有可排查的主机（/api/ai/targets 为空）');
  const hostId = args.host || hosts[0].id;
  if (!args.host) {
    console.error(`! 没给 --host，用列表里第一台：${hosts[0].name || hostId}`);
  }
  console.error(`目标 ${BASE} · 主机 ${hostId} · 每场景 ${RUNS} 次\n`);

  const report = [];
  for (const name of SCENARIOS) {
    if (!PROMPTS[name]) {
      console.error(`? 未知场景 ${name}，跳过（可选 S1/S3/S6/S7）`);
      continue;
    }
    if (name === 'S7') continue;
    const samples = await runScenario(name, hostId, RUNS);
    report.push({
      scenario: name,
      ttfb: [percentile(samples.map((s) => s.ttfbMs), 50), percentile(samples.map((s) => s.ttfbMs), 95)],
      total: [percentile(samples.map((s) => s.totalMs), 50), percentile(samples.map((s) => s.totalMs), 95)],
      steps: percentile(samples.map((s) => s.steps), 50),
    });
  }
  for (const width of CONCURRENCY) {
    const samples = await runConcurrency(hostId, width, Math.max(2, Math.round(RUNS / 2)));
    report.push({
      scenario: `S7 x${width}`,
      ttfb: [0, 0],
      total: [percentile(samples.map((s) => s.totalMs), 50), percentile(samples.map((s) => s.totalMs), 95)],
      queue: [percentile(samples.map((s) => s.queueMs), 50), percentile(samples.map((s) => s.queueMs), 95)],
      steps: 0,
    });
  }

  console.error('\n场景           首字 p50 / p95        整轮 p50 / p95        排队 p95   步数');
  for (const row of report) {
    console.error(
      `${row.scenario.padEnd(14)}${`${ms(row.ttfb[0])} / ${ms(row.ttfb[1])}`.padEnd(22)}` +
        `${`${ms(row.total[0])} / ${ms(row.total[1])}`.padEnd(22)}` +
        `${ms(row.queue ? row.queue[1] : 0).padEnd(10)}${row.steps || '—'}`,
    );
  }
  if (args.json) process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

main().catch((error) => fail(error.message));
