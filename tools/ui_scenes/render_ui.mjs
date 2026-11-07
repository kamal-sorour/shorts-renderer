// Renders one animated "simulated UI" scene to MP4 using headless Chrome (puppeteer-core) + ffmpeg.
// Usage: node render_ui.mjs spec.json out.mp4 frames fps accentHex
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

const [specPath, outPath, framesArg, fpsArg, accentArg] = process.argv.slice(2);
const frames = parseInt(framesArg, 10), fps = parseInt(fpsArg || '24', 10), accent = (accentArg || '9BFF00').replace(/[^0-9a-fA-F]/g, '').slice(0, 6) || '9BFF00';
const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
const here = path.dirname(fileURLToPath(import.meta.url));

let exe = process.env.CHROME_PATH;
for (const p of ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']) { if (!exe && fs.existsSync(p)) exe = p; }
let args = ['--no-sandbox', '--disable-gpu', '--hide-scrollbars', '--force-color-profile=srgb'];
if (!exe) {
  const mod = (await import('@sparticuz/chromium')).default;   // dev fallback only
  exe = await mod.executablePath();
  args = [...mod.args, '--no-sandbox'];
}
let browser;
try { browser = await puppeteer.launch({ executablePath: exe, args, headless: true }); }
catch { browser = await puppeteer.launch({ executablePath: exe, args, headless: 'shell' }); }
const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 720, deviceScaleFactor: 1 });
await page.goto('file://' + path.join(here, 'scene.html'));
await page.evaluate((s, a, d) => window.setup(s, a, d), spec, accent, frames / fps);

const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'mjpeg', '-i', '-',
  '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-r', String(fps), outPath], { stdio: ['pipe', 'inherit', 'inherit'] });
const done = new Promise((res, rej) => { ff.on('close', c => (c === 0 ? res() : rej(new Error('ffmpeg exit ' + c)))); });
for (let f = 0; f < frames; f++) {
  await page.evaluate(t => window.setTime(t), f / fps);
  const buf = await page.screenshot({ type: 'jpeg', quality: 92 });
  if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
}
ff.stdin.end();
await done;
await browser.close();
