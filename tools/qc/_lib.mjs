// _lib.mjs: tiny shared helpers for every skill script (no dependencies).
//
// This file is intentionally copied into each skill's scripts/ folder so a
// single skill can be copied on its own into ~/.claude/skills and still run.
// `npm run check:lib` verifies all copies are identical; edit one, then run
// `node test/check-lib-sync.mjs --write` to propagate.

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const FFMPEG = process.env.FFMPEG_PATH || 'ffmpeg';
export const FFPROBE = process.env.FFPROBE_PATH || 'ffprobe';

// ---------------------------------------------------------------------------
// CLI contract: every script declares a spec, and gets --help / --help --json
// for free. The JSON form is a machine-readable contract an agent can read
// before calling the script (inputs, outputs, how to verify).
// ---------------------------------------------------------------------------
export function parseArgs(spec, argv = process.argv.slice(2)) {
  const opts = {};
  const positional = [];
  for (const [k, o] of Object.entries(spec.options || {})) {
    if (o.default !== undefined) opts[k] = o.default;
    else if (o.type === 'boolean') opts[k] = false;
    else if (o.type === 'list') opts[k] = [];
  }
  if (argv.includes('--help') || argv.includes('-h')) {
    if (argv.includes('--json')) {
      process.stdout.write(JSON.stringify(contract(spec), null, 2) + '\n');
    } else {
      process.stdout.write(helpText(spec));
    }
    process.exit(0);
  }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) { positional.push(a); continue; }
    let key = a.slice(2);
    let val;
    if (key.includes('=')) { [key, ...val] = key.split('='); val = val.join('='); }
    const camel = key.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    const o = (spec.options || {})[camel];
    if (!o) fail(`unknown option --${key} (see --help)`);
    if (o.type === 'boolean') {
      opts[camel] = val === undefined ? true : !/^(false|0|no)$/i.test(val);
      continue;
    }
    if (val === undefined) {
      val = argv[++i];
      if (val === undefined) fail(`--${key} needs a value`);
    }
    if (o.type === 'number') {
      const n = Number(val);
      if (!Number.isFinite(n)) fail(`--${key} must be a number, got "${val}"`);
      opts[camel] = n;
    } else if (o.type === 'list') {
      opts[camel] = [...(opts[camel] || []), ...String(val).split(',').map((s) => s.trim()).filter(Boolean)];
    } else {
      opts[camel] = val;
    }
  }
  opts._ = positional;
  for (const [k, o] of Object.entries(spec.options || {})) {
    if (o.required && (opts[k] === undefined || opts[k] === '' || (Array.isArray(opts[k]) && !opts[k].length))) {
      fail(`missing required --${kebab(k)} (see --help)`);
    }
  }
  return opts;
}

const kebab = (s) => s.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());

export function contract(spec) {
  return {
    name: spec.name,
    summary: spec.summary,
    usage: spec.usage,
    inputs: Object.fromEntries(Object.entries(spec.options || {}).map(([k, o]) => [
      `--${kebab(k)}`, { type: o.type || 'string', required: !!o.required, default: o.default, description: o.desc },
    ])),
    outputs: spec.outputs || [],
    verify: spec.verify || null,
    exitCodes: spec.exitCodes || { 0: 'ok', 1: 'error or check failed' },
  };
}

function helpText(spec) {
  const lines = [`${spec.name}: ${spec.summary}`, ''];
  if (spec.usage) lines.push('Usage:', ...[].concat(spec.usage).map((u) => '  ' + u), '');
  lines.push('Options:');
  for (const [k, o] of Object.entries(spec.options || {})) {
    const flag = `--${kebab(k)}${o.type === 'boolean' ? '' : ' <' + (o.type || 'string') + '>'}`;
    const def = o.default !== undefined ? ` (default: ${JSON.stringify(o.default)})` : '';
    lines.push(`  ${flag.padEnd(30)} ${o.required ? '[required] ' : ''}${o.desc || ''}${def}`);
  }
  lines.push(`  ${'--help [--json]'.padEnd(30)} Show this help, or the machine-readable contract`);
  if (spec.outputs?.length) lines.push('', 'Outputs:', ...spec.outputs.map((o) => '  - ' + o));
  if (spec.verify) lines.push('', 'Verify:', '  ' + spec.verify);
  return lines.join('\n') + '\n';
}

// ---------------------------------------------------------------------------
// Results: scripts print exactly one JSON object on stdout as their last line.
// Progress goes to stderr so stdout stays parseable.
// ---------------------------------------------------------------------------
export function emit(obj) {
  process.stdout.write(JSON.stringify(obj, null, 2) + '\n');
}
export function log(...a) {
  process.stderr.write(a.join(' ') + '\n');
}
export function fail(message, extra = {}, code = 1) {
  emit({ ok: false, error: message, ...extra });
  process.exit(code);
}

// ---------------------------------------------------------------------------
// Process helpers
// ---------------------------------------------------------------------------
export function run(cmd, args, { allowFail = false, input } = {}) {
  const r = spawnSync(cmd, args, { encoding: 'utf8', maxBuffer: 1 << 28, input });
  if (r.error) {
    if (r.error.code === 'ENOENT') throw new Error(`${cmd} not found on PATH (install it or set FFMPEG_PATH / FFPROBE_PATH)`);
    throw r.error;
  }
  if (r.status !== 0 && !allowFail) {
    const tail = String(r.stderr || '').trim().split('\n').slice(-6).join('\n');
    throw new Error(`${path.basename(cmd)} exited ${r.status}: ${tail}`);
  }
  return { stdout: r.stdout || '', stderr: r.stderr || '', status: r.status };
}

