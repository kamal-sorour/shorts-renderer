#!/usr/bin/env python3
"""Upload a finished video to YouTube as a PRIVATE draft (resumable upload, no extra dependencies).
Env: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
Usage: python tools/yt_upload.py out/long_X.mp4 payload.json out/result.json
Skips (exit 0) when: secrets missing, meta.upload is not true, or the render QC failed.
Writes out/youtube.json = {"id": ..., "url": ...} on success.
"""
import json, os, sys, time
from pathlib import Path

import requests

CHUNK = 8 * 1024 * 1024  # multiple of 256 KB


def log(*a):
    print("[yt]", *a, flush=True)


def access_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def build_body(meta, result):
    chapters = "\n".join(f"{c['t']} {c['title']}" for c in result.get("chapters", []))
    credits = "\n".join(f"- {c}" for c in result.get("credits", [])[:40])
    desc = (meta.get("description", "") + ("\n\nCHAPTERS\n" + chapters if chapters else "") +
            ("\n\nSOURCES & CREDITS\n" + credits if credits else "")).strip()[:4900]
    tags = [str(t)[:30] for t in meta.get("tags", [])][:15]
    return {"snippet": {"title": str(meta.get("title", "Untitled"))[:100], "description": desc.replace("<", "").replace(">", ""),
                        "tags": tags, "categoryId": str(meta.get("category_id", "28")), "defaultLanguage": "en"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}


def upload(video, body, token):
    size = Path(video).stat().st_size
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
         "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}
    r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                      headers=h, data=json.dumps(body), timeout=60)
    r.raise_for_status()
    loc = r.headers["Location"]
    sent = 0
    with open(video, "rb") as f:
        while sent < size:
            f.seek(sent)
            data = f.read(CHUNK)
            end = sent + len(data) - 1
            for attempt in range(5):
                try:
                    rr = requests.put(loc, data=data, timeout=300, headers={
                        "Content-Length": str(len(data)), "Content-Range": f"bytes {sent}-{end}/{size}", "Content-Type": "video/mp4"})
                    if rr.status_code in (200, 201):
                        return rr.json()
                    if rr.status_code == 308:
                        break
                    log("chunk status", rr.status_code, rr.text[:200])
                except requests.RequestException as e:
                    log("chunk error", e)
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError("upload chunk failed after retries")
            sent = end + 1
    raise RuntimeError("upload finished without a final response")


def main():
    video, payload_p, result_p = sys.argv[1:4]
    payload = json.loads(Path(payload_p).read_text(encoding="utf-8"))
    result = json.loads(Path(result_p).read_text(encoding="utf-8"))
    meta = payload.get("meta", {})
    if not all(os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")):
        return log("YouTube secrets not set: skipping upload")
    if meta.get("upload") is not True:
        return log("meta.upload is not true: skipping upload")
    qc = result.get("qc")
    if qc is not None and qc.get("ok") is False:
        return log("QC failed, holding the upload:", qc.get("errors"))
    res = upload(video, build_body(meta, result), access_token())
    out = {"id": res.get("id"), "url": f"https://studio.youtube.com/video/{res.get('id')}/edit", "privacy": "private"}
    Path(video).parent.joinpath("youtube.json").write_text(json.dumps(out))
    log("uploaded:", out)


if __name__ == "__main__":
    main()
