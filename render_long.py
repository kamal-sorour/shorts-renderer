#!/usr/bin/env python3
"""
Long-form explainer renderer (16:9, 720p) for GitHub Actions.
  voice (edge-tts) -> per-line B-roll (Pexels / Pixabay / Wikimedia) -> character (emotion swap + audio-reactive
  bounce + neon glow) -> captions / on-screen text / chapter cards -> one finished MP4.
Usage: python render_long.py payload.json
Env: PEXELS_API_KEY, PIXABAY_API_KEY (optional but recommended), CHAR_DIR, FAKE_TTS=1, FAKE_BROLL=1
"""
import json, math, os, random, re, subprocess, sys, time, urllib.parse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import render as base   # reuse tts_line, align_words, probe_dur, run, log, strip_ar, ass_time, clean_ass

W, H, FPS = 1280, 720, 24
ROOT = Path(__file__).parent.resolve()
WORK = ROOT / "work_long"
OUT = ROOT / "out"
CHAR_DIR = Path(os.environ.get("CHAR_DIR", ROOT / "assets" / "character"))
UA = {"User-Agent": "Mozilla/5.0 (compatible; explainer-renderer/1.0)"}
EMOTIONS = ["neutral", "angry", "smile", "side", "cry", "shock", "laugh", "think", "annoyed", "thumbsup", "sleep", "facepalm"]
log, run = base.log, base.run


# ------------------------------------------------------------------ B-roll
def _get(url, **kw):
    for i in range(3):
        try:
            r = requests.get(url, headers=kw.pop("headers", UA), timeout=40, **kw)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                time.sleep(6 * (i + 1))
        except Exception as e:  # noqa
            log("http error", e)
            time.sleep(2)
    return None


def _download(url, path, max_mb=60):
    try:
        with requests.get(url, headers=UA, stream=True, timeout=60) as r:
            if r.status_code != 200:
                return False
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    size += len(chunk)
                    if size > max_mb * 1e6:
                        return False
                    f.write(chunk)
        return size > 20000
    except Exception as e:  # noqa
        log("download error", e)
        return False


def pexels_video(q, used, idx):
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        return None
    r = _get("https://api.pexels.com/videos/search?" + urllib.parse.urlencode({"query": q, "per_page": 10, "orientation": "landscape", "size": "medium"}),
             headers={"Authorization": key, **UA})
    if not r:
        return None
    vids = [v for v in r.json().get("videos", []) if f"pexels:{v['id']}" not in used]
    random.shuffle(vids)
    for v in vids[:4]:
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width")]
        files.sort(key=lambda f: abs(f["width"] - 1280))
        for f in files[:1]:
            p = WORK / f"clip_{idx}.mp4"
            if _download(f["link"], p):
                used.add(f"pexels:{v['id']}")
                return {"kind": "video", "path": p, "dur": float(v.get("duration") or 8), "credit": f"Video by {v.get('user', {}).get('name', 'Pexels')} on Pexels ({v.get('url', '')})"}
    return None


def pixabay_video(q, used, idx):
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        return None
    r = _get("https://pixabay.com/api/videos/?" + urllib.parse.urlencode({"key": key, "q": q, "per_page": 10, "safesearch": "true"}))
    if not r:
        return None
    hits = [h for h in r.json().get("hits", []) if f"pixabay:{h['id']}" not in used]
    random.shuffle(hits)
    for h in hits[:4]:
        vf = h.get("videos", {})
        for size in ("medium", "small", "large"):
            if vf.get(size, {}).get("url"):
                p = WORK / f"clip_{idx}.mp4"
                if _download(vf[size]["url"], p):
                    used.add(f"pixabay:{h['id']}")
                    return {"kind": "video", "path": p, "dur": float(h.get("duration") or 8), "credit": f"Video by {h.get('user', 'Pixabay')} on Pixabay ({h.get('pageURL', '')})"}
    return None


