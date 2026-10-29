#!/usr/bin/env python3
"""
Sonde du catalogue R2 : repère les vidéos que X refuse.

X (via Buffer) n'accepte que certains formats : les vidéos YouTube Shorts téléchargées en AV1 ou VP9, en 60 images/s
ou en 4K sont refusées ("unknown error"), alors que les vidéos TikTok (H.264 / HEVC) passent.
Ce script lit l'en-tête de chaque vidéo à distance (ffprobe sur l'adresse R2, quelques ko par vidéo), note le résultat
dans video_probe.json, et le robot vidéo ignore ensuite toute vidéo marquée "ok": false.

  python probe_catalog.py          # sonde les vidéos jamais examinées + re-teste celles refusées (passe chaque jour :
                                   # une vidéo ré-encodée au même emplacement redevient utilisable toute seule)
  python probe_catalog.py --all    # re-sonde tout le catalogue

Variable : R2_PUBLIC_BASE. Nécessite ffprobe (ffmpeg).
"""
import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
PROBE_FILE = ROOT / "video_probe.json"
R2_PUBLIC_BASE = (os.environ.get("R2_PUBLIC_BASE") or "").strip().rstrip("/")

OK_VIDEO_CODECS = {"h264", "hevc"}        # constaté : H.264 et HEVC partent, AV1 et VP9 sont refusés par X
OK_AUDIO_CODECS = {"aac"}
MAX_FPS = 60.5
MAX_LONG_SIDE, MAX_SHORT_SIDE = 1920, 1200
MAX_DURATION = 140.5
THREADS = 12


def log(msg):
    print(msg, flush=True)


def gh_notice(msg):
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice::" + str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A"), flush=True)


def fps_of(s):
    try:
        a, b = s.split("/")
        return float(a) / float(b) if float(b) else 0.0
    except (ValueError, AttributeError, ZeroDivisionError):
        return 0.0


def probe_one(v):
    url = f"{R2_PUBLIC_BASE}/{v['key']}"
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration", url]
    data, err = None, ""
    for _ in range(2):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                break
            err = (r.stderr or "ffprobe sans réponse")[-120:]
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            err = str(e)[:120]
        time.sleep(2)
    if data is None:
        return v["id"], {"ok": None, "k": v["key"], "e": err}
    streams = data.get("streams") or []
    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    au = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not vs:
        return v["id"], {"ok": False, "k": v["key"], "r": ["pas de piste vidéo"]}
    w, h = int(vs.get("width") or 0), int(vs.get("height") or 0)
    fps = fps_of(vs.get("r_frame_rate"))
    dur = float((data.get("format") or {}).get("duration") or 0)
    reasons = []
    if vs.get("codec_name") not in OK_VIDEO_CODECS:
        reasons.append(f"codec {vs.get('codec_name')}")
    if fps > MAX_FPS:
        reasons.append(f"{fps:.0f} images/s")
    if max(w, h) > MAX_LONG_SIDE or min(w, h) > MAX_SHORT_SIDE:
        reasons.append(f"{w}x{h}")
    if au and au.get("codec_name") not in OK_AUDIO_CODECS:
        reasons.append(f"audio {au.get('codec_name')}")
    if dur > MAX_DURATION:
        reasons.append(f"{dur:.0f} s")
    res = {"ok": not reasons, "k": v["key"], "c": vs.get("codec_name"), "f": round(fps), "w": w, "h": h, "d": round(dur, 1)}
    if reasons:
        res["r"] = reasons
    return v["id"], res


def load_probe():
    if PROBE_FILE.exists():
        try:
            return json.loads(PROBE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("video_probe.json illisible, on repart de zéro")
    return {}


def save_probe(probe):
    PROBE_FILE.write_text(json.dumps(probe, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="re-sonde tout le catalogue")
    args = ap.parse_args(argv)
    if not R2_PUBLIC_BASE:
        log("R2_PUBLIC_BASE manquant")
        return 2
    r = requests.get(f"{R2_PUBLIC_BASE}/catalog.json?t={int(time.time())}", timeout=60)
    r.raise_for_status()
    videos = r.json()["videos"]
    probe = load_probe()
    ids = {v["id"] for v in videos}
    for gone in [k for k in probe if k not in ids]:
        del probe[gone]                      # vidéo retirée du catalogue
    todo = [v for v in videos if args.all or v["id"] not in probe or not probe[v["id"]].get("ok")]
    log(f"{len(videos)} vidéos au catalogue, {len(todo)} à sonder")
    done = 0
    with ThreadPoolExecutor(THREADS) as ex:
        for vid, res in ex.map(probe_one, todo):
            probe[vid] = res
            done += 1
            if done % 200 == 0:
                save_probe(probe)
                log(f"  {done}/{len(todo)}")
    save_probe(probe)

    by_folder = {}
    for v in videos:
        p = probe.get(v["id"])
        by_folder.setdefault((v.get("folder") or "?"), Counter())["ok" if p and p.get("ok") else
                                                                    "inconnu" if not p or p.get("ok") is None else "refusé"] += 1
    reasons = Counter()
    for v in videos:
        p = probe.get(v["id"]) or {}
        for rr in p.get("r") or []:
            reasons[rr.split(" ")[0] if rr.startswith(("codec", "audio")) else rr if "images" in rr else
                    "résolution" if "x" in rr else rr] += 1
    codecs = Counter((probe.get(v["id"]) or {}).get("c") for v in videos)
    head = " | ".join(f"{f}: {c['ok']} ok, {c['refusé']} refusées, {c['inconnu']} inconnues" for f, c in by_folder.items())
    log(head)
    gh_notice(f"[sonde catalogue] {len(todo)} vidéos sondées ce passage | codecs : "
              + ", ".join(f"{k} {n}" for k, n in codecs.most_common())
              + f"\nPar créateur : {head}\nMotifs de refus : " + (", ".join(f"{k} {n}" for k, n in reasons.most_common()) or "aucun"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
