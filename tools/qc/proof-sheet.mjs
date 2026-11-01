#!/usr/bin/env node
// proof-sheet.mjs: contact / proof sheet PNG for human (or model) review.
//
// timeline : N evenly spaced frames from each input, labelled with timestamps.
// firstlast: the first and last frame of every clip side by side: the fastest
//            way to catch clips that open on a loading screen or end mid-action.
// --frames-dir also saves full-resolution frames: judge text legibility and
// overlays on those, not on the downscaled sheet.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { parseArgs, emit, fail, ffmpeg, probe, round, ensureDir } from './_lib.mjs';

const spec = {
  name: 'proof-sheet',
  summary: 'Contact sheet PNG (timeline frames or first/last frame per clip) for review before approval.',
  usage: [
    'node proof-sheet.mjs --inputs final.mp4 --out proof.png --frames 8',
    'node proof-sheet.mjs --input-dir edit/clips --mode firstlast --out edit/clips/proof.png',
  ],
  options: {
    inputs: { type: 'list', desc: 'Comma-separated videos' },
    inputDir: { desc: 'Folder of videos (mp4/mov/webm/mkv)' },
    out: { required: true, desc: 'Output PNG' },
    mode: { default: 'timeline', desc: 'timeline | firstlast' },
    frames: { type: 'number', default: 6, desc: 'Frames per input in timeline mode' },
    cellWidth: { type: 'number', default: 240, desc: 'Cell width px' },
    cols: { type: 'number', desc: 'Columns (default: frames for timeline, 6 for firstlast)' },
    framesDir: { desc: 'Also save full-resolution PNG frames here' },
  },
  outputs: ['proof PNG', 'optional full-res frames', 'JSON: { cells, width, height }'],
  verify: 'Look at it. Every clip opens on content, nothing is cut mid-action, overlays are inside the frame.',
};

const o = parseArgs(spec);
let sharp;
try { sharp = (await import('sharp')).default; } catch { fail('sharp is not installed; run npm install in the repo root'); }
let files = [...o.inputs];
if (o.inputDir) files.push(...fs.readdirSync(o.inputDir).filter((f) => /\.(mp4|mov|webm|mkv)$/i.test(f)).sort().map((f) => path.join(o.inputDir, f)));
if (!files.length) fail('need --inputs or --input-dir');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'proof-'));
const cells = [];
const CW = Math.round(o.cellWidth / 2) * 2;
const LABEL = 26;
try {
  if (o.framesDir) ensureDir(o.framesDir);
  for (const f of files) {
    const info = probe(f);
    if (!info.video) continue;
    const d = Math.min(info.duration, info.video.duration || Infinity);
    const fps = info.video.fps || 30;
    const times = o.mode === 'firstlast'
      ? [0, Math.max(0, d - 1.5 / fps)]
      : Array.from({ length: o.frames }, (_, i) => (o.frames === 1 ? 0 : (i * Math.max(0, d - 1 / fps)) / (o.frames - 1)));
    for (const [k, t] of times.entries()) {
      const png = path.join(tmp, `${cells.length}.png`);
      const isLast = k === times.length - 1 && times.length > 1;
      if (isLast) {
        // true last decoded frame: seek near the end and keep overwriting until EOF
        ffmpeg(['-loglevel', 'error', '-sseof', '-1', '-i', f, '-an', '-update', '1', png]);
      } else {
        // -ss after -i is slower but frame-exact
        ffmpeg(['-loglevel', 'error', '-i', f, '-ss', String(round(t, 3)), '-an', '-frames:v', '1', '-update', '1', png]);
      }
      if (!fs.existsSync(png)) continue;
      if (o.framesDir) fs.copyFileSync(png, path.join(o.framesDir, `${path.basename(f).replace(/\.[^.]+$/, '')}_${String(k).padStart(2, '0')}_${round(t, 2)}s.png`));
      const tag = o.mode === 'firstlast' ? (k === 0 ? 'FIRST' : 'LAST') : `${round(t, 2)}s`;
      cells.push({ file: path.basename(f), t: round(t, 3), label: `${path.basename(f)}  ${tag}`, png });
    }
  }
  if (!cells.length) fail('no frames extracted');
  const meta = await sharp(cells[0].png).metadata();
  const CH = Math.round((CW * meta.height) / meta.width);
  const cols = o.cols || (o.mode === 'firstlast' ? 6 : Math.min(o.frames, 8));
  const rows = Math.ceil(cells.length / cols);
  const gap = 6;
  const W = cols * (CW + gap) + gap;
  const H = rows * (CH + LABEL + gap) + gap;
  const xml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const comps = [];
  for (const [i, c] of cells.entries()) {
    const x = gap + (i % cols) * (CW + gap);
    const y = gap + Math.floor(i / cols) * (CH + LABEL + gap);
    comps.push({ input: await sharp(c.png).resize(CW, CH, { fit: 'contain', background: '#000' }).png().toBuffer(), left: x, top: y + LABEL });
    const label = c.label.length > 34 ? '...' + c.label.slice(-31) : c.label;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${CW}" height="${LABEL}"><rect width="100%" height="100%" fill="#222"/><text x="6" y="18" font-family="DejaVu Sans Mono,monospace" font-size="13" fill="#eee">${xml(label)}</text></svg>`;
    comps.push({ input: Buffer.from(svg), left: x, top: y });
  }
  ensureDir(path.dirname(path.resolve(o.out)));
  await sharp({ create: { width: W, height: H, channels: 3, background: '#111' } }).composite(comps).png().toFile(o.out);
  emit({ ok: true, out: path.resolve(o.out), mode: o.mode, width: W, height: H, cells: cells.map(({ png, ...c }) => c), framesDir: o.framesDir ? path.resolve(o.framesDir) : null });
} catch (e) {
  fail(e.message);
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
