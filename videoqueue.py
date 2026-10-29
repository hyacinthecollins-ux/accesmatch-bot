#!/usr/bin/env python3
"""
VideoQueue — garde ta file Buffer pleine de vidéos, tout seul (un profil par compte X : monfootballfr, accesmatch).

Chaque jour, le robot :
  1. lit le catalogue de tes vidéos (catalog.json, hébergé sur Cloudflare R2)
  2. compte combien de vidéos sont déjà programmées dans la file Buffer du compte
  3. ajoute les vidéos manquantes pour toujours avoir ~14 jours d'avance, dans les créneaux du profil
     (heure de Paris), en comblant d'abord les trous
  4. alterne les joueurs (jamais le même joueur 2 fois de suite) et applique le plan de CTA par créneau
  5. t'alerte (le robot passe au rouge sur GitHub => email) quand il te reste moins de 14 jours de stock

Compte actif : variable VQ_ACCOUNT (défaut : monfootballfr).
Chauffe de monfootballfr (chaque phase AJOUTE des créneaux, aucun ne bouge) :
  jours 1-7  : 2 vidéos/jour (12h30, 21h45), sans CTA
  jours 8-14 : 3 vidéos/jour (+18h30), sans CTA
  jour 15+   : 4 vidéos/jour (+9h15), toujours sans CTA : posts "edit" purs (titre + hashtags)

Commandes :
  python3 videoqueue.py             # remplit la file
  python3 videoqueue.py --dry-run   # montre ce qui serait ajouté, sans rien envoyer
  python3 videoqueue.py --status    # état du stock et de la file

Variables d'environnement : BUFFER_API_KEY, R2_PUBLIC_BASE (ex: https://pub-xxxx.r2.dev), VQ_ACCOUNT (optionnel)
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
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from zoneinfo import ZoneInfo

# ============================================================
# CONFIGURATION
# ============================================================
ROOT = Path(__file__).resolve().parent

# ------------------------------------------------------------
# PROFILS DE COMPTES
# ------------------------------------------------------------
# Plan de CTA par créneau (heure de Paris) :
#   none  = vidéo seule (le contenu qui fait grossir le compte)
#   bio   = phrase d'accroche "regarde ma bio" (sans lien)
#   reply = vidéo propre + lien Telegram dans une RÉPONSE (la portée de la vidéo est préservée)
# "phases" = montée en charge : (jusqu'au jour N exclu, créneaux). None = pour toujours.
# Chaque phase AJOUTE des créneaux : aucun créneau déjà en place ne bouge.
CRUISE_PLAN = [("09:15", "none"), ("12:30", "bio"), ("18:30", "reply"), ("21:45", "bio")]   # plan de CTA d'accesmatch
EDIT_PLAN = [("09:15", "none"), ("12:30", "none"), ("18:30", "none"), ("21:45", "none")]     # posts "edit" purs, sans aucun CTA
ACCOUNTS = {
    "monfootballfr": dict(
        channel_id="6ade26c62f1f5ed648ca2250",          # canal Buffer vérifié via l'API
        state_file="video_state_monfootballfr.json",
        seed="monfootballfr-v1",
        start_date="2026-10-27",                         # 1er jour de publication (jour 1 de la chauffe)
        phases=[
            (7, [("12:30", "none"), ("21:45", "none")]),                        # jours 1-7  : 2/jour, sans CTA
            (14, [("12:30", "none"), ("18:30", "none"), ("21:45", "none")]),    # jours 8-14 : 3/jour, sans CTA
            (None, EDIT_PLAN),                                                   # jour 15+   : 4/jour, toujours sans CTA
        ]),
    # Compte suspendu le 12/10/2026 : pour le réactiver (appel accepté), reconnecter le canal dans Buffer,
    # mettre son nouvel identifiant ci-dessous et lancer le robot avec VQ_ACCOUNT=accesmatch.
    "accesmatch": dict(
        channel_id="6ac7ca726a5c39ccb6535913",
        state_file="video_state.json",
        seed="accesmatch-v1",
        start_date=None,
        phases=[(None, CRUISE_PLAN)]),
}
ACCOUNT = (os.environ.get("VQ_ACCOUNT") or "monfootballfr").strip()
if ACCOUNT not in ACCOUNTS:
    raise SystemExit(f"❌ VQ_ACCOUNT inconnu : {ACCOUNT} (choix : {', '.join(ACCOUNTS)})")
PROFILE = ACCOUNTS[ACCOUNT]
START_DATE = date.fromisoformat(PROFILE["start_date"]) if PROFILE["start_date"] else None
STATE_FILE = ROOT / PROFILE["state_file"]

BUFFER_API_KEY = os.environ.get("BUFFER_API_KEY", "")
BUFFER_URL = "https://api.buffer.com"
BUFFER_CHANNEL_ID = os.environ.get("BUFFER_CHANNEL_ID") or PROFILE["channel_id"]
BUFFER_ORG_ID = os.environ.get("BUFFER_ORG_ID", "6ab1f781b91aadc0d8a2a8ca")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "").rstrip("/")

POSTS_PER_DAY = len(PROFILE["phases"][-1][1])   # rythme de croisière (sert au calcul du stock en jours)
QUEUE_TARGET_DAYS = 14         # jours de vidéos gardés d'avance dans Buffer
MAX_NEW_PER_RUN = 70           # garde-fou par passage
LOW_STOCK_DAYS = 14            # alerte si stock (file + non programmées) < 14 jours
API_DELAY = 2.0                # pause entre deux posts (limite Buffer : 100 / 15 min)
MAX_FAILS = 2                  # une vidéo refusée 2 fois est mise de côté
MAX_CONSECUTIVE_REJECTS = 3    # 3 refus d'affilée = problème général, on arrête
AVOID_LAST_PLAYERS = 4         # pas le même joueur dans les 4 derniers posts
# Mélange des créateurs (champ "folder" du catalogue) : jamais un stock vidé avant de passer au suivant.
# Poids d'un créateur = (vidéos restantes) ** CREATOR_WEIGHT_POWER :
#   0   = tour égal (chacun son tour, les petits stocks fondent vite)
#   0.5 = compromis (le gros stock passe plus souvent sans écraser les petits)  <- défaut
#   1   = proportionnel (tous les créateurs se vident en même temps, mais 2/3 des posts viennent du plus gros)
CREATOR_WEIGHT_POWER = 0.5
CREATOR_WINDOW = 24            # le dosage se calcule sur les 24 derniers posts (~6 jours à 4/jour)
AVOID_LAST_TITLES = 30         # pas deux fois le même texte de post dans les 30 derniers (~1 semaine à 4/jour)
SHUFFLE_SEED = PROFILE["seed"]
TWEET_MAX = 280

# --- Stars : les gros noms du moment sont privilégiés (l'engagement vient plus vite) -----------------------
# Réglages dans video_priority.json (modifiable sans toucher au code) :
#   star_share = part visée de posts avec une star (0.7 = 7 posts sur 10), 0 = désactivé
#   stars      = joueurs prioritaires (nom en minuscules sans accent, comme dans le champ "player" du catalogue)
#   boost      = stars "du moment" qui passent 1,5 fois plus souvent
#   aliases    = autres écritures d'un même joueur (vini -> vinicius)
PRIORITY_FILE = ROOT / "video_priority.json"
DEFAULT_PRIORITY = {
    "star_share": 0.7,
    "stars": ["mbappe", "yamal", "dembele", "messi", "ronaldo", "haaland", "vinicius", "olise", "doue",
              "bellingham", "neymar", "cherki", "barcola", "pedri", "kvaratskhelia", "musiala", "kane", "salah",
              "wirtz", "raphinha", "camavinga", "lewandowski", "rodri", "foden", "saka"],
    "boost": ["mbappe", "yamal", "dembele", "haaland", "vinicius", "olise", "doue", "bellingham"],
    "aliases": {"vini": "vinicius", "cr7": "ronaldo", "cristiano": "ronaldo", "kylian": "mbappe", "lamine": "yamal",
                "kvara": "kvaratskhelia", "khvicha": "kvaratskhelia"},
}
STAR_WINDOW = 20               # la part de stars se calcule sur les 20 derniers posts
STAR_WEIGHT_POWER = 0.5        # une star avec plus de vidéos passe plus souvent, sans écraser les autres
BOOST_FACTOR = 1.5


def _plain(s):
    """minuscules sans accents ('Mbappé' -> 'mbappe')."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def load_priority():
    cfg = dict(DEFAULT_PRIORITY)
    if PRIORITY_FILE.exists():
        try:
            cfg.update(json.loads(PRIORITY_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            print("⚠️  video_priority.json illisible, valeurs par défaut", flush=True)
    return cfg


PRIORITY = load_priority()
ALIASES = {_plain(k): _plain(v) for k, v in PRIORITY["aliases"].items()}
STAR_ORDER = [ALIASES.get(_plain(x), _plain(x)) for x in PRIORITY["stars"]]
STAR_SET = set(STAR_ORDER)
BOOST = {ALIASES.get(_plain(x), _plain(x)) for x in PRIORITY["boost"]}
STAR_SHARE = float(os.environ.get("VQ_STAR_SHARE") or PRIORITY["star_share"])
_words = sorted(STAR_SET | set(ALIASES), key=len, reverse=True)
STAR_TEXT_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(w) for w in _words) + r")(?![a-z0-9])") if _words else None
_STAR_CACHE = {}


