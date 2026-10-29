#!/usr/bin/env python3
"""
Réparateur de vidéos : convertit en H.264 (≤ 1080p, ≤ 30 images/s, audio AAC) les vidéos du catalogue que X refuse
(YouTube Shorts brut en AV1 / VP9, 60 images/s, 4K), et remplace le fichier au MÊME emplacement sur R2.
Le catalogue, les identifiants et les adresses ne changent pas : le robot vidéo peut ensuite les programmer.

Les vidéos à réparer sont celles marquées "ok": false dans video_probe.json (écrit chaque jour par probe_catalog.py).
Les gros noms (Mbappé, Yamal...) passent en premier. Chaque conversion est vérifiée (codec, taille, durée) avant
d'écraser l'original ; en cas de doute, l'original est laissé tel quel.

Variables : R2_PUBLIC_BASE, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET (défaut accesmatch-videos).
  python reencode.py --limit 60 --budget-min 90      # 60 vidéos max, arrêt propre après 90 minutes
  python reencode.py --dry-run                        # liste seulement ce qui serait réparé
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
PROBE_FILE = ROOT / "video_probe.json"
DONE_FILE = ROOT / "video_reencoded.json"
R2_PUBLIC_BASE = (os.environ.get("R2_PUBLIC_BASE") or "").strip().rstrip("/")
R2_ACCOUNT_ID = (os.environ.get("R2_ACCOUNT_ID") or "").strip()
R2_ACCESS_KEY_ID = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
R2_SECRET_ACCESS_KEY = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip()
R2_BUCKET = (os.environ.get("R2_BUCKET") or "accesmatch-videos").strip()

MAX_LONG, MAX_SHORT = 1920, 1080      # on vise 1080p (X accepte jusqu'à 1920x1200)
MAX_FPS = 30
MAX_BYTES = 200 * 1024 * 1024
DURATION_TOLERANCE_S = 1.5


def log(msg):
    print(msg, flush=True)


def gh_notice(msg):
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice::" + str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A"), flush=True)


def gh_warning(msg):
    if os.environ.get("GITHUB_ACTIONS"):
        print("::warning::" + str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A"), flush=True)


def probe_file(path):
    """(codec vidéo, largeur, hauteur, images/s, codec audio ou None, durée) d'un fichier local."""
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
                        str(path)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("ffprobe : " + (r.stderr or "")[-150:])
    d = json.loads(r.stdout)
    vs = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    au = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not vs:
        raise RuntimeError("pas de piste vidéo")
    a, _, b = (vs.get("r_frame_rate") or "0/1").partition("/")
    fps = float(a) / float(b or 1) if float(b or 1) else 0.0
    return (vs.get("codec_name"), int(vs.get("width") or 0), int(vs.get("height") or 0), fps,
            au.get("codec_name") if au else None, float((d.get("format") or {}).get("duration") or 0))


