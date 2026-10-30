# shorts-renderer

Free cloud renderer for the "Ancient Answers" n8n workflow.
n8n sends a script -> this repo's GitHub Action renders a 1080x1920 Short -> publishes it as a Release asset -> calls n8n back.

Stack (all free): edge-tts (voice + word timing), Pollinations / Cloudflare FLUX (images), ffmpeg (Ken Burns, crossfades, film look, captions, synthesized score).

## Setup
1. Create a **public** GitHub repo named `shorts-renderer` and push all files in this folder (keep `.github/workflows/render.yml`).
2. Repo > Settings > Secrets and variables > Actions > New secret:
   - `N8N_CALLBACK_URL` = your n8n webhook PRODUCTION url (required)
   - `POLLINATIONS_KEY` (optional, free key from auth.pollinations.ai, avoids rate limits)
   - `CF_ACCOUNT_ID` + `CF_API_TOKEN` (optional, Cloudflare Workers AI, reliable FLUX fallback)
3. Repo > Settings > Actions > General > Workflow permissions: **Read and write**.
4. Optional: put .mp3 music in `assets/music/`.

## Local test (no network needed)
FAKE_TTS=1 FAKE_IMAGES=1 python render.py payload.json
