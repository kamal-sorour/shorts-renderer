#!/usr/bin/env python3
"""
Ancient Answers - Shorts Renderer
Runs on GitHub Actions (free). Takes a script JSON, produces a finished 1080x1920 MP4:
  voice (edge-tts, with word timestamps) + AI images (Pollinations / Cloudflare FLUX)
  + Ken Burns + crossfades + film look + word-by-word captions (voice only, no background music).
Usage: python render.py payload.json
Env (all optional): CF_ACCOUNT_ID, CF_API_TOKEN, POLLINATIONS_KEY, FAKE_TTS=1, FAKE_IMAGES=1
"""
import asyncio, base64, json, math, os, random, re, subprocess, sys, time, urllib.parse
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter

W, H, FPS = 1080, 1920, 30
XF = 0.45                      # crossfade seconds
KB_W, KB_H = 1620, 2880        # Ken Burns working canvas (1.5x)
GOLD_ASS = "&H0000D7FF&"       # #FFD700 in ASS BGR
ROOT = Path(__file__).parent.resolve()
WORK = ROOT / "work"
OUT = ROOT / "out"


def log(*a):
    print("[render]", *a, flush=True)


def run(cmd, **kw):
    log("$", " ".join(str(c) for c in cmd)[:300])
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-4000:], file=sys.stderr)
        raise RuntimeError(f"command failed: {cmd[0]}")
    return r


def probe_dur(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(r.stdout.strip())


# ----------------------------------------------------------------------------- TTS
async def _edge(text, voice, rate, pitch, path):
    import edge_tts
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary")
    except TypeError:  # old edge-tts without `boundary`
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    words = []
    with open(path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["text"], ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7))
    if os.path.getsize(path) < 2000:
        raise RuntimeError("edge-tts returned empty audio")
    return words


