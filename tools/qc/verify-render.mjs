#!/usr/bin/env node
// verify-render.mjs: automated QC gate for a finished render (ffprobe + ffmpeg
// analysis filters). Checks resolution, fps, duration, codec, audio presence,
// silence, loudness and true peak, black frames (especially a black opening
// frame) and optional frozen video. Exit 1 if any error-level check fails.
import fs from 'node:fs';
import path from 'node:path';
import { parseArgs, emit, log, run, probe, round, FFMPEG, parseSize } from './_lib.mjs';

const PROFILES = {
  shorts: { size: '1080x1920', fps: 30, minDuration: 1, maxDuration: 180 },
  reels: { size: '1080x1920', fps: 30, minDuration: 1, maxDuration: 90 },
  tiktok: { size: '1080x1920', fps: 30, minDuration: 1, maxDuration: 600 },
  square: { size: '1080x1080', fps: 30, minDuration: 1, maxDuration: 600 },
  landscape: { size: '1920x1080', fps: 30, minDuration: 1, maxDuration: 43200 },
};

const spec = {
  name: 'verify-render',
  summary: 'QC a render: resolution, fps, duration, codecs, audio level/peak, black + frozen frames.',
  usage: [
    'node verify-render.mjs --input final.mp4 --profile shorts --write-report',
    'node verify-render.mjs --input final.mp4 --size 1080x1920 --fps 30 --max-duration 60',
  ],
  options: {
    input: { required: true, desc: 'Rendered file' },
    profile: { desc: `Preset expectations: ${Object.keys(PROFILES).join(' | ')}` },
    size: { desc: 'Expected WxH' },
    fps: { type: 'number', desc: 'Expected fps (tolerance 0.5)' },
    minDuration: { type: 'number', desc: 'Minimum seconds' },
    maxDuration: { type: 'number', desc: 'Maximum seconds' },
    noAudio: { type: 'boolean', desc: 'The render is meant to be silent (skip audio checks)' },
    maxTruePeak: { type: 'number', default: -0.5, desc: 'Error if true peak (dBTP) is above this' },
    loudness: { default: '-20:-9', desc: 'Warn if integrated LUFS is outside min:max' },
    silenceDb: { type: 'number', default: -60, desc: 'Error if integrated loudness is below this (silent audio)' },
    maxBlackRatio: { type: 'number', default: 0.1, desc: 'Error if more than this fraction of the video is black' },
    blackOpening: { type: 'boolean', default: true, desc: 'Error if the video opens on black (>0.1s)' },
    freeze: { type: 'number', default: 0, desc: 'Warn on frozen video longer than N seconds (0 = skip)' },
    writeReport: { type: 'boolean', desc: 'Write <input>.qc.json beside the file (approve.mjs reads it)' },
    report: { desc: 'Write the report JSON to this path instead' },
  },
  outputs: ['JSON report { ok, checks[{ name, level, ok, value, expected }] }', 'optional <input>.qc.json'],
  verify: 'ok === true. Then look at the proof sheet: automated checks cannot judge content.',
};

const o = parseArgs(spec);
const prof = o.profile ? PROFILES[o.profile] : {};
if (o.profile && !prof) { emit({ ok: false, error: `unknown profile ${o.profile}` }); process.exit(1); }
const exp = {
  size: o.size || prof.size, fps: o.fps ?? prof.fps,
  minDuration: o.minDuration ?? prof.minDuration, maxDuration: o.maxDuration ?? prof.maxDuration,
};
const checks = [];
const add = (name, ok, value, expected, level = 'error') => checks.push({ name, level, ok: !!ok, value, expected });

let info;
try { info = probe(o.input); } catch (e) { emit({ ok: false, error: e.message }); process.exit(1); }
add('file-not-empty', info.sizeBytes > 1024, info.sizeBytes, '> 1KB');
add('has-video', !!info.video, info.video?.codec || null, 'video stream');
const v = info.video || {};
if (exp.size) {
  const s = parseSize(exp.size);
  add('resolution', v.width === s.width && v.height === s.height, `${v.width}x${v.height}`, exp.size);
}
if (exp.fps) add('fps', Math.abs((v.fps || 0) - exp.fps) <= 0.5, round(v.fps || 0, 3), exp.fps);
if (exp.minDuration != null) add('min-duration', info.duration >= exp.minDuration, round(info.duration), `>= ${exp.minDuration}s`);
if (exp.maxDuration != null) add('max-duration', info.duration <= exp.maxDuration, round(info.duration), `<= ${exp.maxDuration}s`);
add('codec-h264', v.codec === 'h264', v.codec, 'h264 (widest compatibility)', 'warn');
add('pix-fmt-yuv420p', v.pixFmt === 'yuv420p', v.pixFmt, 'yuv420p (plays everywhere)', 'warn');

