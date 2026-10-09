#!/usr/bin/env python3
"""
AccesMatch VideoQueue — garde ta file Buffer pleine de vidéos, tout seul.

Chaque jour, le robot :
  1. lit le catalogue de tes vidéos (catalog.json, hébergé sur Cloudflare R2)
  2. compte combien de vidéos sont déjà programmées dans la file Buffer de @accesmatch
  3. ajoute les vidéos manquantes pour toujours avoir ~14 jours d'avance (4 posts/jour)
     -> Buffer les place dans TES créneaux (9h15, 12h30, 18h30, 21h45), à la suite de ce qui existe
  4. alterne les joueurs (jamais le même joueur 2 fois de suite) et glisse un CTA ~1 post sur 7
  5. t'alerte (le robot passe au rouge sur GitHub => email) quand il te reste moins de 14 jours de stock

Commandes :
  python3 videoqueue.py             # remplit la file
  python3 videoqueue.py --dry-run   # montre ce qui serait ajouté, sans rien envoyer
  python3 videoqueue.py --status    # état du stock et de la file

Variables d'environnement : BUFFER_API_KEY, R2_PUBLIC_BASE (ex: https://pub-xxxx.r2.dev)
Codes de sortie : 0 = OK, 2 = erreur, 3 = stock bas (alerte)
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ============================================================
# CONFIGURATION
# ============================================================
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "video_state.json"

BUFFER_API_KEY = os.environ.get("BUFFER_API_KEY", "")
BUFFER_URL = "https://api.buffer.com"
BUFFER_CHANNEL_ID = os.environ.get("BUFFER_CHANNEL_ID", "6ac7ca726a5c39ccb6535913")   # @accesmatch
BUFFER_ORG_ID = os.environ.get("BUFFER_ORG_ID", "6ab1f781b91aadc0d8a2a8ca")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "").rstrip("/")

POSTS_PER_DAY = 4              # tes 4 créneaux Buffer
QUEUE_TARGET_DAYS = 14         # jours de vidéos gardés d'avance dans Buffer
MAX_NEW_PER_RUN = 70           # garde-fou par passage
LOW_STOCK_DAYS = 14            # alerte si stock (file + non programmées) < 14 jours
API_DELAY = 2.0                # pause entre deux posts (limite Buffer : 100 / 15 min)
MAX_FAILS = 2                  # une vidéo refusée 2 fois est mise de côté
MAX_CONSECUTIVE_REJECTS = 3    # 3 refus d'affilée = problème général, on arrête
AVOID_LAST_PLAYERS = 3         # pas le même joueur dans les 3 derniers posts
SHUFFLE_SEED = "accesmatch-v1"
TWEET_MAX = 280


def log(msg):
    print(msg, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================
# ÉTAT
# ============================================================
def load_state():
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            st.setdefault("posted", {})
            st.setdefault("failed", {})
            st.setdefault("counter", 0)
            st.setdefault("recent_players", [])
            return st
        except json.JSONDecodeError:
            log("⚠️  video_state.json illisible, on repart de zéro")
    return {"posted": {}, "failed": {}, "counter": 0, "recent_players": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ============================================================
# CATALOGUE
# ============================================================
def fetch_catalog():
    if not R2_PUBLIC_BASE:
        raise SystemExit("❌ R2_PUBLIC_BASE manquant")
    url = f"{R2_PUBLIC_BASE}/catalog.json?t={int(time.time())}"
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"catalogue introuvable ({r.status_code}) : {url}")
    cat = r.json()
    if not cat.get("videos"):
        raise RuntimeError("catalogue vide")
    return cat


def candidates(videos, state):
    return [v for v in videos
            if v["id"] not in state["posted"]
            and state["failed"].get(v["id"], {}).get("count", 0) < MAX_FAILS]


def order_key(v):
    return hashlib.sha1((v["id"] + SHUFFLE_SEED).encode()).hexdigest()


def pick_next(cands, recent_players):
    """Mélange stable + jamais le même joueur dans les derniers posts."""
    ordered = sorted(cands, key=order_key)
    for v in ordered:
        p = v.get("player")
        if not p or p not in recent_players:
            return v
    return ordered[0] if ordered else None


def build_text(v, counter, catalog):
    every = catalog.get("cta_every", 7)
    title = v["title"].strip()
    tags = (v.get("hashtags") or "").strip()
    kind, cta = "none", None
    if every and (counter + 1) % every == 0 and catalog.get("ctas"):
        rng = random.Random(f"cta-{counter}")
        kind = "bio" if rng.random() < catalog.get("cta_bio_ratio", 0.7) else "direct"
        cta = rng.choice(catalog["ctas"][kind])

    def assemble(tag_list):
        parts = [title] + ([cta] if cta else []) + ([" ".join(tag_list)] if tag_list else [])
        return "\n\n".join(parts)

    tag_list = tags.split()
    text = assemble(tag_list)
    while len(text) > TWEET_MAX and tag_list:   # trop long : on retire des hashtags
        tag_list.pop()
        text = assemble(tag_list)
    return text, kind


# ============================================================
# BUFFER (GraphQL)
# ============================================================
QUEUE_Q = """
query Queue($input: PostsInput!, $after: String) {
  posts(first: 100, after: $after, input: $input) {
    edges { node { id dueAt assets { type } } }
    pageInfo { hasNextPage endCursor }
  }
}"""

CREATE_POST_Q = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id dueAt status } }
    ... on MutationError { message }
  }
}"""


class VideoRejected(Exception):
    """Buffer a refusé CETTE vidéo (problème propre à la vidéo, pas général)."""