def tts_line(text, voice, rate, pitch, lang, path):
    """returns list of (word, start, end) relative to the line audio, or None if no timing available"""
    if os.environ.get("FAKE_TTS") == "1":
        n = max(1, len(text.split()))
        dur = n * 0.36
        run(["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"sine=f=180:d={dur:.2f}", "-af", "tremolo=f=6:d=0.7,volume=0.5", "-c:a", "libmp3lame", path])
        return None
    last = None
    for attempt in range(1, 4):
        try:
            words = asyncio.run(_edge(text, voice, rate, pitch, path))
            return words or None
        except Exception as e:  # noqa
            last = e
            log(f"edge-tts attempt {attempt} failed: {e}")
            time.sleep(3 * attempt)
    log("falling back to gTTS (robotic, no word timing):", last)
    from gtts import gTTS
    gTTS(text, lang=lang[:2]).save(path)
    return None


def span_map(tokens, speech_start, speech_end):
    """spread tokens over the real speech span, weighted by length"""
    wts = [len(strip_ar(t)) + 2 for t in tokens]
    tot, t, out = float(sum(wts)), speech_start, []
    for tok, w in zip(tokens, wts):
        span = (speech_end - speech_start) * w / tot
        out.append((tok, t, t + span))
        t += span
    return out


def align_words(text, events, dur, audio_path=None, lang="en", force_whisper=False):
    """Return [(token, start, end)] for one line, always covering the whole line."""
    tokens = text.split()
    out = None
    if events and len(events) == len(tokens) and not force_whisper:
        out = [(tokens[i], events[i][1], events[i][2]) for i in range(len(tokens))]
    else:
        ww = whisper_words(audio_path, lang) if audio_path else None
        if ww:
            log(f"whisper timing used ({len(ww)} words vs {len(tokens)} tokens)")
            if len(ww) == len(tokens):
                out = [(tokens[i], ww[i][1], ww[i][2]) for i in range(len(tokens))]
            else:  # different segmentation: keep the real speech span, spread tokens inside it
                out = span_map(tokens, ww[0][1], ww[-1][2])
        elif events:
            out = [(e[0], e[1], e[2]) for e in events]
        else:
            out = span_map(tokens, 0.0, max(dur - 0.15, 0.5))
    return [(w, max(0.0, min(s, dur)), max(0.0, min(e, dur))) for w, s, e in out]


# ----------------------------------------------------------------------------- Whisper (word timing fallback)
_WHISPER = {}


def whisper_words(path, lang):
    """Word timestamps from faster-whisper (free, CPU). Returns [(word,start,end)] or None."""
    try:
        from faster_whisper import WhisperModel
        size = "small" if lang.startswith("ar") else "base.en" if lang.startswith("en") else "base"
        if size not in _WHISPER:
            _WHISPER[size] = WhisperModel(size, device="cpu", compute_type="int8")
        segs, _ = _WHISPER[size].transcribe(str(path), language=lang[:2], word_timestamps=True, beam_size=1)
        out = [(w.word.strip(), float(w.start), float(w.end)) for sg in segs for w in (sg.words or []) if w.word.strip()]
        return out or None
    except Exception as e:  # noqa
        log("whisper unavailable:", e)
        return None


AR_MARKS = re.compile("[\u064B-\u065F\u0670\u0640]")


def strip_ar(t):
    return AR_MARKS.sub("", t)


# ----------------------------------------------------------------------------- Images
def gradient_fallback(path, seed):
    rnd = random.Random(seed)
    im = Image.new("RGB", (1080, 1920), (8, 10, 16))
    d = ImageDraw.Draw(im)
    for y in range(1920):
        k = y / 1920
        d.line([(0, y), (1080, y)], fill=(int(14 + 30 * (1 - k)), int(16 + 22 * (1 - k)), int(26 + 40 * (1 - k))))
    glow = Image.new("RGB", (1080, 1920), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = rnd.randint(300, 780), rnd.randint(500, 1100)
    gd.ellipse([cx - 380, cy - 380, cx + 380, cy + 380], fill=(150, 110, 40))
    glow = glow.filter(ImageFilter.GaussianBlur(220))
    Image.blend(im, glow, 0.5).save(path, quality=92)


def fetch_pollinations(prompt, seed, path):
    key = os.environ.get("POLLINATIONS_KEY", "").strip()
    delays = [0, 16, 28, 45]
    for attempt, delay in enumerate(delays, 1):
        time.sleep(delay)
        model = "flux" if attempt < 3 else "turbo"
        url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:700], safe="")
               + f"?width={W}&height={H}&model={model}&seed={seed + attempt}&nologo=true&private=true&safe=true")
        headers = {"User-Agent": "Mozilla/5.0 shorts-renderer"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            r = requests.get(url, headers=headers, timeout=150)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image") and len(r.content) > 15000:
                Path(path).write_bytes(r.content)
                Image.open(path).convert("RGB").save(path, quality=93)
                return True
            log(f"pollinations attempt {attempt}: HTTP {r.status_code}, {len(r.content)} bytes")
        except Exception as e:  # noqa
            log(f"pollinations attempt {attempt} error: {e}")
    return False


def fetch_cloudflare(prompt, path):
    acc, tok = os.environ.get("CF_ACCOUNT_ID", "").strip(), os.environ.get("CF_API_TOKEN", "").strip()
    if not (acc and tok):
        return False
    try:
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/black-forest-labs/flux-1-schnell",
            headers={"Authorization": f"Bearer {tok}"}, json={"prompt": prompt[:2000], "steps": 6}, timeout=120)
        data = r.json()
        img = base64.b64decode(data["result"]["image"])
        Path(path).write_bytes(img)
        Image.open(path).convert("RGB").save(path, quality=93)
        return True
    except Exception as e:  # noqa
        log("cloudflare flux failed:", e)
        return False


def get_image(prompt, idx, path):
    if os.environ.get("FAKE_IMAGES") == "1":
        gradient_fallback(path, idx * 77)
        return "fake"
    # Cloudflare first when configured (more reliable, no 15s rate limit), Pollinations otherwise
    order = ["cf", "poll"] if os.environ.get("CF_API_TOKEN") else ["poll", "cf"]
    for p in order:
        ok = fetch_cloudflare(prompt, path) if p == "cf" else fetch_pollinations(prompt, random.randint(1, 10**6), path)
        if ok:
            return p
    log(f"scene {idx}: all image providers failed -> gradient fallback")
    gradient_fallback(path, idx * 77)
    return "fallback"