if (!o.noAudio) {
  add('has-audio', !!info.audio, info.audio?.codec || null, 'audio stream');
  if (info.audio) {
    const { stderr } = run(FFMPEG, ['-hide_banner', '-nostdin', '-i', o.input, '-vn', '-af',
      'loudnorm=print_format=json', '-f', 'null', '-'], { allowFail: true });
    const m = stderr.match(/\{[^{}]*"input_i"[^{}]*\}/s);
    if (m) {
      const j = JSON.parse(m[0]);
      const I = Number(j.input_i), TP = Number(j.input_tp);
      const [lo, hi] = String(o.loudness).split(':').map(Number);
      add('audio-not-silent', Number.isFinite(I) && I > o.silenceDb, I, `> ${o.silenceDb} LUFS`);
      add('true-peak', Number.isFinite(TP) && TP <= o.maxTruePeak, TP, `<= ${o.maxTruePeak} dBTP`);
      add('loudness-range', I >= lo && I <= hi, I, `${lo}..${hi} LUFS (fix with audio-polish/loudnorm)`, 'warn');
    } else {
      add('audio-measurable', false, null, 'loudnorm measurement');
    }
  }
}

if (info.video) {
  const { stderr } = run(FFMPEG, ['-hide_banner', '-nostdin', '-i', o.input, '-an', '-vf',
    'blackdetect=d=0.1:pic_th=0.98:pix_th=0.10', '-f', 'null', '-'], { allowFail: true });
  const blacks = [...stderr.matchAll(/black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)/g)].map((x) => ({ start: +x[1], end: +x[2] }));
  const blackSec = blacks.reduce((s, b) => s + (b.end - b.start), 0);
  const ratio = info.duration ? blackSec / info.duration : 0;
  add('black-ratio', ratio <= o.maxBlackRatio, round(ratio), `<= ${o.maxBlackRatio}`);
  if (o.blackOpening) {
    const opening = blacks.find((b) => b.start < 0.05);
    add('opens-on-content', !opening, opening ? `black 0-${round(opening.end, 2)}s` : 'content', 'first frame is not black (auto-cut/trim-lead)');
  }
  if (o.freeze > 0) {
    const { stderr: fz } = run(FFMPEG, ['-hide_banner', '-nostdin', '-i', o.input, '-an', '-vf',
      `freezedetect=n=0.003:d=${o.freeze}`, '-f', 'null', '-'], { allowFail: true });
    const freezes = [...fz.matchAll(/freeze_start:\s*([0-9.]+)/g)].map((x) => round(+x[1], 2));
    add('no-frozen-video', freezes.length === 0, freezes, `no freeze > ${o.freeze}s`, 'warn');
  }
}

const errors = checks.filter((c) => !c.ok && c.level === 'error');
const warnings = checks.filter((c) => !c.ok && c.level === 'warn');
const report = {
  ok: errors.length === 0, file: path.resolve(o.input), checkedAt: new Date().toISOString(),
  summary: { duration: round(info.duration), size: `${v.width}x${v.height}`, fps: round(v.fps || 0, 3), sizeMB: round(info.sizeBytes / 1048576, 2) },
  errors: errors.map((c) => c.name), warnings: warnings.map((c) => c.name), checks,
};
const reportPath = o.report || (o.writeReport ? `${o.input}.qc.json` : null);
if (reportPath) { fs.writeFileSync(reportPath, JSON.stringify(report, null, 2) + '\n'); report.report = path.resolve(reportPath); }
log(`${report.ok ? 'PASS' : 'FAIL'} ${path.basename(o.input)}: ${errors.length} error(s), ${warnings.length} warning(s)`);
emit(report);
process.exit(report.ok ? 0 : 1);