def gql(query, variables):
    if not BUFFER_API_KEY:
        raise SystemExit("❌ BUFFER_API_KEY manquant")
    headers = {"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"}
    r = None
    for attempt in range(2):
        r = requests.post(BUFFER_URL, headers=headers, json={"query": query, "variables": variables}, timeout=90)
        if r.status_code == 429 and attempt == 0:
            wait = min(int(r.headers.get("Retry-After", "30") or 30), 180)
            log(f"⏳ Buffer: limite atteinte, pause {wait} s")
            time.sleep(wait)
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"Buffer HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(f"Buffer GraphQL: {json.dumps(j['errors'])[:400]}")
    return j["data"]


def queue_stats():
    """(nombre de vidéos déjà programmées dans la file, dernière date prévue)."""
    after, count, last = None, 0, None
    while True:
        inp = {"organizationId": BUFFER_ORG_ID,
               "filter": {"channelIds": [BUFFER_CHANNEL_ID], "status": ["scheduled"]}}
        data = gql(QUEUE_Q, {"input": inp, "after": after})["posts"]
        for e in data.get("edges") or []:
            node = e["node"]
            if any(a.get("type") == "video" for a in node.get("assets") or []):
                count += 1
                due = node.get("dueAt")
                if due and (last is None or due > last):
                    last = due
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return count, last


def create_video_post(text, video_url):
    inp = {"channelId": BUFFER_CHANNEL_ID, "schedulingType": "automatic", "mode": "addToQueue",
           "text": text, "assets": [{"video": {"url": video_url}}]}
    res = gql(CREATE_POST_Q, {"input": inp})["createPost"]
    t = res["__typename"]
    if t == "PostActionSuccess":
        return res["post"]["id"], res["post"].get("dueAt")
    msg = f"{t}: {res.get('message')}"
    if t in ("InvalidInputError", "NotFoundError"):
        raise VideoRejected(msg)
    raise RuntimeError(f"Buffer a refusé la création ({msg})")   # limite atteinte, droits, etc. = général


# ============================================================
# LOGIQUE PRINCIPALE
# ============================================================
def run(dry_run=False, status_only=False):
    catalog = fetch_catalog()
    videos = catalog["videos"]
    state = load_state()
    in_queue, last_due = queue_stats()
    cands = candidates(videos, state)

    target = QUEUE_TARGET_DAYS * POSTS_PER_DAY
    need = max(0, target - in_queue)
    log(f"📦 Catalogue : {len(videos)} vidéos | déjà postées par le robot : {len(state['posted'])} | "
        f"disponibles : {len(cands)}")
    log(f"📅 File Buffer : {in_queue} vidéos programmées (jusqu'au {last_due or '—'}) | objectif {target}")

    if status_only:
        days = (in_queue + len(cands)) / POSTS_PER_DAY
        log(f"⏳ Stock total : ~{days:.0f} jours de vidéos")
        return 3 if days < LOW_STOCK_DAYS else 0

    created, rejects = 0, 0
    pool = list(cands)
    while need > 0 and created < MAX_NEW_PER_RUN and pool:
        v = pick_next(pool, state["recent_players"])
        if not v:
            break
        text, kind = build_text(v, state["counter"], catalog)
        url = f"{R2_PUBLIC_BASE}/{v['key']}"
        if dry_run:
            log(f"🆕 [dry-run] {v['title']} ({v.get('player') or '—'}) CTA={kind}\n      {text.replace(chr(10), ' | ')}")
            post_id, due = None, None
        else:
            try:
                post_id, due = create_video_post(text, url)
            except VideoRejected as e:
                rejects += 1
                f = state["failed"].setdefault(v["id"], {"count": 0})
                f["count"] += 1
                f["error"] = str(e)[:200]
                log(f"⚠️  vidéo refusée ({v['id']}) : {e}")
                save_state(state)
                pool = [x for x in pool if x["id"] != v["id"]]
                if rejects >= MAX_CONSECUTIVE_REJECTS:
                    log("❌ 3 refus d'affilée : problème général (clé, vidéos inaccessibles ?), arrêt.")
                    return 2
                continue
            rejects = 0
            state["posted"][v["id"]] = {"post_id": post_id, "due": due, "at": now_iso(), "title": v["title"]}
            log(f"📤 {v['title']} → file Buffer ({due or 'créneau auto'})")
            time.sleep(API_DELAY)
        pool = [x for x in pool if x["id"] != v["id"]]
        state["counter"] += 1
        state["recent_players"] = (state["recent_players"] + [v.get("player")] if v.get("player")
                                   else state["recent_players"])[-AVOID_LAST_PLAYERS:]
        created += 1
        need -= 1
        if not dry_run:
            save_state(state)

    remaining = len(pool)
    days_left = (in_queue + created + remaining) / POSTS_PER_DAY
    log(f"✅ {created} vidéo(s) ajoutée(s). Stock restant : {remaining} non programmées "
        f"(~{days_left:.0f} jours au total)")
    if not dry_run:
        save_state(state)
    if days_left < LOW_STOCK_DAYS:
        log(f"🚨 STOCK BAS : il te reste ~{days_left:.0f} jours de vidéos. Ajoute de nouvelles vidéos "
            f"(upload_videos.py sur ton Mac).")
        return 3
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="AccesMatch VideoQueue")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    try:
        code = run(dry_run=args.dry_run, status_only=args.status)
    except (RuntimeError, requests.RequestException) as e:
        log(f"❌ {e}")
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