# ----------------------------------------------------------------------------- Captions
def ass_time(t):
    t = max(t, 0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def clean_ass(s):
    return re.sub(r"[{}\\]", "", s).replace("\n", " ")


def build_ass(words_abs, lang, channel, total, accent="FFD700"):
    """words_abs: [(token, start, end)] absolute timeline"""
    arabic = lang.startswith("ar")
    accent = re.sub(r"[^0-9A-Fa-f]", "", accent or "FFD700")[:6].ljust(6, "0")
    gold = "&H00" + accent[4:6] + accent[2:4] + accent[0:2] + "&"   # RRGGBB -> ASS BGR
    font = "Noto Sans Arabic" if arabic else "Montserrat"
    size = 84 if arabic else 90
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,610,1
Style: Brand,{font},34,&H78FFFFFF,&H78FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,2,0,7,48,48,175,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if arabic:
        words_abs = [(strip_ar(w), s_, e_) for w, s_, e_ in words_abs]
    ev = []
    if channel:
        ev.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Brand,,0,0,0,,{clean_ass(channel)}")
    # group words
    groups, cur = [], []
    for w in words_abs:
        cur.append(w)
        chars = sum(len(x[0]) for x in cur) + len(cur)
        end_punct = re.search(r"[.,!?;:…—،؟]$", w[0]) is not None
        if len(cur) >= 3 or chars > 24 or end_punct:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0][1] if gi + 1 < len(groups) else None
        disp = [clean_ass(t if arabic else t.upper()) for t, _, _ in g]
        for k, (_, s, e) in enumerate(g):
            if k + 1 < len(g):
                end = g[k + 1][1]
            else:
                end = e + 0.12
                if nxt is not None:
                    end = min(max(end, s + 0.25), nxt)
            end = max(end, s + 0.08)
            hl = "{\\c" + gold + "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}" + disp[k] + "{\\r}"
            if arabic:
                # libass lays override-runs out left-to-right, so emit runs as: after + highlighted + before
                runs = [" ".join(disp[k + 1:]), hl, " ".join(disp[:k])]
                text = " ".join(r for r in runs if r)
            else:
                text = " ".join(disp[:k] + [hl] + disp[k + 1:])
            ev.append(f"Dialogue: 1,{ass_time(s)},{ass_time(end)},Cap,,0,0,0,,{text}")
    return hdr + "\n".join(ev) + "\n"


# ----------------------------------------------------------------------------- Video / Audio
def kenburns(i, frames):
    f = max(frames, 1)
    if i == 0:   # hook: fast push-in
        return (f"min(1+0.30*on/{f},1.30)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")
    m = i % 4
    if m == 1:
        return ("1.16", f"(iw-iw/zoom)*on/{f}", "ih/2-(ih/zoom/2)")
    if m == 2:
        return (f"max(1.20-0.20*on/{f},1.0)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")
    if m == 3:
        return ("1.16", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-on/{f})")
    return (f"min(1+0.20*on/{f},1.20)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")


TRANSITIONS = ["fade", "dissolve", "fadeblack", "smoothleft", "fade", "smoothup", "dissolve"]

