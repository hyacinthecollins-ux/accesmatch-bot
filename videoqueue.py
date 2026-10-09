#!/usr/bin/env python3
"""
AccesMatch VideoQueue — garde ta file Buffer pleine de vidéos, tout seul.

Chaque jour, le robot :
  1. lit le catalogue de tes vidéos (catalog.json, hébergé sur Cloudflare R2)
  2. compte combien de vidéos sont déjà programmées dans la file Buffer de @accesmatch
  3. ajoute les vidéos manquantes pour toujours avoir ~14 jours d'avance (4 posts/jour),
     dans tes créneaux (9h15, 12h30, 18h30, 21h45, heure de Paris), en comblant d'abord les trous
  4. alterne les joueurs (jamais le même joueur 2 fois de suite) et applique le plan de CTA par créneau :
       9h15  = vidéo seule | 12h30 = CTA "regarde ma bio" | 18h30 = lien Telegram en RÉPONSE | 21h45 = CTA bio
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
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from zoneinfo import ZoneInfo

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

POST_TZ = ZoneInfo("Europe/Paris")
MIN_LEAD_MIN = 15              # un créneau doit être au moins 15 min dans le futur
# Plan de CTA par créneau (heure de Paris) :
#   none  = vidéo seule (le contenu qui fait grossir le compte)
#   bio   = phrase d'accroche "regarde ma bio" (sans lien)
#   reply = vidéo propre + lien Telegram dans une RÉPONSE (la portée de la vidéo est préservée)
SLOT_PLAN = [("09:15", "none"), ("12:30", "bio"), ("18:30", "reply"), ("21:45", "bio")]


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


def clean_title(t):
    """Nettoie le titre : '⧸' (faux slash des noms YouTube) -> '/', '#' orphelin retiré, espaces propres."""
    t = (t or "").replace("\u29f8", "/").replace("\u2044", "/")
    t = re.sub(r"\s#(?=\s|$)", "", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def next_cta(state, kind, pool):
    """CTA suivant dans une rotation mélangée : aucune phrase n'est répétée avant d'avoir fait tout le tour."""
    if not pool:
        return None
    order = sorted(pool, key=lambda t: hashlib.sha1((t + SHUFFLE_SEED + kind).encode()).hexdigest())
    key = f"cta_idx_{kind}"
    idx = state.get(key, 0)
    state[key] = idx + 1
    return order[idx % len(order)]


def build_post(v, kind, state, catalog):
    """Retourne (texte du post, texte de la réponse ou None) selon le type de créneau."""
    title = clean_title(v["title"])
    tags = (v.get("hashtags") or "").strip()
    cta = None
    reply = None
    ctas = catalog.get("ctas") or {}
    if kind == "bio":
        cta = next_cta(state, "bio", ctas.get("bio") or [])
    elif kind == "reply":
        reply = next_cta(state, "direct", ctas.get("direct") or [])

    def assemble(tag_list):
        parts = [title] + ([cta] if cta else []) + ([" ".join(tag_list)] if tag_list else [])
        return "\n\n".join(parts)

    tag_list = tags.split()
    text = assemble(tag_list)
    while len(text) > TWEET_MAX and tag_list:   # trop long : on retire des hashtags
        tag_list.pop()
        text = assemble(tag_list)
    return text, reply


# ============================================================
# CRÉNEAUX (heure de Paris)
# ============================================================
def minute_key(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def parse_iso(s):
    return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)


def kind_for_due(due_iso):
    """Type de CTA prévu pour un post programmé à cette date (UTC) ; None si ce n'est pas un de nos créneaux."""
    local = parse_iso(due_iso).astimezone(POST_TZ)
    return dict(SLOT_PLAN).get(local.strftime("%H:%M"))


def iter_free_slots(now, occupied):
    """Prochains créneaux libres (UTC, type de CTA), dans l'ordre, en comblant les trous."""
    earliest = now + timedelta(minutes=MIN_LEAD_MIN)
    day = now.astimezone(POST_TZ).date()
    for _ in range(3 * 365):
        for hhmm, kind in SLOT_PLAN:
            h, m = map(int, hhmm.split(":"))
            dt = datetime(day.year, day.month, day.day, h, m, tzinfo=POST_TZ).astimezone(timezone.utc)
            if dt >= earliest and minute_key(dt) not in occupied:
                yield dt, kind
        day += timedelta(days=1)