def download(key, dest):
    with requests.get(f"{R2_PUBLIC_BASE}/{key}", stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def transcode(src, dst, fps_in):
    """Vers H.264 + AAC, 1080p maximum, 30 images/s maximum, sans jamais agrandir."""
    vf = ("scale=w='min(iw,if(gte(iw,ih),%d,%d))':h='min(ih,if(gte(iw,ih),%d,%d))'"
          ":force_original_aspect_ratio=decrease:force_divisible_by=2" % (MAX_LONG, MAX_SHORT, MAX_SHORT, MAX_LONG))
    if fps_in > MAX_FPS + 0.5:
        vf += f",fps={MAX_FPS}"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
           "-movflags", "+faststart", "-map_metadata", "-1", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg : " + (r.stderr or "")[-200:])


def verify(dst, dur_src):
    codec, w, h, fps, ac, dur = probe_file(dst)
    size = Path(dst).stat().st_size
    problems = []
    if codec != "h264":
        problems.append(f"codec {codec}")
    if ac not in (None, "aac"):
        problems.append(f"audio {ac}")
    if max(w, h) > 1920 or min(w, h) > 1200 or min(w, h) < 2:
        problems.append(f"{w}x{h}")
    if fps > MAX_FPS + 0.5 or fps < 1:
        problems.append(f"{fps:.1f} images/s")
    if size > MAX_BYTES or size < 20_000:
        problems.append(f"taille {size} octets")
    if dur_src and abs(dur - dur_src) > DURATION_TOLERANCE_S:
        problems.append(f"durée {dur:.1f} s au lieu de {dur_src:.1f} s")
    return problems, (codec, w, h, round(fps), round(dur, 1), size)


def make_s3():
    import boto3
    from botocore.config import Config
    kw = dict(endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com", aws_access_key_id=R2_ACCESS_KEY_ID,
              aws_secret_access_key=R2_SECRET_ACCESS_KEY, region_name="auto")
    try:   # boto3 récent : pas de somme de contrôle automatique, que R2 ne demande pas
        kw["config"] = Config(request_checksum_calculation="when_required", response_checksum_validation="when_required")
    except TypeError:
        pass
    return boto3.client("s3", **kw)


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def priority_order(todo, videos_by_id):
    """Stars d'abord (comme le robot vidéo), puis le reste, dans l'ordre stable."""
    try:
        import videoqueue as vq
        def rank(v):
            s = vq.star_of(videos_by_id.get(v["id"], v))
            return (0 if s in vq.BOOST else 1 if s else 2, vq.STAR_ORDER.index(s) if s in vq.STAR_ORDER else 99, v["id"])
        return sorted(todo, key=rank)
    except Exception as e:                     # sans le robot vidéo, ordre simple
        log(f"(ordre de priorité indisponible : {e})")
        return sorted(todo, key=lambda v: v["id"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60, help="nombre maximal de vidéos réparées par passage")
    ap.add_argument("--budget-min", type=float, default=120, help="arrêt propre après ce nombre de minutes")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not R2_PUBLIC_BASE:
        log("R2_PUBLIC_BASE manquant")
        return 2
    probe = load_json(PROBE_FILE, {})
    done = load_json(DONE_FILE, {})
    r = requests.get(f"{R2_PUBLIC_BASE}/catalog.json?t={int(time.time())}", timeout=60)
    r.raise_for_status()
    videos = r.json()["videos"]
    by_id = {v["id"]: v for v in videos}
    todo = [v for v in videos if probe.get(v["id"], {}).get("ok") is False and v["id"] not in done]
    todo = priority_order(todo, by_id)
    log(f"{len(todo)} vidéo(s) à réparer sur {len(videos)} (déjà réparées : {len(done)})")
    if args.dry_run:
        lines = [f"{v.get('folder')} | {v.get('player') or '-'} | {v.get('title', '')[:45]} | {','.join(probe[v['id']].get('r') or [])}"
                 for v in todo[:12]]
        for line in lines:
            log("  " + line)
        gh_notice(f"[réparation, simulation] {len(todo)} vidéos à convertir. Les 12 premières :\n" + "\n".join(lines))
        return 0
    if not todo:
        gh_notice("[réparation] rien à réparer")
        return 0
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
        gh_warning(f"[réparation] {len(todo)} vidéos refusées par X attendent une conversion, mais les accès R2 "
                   "(R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY) ne sont pas encore enregistrés dans GitHub "
                   "(Settings → Secrets and variables → Actions).")
        return 0
    s3 = make_s3()
    t0 = time.time()
    ok_n, fail_n, saved = 0, 0, 0
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for v in todo:
            if ok_n + fail_n >= args.limit or (time.time() - t0) / 60 > args.budget_min:
                break
            vid, key = v["id"], v["key"]
            src, dst = Path(tmp) / "in.mp4", Path(tmp) / "out.mp4"
            try:
                t1 = time.time()
                download(key, src)
                codec, w, h, fps, ac, dur = probe_file(src)
                transcode(src, dst, fps)
                problems, info = verify(dst, dur)
                if problems:
                    raise RuntimeError("contrôle refusé : " + ", ".join(problems))
                s3.put_object(Bucket=R2_BUCKET, Key=key, Body=dst.read_bytes(), ContentType="video/mp4",
                              CacheControl="public, max-age=300")
                done[vid] = {"key": key, "from": f"{codec} {w}x{h} {fps:.0f}fps", "to": "%s %dx%d %dfps %.0fs" % info[:5],
                             "bytes_before": src.stat().st_size, "bytes_after": info[5],
                             "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                ok_n += 1
                saved += src.stat().st_size - info[5]
                log(f"✅ {key} : {codec} {w}x{h} {fps:.0f}fps → h264 {info[1]}x{info[2]} {info[3]}fps "
                    f"({src.stat().st_size // 1024} → {info[5] // 1024} Ko, {time.time() - t1:.0f} s)")
            except Exception as e:                         # on garde l'original et on passe à la suivante
                fail_n += 1
                failures.append(f"{key} : {str(e)[:160]}")
                log(f"❌ {key} : {e}")
            finally:
                for p in (src, dst):
                    p.unlink(missing_ok=True)
            DONE_FILE.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    left = len(todo) - ok_n - fail_n
    gh_notice(f"[réparation] {ok_n} vidéo(s) converties en H.264, {fail_n} échec(s), il en reste {left} "
              f"({(time.time() - t0) / 60:.0f} min)" + ("\n" + "\n".join(failures[:6]) if failures else ""))
    return 0 if fail_n <= max(3, ok_n) else 1


if __name__ == "__main__":
    sys.exit(main())