def main():
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    job = payload["job_id"]
    lang = payload.get("lang", "en")
    voice = payload.get("voice", "en-GB-RyanNeural")
    rate = payload.get("rate", "-6%")
    pitch = payload.get("pitch", "-2Hz")
    style = payload.get("style", "")
    channel = payload.get("channel", "")
    lines = payload["lines"]
    n = len(lines)
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    log(f"job {job}: {n} scenes, voice={voice}")

    # 1) voice per line -> exact scene durations + word timings
    scenes, t0 = [], 0.0
    for i, ln in enumerate(lines):
        mp3 = WORK / f"line_{i}.mp3"
        wav = WORK / f"line_{i}.wav"
        ev = tts_line(ln["text"], voice, rate, pitch, lang, str(mp3))
        dur = probe_dur(mp3)
        gap = 0.40 if i == 0 else (0.12 if i == n - 1 else 0.14)
        d = max(dur + gap, 2.1)
        run(["ffmpeg", "-y", "-i", mp3, "-ar", 48000, "-ac", 2, "-af", f"apad=whole_dur={d:.3f}", wav])
        words = align_words(ln["text"], ev, dur, mp3, lang, payload.get("whisper") == "always")
        scenes.append({"i": i, "d": d, "start": t0, "dur": dur, "wav": wav,
                       "words": [(w, t0 + s, t0 + e) for w, s, e in words], "prompt": ln.get("image_prompt", "")})
        t0 += d
    total = t0 + 0.25
    log(f"total narration {t0:.1f}s")
    (WORK / "list.txt").write_text("".join(f"file '{s['wav'].name}'\n" for s in scenes))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", 0, "-i", "list.txt", "-c", "copy", "voice.wav"], cwd=WORK)

    # 2) images
    providers = []
    for s in scenes:
        p = WORK / f"img_{s['i']}.jpg"
        full = f"{s['prompt']}. {style}".strip()
        providers.append(get_image(full, s["i"], str(p)))
        s["img"] = p
    log("image providers:", providers)

    # 3) captions
    all_words = [w for s in scenes for w in s["words"]]
    (WORK / "captions.ass").write_text(build_ass(all_words, lang, channel, total, payload.get("accent", "FFD700")), encoding="utf-8")

    # 4) ffmpeg: inputs
    bar_hex = re.sub(r"[^0-9A-Fa-f]", "", payload.get("accent", "FFD700"))[:6].ljust(6, "0")
    cmd = ["ffmpeg", "-y", "-hide_banner"]
    for s in scenes:
        cmd += ["-i", s["img"].name]
    cmd += ["-f", "lavfi", "-i", f"color=c=0x{bar_hex}:s={W}x10:r={FPS}:d={total:.2f}"]          # n   : progress bar
    cmd += ["-i", "voice.wav"]                                                              # n+1 : voice
    music_files = sorted(list((ROOT / "assets" / "music").glob("*.mp3")) + list((ROOT / "assets" / "music").glob("*.wav")))
    mus_i = None
    if music_files:  # music is OFF by default; only used if you drop a track in assets/music/
        mf = random.choice(music_files)
        log("using music:", mf.name)
        cmd += ["-stream_loop", "-1", "-i", str(mf)]
        mus_i = n + 2
    bar_i, voice_i = n, n + 1

    # 5) filter graph
    fg = []
    for s in scenes:
        i = s["i"]
        L = s["d"] + XF
        F = int(round(L * FPS))
        z, x, y = kenburns(i, F)
        fg.append(f"[{i}:v]scale={KB_W}:{KB_H}:force_original_aspect_ratio=increase,crop={KB_W}:{KB_H},setsar=1,"
                  f"zoompan=z='{z}':x='{x}':y='{y}':d={F}:s={W}x{H}:fps={FPS},setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
    prev, acc = "v0", scenes[0]["d"] + XF
    for i in range(1, n):
        off = acc - XF
        tr = TRANSITIONS[(i - 1) % len(TRANSITIONS)]
        fg.append(f"[{prev}][v{i}]xfade=transition={tr}:duration={XF}:offset={off:.3f}[x{i}]")
        acc += scenes[i]["d"] + XF - XF
        prev = f"x{i}"
    fg.append(f"[{prev}]eq=contrast=1.07:saturation=0.92:gamma=0.97,colorbalance=rs=0.04:bs=-0.03:rh=0.03:bh=-0.02,"
              f"vignette=PI/4.5,noise=alls=5:allf=t,format=yuv420p[g]")
    fg.append(f"[g][{bar_i}:v]overlay=x='-w+w*t/{total:.2f}':y=0:eval=frame:shortest=1[o]")
    fg.append("[o]ass=captions.ass[vout]")

    voice_chain = (f"[{voice_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,highpass=f=70,"
                   f"acompressor=threshold=0.1:ratio=3:attack=5:release=80:makeup=2,"
                   f"aecho=0.8:0.6:40|85:0.16|0.09,volume=1.25,apad=whole_dur={total:.2f}")
    if mus_i is None:
        fg.append(voice_chain + f",loudnorm=I=-14:TP=-1.5:LRA=11,atrim=0:{total:.2f}[aout]")
    else:
        fg.append(voice_chain + ",asplit=2[vo][vsc]")
        fg.append(f"[{mus_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.22,"
                  f"afade=t=in:d=1.2,afade=t=out:st={max(total - 2.2, 0):.2f}:d=2.2[mu]")
        fg.append("[mu][vsc]sidechaincompress=threshold=0.015:ratio=9:attack=15:release=450[du]")
        fg.append(f"[vo][du]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,"
                  f"loudnorm=I=-14:TP=-1.5:LRA=11,atrim=0:{total:.2f}[aout]")
    (WORK / "graph.txt").write_text(";\n".join(fg))

    out_name = f"short_{job}.mp4"
    cmd += ["-filter_complex_script", "graph.txt", "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-maxrate", "5M", "-bufsize", "10M",
            "-pix_fmt", "yuv420p", "-r", FPS, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-t", f"{total:.2f}", str(OUT / out_name)]
    run(cmd, cwd=WORK)

    size = (OUT / out_name).stat().st_size
    result = {"job_id": job, "file": out_name, "duration": round(total, 2), "size_mb": round(size / 1e6, 2),
              "scenes": n, "image_providers": providers}
    (OUT / "result.json").write_text(json.dumps(result))
    log("DONE", result)


if __name__ == "__main__":
    main()
