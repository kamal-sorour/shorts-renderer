# shorts-renderer

Free cloud renderer for the "Ancient Answers" n8n workflow.
n8n sends a script -> this repo's GitHub Action renders a 1080x1920 Short -> publishes it as a Release asset -> calls n8n back.

Stack (all free): edge-tts (voice + word timing), Pollinations / Cloudflare FLUX (images), ffmpeg (Ken Burns, crossfades, film look, captions, synthesized score).

## Setup
1. Create a **public** GitHub repo named `shorts-renderer` and push all files in this folder (keep `.github/workflows/render.yml`).
2. Repo > Settings > Secrets and variables > Actions > New secret:
   - `N8N_CALLBACK_URL` = webhook PRODUCTION url of the Stoic workflow (required)
   - `N8N_CALLBACK_URL_MOTIV` = webhook PRODUCTION url of the Motivation workflow
   - `POLLINATIONS_KEY` (optional, free key from auth.pollinations.ai, avoids rate limits)
   - `CF_ACCOUNT_ID` + `CF_API_TOKEN` (optional, Cloudflare Workers AI, reliable FLUX fallback)
3. Repo > Settings > Actions > General > Workflow permissions: **Read and write**.
4. Optional: put .mp3 music in `assets/music/`.

## Local test (no network needed)
FAKE_TTS=1 FAKE_IMAGES=1 python render.py payload.json

## Long-form explainer (render_long.py)
Workflow: `.github/workflows/render_long.yml`. Extra secrets: `N8N_CALLBACK_URL_LONG`, `PEXELS_API_KEY`, `PIXABAY_API_KEY`.
Character PNGs go in `assets/character/` (see the README.txt there). Optional music in `assets/music/`.
Local test (no network): FAKE_TTS=1 FAKE_BROLL=1 CHAR_DIR=/path/to/pngs python render_long.py payload.json

## v3 additions
- **No Pexels key needed.** Image/video sources: Pixabay (video + photo, key), Wikimedia Commons, Openverse (no key), Pexels only if a key exists.
- **Auto QC** after every render (tools/qc, MIT, from video-editing-skills): resolution, fps, duration, audio level/peak, black frames. Result is added to the callback (`result.qc`) and a proof sheet PNG is attached to the release (`proof_url`).
- **Long video extras:** neon-flicker title bumper, chapter progress bar with ticks, "Sources & credits" end card, attribution built into the video.

## v4 (production)
- Long video: fixed character size/position, 2-3 cuts per line with punch-in, slow-mo for short clips, animated charts (bar/line/timeline), no progress bar, no credits card (credits go to the description).
- **Resume:** finished voice/segments are cached per job; failed renders auto-resume (up to 3 attempts).
- **YouTube private upload** (tools/yt_upload.py): secrets YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN. Uploads as PRIVATE, only if meta.upload is true and QC passed.