def player_slug(v):
    """Joueur de la vidéo, normalisé ('Mbappé' et 'mbappe' sont le même joueur, 'vini' = 'vinicius')."""
    p = _plain(v.get("player"))
    return ALIASES.get(p, p)


def star_of(v):
    """La star de la vidéo (minuscules sans accent) ou None. Joueur du catalogue d'abord ;
    sans joueur, on cherche le nom d'une star dans le titre et les hashtags."""
    key = v.get("id")
    if key in _STAR_CACHE:
        return _STAR_CACHE[key]
    p = player_slug(v)
    if p:
        res = p if p in STAR_SET else None
    elif STAR_TEXT_RE:
        m = STAR_TEXT_RE.search(_plain((v.get("title") or "") + " " + (v.get("hashtags") or "")))
        res = ALIASES.get(m.group(1), m.group(1)) if m else None
    else:
        res = None
    if key is not None:
        _STAR_CACHE[key] = res
    return res


POST_TZ = ZoneInfo("Europe/Paris")
MIN_LEAD_MIN = 15              # un créneau doit être au moins 15 min dans le futur


def log(msg):
    print(msg, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_for(day):
    """Créneaux [(heure de Paris, type de CTA)] prévus pour une date, selon la phase de chauffe du compte."""
    if START_DATE is None:
        n = 10 ** 6
    else:
        n = (day - START_DATE).days
        if n < 0:
            return []                      # avant le jour 1 : rien n'est publié
    for until, slots in PROFILE["phases"]:
        if until is None or n < until:
            return slots
    return PROFILE["phases"][-1][1]


def queue_target(today):
    """Nombre de vidéos à garder programmées : tous les créneaux des QUEUE_TARGET_DAYS prochains jours."""
    return sum(len(plan_for(today + timedelta(days=i))) for i in range(QUEUE_TARGET_DAYS))


def _gh_annotation(kind, msg):
    """Annotation GitHub (visible dans l'onglet Actions sans ouvrir les logs). Sans effet hors GitHub."""
    if os.environ.get("GITHUB_ACTIONS"):
        msg = str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::{kind}::{msg}", flush=True)


def gh_notice(msg):
    _gh_annotation("notice", msg)


def gh_error(msg):
    _gh_annotation("error", msg)


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
            st.setdefault("recent_creators", [])
            st.setdefault("recent_titles", [])
            st.setdefault("recent_stars", [])
            st.setdefault("recent_long", [])
            return st
        except json.JSONDecodeError:
            log("⚠️  video_state.json illisible, on repart de zéro")
    return {"posted": {}, "failed": {}, "counter": 0, "recent_players": [], "recent_creators": [],
            "recent_titles": [], "recent_stars": [], "recent_long": []}


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


def creator_of(v):
    """Créateur d'une vidéo = son dossier d'origine dans le catalogue (ex. 'L7-Shorts')."""
    return (v.get("folder") or "").strip() or "(sans dossier)"


def creator_stock(cands):
    """{créateur: nombre de vidéos restantes}, du plus gros au plus petit."""
    from collections import Counter
    return dict(Counter(creator_of(v) for v in cands).most_common())


def pick_next(cands, recent_players, recent_creators=(), recent_titles=(), recent_stars=(), recent_long=()):
    """Choisit la prochaine vidéo : mélange des créateurs + priorité aux stars.

    1. Créateur : celui qui est le plus "en retard" sur sa part, calculée sur les derniers posts
       (poids = stock restant ** CREATOR_WEIGHT_POWER). Jamais le même créateur deux fois d'affilée
       tant qu'un autre en a en stock. Un nouveau créateur (ou de nouvelles vidéos) entre
       automatiquement dans le tour, sans rien changer.
    2. Star ou pas : STAR_SHARE des posts (ex. 7 sur 10) montrent une star (Mbappé, Yamal, Messi...),
       le reste est du contenu varié. Parmi les stars, celle qui est passée le moins récemment
       (pondérée par son stock et son statut "du moment") prend le tour.
    3. Vidéo : dans l'ordre mélangé stable, en évitant les joueurs et les textes de post déjà vus
       dans les derniers posts.
    Si une préférence est impossible (plus de stars en stock, par ex.), elle est simplement relâchée.
    """
    from collections import Counter
    by_creator = {}
    for v in sorted(cands, key=order_key):
        by_creator.setdefault(creator_of(v), []).append(v)
    if not by_creator:
        return None
    weights = {c: len(vs) ** CREATOR_WEIGHT_POWER for c, vs in by_creator.items()}
    total = sum(weights.values())
    window = list(recent_creators)[-CREATOR_WINDOW:]
    n = len(window) + 1

    def rank(c):
        retard = weights[c] / total * n - window.count(c)
        return (-retard, hashlib.sha1(f"{c}|{len(window)}|{SHUFFLE_SEED}".encode()).hexdigest())

    ranked = sorted(by_creator, key=rank)
    last = window[-1] if window else None
    if len(ranked) > 1 and last in ranked:
        ranked = [c for c in ranked if c != last] + [last]      # le dernier créateur passe en dernier recours
    seen_titles = set(recent_titles)
    recent = {ALIASES.get(_plain(p), _plain(p)) for p in recent_players if p}

    flags = list(recent_stars)[-STAR_WINDOW:]
    if STAR_SHARE > 0:
        want_star = sum(flags) < STAR_SHARE * (len(flags) + 1)
        prefs = (want_star, not want_star)
    else:
        prefs = (None,)
    star_stock = Counter(star_of(v) for v in cands if star_of(v))
    long_counts = Counter(x for x in list(recent_long)[-STAR_WINDOW:] if x)

    def choose(pool, pref):
        if not pref:
            return pool[0]
        by_star = {}
        for v in pool:
            by_star.setdefault(star_of(v), []).append(v)

        def score(s):
            w = (star_stock[s] ** STAR_WEIGHT_POWER) * (BOOST_FACTOR if s in BOOST else 1.0)
            return (long_counts[s] / w, -w, STAR_ORDER.index(s) if s in STAR_ORDER else 99)

        return by_star[min(by_star, key=score)][0]

    for strict in (True, False):          # d'abord joueur ET texte de post jamais vus, puis seulement le joueur
        for pref in prefs:
            for c in ranked:
                pool = []
                for v in by_creator[c]:
                    p = player_slug(v)
                    if p and p in recent:
                        continue
                    if strict:
                        tk = title_key(v)
                        if tk is not None and tk in seen_titles:
                            continue
                    if pref is not None and bool(star_of(v)) != pref:
                        continue
                    pool.append(v)
                if pool:
                    return choose(pool, pref)
    return by_creator[ranked[0]][0]


def rebuild_recent(state, videos):
    """Recalcule les mémoires "derniers posts" à partir des vidéos réellement programmées (après un nettoyage de file)."""
    by_id = {v["id"]: v for v in videos}
    seq = [by_id[i] for i, _ in sorted(state["posted"].items(), key=lambda kv: kv[1].get("due", "")) if i in by_id]
    state["recent_players"] = [player_slug(v) for v in seq if v.get("player")][-AVOID_LAST_PLAYERS:]
    state["recent_creators"] = [creator_of(v) for v in seq][-CREATOR_WINDOW:]
    state["recent_titles"] = [k for k in (title_key(v) for v in seq) if k][-AVOID_LAST_TITLES:]
    state["recent_stars"] = [1 if star_of(v) else 0 for v in seq][-STAR_WINDOW:]
    state["recent_long"] = [star_of(v) or "" for v in seq][-STAR_WINDOW:]
    state["counter"] = len(state["posted"])


def clean_title(t):
    """Nettoie le titre : '⧸' (faux slash des noms YouTube) -> '/', '#' orphelin retiré, espaces propres."""
    t = (t or "").replace("\u29f8", "/").replace("\u2044", "/")
    t = re.sub(r"\s#(?=\s|$)", "", t)
    # l\u00e9gende TikTok coup\u00e9e par TikTok \u00e0 72 caract\u00e8res : un hashtag tronqu\u00e9 en fin de titre ("#foo...") est retir\u00e9
    t = re.sub(r"\s*#[^\s#]*(?:\.{3}|\u2026)\s*$", "", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def is_blank_title(t):
    """Titre sans texte exploitable : vide, emojis seuls, ou juste le num\u00e9ro de la vid\u00e9o (TikTok sans l\u00e9gende)."""
    core = re.sub(r"[\W_]+", "", t or "", flags=re.UNICODE)
    return not core or core.isdigit()


FALLBACK_TITLES = ["\U0001F525⚽", "⚽\U0001F525", "\U0001F440⚽", "\U0001F929⚽"]   # 🔥⚽ ⚽🔥 👀⚽ 🤩⚽


def post_title(v):
    """Texte d'ouverture du post : le titre nettoy\u00e9, ou \u00e0 d\u00e9faut le joueur, ou un simple emoji (jamais un num\u00e9ro)."""
    t = clean_title(v.get("title"))
    if not is_blank_title(t):
        return t
    p = (v.get("player") or "").strip()
    if p and not p.isdigit():
        return f"{p.title()} \u26bd"
    return FALLBACK_TITLES[int(hashlib.sha1(str(v.get("id")).encode()).hexdigest(), 16) % len(FALLBACK_TITLES)]


def title_key(v):
    """Cl\u00e9 de comparaison du texte d'un post (None si le titre n'a pas de texte propre)."""
    t = clean_title(v.get("title"))
    return None if is_blank_title(t) else t.lower()


# Hashtags g\u00e9n\u00e9riques TikTok, jamais utiles sur X (y compris leurs versions coup\u00e9es : "#footballtikto", "#vira")
GENERIC_TAG_RE = re.compile(r"^#(footballt(?:i\w*)?|tikt\w*|fyp\w*|foryou\w*|pourtoi\w*|viral\w*|vira|xyzbca\w*|trending)$",
                            re.I)
TAG_VOCAB = {}       # hashtag (minuscules) -> nombre de vid\u00e9os du catalogue qui l'utilisent (rempli \u00e0 chaque passage)
_TRUNCATED = {}      # cache : hashtag -> True s'il ressemble \u00e0 un hashtag coup\u00e9 par TikTok


def set_tag_vocab(videos):
    """M\u00e9morise les hashtags du catalogue pour rep\u00e9rer les hashtags coup\u00e9s ('#cham' alors que '#championsleague' existe)."""
    from collections import Counter
    TAG_VOCAB.clear()
    TAG_VOCAB.update(Counter(t.lower() for v in videos for t in set((v.get("hashtags") or "").split())
                             if t.startswith("#")))
    _TRUNCATED.clear()


def looks_truncated(low):
    if low not in _TRUNCATED:
        mine = max(TAG_VOCAB.get(low, 0), 1)
        _TRUNCATED[low] = any(u != low and u.startswith(low) and n >= 3 * mine for u, n in TAG_VOCAB.items())
    return _TRUNCATED[low]


def clean_tags(tags):
    """Hashtags propres : sans doublon, sans "#" orphelin, sans hashtag g\u00e9n\u00e9rique TikTok, sans hashtag coup\u00e9."""
    out, seen = [], set()
    for tag in (tags or "").split():
        low = tag.lower()
        if (not tag.startswith("#") or len(tag) < 2 or tag.endswith(("...", "\u2026"))
                or GENERIC_TAG_RE.match(tag) or looks_truncated(low)):
            continue
        if low not in seen:
            seen.add(low)
            out.append(tag)
    return out


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
    title = post_title(v)
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

    tag_list = clean_tags(tags)
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
    return dict(plan_for(local.date())).get(local.strftime("%H:%M"))


def iter_free_slots(now, occupied):
    """Prochains créneaux libres (UTC, type de CTA), dans l'ordre, en comblant les trous."""
    earliest = now + timedelta(minutes=MIN_LEAD_MIN)
    day = now.astimezone(POST_TZ).date()
    for _ in range(3 * 365):
        for hhmm, kind in plan_for(day):
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
    set_tag_vocab(videos)
    if state.pop("rebuild_recent", False):
        rebuild_recent(state, videos)
        log("🔁 Mémoires des derniers posts reconstruites à partir des vidéos programmées")
    if not state["recent_titles"] and state["posted"]:       # état d'avant cette règle : on la reconstruit
        last = sorted(state["posted"].values(), key=lambda p: p.get("due", ""))[-AVOID_LAST_TITLES:]
        state["recent_titles"] = [k for k in (title_key({"title": p.get("title")}) for p in last) if k]
    in_queue, last_due, queued_urls, occupied = queue_stats()
    cands = candidates(videos, state)
    # Sécurité anti-doublon : une vidéo déjà présente dans la file Buffer n'est jamais reprogrammée,
    # même si la mémoire du robot (video_state.json) n'a pas été sauvegardée.
    before = len(cands)
    cands = [v for v in cands if f"{R2_PUBLIC_BASE}/{v['key']}" not in queued_urls]
    if before != len(cands):
        log(f"🛡️  {before - len(cands)} vidéo(s) déjà dans la file Buffer : ignorées (anti-doublon)")

    target = queue_target(datetime.now(POST_TZ).date())
    need = max(0, target - in_queue)
    log(f"📦 Catalogue : {len(videos)} vidéos | déjà postées par le robot : {len(state['posted'])} | "
        f"disponibles : {len(cands)}")
    log(f"📅 File Buffer : {in_queue} vidéos programmées (jusqu'au {last_due or '—'}) | objectif {target}")
    log(f"🎯 Compte : {ACCOUNT} → canal Buffer {BUFFER_CHANNEL_ID} | 1er jour de publication : {START_DATE or '—'}")
    gh_notice(f"[{ACCOUNT}] catalogue {len(videos)} vidéos, {len(cands)} disponibles, "
              f"file Buffer {in_queue} (objectif {target})")

    if status_only:
        days = (in_queue + len(cands)) / POSTS_PER_DAY
        log(f"⏳ Stock total : ~{days:.0f} jours de vidéos")
        return 3 if days < LOW_STOCK_DAYS else 0

    created, rejects = 0, 0
    pool = list(cands)
    slots = iter_free_slots(datetime.now(timezone.utc), set(occupied))
    slot = next(slots)
    while need > 0 and created < MAX_NEW_PER_RUN and pool:
        v = pick_next(pool, state["recent_players"], state["recent_creators"], state["recent_titles"],
                      state["recent_stars"], state["recent_long"])
        if not v:
            break
        slot_dt, kind = slot
        due_iso = slot_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        text, reply = build_post(v, kind, state, catalog)
        url = f"{R2_PUBLIC_BASE}/{v['key']}"
        if dry_run:
            log(f"🆕 [dry-run] {slot_dt.astimezone(POST_TZ):%a %d/%m %H:%M} [{kind}] {v['title']} "
                f"({v.get('player') or '—'}{' ⭐' if star_of(v) else ''} · {creator_of(v)})\n      {text.replace(chr(10), ' | ')}"
                + (f"\n      ↳ réponse : {reply.replace(chr(10), ' | ')}" if reply else ""))
            post_id, due = None, None
            if created < 6:
                gh_notice(f"[simulation] {slot_dt.astimezone(POST_TZ):%a %d/%m %H:%M} [{kind}] "
                          f"{creator_of(v)} · {clean_title(v['title'])}")
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
                    gh_error(f"3 refus d'affilée de Buffer, dernière erreur : {e}")
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
        state["recent_players"] = (state["recent_players"] + [player_slug(v)] if v.get("player")
                                   else state["recent_players"])[-AVOID_LAST_PLAYERS:]
        state["recent_stars"] = (state["recent_stars"] + [1 if star_of(v) else 0])[-STAR_WINDOW:]
        state["recent_long"] = (state["recent_long"] + [star_of(v) or ""])[-STAR_WINDOW:]
        state["recent_creators"] = (state["recent_creators"] + [creator_of(v)])[-CREATOR_WINDOW:]
        tk = title_key(v)
        if tk:
            state["recent_titles"] = (state["recent_titles"] + [tk])[-AVOID_LAST_TITLES:]
        created += 1
        need -= 1
        if not dry_run:
            save_state(state)

    remaining = len(pool)
    days_left = (in_queue + created + remaining) / POSTS_PER_DAY
    log(f"✅ {created} vidéo(s) ajoutée(s). Stock restant : {remaining} non programmées "
        f"(~{days_left:.0f} jours au total)")
    gh_notice(f"[{ACCOUNT}] {'simulation : ' if dry_run else ''}{created} vidéo(s) ajoutée(s), "
              f"stock restant {remaining} (~{days_left:.0f} jours)")
    stock = creator_stock(pool)
    log("🎬 Stock par créateur : " + " | ".join(f"{c} {n}" for c, n in stock.items()))
    gh_notice("[stock par créateur] " + " | ".join(f"{c} {n}" for c, n in stock.items()))
    if not dry_run:
        save_state(state)
    if days_left < LOW_STOCK_DAYS:
        log(f"🚨 STOCK BAS : il te reste ~{days_left:.0f} jours de vidéos. Ajoute de nouvelles vidéos "
            f"(upload_videos.py sur ton Mac).")
        return 3
    return 0


def catalog_summary():
    """Contrôle du catalogue R2, en peu d'annotations GitHub (max 10 par étape). N'envoie rien à Buffer."""
    from collections import Counter
    videos = fetch_catalog()["videos"]
    set_tag_vocab(videos)
    by_creator = {}
    for v in videos:
        by_creator.setdefault(creator_of(v), []).append(v)
    ordered = sorted(by_creator.items(), key=lambda kv: -len(kv[1]))
    head = f"{len(videos)} vidéos, {len(by_creator)} créateurs : " + " | ".join(f"{c} {len(vs)}" for c, vs in ordered)
    log("📦 " + head)
    gh_notice("[catalogue] " + head)
    pc = Counter(v.get("player") for v in videos if v.get("player"))
    no_player = sum(1 for v in videos if not v.get("player"))
    gh_notice(f"[joueurs] {len(pc)} joueurs, {no_player} vidéos sans joueur | "
              + " | ".join(f"{p} {n}" for p, n in pc.most_common(30)))
    sc = Counter(star_of(v) for v in videos if star_of(v))
    via_text = [v for v in videos if star_of(v) and not v.get("player")]
    gh_notice(f"[stars] {sum(sc.values())} vidéos de stars sur {len(videos)} ({100 * sum(sc.values()) / len(videos):.0f} %), "
              f"dont {len(via_text)} repérées par le titre | part visée {STAR_SHARE:.0%} | "
              + " | ".join(f"{p} {n}" for p, n in sc.most_common())
              + "\nexemples repérés par le titre : "
              + " || ".join(f"{star_of(v)} <- {(v.get('title') or '')[:50]}" for v in via_text[:8]))

    ids = Counter(v["id"] for v in videos)
    keys = Counter(v["key"] for v in videos)
    titles = Counter(title_key(v) for v in videos if title_key(v))
    dup_titles = sum(c for c in titles.values() if c > 1)
    lines = [f"doublons : {sum(1 for c in ids.values() if c > 1)} id, {sum(1 for c in keys.values() if c > 1)} fichier(s) | "
             f"{dup_titles} vidéos partagent un titre avec une autre (normal : titres du type « Neymar ⚽ »)"]
    samples = []
    for c, vs in ordered:
        blank = sum(1 for v in vs if is_blank_title(clean_title(v.get("title"))))
        cut = sum(1 for v in vs if "..." in (v.get("title") or "") or "\u2026" in (v.get("title") or "")
                  or any(t.endswith(("...", "\u2026")) for t in (v.get("hashtags") or "").split()))
        no_tags = sum(1 for v in vs if not clean_tags(v.get("hashtags")))
        long_posts = sum(1 for v in vs if len(build_post(v, "none", {"counter": 0}, {})[0]) > TWEET_MAX)
        lines.append(f"{c} : {len(vs)} | titre sans texte (numéro) {blank} | coupé « ... » {cut} | "
                     f"sans hashtag {no_tags} | trop long {long_posts}")
        for v in [x for x in vs if "..." in (x.get("title") or "") or "…" in (x.get("title") or "")][:2]:
            samples.append(f"{c} (titre avec « ... ») → {(v.get('title') or '')[:100]} || hashtags : {v.get('hashtags')}")
        for v in vs[-2:]:                         # les 2 dernières entrées du créateur (les plus récemment ajoutées)
            samples.append(f"{c} → " + build_post(v, "none", {"counter": 0}, {})[0].replace("\n", " | "))
    for line in lines:
        log("🔎 " + line)
    gh_notice("[contrôle] " + "\n".join(lines))
    gh_notice("[exemples de posts]\n" + "\n".join(samples))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="VideoQueue")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--catalog", action="store_true", help="résume le catalogue R2 (dossiers, créateurs), n'envoie rien")
    args = ap.parse_args(argv)
    try:
        code = catalog_summary() if args.catalog else run(dry_run=args.dry_run, status_only=args.status)
    except SystemExit as e:
        if not isinstance(e.code, str):
            raise
        log(e.code)
        gh_error(e.code)
        code = 2
    except (RuntimeError, requests.RequestException) as e:
        log(f"❌ {e}")
        gh_error(e)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