def wikimedia_image(q, used, idx):
    r = _get("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "generator": "search", "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 12,
        "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 1600, "format": "json"}))
    if not r:
        return None
    pages = list((r.json().get("query") or {}).get("pages", {}).values())
    random.shuffle(pages)
    for pg in pages:
        ii = (pg.get("imageinfo") or [{}])[0]
        md = ii.get("extmetadata", {})
        lic = (md.get("LicenseShortName", {}).get("value") or "").strip()
        ok = lic.upper().startswith(("CC0", "PUBLIC DOMAIN", "PD")) or re.match(r"^CC BY( [0-9.]+)?$", lic.upper())
        if not ok or f"wm:{pg.get('pageid')}" in used or (ii.get("width") or 0) < 900:
            continue
        p = WORK / f"img_{idx}.jpg"
        if _download(ii.get("thumburl") or ii.get("url"), p, 25):
            try:
                Image.open(p).convert("RGB").save(p, quality=92)
            except Exception:
                continue
            used.add(f"wm:{pg.get('pageid')}")
            artist = re.sub(r"<[^>]+>", "", md.get("Artist", {}).get("value", "unknown"))[:60]
            return {"kind": "image", "path": p, "credit": f"{pg.get('title', 'image')} by {artist} ({lic}) via Wikimedia Commons"}
    return None


def pexels_photo(q, used, idx):
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        return None
    r = _get("https://api.pexels.com/v1/search?" + urllib.parse.urlencode({"query": q, "per_page": 10, "orientation": "landscape"}), headers={"Authorization": key, **UA})
    if not r:
        return None
    ph = [x for x in r.json().get("photos", []) if f"pexphoto:{x['id']}" not in used]
    random.shuffle(ph)
    for x in ph[:3]:
        p = WORK / f"img_{idx}.jpg"
        if _download(x["src"].get("large2x") or x["src"]["large"], p, 20):
            used.add(f"pexphoto:{x['id']}")
            return {"kind": "image", "path": p, "credit": f"Photo by {x.get('photographer', 'Pexels')} on Pexels ({x.get('url', '')})"}
    return None



def pixabay_photo(q, used, idx):
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        return None
    r = _get("https://pixabay.com/api/?" + urllib.parse.urlencode({"key": key, "q": q, "image_type": "photo", "orientation": "horizontal", "per_page": 12, "safesearch": "true"}))
    if not r:
        return None
    hits = [h for h in r.json().get("hits", []) if f"pixabayimg:{h['id']}" not in used]
    random.shuffle(hits)
    for h in hits[:3]:
        p = WORK / f"img_{idx}.jpg"
        if _download(h.get("largeImageURL") or h.get("webformatURL"), p, 20):
            used.add(f"pixabayimg:{h['id']}")
            return {"kind": "image", "path": p, "credit": f"Image by {h.get('user', 'Pixabay')} on Pixabay ({h.get('pageURL', '')})"}
    return None


def openverse_image(q, used, idx):
    """No API key needed. Only CC0 / public domain / CC BY (no ShareAlike, no NonCommercial)."""
    r = _get("https://api.openverse.org/v1/images/?" + urllib.parse.urlencode({
        "q": q, "license": "cc0,pdm,by", "page_size": 12, "mature": "false", "aspect_ratio": "wide"}))
    if not r:
        return None
    res = [x for x in r.json().get("results", []) if f"ov:{x.get('id')}" not in used]
    random.shuffle(res)
    for x in res[:4]:
        p = WORK / f"img_{idx}.jpg"
        if _download(x.get("url") or x.get("thumbnail"), p, 20):
            try:
                im = Image.open(p).convert("RGB")
                if im.width < 900:
                    continue
                im.save(p, quality=92)
            except Exception:
                continue
            used.add(f"ov:{x.get('id')}")
            return {"kind": "image", "path": p, "credit": x.get("attribution") or f"{x.get('title', 'image')} by {x.get('creator', 'unknown')} ({x.get('license', '').upper()}) {x.get('foreign_landing_url', '')}"}
    return None


def fake_visual(q, idx):
    p = WORK / f"clip_{idx}.mp4"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=s=1280x720:d=6:r=24", "-pix_fmt", "yuv420p", p])
    return {"kind": "video", "path": p, "dur": 6.0, "credit": f"FAKE clip for '{q}'"}


def card_visual(text, idx):
    p = WORK / f"img_{idx}.jpg"
    im = Image.new("RGB", (W, H), (10, 12, 18))
    d = ImageDraw.Draw(im)
    for k in range(0, W, 64):
        d.line([(k, 0), (k - 240, H)], fill=(18, 24, 20), width=2)
    im = im.filter(ImageFilter.GaussianBlur(1))
    im.save(p, quality=92)
    return {"kind": "image", "path": p, "credit": None}


def get_visual(queries, idx, used):
    if os.environ.get("FAKE_BROLL") == "1":
        return fake_visual(queries[0] if queries else "x", idx)
    for q in queries:
        for fn in (pexels_video, pixabay_video, wikimedia_image, openverse_image, pixabay_photo, pexels_photo):
            try:
                v = fn(q, used, idx)
            except Exception as e:  # noqa
                log(f"{fn.__name__} failed: {e}")
                v = None
            if v:
                return v
        time.sleep(0.3)
    log(f"line {idx}: no stock visual found for {queries}, using generated card")
    return card_visual("", idx)


# ------------------------------------------------------------------ Character
def normalize_chars():
    imgs = {}
    for f in sorted(CHAR_DIR.glob("*.png")):
        imgs[f.stem.lower().strip()] = Image.open(f).convert("RGBA")
    if "neutral" not in imgs:
        raise SystemExit(f"assets/character/neutral.png is required. Found: {sorted(imgs)}")
    log("character emotions:", sorted(imgs))
    sizes = {im.size for im in imgs.values()}

    def bbox(im):
        return im.getchannel("A").point(lambda p: 255 if p > 16 else 0).getbbox() or (0, 0) + im.size

    if len(sizes) == 1:   # same canvas for all -> crop with one shared box so the head never jumps
        boxes = [bbox(im) for im in imgs.values()]
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        return {k: im.crop(box) for k, im in imgs.items()}
    out = {}
    ref_h = bbox(imgs["neutral"])[3] - bbox(imgs["neutral"])[1]
    for k, im in imgs.items():
        b = bbox(im)
        c = im.crop(b)
        s = ref_h / max(c.height, 1)
        out[k] = c.resize((max(1, int(c.width * s)), ref_h), Image.LANCZOS)
    return out


_G = {}
CHAR_H = 470          # the character is ALWAYS this size and in this corner
GRADE = "eq=brightness=-0.05:contrast=1.07:saturation=1.05,colorbalance=bs=0.05:bh=0.03:rs=-0.02,vignette=PI/6"


def build_char_sets(chars, accent):
    rgb = tuple(int(accent[i:i + 2], 16) for i in (0, 2, 4))
    sets = {}
    for k, im in chars.items():
        s = CHAR_H / im.height
        c = im.resize((max(1, int(im.width * s)), CHAR_H), Image.LANCZOS)
        pad = 46
        a = Image.new("L", (c.width + pad * 2, c.height + pad * 2), 0)
        a.paste(c.getchannel("A"), (pad, pad))
        a = a.filter(ImageFilter.GaussianBlur(22))
        sets[k] = {"img": c, "alpha": a, "pad": pad, "w": c.width, "h": c.height}
    _G["rgb"] = rgb
    return sets


def glow_img(entry, lvl):
    key = (id(entry), lvl)
    cache = _G.setdefault("glow", {})
    if key not in cache:
        a = entry["alpha"].point(lambda p: int(min(255, p * (0.15 + 0.85 * lvl / 7.0) * 0.9)))
        g = Image.new("RGBA", a.size, _G["rgb"] + (0,))
        g.putalpha(a)
        cache[key] = g
    return cache[key]


def render_chunk(args):
    s, e, outdir = args
    P = _G["plan"]
    for i in range(s, e):
        out = Path(outdir) / f"f_{i + 1:06d}.png"
        if out.exists():
            continue
        emo, amp, since = P["emo"][i], float(P["amp"][i]), int(P["since"][i])
        ent = _G["sets"].get(emo) or _G["sets"]["neutral"]
        t = i / FPS
        canvas = Image.new("RGBA", (W, H + 80), (0, 0, 0, 0))
        x = W - ent["w"] - 34
        bounce = -int(amp * 9) - int(8 * max(0.0, 1 - since / 6.0)) + int(2 * math.sin(t * 2.0))
        y = H - ent["h"] + 24 + bounce
        canvas.alpha_composite(glow_img(ent, int(round(amp * 7))), (x - ent["pad"], y - ent["pad"]))
        canvas.alpha_composite(ent["img"], (x, y))
        canvas.crop((0, 0, W, H)).save(out, compress_level=1)
    return e - s


def envelope(wav, nframes):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    hop = 16000 // FPS
    n = len(x) // hop
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1)) if n else np.zeros(1)
    ref = np.percentile(rms, 95) + 1e-6
    a = np.clip(rms / ref, 0, 1)
    a[a < 0.10] = 0
    out = np.zeros(len(a), dtype=np.float32)
    for i in range(len(a)):
        out[i] = max(a[i], out[i - 1] * 0.72 if i else 0)
    if len(out) < nframes:
        out = np.pad(out, (0, nframes - len(out)))
    return out[:nframes]