// Binary-safe variant (raw frames on stdout).
export function runBuffer(cmd, args) {
  const r = spawnSync(cmd, args, { maxBuffer: 1 << 30 });
  if (r.error) throw r.error;
  if (r.status !== 0) throw new Error(`${path.basename(cmd)} exited ${r.status}: ${String(r.stderr).trim().split('\n').slice(-4).join('\n')}`);
  return r.stdout;
}

export function ffmpeg(args, opts) {
  return run(FFMPEG, ['-hide_banner', '-nostdin', '-y', ...args], opts);
}

export function probe(file) {
  if (!fs.existsSync(file)) throw new Error(`file not found: ${file}`);
  const { stdout } = run(FFPROBE, ['-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', file]);
  const j = JSON.parse(stdout);
  const v = (j.streams || []).find((s) => s.codec_type === 'video');
  const a = (j.streams || []).find((s) => s.codec_type === 'audio');
  const ratio = (r) => {
    if (!r) return null;
    const [n, d] = String(r).split('/').map(Number);
    const f = d ? n / d : n;
    return Number.isFinite(f) && f > 0 ? f : null;
  };
  return {
    file,
    duration: Number(j.format?.duration) || Number(v?.duration) || 0,
    sizeBytes: Number(j.format?.size) || 0,
    formatName: j.format?.format_name,
    video: v ? {
      codec: v.codec_name,
      width: v.width,
      height: v.height,
      // avg_frame_rate is the real playback rate; r_frame_rate can misreport VFR sources
      fps: ratio(v.avg_frame_rate) || ratio(v.r_frame_rate),
      pixFmt: v.pix_fmt,
      frames: Number(v.nb_frames) || null,
      duration: Number(v.duration) || null,
    } : null,
    audio: a ? {
      codec: a.codec_name,
      sampleRate: Number(a.sample_rate),
      channels: a.channels,
    } : null,
  };
}

// ---------------------------------------------------------------------------
// Brand / style packs (brands/*.json). Missing keys fall back to defaults.
// ---------------------------------------------------------------------------
export const DEFAULT_BRAND = {
  name: 'default',
  canvas: { width: 1080, height: 1920, fps: 30 },
  font: { family: 'Sans', weight: 'bold', file: null },
  colors: { text: '#111111', plate: '#ffffff', plateOpacity: 0.92, accent: '#ffd400', outline: '#000000' },
  plate: {
    width: 760, top: 150, radius: 36, padX: 56, padY: 40,
    maxLines: 2, targetFill: 0.86, fontMin: 40, fontMax: 110, lineHeight: 1.12, image: null,
  },
  captions: {
    fontSize: 78, marginV: 520, outline: 6, shadow: 0, uppercase: false,
    maxWords: 3, maxChars: 18, highlightActiveWord: true,
  },
};

export function scriptDir(metaUrl) {
  return path.dirname(fileURLToPath(metaUrl));
}

export function loadBrand(brandPath, metaUrl) {
  let file = brandPath;
  if (!file && metaUrl) {
    // default: <repo>/brands/example.json when running from a full checkout
    const guess = path.resolve(scriptDir(metaUrl), '..', '..', '..', 'brands', 'example.json');
    if (fs.existsSync(guess)) file = guess;
  }
  if (!file) return structuredClone(DEFAULT_BRAND);
  const user = JSON.parse(fs.readFileSync(file, 'utf8'));
  const merged = deepMerge(structuredClone(DEFAULT_BRAND), user);
  merged._file = path.resolve(file);
  // resolve relative asset paths against the brand file
  const rel = (p) => (p && !path.isAbsolute(p) ? path.resolve(path.dirname(file), p) : p);
  merged.font.file = rel(merged.font.file);
  merged.plate.image = rel(merged.plate.image);
  return merged;
}

function deepMerge(a, b) {
  for (const [k, v] of Object.entries(b || {})) {
    if (v && typeof v === 'object' && !Array.isArray(v) && a[k] && typeof a[k] === 'object') a[k] = deepMerge(a[k], v);
    else a[k] = v;
  }
  return a;
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------
export const round = (n, p = 3) => Math.round(n * 10 ** p) / 10 ** p;
export const slug = (s) => path.basename(s).toLowerCase().replace(/\.[^.]+$/, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'clip';
export function ensureDir(d) { fs.mkdirSync(d, { recursive: true }); return d; }
export function defaultOut(input, suffix, ext) {
  // outputs go to ./edit/ beside the source footage, never over the original
  const dir = path.join(path.dirname(path.resolve(input)), 'edit');
  return path.join(dir, `${path.basename(input).replace(/\.[^.]+$/, '')}${suffix}${ext || path.extname(input)}`);
}
export function parseSize(s) {
  const m = String(s).match(/^(\d+)x(\d+)$/i);
  if (!m) throw new Error(`size must look like 1080x1920, got "${s}"`);
  return { width: Number(m[1]), height: Number(m[2]) };
}
