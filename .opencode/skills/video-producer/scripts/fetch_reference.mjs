#!/usr/bin/env node
/**
 * fetch_reference.mjs — 抓取抖音参考成品
 *
 * 用途：给定抖音分享链接或视频 ID，用真实浏览器打开页面，截获媒体流并落盘，
 *       同时抓取标题/作者/文案等元信息，作为后续"拆解"阶段的输入。
 *
 * 设计要点：
 *   - 不依赖第三方解析 API，降低风控与服务失效风险
 *   - 候选流按来源与体积打分，下载后必须通过 ffprobe 校验才采纳（避免抓到页面占位素材）
 *   - 支持直链 mp4 与 HLS(m3u8) 两种形态
 *   - 产出 sidecar json，保证下游可追溯（来源、抓取时间、校验和）
 *
 * 用法：
 *   node fetch_reference.mjs --url "https://v.douyin.com/xxxx/" --out 参考视频/
 *   node fetch_reference.mjs --url "7681730998930188922" --out 参考视频/ --headed --debug
 */

import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { mkdir, writeFile, stat, rm, readFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
import { createHash } from 'node:crypto';

const execFileAsync = promisify(execFile);
const require = createRequire(import.meta.url);

const ROOT = path.resolve(import.meta.dirname, '../../../..');
const TOOLS_NODE = process.env.VIDEO_PIPELINE_NODE_MODULES
  || path.join(ROOT, '.tools/node/node_modules');
const FFMPEG = process.env.VIDEO_PIPELINE_FFMPEG
  || '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg';
const FFPROBE = process.env.VIDEO_PIPELINE_FFPROBE
  || FFMPEG.replace(/ffmpeg$/, 'ffprobe');

/** 页面静态资源宿主，出现这些前缀的 mp4 一律视为 UI 素材 */
const STATIC_ASSET_PATTERNS = [
  /douyin-pc-web/i,
  /douyinstatic\.com\/obj\//i,
  /\/obj\/douyin-/i,
];

function parseArgs(argv) {
  const out = { out: '参考视频', headed: false, timeout: 60000, minBytes: 300 * 1024, debug: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--url') out.url = argv[++i];
    else if (a === '--out') out.out = argv[++i];
    else if (a === '--name') out.name = argv[++i];
    else if (a === '--headed') out.headed = true;
    else if (a === '--debug') out.debug = true;
    else if (a === '--timeout') out.timeout = Number(argv[++i]) * 1000;
    else if (a === '--min-bytes') out.minBytes = Number(argv[++i]);
    else if (a === '-h' || a === '--help') out.help = true;
  }
  return out;
}

function log(step, msg) {
  process.stderr.write(`[${step}] ${msg}\n`);
}

async function loadPlaywright() {
  try {
    const entry = require.resolve('playwright', { paths: [TOOLS_NODE] });
    const mod = await import(pathToFileURL(entry).href);
    return mod.chromium ?? mod.default?.chromium;
  } catch (err) {
    throw new Error(
      `找不到 playwright。请先运行 scripts/setup.sh，或手动执行：\n` +
      `  npm --prefix .tools/node install playwright && npx --prefix .tools/node playwright install chromium\n` +
      `原始错误：${err.message}`
    );
  }
}

/** 把分享短链 / 纯数字 ID / 完整 URL 统一成视频 ID */
async function resolveAwemeId(input) {
  if (/^\d{15,25}$/.test(input.trim())) return input.trim();

  let current = input.trim();
  for (let hop = 0; hop < 6; hop++) {
    const m = current.match(/(?:video|note)\/(\d{15,25})/);
    if (m) return m[1];

    const res = await fetch(current, {
      redirect: 'manual',
      headers: {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
      },
    });
    const location = res.headers.get('location');
    if (!location) break;
    current = new URL(location, current).href;
  }
  throw new Error(`无法从输入中解析出视频 ID：${input}`);
}

function isMediaResponse(url, contentType) {
  if (contentType && /^(video\/|application\/vnd\.apple\.mpegurl)/i.test(contentType)) return true;
  return /\.(mp4|m3u8)(\?|$)/i.test(url) || /douyinvod\.com|\/aweme\/v1\/play/i.test(url);
}

function isStaticAsset(url) {
  return STATIC_ASSET_PATTERNS.some((re) => re.test(url));
}

function scoreCandidate(c) {
  const isVod = /douyinvod\.com|bytecdn\.cn|byteimg|aweme\/v1\/play/i.test(c.url);
  const isHls = /\.m3u8(\?|$)/i.test(c.url);
  // 抖音自有 CDN 优先；同源下按体积；HLS 单文件体积小但可能更完整，给固定加成
  return (isVod ? 1e12 : 0) + (isHls ? 5e11 : 0) + Math.min(c.length, 5e11);
}

async function probe(file) {
  const { stdout } = await execFileAsync(FFPROBE, [
    '-v', 'error',
    '-show_entries', 'format=duration',
    '-show_entries', 'stream=codec_type,codec_name,width,height,r_frame_rate',
    '-of', 'json', file,
  ]);
  return JSON.parse(stdout);
}

/** 下载候选并校验；返回第一个真正可用的视频文件路径 */
async function materialize(context, candidate, destPath, args) {
  const headers = { Referer: 'https://www.douyin.com/', 'User-Agent': 'Mozilla/5.0' };
  const isHls = /\.m3u8(\?|$)/i.test(candidate.url);

  if (isHls) {
    await execFileAsync(FFMPEG, [
      '-y', '-loglevel', 'error',
      '-headers', `Referer: https://www.douyin.com/\r\n`,
      '-protocol_whitelist', 'file,http,https,tcp,tls,crypto',
      '-i', candidate.url,
      '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
      destPath,
    ], { timeout: 300000 });
  } else {
    let bytes = null;
    const res = await context.request.get(candidate.url, { headers, timeout: 180000 }).catch(() => null);
    if (res && res.ok()) bytes = await res.body();
    if (!bytes) {
      const fallback = await fetch(candidate.url, { headers });
      if (!fallback.ok) throw new Error(`下载失败 HTTP ${fallback.status}`);
      bytes = Buffer.from(await fallback.arrayBuffer());
    }
    await writeFile(destPath, bytes);
  }

  const info = await probe(destPath).catch(() => null);
  const video = info?.streams?.find((s) => s.codec_type === 'video');
  const duration = Number(info?.format?.duration || 0);
  if (!video || duration < 1) {
    await rm(destPath, { force: true });
    throw new Error(`产物不合格（无视频流或时长 <1s）：${JSON.stringify(info?.format || {})}`);
  }
  return { info, duration, video };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.url) {
    console.log('用法: node fetch_reference.mjs --url <分享链接|视频ID> [--out 参考视频] [--name reference] [--headed] [--debug]');
    process.exit(args.help ? 0 : 2);
  }

  const chromium = await loadPlaywright();
  const awemeId = await resolveAwemeId(args.url);
  log('resolve', `aweme_id=${awemeId}`);

  const pageUrl = `https://www.douyin.com/video/${awemeId}`;
  const outDir = path.resolve(args.out);
  await mkdir(outDir, { recursive: true });
  const baseName = args.name || 'reference';

  const browser = await chromium.launch({
    headless: !args.headed,
    args: ['--disable-blink-features=AutomationControlled', '--autoplay-policy=no-user-gesture-required'],
  });

  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    extraHTTPHeaders: { 'Accept-Language': 'zh-CN,zh;q=0.9' },
  });

  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });

  const page = await context.newPage();
  const mediaCandidates = new Map();

  page.on('response', (res) => {
    const url = res.url();
    const headers = res.headers();
    const ct = headers['content-type'] || '';
    if (!isMediaResponse(url, ct)) return;
    if (isStaticAsset(url)) return;

    const len = Number(headers['content-length'] || 0);
    const prev = mediaCandidates.get(url);
    if (!prev || len > prev.length) {
      mediaCandidates.set(url, { url, contentType: ct, length: len, status: res.status() });
      if (args.debug) log('media', `${ct || 'n/a'} ${len || '?'}B ${url}`);
    }
  });

  log('open', pageUrl);
  await page.goto(pageUrl, { waitUntil: 'domcontentloaded', timeout: args.timeout });

  await page.waitForSelector('video', { timeout: args.timeout }).catch(() => {});
  await page.evaluate(async () => {
    const v = document.querySelector('video');
    if (!v) return;
    v.muted = true;
    try { await v.play(); } catch { /* 自动播放被拦截时依赖已发出的请求 */ }
  }).catch(() => {});

  // 等到出现一条像样的候选，或超时
  const deadline = Date.now() + Math.min(args.timeout, 40000);
  while (Date.now() < deadline) {
    const usable = [...mediaCandidates.values()]
      .some((c) => c.length >= args.minBytes || /douyinvod|\.m3u8/i.test(c.url));
    if (usable) break;
    await page.waitForTimeout(500);
  }

  const meta = await page.evaluate(() => {
    const pick = (sel) => document.querySelector(sel)?.content || '';
    return {
      pageTitle: document.title,
      ogTitle: pick('meta[property="og:title"]'),
      ogDescription: pick('meta[property="description"]') || pick('meta[name="description"]'),
      ogImage: pick('meta[property="og:image"]'),
      canonical: document.querySelector('link[rel="canonical"]')?.href || '',
    };
  }).catch(() => ({}));

  // 兜底：<video> 元素自身地址
  const elementSrc = await page.evaluate(() => {
    const v = document.querySelector('video');
    return v?.currentSrc || v?.src || '';
  }).catch(() => '');
  if (elementSrc && /^https?:/.test(elementSrc) && !isStaticAsset(elementSrc)) {
    if (!mediaCandidates.has(elementSrc)) {
      mediaCandidates.set(elementSrc, { url: elementSrc, contentType: 'video/mp4', length: 0, status: 200 });
    }
  }

  const candidates = [...mediaCandidates.values()].sort((a, b) => scoreCandidate(b) - scoreCandidate(a));

  if (args.debug) {
    log('candidates', `${candidates.length} 条，按优先级排列：`);
    candidates.forEach((c, i) => log('  #' + i, `${c.contentType || 'n/a'} ${c.length || '?'}B ${c.url.slice(0, 160)}`));
  }

  if (!candidates.length) {
    await browser.close();
    throw new Error(
      '未截获到视频流。可能原因：页面要求登录/验证、视频已删除、或需要人工过验证码。\n' +
      '建议：加 --headed 重新运行，手动完成验证后再观察。'
    );
  }

  const mp4Path = path.join(outDir, `${baseName}.mp4`);
  let chosen = null;
  let result = null;
  const errors = [];

  // 浏览器必须保持打开：context.request 依赖它携带 cookie
  for (const c of candidates.slice(0, 5)) {
    log('download', `尝试 ${c.length || '?'}B ${c.url.slice(0, 100)}`);
    try {
      result = await materialize(context, c, mp4Path, args);
      chosen = c;
      break;
    } catch (err) {
      errors.push(`${c.url.slice(0, 80)} → ${err.message}`);
      log('retry', err.message);
    }
  }

  await browser.close();

  if (!chosen) {
    throw new Error('所有候选流都未通过校验：\n' + errors.map((e) => '  - ' + e).join('\n'));
  }

  const bytes = await readFile(mp4Path);
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  const sidecar = {
    schemaVersion: 1,
    source: {
      input: args.url,
      awemeId,
      pageUrl,
      fetchedAt: new Date().toISOString(),
      mediaUrl: chosen.url,
      contentType: chosen.contentType,
    },
    file: {
      path: path.relative(ROOT, mp4Path),
      bytes: bytes.length,
      sha256,
    },
    media: {
      duration: result.duration,
      video: result.video,
      streams: result.info.streams,
    },
    meta,
  };
  await writeFile(path.join(outDir, `${baseName}.json`), JSON.stringify(sidecar, null, 2) + '\n');

  const st = await stat(mp4Path);
  log('done', `${path.relative(ROOT, mp4Path)} ${(st.size / 1e6).toFixed(2)}MB ${result.video.width}x${result.video.height} ${result.duration.toFixed(2)}s`);
  log('meta', `标题：${meta.ogTitle || meta.pageTitle || '未知'}`);
}

main().catch((err) => {
  log('error', err.message);
  process.exit(1);
});