# ------------------------------------------------------------------ segments
def host_bg():
    p = WORK / "host_bg.png"
    if p.exists():
        return p
    im = Image.new("RGB", (W, H), (246, 248, 244))
    d = ImageDraw.Draw(im)
    for y in range(H):
        k = y / H
        d.line([(0, y), (W, y)], fill=(int(248 - 14 * k), int(250 - 10 * k), int(246 - 14 * k)))
    for gx in range(0, W, 80):
        d.line([(gx, 0), (gx, H)], fill=(232, 236, 230), width=1)
    for gy in range(0, H, 80):
        d.line([(0, gy), (W, gy)], fill=(232, 236, 230), width=1)
    d.polygon([(0, 0), (260, 0), (0, 260)], fill=(155, 255, 0))
    d.polygon([(0, 0), (190, 0), (0, 190)], fill=(20, 24, 22))
    im.save(p)
    return p


ENC = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(FPS)]


def seg_video(out, vis, frames):
    need = frames / FPS
    f = max(0.6, min(1.0, vis["dur"] / need)) if vis["dur"] < need else 1.0
    eff = vis["dur"] / f
    if eff < need + 0.3:
        pre = ["-stream_loop", "-1"]
    else:
        pre = ["-ss", f"{random.uniform(0, max(eff - need - 0.2, 0)) * f:.2f}"]
    pz = f"(1+0.07*pow(max(0,1-t/0.45),2))"
    vf = (("setpts=PTS/%.3f," % f) if f < 1 else "") + (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
        f"scale=w='2*trunc({W}*{pz}/2)':h='2*trunc({H}*{pz}/2)':eval=frame,crop={W}:{H},{GRADE},format=yuv420p")
    run(["ffmpeg", "-y"] + pre + ["-i", vis["path"], "-vf", vf, "-frames:v", str(frames)] + ENC + [out])