# ============================================================
# BUFFER (GraphQL)
# ============================================================
QUEUE_Q = """
query Queue($input: PostsInput!, $after: String) {
  posts(first: 100, after: $after, input: $input) {
    edges { node { id dueAt assets { type source } } }
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
    """(vidéos déjà programmées, dernière date prévue, adresses des vidéos en file, créneaux occupés)."""
    after, count, last, urls, occupied = None, 0, None, set(), set()
    while True:
        inp = {"organizationId": BUFFER_ORG_ID,
               "filter": {"channelIds": [BUFFER_CHANNEL_ID], "status": ["scheduled"]}}
        data = gql(QUEUE_Q, {"input": inp, "after": after})["posts"]
        for e in data.get("edges") or []:
            node = e["node"]
            due = node.get("dueAt")
            vids = [a for a in node.get("assets") or [] if a.get("type") == "video"]
            # Seules les VIDÉOS occupent un créneau : une affiche MatchDay à la même minute
            # ne doit jamais décaler une vidéo (les deux peuvent partir en même temps).
            if due and vids:
                occupied.add(due[:16])
            if vids:
                count += 1
                for a in vids:
                    if a.get("source"):
                        urls.add(a["source"])
                if due and (last is None or due > last):
                    last = due
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return count, last, urls, occupied


def create_video_post(text, video_url, due_iso, reply=None):
    """Programme une vidéo à une heure précise ; `reply` = 2e tweet (réponse) avec le lien Telegram."""
    inp = {"channelId": BUFFER_CHANNEL_ID, "schedulingType": "automatic", "mode": "customScheduled",
           "dueAt": due_iso, "text": text, "assets": [{"video": {"url": video_url}}]}
    if reply:
        inp["metadata"] = {"twitter": {"thread": [
            {"text": text, "assets": [{"video": {"url": video_url}}]},
            {"text": reply},
        ]}}
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
    in_queue, last_due, queued_urls, occupied = queue_stats()
    cands = candidates(videos, state)
    # Sécurité anti-doublon : une vidéo déjà présente dans la file Buffer n'est jamais reprogrammée,
    # même si la mémoire du robot (video_state.json) n'a pas été sauvegardée.
    before = len(cands)
    cands = [v for v in cands if f"{R2_PUBLIC_BASE}/{v['key']}" not in queued_urls]
    if before != len(cands):
        log(f"🛡️  {before - len(cands)} vidéo(s) déjà dans la file Buffer : ignorées (anti-doublon)")

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
    slots = iter_free_slots(datetime.now(timezone.utc), set(occupied))
    slot = next(slots)
    while need > 0 and created < MAX_NEW_PER_RUN and pool:
        v = pick_next(pool, state["recent_players"])
        if not v:
            break
        slot_dt, kind = slot
        due_iso = slot_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        text, reply = build_post(v, kind, state, catalog)
        url = f"{R2_PUBLIC_BASE}/{v['key']}"
        if dry_run:
            log(f"🆕 [dry-run] {slot_dt.astimezone(POST_TZ):%a %d/%m %H:%M} [{kind}] {v['title']} "
                f"({v.get('player') or '—'})\n      {text.replace(chr(10), ' | ')}"
                + (f"\n      ↳ réponse : {reply.replace(chr(10), ' | ')}" if reply else ""))
            post_id, due = None, None
        else:
            try:
                post_id, due = create_video_post(text, url, due_iso, reply)
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
                continue          # le créneau n'est pas consommé : la vidéo suivante le prend
            rejects = 0
            state["posted"][v["id"]] = {"post_id": post_id, "due": due or due_iso, "at": now_iso(), "title": v["title"]}
            log(f"📤 {v['title']} → {slot_dt.astimezone(POST_TZ):%a %d/%m %H:%M} [{kind}]"
                + (" + lien en réponse" if reply else ""))
            time.sleep(API_DELAY)
        pool = [x for x in pool if x["id"] != v["id"]]
        occupied.add(minute_key(slot_dt))
        slot = next(slots)
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