def seg_image(out, src, frames, k, host=False):
    zoom = f"min(1+0.06*on/{frames},1.06)" if k % 2 == 0 else f"max(1.06-0.06*on/{frames},1.0)"
    vf = (f"scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,"
          f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
    if not host:
        vf += "," + GRADE
    run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", str(frames)] + ENC + [out])


# ------------------------------------------------------------------ animated charts
def _font(size):
    for p in ("/usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf", "/usr/share/fonts/truetype/montserrat/Montserrat-SemiBold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _ease(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def _fmt(v, unit):
    s = f"{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:,.1f}"
    return s + (unit or "")


def chart_ok(c):
    try:
        if not isinstance(c, dict) or c.get("type") not in ("bar", "line", "timeline"):
            return False
        lab, val = c.get("labels"), c.get("values")
        if not (isinstance(lab, list) and isinstance(val, list) and 2 <= len(lab) == len(val) <= 8):
            return False
        c["values"] = [float(v) for v in val]
        return True
    except Exception:
        return False


def draw_chart(c, p, accent):
    acc = tuple(int(accent[i:i + 2], 16) for i in (0, 2, 4))
    im = Image.new("RGB", (W, H), (10, 15, 24))
    d = ImageDraw.Draw(im)
    for y in range(H):
        k = y / H
        d.line([(0, y), (W, y)], fill=(int(10 + 8 * k), int(15 + 10 * k), int(24 + 14 * k)))
    for gx in range(70, 780, 70):
        d.line([(gx, 120), (gx, 650)], fill=(24, 34, 48), width=1)
    ft, fl, fv = _font(40), _font(25), _font(27)
    d.text((70, 46), str(c.get("title", ""))[:48], font=ft, fill=(240, 245, 250))
    d.rectangle([70, 100, 70 + int(160 * _ease(p * 3)), 105], fill=acc)
    lab, val, unit, typ = c["labels"], c["values"], c.get("unit", ""), c["type"]
    n = len(val)
    if typ == "bar":
        mx = max(val) or 1
        top, area = 150, 470
        bh = int(min(54, area / n * 0.62))
        for k in range(n):
            y = top + int(k * area / n)
            pk = _ease((p * 1.5 - k * 0.09) / 0.55)
            d.text((70, y + bh // 2 - 14), str(lab[k])[:16], font=fl, fill=(170, 185, 200))
            x0, wmax = 290, 380
            wbar = int(wmax * (val[k] / mx) * pk)
            if wbar > 2:
                d.rectangle([x0, y, x0 + wbar, y + bh], fill=acc)
                d.rectangle([x0, y, x0 + wbar, y + 4], fill=tuple(min(255, ch + 70) for ch in acc))
            if pk > 0.85:
                d.text((x0 + wbar + 14, y + bh // 2 - 15), _fmt(val[k] * 1.0, unit), font=fv, fill=(240, 245, 250))
    elif typ == "line":
        mn, mx = min(val), max(val)
        rng = (mx - mn) or 1
        x0, x1, y0, y1 = 110, 660, 560, 190
        pts = [(x0 + (x1 - x0) * k / (n - 1), y0 - (y0 - y1) * (val[k] - mn) / rng) for k in range(n)]
        d.line([(x0, y0 + 20), (x1, y0 + 20)], fill=(60, 75, 95), width=2)
        prog = _ease(p * 1.3) * (n - 1)
        for k in range(n - 1):
            if prog >= k:
                fr = min(1.0, prog - k)
                ax, ay = pts[k]
                bx, by = pts[k + 1]
                d.line([(ax, ay), (ax + (bx - ax) * fr, ay + (by - ay) * fr)], fill=acc, width=6)
        for k in range(n):
            if prog >= k - 0.01:
                x, y = pts[k]
                d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=(10, 15, 24), outline=acc, width=4)
                d.text((x - 30, y - 46), _fmt(val[k], unit), font=fv, fill=(240, 245, 250))
                d.text((x - 28, y0 + 34), str(lab[k])[:10], font=fl, fill=(170, 185, 200))
    else:  # timeline
        order = sorted(range(n), key=lambda k: val[k])
        mn, mx = val[order[0]], val[order[-1]]
        rng = (mx - mn) or 1
        ay = 380
        d.line([(100, ay), (100 + int(560 * _ease(p * 2)), ay)], fill=(90, 105, 125), width=4)
        for r, k in enumerate(order):
            pk = _ease((p * 1.6 - r * 0.12) / 0.4)
            if pk <= 0:
                continue
            x = 130 + int(500 * (val[k] - mn) / rng) if n > 1 else 400
            up = r % 2 == 0
            d.ellipse([x - 11, ay - 11, x + 11, ay + 11], fill=acc)
            ty = ay - 40 - int(110 * pk) if up else ay + 40 + int(110 * pk) - 40
            d.line([(x, ay), (x, ty + (40 if up else 0))], fill=acc, width=2)
            d.text((x + 12, ty - (34 if up else 0)), f"{val[k]:.0f}", font=fv, fill=acc)
            words, line, lines = str(lab[k]).split(), "", []
            for w_ in words:
                if len(line) + len(w_) > 16:
                    lines.append(line)
                    line = w_
                else:
                    line = (line + " " + w_).strip()
            lines.append(line)
            for li, tx in enumerate(lines[:3]):
                d.text((x + 12, ty + (4 if up else 34) + li * 26), tx, font=fl, fill=(225, 232, 240))
    return im


def seg_chart(out, c, frames, accent):
    proc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                             "-vf", "format=yuv420p"] + ENC + [str(out)], stdin=subprocess.PIPE)
    for f in range(frames):
        p = min(1.0, f / (frames * 0.62))
        proc.stdin.write(draw_chart(c, p, accent).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("chart ffmpeg failed")


# ------------------------------------------------------------------ captions
def build_ass(words, scenes, accent, channel, total, title=""):
    acc = re.sub(r"[^0-9A-Fa-f]", "", accent)[:6].ljust(6, "0")
    acc_ass = "&H00" + acc[4:6] + acc[2:4] + acc[0:2] + "&"
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Montserrat,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,3.2,0,2,60,470,44,1
Style: Big,Montserrat ExtraBold,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,3,10,0,4,70,520,0,1
Style: Chap,Montserrat ExtraBold,46,{acc_ass},{acc_ass},&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,3,0,8,60,60,52,1
Style: Title,Montserrat ExtraBold,64,{acc_ass},{acc_ass},&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,3,0,5,60,470,0,1
Style: Brand,Montserrat,26,&H78FFFFFF,&H78FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,1.5,0,9,40,40,26,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if channel:
        ev.append(f"Dialogue: 0,{base.ass_time(0)},{base.ass_time(total)},Brand,,0,0,0,,{base.clean_ass(channel)}")
    groups, cur = [], []
    for w in words:
        cur.append(w)
        chars = sum(len(x[0]) for x in cur) + len(cur)
        if len(cur) >= 6 or chars > 38 or re.search(r"[.!?;:…]$", w[0]):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0][1] if gi + 1 < len(groups) else None
        for k, (_, s, e) in enumerate(g):
            end = g[k + 1][1] if k + 1 < len(g) else e + 0.15
            if k + 1 == len(g) and nxt is not None:
                end = min(max(end, s + 0.25), nxt)
            end = max(end, s + 0.08)
            parts = []
            for j, (tok, _, _) in enumerate(g):
                t = base.clean_ass(tok)
                parts.append("{\\c" + acc_ass + "}" + t + "{\\r}" if j == k else t)
            ev.append(f"Dialogue: 1,{base.ass_time(s)},{base.ass_time(end)},Cap,,0,0,0,,{' '.join(parts)}")
    if title:
        flick = ("{\\blur6\\alpha&H00&\\t(90,130,\\alpha&HFF&)\\t(130,190,\\alpha&H00&)\\t(300,340,\\alpha&HFF&)\\t(340,380,\\alpha&H00&)"
                 "\\t(520,560,\\alpha&H90&)\\t(560,610,\\alpha&H00&)\\t(2600,3200,\\alpha&HFF&)}")
        ev.append(f"Dialogue: 4,{base.ass_time(0.3)},{base.ass_time(3.5)},Title,,0,0,0,,{flick}{base.clean_ass(title[:90].upper())}")
    for sc in scenes:
        if sc.get("onscreen") and sc["beat"] != "chart":
            e = sc["start"] + max(sc["d"], 2.6)
            ev.append(f"Dialogue: 2,{base.ass_time(sc['start'] + 0.2)},{base.ass_time(e)},Big,,0,0,0,,{{\\fad(200,200)}}{base.clean_ass(sc['onscreen'][:46].upper())}")
        if sc.get("chapter") and sc["beat"] != "chart":
            cs = sc["start"] + (3.7 if sc["i"] == 0 and title else 0)
            ev.append(f"Dialogue: 3,{base.ass_time(cs)},{base.ass_time(cs + 3.0)},Chap,,0,0,0,,{{\\fad(350,350)\\blur4}}{base.clean_ass(sc['chapter'])}")
    return hdr + "\n".join(ev) + "\n"


def fmt_ts(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


# ------------------------------------------------------------------ main
def main():
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    job = payload["job_id"]
    lang = payload.get("lang", "en")
    voice, rate, pitch = payload.get("voice", "en-US-AndrewNeural"), payload.get("rate", "+0%"), payload.get("pitch", "+0Hz")
    accent = payload.get("accent", "9BFF00")
    lines = payload["lines"]
    n = len(lines)
    import shutil
    marker = WORK / ".job"
    if WORK.exists() and marker.exists() and marker.read_text().strip() == job:
        log(f"RESUME: reusing finished work for {job}")
        shutil.rmtree(WORK / "frames", ignore_errors=True)
        for f in ("bg.mp4", "voice.wav"):
            (WORK / f).unlink(missing_ok=True)
    else:
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(parents=True)
        marker.write_text(job)
    OUT.mkdir(exist_ok=True)
    t_start = time.time()
    log(f"job {job}: {n} lines")

    # 1) voice + timing (cached per line)
    scenes, t0 = [], 0.0
    for i, ln in enumerate(lines):
        mp3, wav, meta = WORK / f"line_{i:03d}.mp3", WORK / f"line_{i:03d}.wav", WORK / f"line_{i:03d}.json"
        if mp3.exists() and meta.exists():
            m = json.loads(meta.read_text())
            dur, words = m["dur"], [tuple(w) for w in m["words"]]
        else:
            ev = base.tts_line(ln["text"], voice, rate, pitch, lang, str(mp3))
            dur = base.probe_dur(mp3)
            words = base.align_words(ln["text"], ev, dur, mp3, lang, payload.get("whisper") == "always")
            meta.write_text(json.dumps({"dur": dur, "words": words}))
        nxt_chapter = i + 1 < n and lines[i + 1].get("chapter")
        gap = 0.7 if nxt_chapter else (0.45 if ln["text"].rstrip().endswith(("?", ":")) else 0.28)
        frames = max(int(math.ceil((dur + gap) * FPS)), int(2 * FPS))
        d = frames / FPS
        run(["ffmpeg", "-y", "-i", mp3, "-ar", 48000, "-ac", 2, "-af", f"apad=whole_dur={d:.4f}", wav])
        beat = ln.get("beat") if ln.get("beat") in ("host", "chart") else "broll"
        scenes.append({"i": i, "start": t0, "d": d, "frames": frames, "wav": wav, "text": ln["text"],
                       "words": [(w, t0 + s, t0 + e) for w, s, e in words], "emotion": (ln.get("emotion") or "neutral").lower(),
                       "beat": beat, "queries": ln.get("queries") or [], "onscreen": ln.get("onscreen"), "chapter": ln.get("chapter"), "chart": ln.get("chart")})
        t0 += d
    total_frames = sum(s["frames"] for s in scenes)
    total = total_frames / FPS
    log(f"narration {total:.1f}s ({total / 60:.1f} min), tts done in {time.time() - t_start:.0f}s")
    (WORK / "list.txt").write_text("".join(f"file '{s['wav'].name}'\n" for s in scenes))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", 0, "-i", "list.txt", "-c", "copy", "voice.wav"], cwd=WORK)

    # 2) visuals: several cuts per line, charts, host cards (cached per part)
    used, credits, seg_files = set(), [], []
    hb = host_bg()
    for s in scenes:
        i, F = s["i"], s["frames"]
        if s["beat"] == "chart" and chart_ok(s["chart"]):
            parts = [("chart", F)]
        elif s["beat"] == "host":
            parts = [("host", F)]
        else:
            k = 1 if F <= 5.0 * FPS else (2 if F <= 9.5 * FPS else 3)
            parts = [("broll", F // k + (F % k if j == k - 1 else 0)) for j in range(k)]
        for j, (kind, fr) in enumerate(parts):
            out, side = WORK / f"seg_{i:03d}_{j}.mp4", WORK / f"seg_{i:03d}_{j}.json"
            if out.exists() and side.exists():
                cr = json.loads(side.read_text()).get("credit")
            else:
                cr = None
                if kind == "chart":
                    seg_chart(out, s["chart"], fr, accent)
                elif kind == "host":
                    seg_image(out, hb, fr, i, host=True)
                else:
                    qs = [x for x in s["queries"] if x] or [payload.get("topic", "abstract light")]
                    ordered = qs[j % len(qs):] + qs[:j % len(qs)]
                    vis = get_visual(ordered, f"{i}_{j}", used)
                    cr = vis.get("credit")
                    if vis["kind"] == "video":
                        seg_video(out, vis, fr)
                    else:
                        seg_image(out, vis["path"], fr, i + j)
                    try:
                        Path(vis["path"]).unlink()
                    except Exception:
                        pass
                side.write_text(json.dumps({"credit": cr}))
            if cr:
                credits.append(cr)
            seg_files.append(out)
        if i % 10 == 0:
            log(f"segments {i + 1}/{n}  ({time.time() - t_start:.0f}s)")
    uniq_credits = list(dict.fromkeys(credits))
    (WORK / "segs.txt").write_text("".join(f"file '{p.name}'\n" for p in seg_files))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", 0, "-i", "segs.txt", "-c", "copy", "bg.mp4"], cwd=WORK)

    # 3) character frames (fixed size + position, audio-reactive)
    chars = normalize_chars()
    _G["sets"] = build_char_sets(chars, accent)
    emo, since = [], []
    for s in scenes:
        e = s["emotion"] if s["emotion"] in chars else "neutral"
        for k in range(s["frames"]):
            emo.append(e)
            since.append(k)
    _G["plan"] = {"emo": emo, "since": since, "amp": envelope(WORK / "voice.wav", total_frames)}
    fdir = WORK / "frames"
    fdir.mkdir(exist_ok=True)
    jobs = [(a, min(a + 240, total_frames), str(fdir)) for a in range(0, total_frames, 240)]
    with ProcessPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
        list(ex.map(render_chunk, jobs))
    log(f"character frames done ({time.time() - t_start:.0f}s)")

    # 4) captions + final mix
    allw = [w for s in scenes for w in s["words"]]
    (WORK / "captions.ass").write_text(build_ass(allw, scenes, accent, payload.get("channel", ""), total, (payload.get("meta") or {}).get("title", "")), encoding="utf-8")
    music = sorted(list((ROOT / "assets" / "music").glob("*.mp3")) + list((ROOT / "assets" / "music").glob("*.wav")))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", "bg.mp4", "-framerate", str(FPS), "-i", "frames/f_%06d.png", "-i", "voice.wav"]
    voice_chain = ("[2:a]aformat=sample_rates=48000:channel_layouts=stereo,highpass=f=70,acompressor=threshold=0.1:ratio=3:attack=5:release=80:makeup=2,"
                   f"volume=1.2,apad=whole_dur={total:.2f}")
    fg = ["[0:v][1:v]overlay=0:0:format=auto[ov]", "[ov]ass=captions.ass[vout]"]
    if music:
        mf = random.choice(music)
        log("music:", mf.name)
        cmd += ["-stream_loop", "-1", "-i", str(mf)]
        fg.append(voice_chain + ",asplit=2[vo][vsc]")
        fg.append(f"[3:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.16,afade=t=in:d=2,afade=t=out:st={max(total - 3, 0):.2f}:d=3[mu]")
        fg.append("[mu][vsc]sidechaincompress=threshold=0.015:ratio=9:attack=15:release=500[du]")
        fg.append(f"[vo][du]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,loudnorm=I=-16:TP=-1.5:LRA=11,atrim=0:{total:.2f}[aout]")
    else:
        fg.append(voice_chain + f",loudnorm=I=-16:TP=-1.5:LRA=11,atrim=0:{total:.2f}[aout]")
    (WORK / "graph.txt").write_text(";\n".join(fg))
    outname = f"long_{job}.mp4"
    cmd += ["-filter_complex_script", "graph.txt", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-maxrate", "4M", "-bufsize", "8M", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            "-t", f"{total:.2f}", str(OUT / outname)]
    run(cmd, cwd=WORK)

    chapters = [{"t": fmt_ts(s["start"]), "title": s["chapter"]} for s in scenes if s.get("chapter")]
    if chapters and chapters[0]["t"] != "0:00":
        chapters.insert(0, {"t": "0:00", "title": "Intro"})
    res = {"job_id": job, "file": outname, "duration": round(total, 1), "size_mb": round((OUT / outname).stat().st_size / 1e6, 1),
           "lines": n, "chapters": chapters, "credits": uniq_credits[:60], "render_seconds": int(time.time() - t_start)}
    (OUT / "result.json").write_text(json.dumps(res, ensure_ascii=False))
    log("DONE", {k: v for k, v in res.items() if k not in ("credits", "chapters")})


if __name__ == "__main__":
    main()
