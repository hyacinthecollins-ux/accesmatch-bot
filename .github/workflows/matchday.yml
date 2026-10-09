#!/usr/bin/env python3
"""
AccesMatch MatchDay — affiches de match (logos officiels des clubs + VS) + posts Buffer automatiques.

Ce que fait le script a chaque passage (toutes les 30 min via GitHub Actions) :
  1. Interroge football-data.org : matchs des 10 clubs suivis (prochaines 36 h + resultats recents)
  2. Cree une affiche par match : logos officiels des deux clubs, VS, date, competition, heure (gratuit, sans IA)
  3. Programme sur Buffer (@accesmatch) un post avec l'affiche, 1 h avant le coup d'envoi
  4. Apres le match : poste le score final avec une affiche "score final"
  5. Anti-doublons (state.json) : un match = une affiche = un post
     Si l'horaire change, l'ancien post est supprime et recree avec la bonne heure.

Commandes :
  python3 matchday.py prepare     # matchs + affiches (aucun envoi Buffer)
  python3 matchday.py publish     # envoie les affiches pretes vers Buffer
  python3 matchday.py all         # prepare + publish (usage local)
  python3 matchday.py all --dry-run   # affiche ce qui serait fait, sans rien depenser ni publier
  python3 matchday.py --sample    # genere UNE affiche d'essai (gratuit)

Variables d'environnement (secrets GitHub) :
  FOOTBALL_DATA_TOKEN, BUFFER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  (OPENAI_API_KEY n'est plus necessaire : seulement si POSTER_MODE=ai)
"""

import argparse
import base64
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import poster

# ============================================================
# CONFIGURATION
# ============================================================
ROOT = Path(__file__).resolve().parent
POSTER_DIR = ROOT / "posters"
STATE_FILE = ROOT / "state.json"

FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
BUFFER_API_KEY = os.environ.get("BUFFER_API_KEY", "").strip()

# Canal Buffer @accesmatch (id verifie via l'API Buffer)
BUFFER_CHANNEL_ID = os.environ.get("BUFFER_CHANNEL_ID", "6ac7ca726a5c39ccb6535913")
BUFFER_URL = "https://api.buffer.com"

TELEGRAM_URL = "t.me/accesmatch"

# Telegram : meme affiche envoyee dans ton canal, avec un bouton vers ton DM (essai gratuit)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip() or "@accesmatch"
# Ton lien de message prive (essai gratuit). A MODIFIER : https://t.me/TON_PSEUDO
TELEGRAM_DM_URL = os.environ.get("TELEGRAM_DM_URL") or "https://t.me/EliteTVSupport"
TELEGRAM_BUTTON_TEXT = "🎁 Obtenir mon essai gratuit"
TELEGRAM_MIN_BEFORE_KICKOFF_MIN = 5   # on n'annonce plus un match a moins de 5 min du coup d'envoi

# Modele d'images OpenAI (celui de ChatGPT). Qualite : low / medium / high
# Mode d'affiche : "render" = logos officiels + VS (gratuit, par defaut) ; "ai" = ancienne version OpenAI
POSTER_MODE = (os.environ.get("POSTER_MODE") or "render").strip().lower()
# Change quand le design change : les affiches pas encore publiees sont alors refaites automatiquement
DESIGN_VERSION = "crests-v1" if POSTER_MODE == "render" else "ai-v1"
IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
IMAGE_SIZE = os.environ.get("OPENAI_IMAGE_SIZE", "1024x1536")
FORCE_QUALITY = os.environ.get("OPENAI_IMAGE_QUALITY", "")  # vide = auto (high pour gros matchs)

# Heure locale du public (affichage dans les posts et sur les affiches)
AUDIENCE_TZ = ZoneInfo("Europe/Paris")

# Reglages
POST_BEFORE_MIN = 60          # post programme 1 h avant le coup d'envoi
LOOKAHEAD_H = 36              # on prepare les matchs des 36 prochaines heures
MIN_LEAD_MIN = 10             # on ignore un match qui commence dans moins de 10 min
RESULT_MAX_AGE_H = 6          # score final poste seulement si le coup d'envoi date de moins de 6 h
MAX_NEW_ITEMS_PER_RUN = 6     # garde-fou cout : max 6 nouvelles affiches par passage
MAX_IMAGES_PER_DAY = 12       # garde-fou cout : max 12 affiches par jour (UTC)
KEEP_DAYS = 10                # on supprime les affiches de plus de 10 jours
RESULT_POSTS = True           # poster les scores finaux
RESULT_IMAGES = True          # avec une affiche "score final" (sinon texte seul)

# Competitions suivies (ids football-data.org, plan gratuit)
COMPETITION_IDS = "2021,2014,2002,2015,2001"  # PL, Liga, Bundesliga, Ligue 1, Ligue des champions
COMPETITIONS = {
    "CL": ("LIGUE DES CHAMPIONS", "Ligue des champions", "#UCL"),
    "PL": ("PREMIER LEAGUE", "Premier League", "#PremierLeague"),
    "PD": ("LIGA", "Liga", "#LaLiga"),
    "BL1": ("BUNDESLIGA", "Bundesliga", "#Bundesliga"),
    "FL1": ("LIGUE 1", "Ligue 1", "#Ligue1"),
}

# Joueurs phares optionnels par club (cle = "key" du club). Laisse vide = joueurs generiques.
# A mettre a jour toi-meme si tu veux des joueurs precis, ex: {"psg": ["Dembélé"], ...}
STAR_PLAYERS = {}

# ============================================================
# LES 10 CLUBS SUIVIS (ids football-data.org)
# ============================================================
TEAMS = {
    524: dict(key="psg", short="PSG", long="Paris Saint-Germain", colors="deep navy blue with red accents",
              landmark="the Eiffel Tower", stadium="Parc des Princes", tag="#PSG",
              names=["paris saint-germain fc", "paris saint-germain", "psg"]),
    516: dict(key="om", short="OM", long="Olympique de Marseille", colors="sky blue and white",
              landmark="the Notre-Dame de la Garde basilica", stadium="Stade Vélodrome", tag="#OM",
              names=["olympique de marseille", "olympique marseille", "marseille", "om"]),
    86: dict(key="real", short="REAL MADRID", long="Real Madrid", colors="white with subtle gold and purple accents",
             landmark="the Cibeles fountain", stadium="Santiago Bernabéu", tag="#RealMadrid",
             names=["real madrid cf", "real madrid"]),
    81: dict(key="barca", short="BARÇA", long="FC Barcelona", colors="deep blue and garnet red stripes",
             landmark="the Sagrada Família", stadium="Camp Nou", tag="#FCBarcelona",
             names=["fc barcelona", "barça", "barca"]),
    57: dict(key="arsenal", short="ARSENAL", long="Arsenal", colors="red and white",
             landmark="Tower Bridge", stadium="Emirates Stadium", tag="#Arsenal",
             names=["arsenal fc", "arsenal"]),
    64: dict(key="liverpool", short="LIVERPOOL", long="Liverpool", colors="vivid red",
             landmark="the Royal Liver Building", stadium="Anfield", tag="#LFC",
             names=["liverpool fc", "liverpool"]),
    65: dict(key="city", short="MAN CITY", long="Manchester City", colors="sky blue and white",
             landmark="the Manchester skyline", stadium="Etihad Stadium", tag="#MCFC",
             names=["manchester city fc", "manchester city", "man city"]),
    61: dict(key="chelsea", short="CHELSEA", long="Chelsea", colors="royal blue",
             landmark="Big Ben", stadium="Stamford Bridge", tag="#CFC",
             names=["chelsea fc", "chelsea"]),
    66: dict(key="united", short="MAN UNITED", long="Manchester United", colors="red, white and black",
             landmark="the Manchester skyline", stadium="Old Trafford", tag="#MUFC",
             names=["manchester united fc", "manchester united", "man united"]),
    5: dict(key="bayern", short="BAYERN", long="Bayern Munich", colors="red and white",
            landmark="the Frauenkirche towers of Munich", stadium="Allianz Arena", tag="#FCBayern",
            names=["fc bayern münchen", "fc bayern munchen", "fc bayern munich", "bayern munich",
                   "bayern münchen", "bayern"]),
}

# ============================================================
# TEXTES DES POSTS
# ============================================================
PREVIEW_TEMPLATES = [
    "🔥 {m} à {h} !\n\nTu veux le voir en direct en HD ? Test gratuit 👉 {url}",
    "⚽ {m} — coup d'envoi à {h}\n\nPas envie de chercher un stream qui lag ? Teste gratuitement 📺\n{url}",
    "🚨 {c} : {m} à {h}\n\nChope ton accès gratuit pour suivre le match 👇\n{url}",
    "📺 {m} à {h}\n\nRegarde-le en direct, en HD, sans galère — test gratuit 👉 {url}",
    "🔴 Gros match : {m} ({h})\n\nTon test gratuit pour le voir en direct est ici 👇\n{url}",
    "👀 {m}, c'est à {h}\n\nÉvite les streams pourris, teste gratuitement 👉 {url}",
    "⏰ {h} : {m}\n\nAccès gratuit pour regarder le match en direct ⬇️\n{url}",
    "🏟️ {c} — {m} à {h}\n\nTest gratuit, en HD 👉 {url}",
    "💥 {m} à {h} !\n\nOn te file un test gratuit pour tout voir en direct 👇\n{url}",
    "⚡ {m} ({h})\n\nLe match en direct, en HD : essaie gratuitement 👉 {url}",
    "🔥 Le choc {m} arrive à {h}\n\nTeste gratuitement pour ne rien rater 📺\n{url}",
    "🚀 {m} — {h}\n\nPrends ton test gratuit et regarde-le en direct 👇\n{url}",
]

RESULT_TEMPLATES = [
    "🏁 Terminé : {s}\n\nLe prochain match en direct ? Test gratuit 👉 {url}",
    "⏱️ Score final : {s}\n\nNe rate pas le prochain, teste gratuitement 📺\n{url}",
    "📊 {s} — c'est fini !\n\nPour les prochains matchs en direct, test gratuit 👇\n{url}",
    "🔔 Fin du match : {s}\n\nProchain choc en direct, en HD : essaie gratuitement 👉 {url}",
    "✅ {s}\n\nTu veux vivre le prochain match en direct ? Test gratuit ⬇️\n{url}",
    "⚽ Résultat : {s}\n\nAccès gratuit pour le prochain match 👇\n{url}",
]

JOURS = ["LUNDI", "MARDI", "MERCREDI", "JEUDI", "VENDREDI", "SAMEDI", "DIMANCHE"]
MOIS = ["JANVIER", "FÉVRIER", "MARS", "AVRIL", "MAI", "JUIN", "JUILLET", "AOÛT",
        "SEPTEMBRE", "OCTOBRE", "NOVEMBRE", "DÉCEMBRE"]


# ============================================================
# OUTILS
# ============================================================
def log(msg):
    print(msg, flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def norm(s):
    return " ".join((s or "").lower().split())


def fr_date(dt_utc):
    d = dt_utc.astimezone(AUDIENCE_TZ)
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


def fr_hour(dt_utc):
    d = dt_utc.astimezone(AUDIENCE_TZ)
    return f"{d.hour}h" if d.minute == 0 else f"{d.hour}h{d.minute:02d}"


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("⚠️  state.json illisible, on repart de zéro")
    return {"items": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ============================================================
# MATCHS (football-data.org)
# ============================================================
def fetch_matches(now, mock_file=None):
    if mock_file:
        return json.loads(Path(mock_file).read_text(encoding="utf-8")).get("matches", [])
    if not FOOTBALL_DATA_TOKEN:
        raise SystemExit("❌ FOOTBALL_DATA_TOKEN manquant")
    d_from = (now - timedelta(days=1)).date().isoformat()
    d_to = (now + timedelta(days=2)).date().isoformat()
    url = "https://api.football-data.org/v4/matches"
    params = {"dateFrom": d_from, "dateTo": d_to, "competitions": COMPETITION_IDS}
    for attempt in range(2):
        r = requests.get(url, params=params, headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN}, timeout=30)
        if r.status_code == 429 and attempt == 0:
            log("⏳ football-data.org: limite atteinte, pause 60 s")
            time.sleep(60)
            continue
        break
    if r.status_code != 200:
        raise SystemExit(f"❌ football-data.org {r.status_code}: {r.text[:300]}")
    return r.json().get("matches", [])


def team_entry(team):
    tid = team.get("id")
    if tid in TEAMS:
        return TEAMS[tid]
    names = {norm(team.get("name")), norm(team.get("shortName"))}
    for e in TEAMS.values():
        if names & set(e["names"]):
            return e
    return None


def crest_urls(team):
    tid = team.get("id")
    urls = [team.get("crest")]
    if tid:
        urls += [f"https://crests.football-data.org/{tid}.png", f"https://crests.football-data.org/{tid}.svg"]
    return [u for u in urls if u]


def side(team):
    e = team_entry(team)
    if e:
        return dict(tracked=True, key=e["key"], short=e["short"], long=e["long"], colors=e["colors"],
                    landmark=e["landmark"], stadium=e["stadium"], tag=e["tag"],
                    id=team.get("id"), crest_urls=crest_urls(team))
    short = (team.get("shortName") or team.get("name") or "").strip()
    return dict(tracked=False, key=None, short=short.upper(), long=team.get("name") or short,
                colors=None, landmark=None, stadium=None, tag=None,
                id=team.get("id"), crest_urls=crest_urls(team))


def parse_match(m):
    comp_code = (m.get("competition") or {}).get("code", "")
    comp = COMPETITIONS.get(comp_code)
    if comp:
        comp_poster, comp_fr, comp_tag = comp
    else:
        name = (m.get("competition") or {}).get("name", "")
        comp_poster, comp_fr, comp_tag = name.upper(), name, ""
    ft = (m.get("score") or {}).get("fullTime") or {}
    return dict(
        id=m["id"],
        kickoff=parse_iso(m["utcDate"]),
        status=m.get("status", ""),
        comp_code=comp_code, comp_poster=comp_poster, comp_fr=comp_fr, comp_tag=comp_tag,
        home=side(m["homeTeam"]), away=side(m["awayTeam"]),
        hs=ft.get("home"), as_=ft.get("away"),
    )


def tracked_matches(raw):
    out = []
    for m in raw:
        try:
            p = parse_match(m)
        except (KeyError, ValueError):
            continue
        if p["home"]["tracked"] or p["away"]["tracked"]:
            out.append(p)
    return out


# ============================================================
# PROMPTS D'AFFICHE
# ============================================================
def quality_for(m):
    if FORCE_QUALITY:
        return FORCE_QUALITY
    big = m["comp_code"] == "CL" or (m["home"]["tracked"] and m["away"]["tracked"])
    return "high" if big else "medium"


def _stars_line(m):
    parts = []
    for label, s in (("LEFT", m["home"]), ("RIGHT", m["away"])):
        names = STAR_PLAYERS.get(s["key"] or "", [])
        if names:
            parts.append(f"On the {label} side feature {', '.join(names)} (current {s['long']} player(s)).")
    return " ".join(parts) or "Use generic, realistic players (no need for specific real faces)."


def build_preview_prompt(m):
    h, a = m["home"], m["away"]
    stadium = (f"{h['stadium']} (home stadium of {h['long']})" if h["stadium"]
               else "a packed modern football stadium")
    landmarks = [x for x in (h["landmark"], a["landmark"]) if x]
    landmark_line = (f"Subtle city landmarks in the background: {' and '.join(landmarks)}. " if landmarks else "")
    return f"""Create an ORIGINAL, premium football match-day poster. Vertical portrait format, built for X/Twitter.
Photorealistic, cinematic sports-advertising look, like a promo poster from a major sports broadcaster: high contrast, volumetric smoke, subtle sparks, stadium floodlights. Not cartoon, no obvious AI look.

MATCH: {h['long']} (home team, LEFT side) versus {a['long']} (away team, RIGHT side).

COMPOSITION:
- Left half uses the visual identity of {h['long']}: {h['colors'] or 'its real club colours'}. Right half uses the real colours of {a['long']}{(': ' + a['colors']) if a['colors'] else ''}.
- 2 players per side, chest-up, intense expressions, kits in the clubs' colours, natural faces, correct anatomy, no duplicated player, no deformed face. {_stars_line(m)}
- Background: {stadium}, fans, flags, smoke. {landmark_line}
- Strong, spectacular colour separation between the two halves, with an energy burst at the centre where "VS" sits.
- Keep every piece of text inside the central 85% of the image so it survives cropping in social feeds.
- Do NOT draw club crests or logos (no fake logos). Keep the chest area of the kits clean.

TEXT — render EXACTLY these strings, spelled exactly as written, large and perfectly legible, and nothing else:
1. Top, small: "{fr_date(m['kickoff'])}"
2. Just below, small: "{m['comp_poster']}"
3. Centre, huge, bold metallic letters: "{h['short']} VS {a['short']}"
4. Below the centre: "{fr_hour(m['kickoff']).upper()}"
Do not add any other text, watermark, URL, slogan or caption. Never invent letters. If unsure about a word, leave it out rather than misspell it.
The poster must be publishable immediately and be totally original."""


def build_result_prompt(m):
    h, a = m["home"], m["away"]
    if m["hs"] == m["as_"]:
        mood = "Both sides shown intense and evenly matched after a draw."
    elif m["hs"] > m["as_"]:
        mood = f"The winning side ({h['long']}, LEFT) celebrates; the other side looks dejected."
    else:
        mood = f"The winning side ({a['long']}, RIGHT) celebrates; the other side looks dejected."
    stadium = h["stadium"] or "a packed modern football stadium"
    return f"""Create an ORIGINAL, premium football full-time result graphic. Vertical portrait format, built for X/Twitter.
Photorealistic, cinematic sports-advertising look, high contrast, volumetric smoke, subtle sparks, stadium floodlights. Not cartoon, no obvious AI look.

MATCH: {h['long']} (LEFT side, colours: {h['colors'] or 'real club colours'}) versus {a['long']} (RIGHT side, colours: {a['colors'] or 'real club colours'}). {mood}
- 2 players per side, chest-up, natural faces, correct anatomy, no duplicates, kits in the clubs' colours. {_stars_line(m)}
- Background: {stadium}, fans, flags, smoke. Strong colour separation between the two halves.
- Keep all text inside the central 85% of the image. Do NOT draw club crests or logos.

TEXT — render EXACTLY these strings, spelled exactly, large and perfectly legible, and nothing else:
1. Top, small: "SCORE FINAL"
2. Just below, small: "{m['comp_poster']}"
3. Centre, huge, bold metallic letters: "{h['short']} {m['hs']} - {m['as_']} {a['short']}"
Do not add any other text, watermark, URL or caption. Never invent letters."""


# ============================================================
# TEXTE DU POST
# ============================================================
def hashtags(m):
    tags = [s["tag"] for s in (m["home"], m["away"]) if s["tag"]]
    if m["comp_tag"]:
        tags.append(m["comp_tag"])
    return " ".join(tags[:3])


def build_post_text(m, kind):
    rng = random.Random(f"{m['id']}-{kind}")  # stable : meme match = meme texte
    tags = hashtags(m)
    if kind == "preview":
        title = f"{m['home']['short']} × {m['away']['short']}"
        t = rng.choice(PREVIEW_TEMPLATES).format(m=title, h=fr_hour(m["kickoff"]), c=m["comp_fr"], url=TELEGRAM_URL)
    else:
        score = f"{m['home']['short']} {m['hs']}-{m['as_']} {m['away']['short']}"
        t = rng.choice(RESULT_TEMPLATES).format(s=score, url=TELEGRAM_URL)
    return f"{t}\n\n{tags}".strip()


def alt_text(m, kind):
    if kind == "preview":
        return f"Affiche du match {m['home']['short']} contre {m['away']['short']} - {m['comp_fr']}"
    return f"Score final {m['home']['short']} {m['hs']}-{m['as_']} {m['away']['short']}"


# ============================================================
# OPENAI — GENERATION D'AFFICHE
# ============================================================
def generate_image(prompt, quality, out_path):
    if not OPENAI_API_KEY:
        raise SystemExit("❌ OPENAI_API_KEY manquant")
    body = {"model": IMAGE_MODEL, "prompt": prompt, "size": IMAGE_SIZE, "quality": quality, "n": 1,
            "output_format": "jpeg", "output_compression": 88}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    r = None
    for attempt in range(3):
        r = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=body, timeout=300)
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(20 * (attempt + 1))
            continue
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:400]}")
    data = r.json()["data"][0]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(data["b64_json"]))


def make_poster(m, kind, out_path):
    """Cree l'affiche (logos officiels + VS) ou, si POSTER_MODE=ai, la version OpenAI."""
    if POSTER_MODE == "ai":
        prompt = build_preview_prompt(m) if kind == "preview" else build_result_prompt(m)
        generate_image(prompt, quality_for(m), out_path)
        return
    h, a = m["home"], m["away"]
    poster.render_poster(
        out_path, kind,
        {"id": h.get("id"), "short": h["short"], "crest_urls": h.get("crest_urls")},
        {"id": a.get("id"), "short": a["short"], "crest_urls": a.get("crest_urls")},
        fr_date(m["kickoff"]), m["comp_poster"],
        time_text=fr_hour(m["kickoff"]).upper(),
        score=(m["hs"], m["as_"]) if kind == "result" else None)


# ============================================================
# BUFFER — API GRAPHQL
# ============================================================
CREATE_POST_Q = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id dueAt status } }
    ... on MutationError { message }
  }
}"""

DELETE_POST_Q = """
mutation DeletePost($input: DeletePostInput!) {
  deletePost(input: $input) {
    __typename
    ... on DeletePostSuccess { id }
    ... on MutationError { message }
  }
}"""


def buffer_gql(query, variables):
    if not BUFFER_API_KEY:
        raise SystemExit("❌ BUFFER_API_KEY manquant")
    headers = {"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"}
    r = None
    for attempt in range(2):
        r = requests.post(BUFFER_URL, headers=headers, json={"query": query, "variables": variables}, timeout=60)
        if r.status_code == 429 and attempt == 0:
            wait = min(int(r.headers.get("Retry-After", "30") or 30), 120)
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


def buffer_create_post(text, image_url, alt, due_iso):
    inp = {
        "channelId": BUFFER_CHANNEL_ID,
        "schedulingType": "automatic",
        "mode": "customScheduled" if due_iso else "shareNow",
        "text": text,
        "assets": [{"image": {"url": image_url, "metadata": {"altText": alt}}}] if image_url else [],
    }
    if due_iso:
        inp["dueAt"] = due_iso
    res = buffer_gql(CREATE_POST_Q, {"input": inp})["createPost"]
    if res["__typename"] != "PostActionSuccess":
        raise RuntimeError(f"Buffer a refusé le post: {res.get('__typename')} - {res.get('message')}")
    return res["post"]["id"]


def buffer_delete_post(post_id):
    res = buffer_gql(DELETE_POST_Q, {"input": {"id": post_id}})["deletePost"]
    return res["__typename"] == "DeletePostSuccess"


def image_url_for(rel_path):
    base = os.environ.get("IMAGE_BASE_URL", "")
    if not base:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        if not repo:
            raise SystemExit("❌ IMAGE_BASE_URL manquant (hors GitHub Actions)")
        base = f"https://raw.githubusercontent.com/{repo}/{branch}"
    return f"{base.rstrip('/')}/{rel_path}"


def wait_for_url(url, tries=10, delay=6):
    for _ in range(tries):
        try:
            r = requests.get(url, stream=True, timeout=20)
            ok = r.status_code == 200
            r.close()
            if ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(delay)
    return False


# ============================================================
# TELEGRAM — meme affiche dans le canal, message plus direct
# ============================================================
TG_PREVIEW_TEMPLATES = [
    "⚽ <b>{m}</b>\n{c} · coup d'envoi à <b>{h}</b> (heure de Paris)\n\nPlus de lien mort ni de stream qui coupe 🔥\n🎁 Ton <b>essai gratuit</b> : écris-moi en privé, je te l'envoie tout de suite 👇",
    "🔥 <b>{m}</b> — ça démarre à <b>{h}</b> !\n{c}\n\nTu veux le regarder en HD, sans pub ? 📺\n🎁 Essai gratuit : clique sur le bouton et envoie-moi un message 👇",
    "🚨 Match dans 1h : <b>{m}</b>\n{c} · <b>{h}</b> (heure de Paris)\n\nActive ton <b>essai gratuit</b> en 2 minutes, avant le coup d'envoi 👇",
]
TG_RESULT_TEMPLATES = [
    "🏁 <b>SCORE FINAL</b> : {s}\n\nTu l'as raté ? La prochaine fois, tu le regardes en direct 🔥\n🎁 Essai gratuit : écris-moi en privé 👇",
    "📣 Terminé : <b>{s}</b>\n\nÀ la prochaine, plus aucun match à rater 📺\n🎁 Ton essai gratuit se demande ici 👇",
]


def build_telegram_text(m, kind):
    rng = random.Random(f"tg-{m['id']}-{kind}")
    if kind == "preview":
        title = f"{m['home']['short']} × {m['away']['short']}"
        return rng.choice(TG_PREVIEW_TEMPLATES).format(m=title, h=fr_hour(m["kickoff"]), c=m["comp_fr"])
    score = f"{m['home']['short']} {m['hs']}-{m['as_']} {m['away']['short']}"
    return rng.choice(TG_RESULT_TEMPLATES).format(s=score)


def telegram_ready():
    return bool(TELEGRAM_BOT_TOKEN) and "TON_PSEUDO" not in TELEGRAM_DM_URL


def telegram_send(text, photo_path=None):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    markup = json.dumps({"inline_keyboard": [[{"text": TELEGRAM_BUTTON_TEXT, "url": TELEGRAM_DM_URL}]]})
    data = {"chat_id": TELEGRAM_CHAT_ID, "parse_mode": "HTML", "reply_markup": markup}
    if photo_path:
        data["caption"] = text[:1000]
        with open(photo_path, "rb") as fh:
            r = requests.post(f"{api}/sendPhoto", data=data, files={"photo": fh}, timeout=60)
    else:
        data["text"] = text
        r = requests.post(f"{api}/sendMessage", data=data, timeout=30)
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:250]}")


def telegram_step(raw_matches, state, now, dry_run=False):
    """Envoie dans le canal Telegram : avant-match au meme moment que le post Buffer (1 h avant), score final tout de suite."""
    if not dry_run and not telegram_ready():
        if TELEGRAM_BOT_TOKEN:
            log("ℹ️ Telegram : TELEGRAM_DM_URL pas encore renseigné dans matchday.py, étape ignorée")
        return 0
    by_id = {p["id"]: p for p in tracked_matches(raw_matches)}
    sent = 0
    for key, it in list(state["items"].items()):
        if it.get("tg") in ("sent", "skip", "failed") or it["status"] in ("cancelled", "failed"):
            continue
        kickoff = parse_iso(it["kickoff"])
        if it["kind"] == "preview":
            if now < kickoff - timedelta(minutes=POST_BEFORE_MIN):
                continue                     # pas encore l'heure
            if now > kickoff - timedelta(minutes=TELEGRAM_MIN_BEFORE_KICKOFF_MIN):
                it["tg"] = "skip"            # trop tard, le match va commencer
                continue
        m = by_id.get(it["match_id"])
        if not m:
            continue
        text = build_telegram_text(m, it["kind"])
        photo = ROOT / it["poster"] if it.get("poster") else None
        if photo is not None and not photo.exists():
            photo = None
        if dry_run:
            log(f"✈️ [dry-run] {it['label']} → Telegram\n      " + text.replace("\n", "\n      "))
            continue
        try:
            telegram_send(text, photo)
            it["tg"] = "sent"
            sent += 1
            log(f"✈️ {it['label']} → Telegram OK")
        except (RuntimeError, requests.RequestException) as e:
            it["tg_fails"] = it.get("tg_fails", 0) + 1
            if it["tg_fails"] >= 3:
                it["tg"] = "failed"
            log(f"❌ Telegram {it['label']} : {e}")
        save_state(state)
        time.sleep(1)
    return sent


# ============================================================
# LOGIQUE : PREPARE
# ============================================================
def due_for(item_kind, kickoff, now):
    """Retourne l'heure de publication (iso) ou None pour 'maintenant'. 'SKIP' si trop tard."""
    if item_kind == "result":
        return None
    due = kickoff - timedelta(minutes=POST_BEFORE_MIN)
    if due >= now + timedelta(minutes=3):
        return iso(due)
    if kickoff > now + timedelta(minutes=8):
        return iso(now + timedelta(minutes=3))
    return "SKIP"


def prepare(raw_matches, state, now, dry_run=False):
    items = state["items"]
    matches = tracked_matches(raw_matches)
    created = 0
    today = now.date().isoformat()
    images_today = sum(1 for it in items.values() if it.get("created", "")[:10] == today)

    # 0) Nouveau design : les affiches pas encore publiées (ancien design) sont retirées puis refaites
    for key, it in list(items.items()):
        if it.get("design") == DESIGN_VERSION or it["status"] not in ("poster_ready", "scheduled"):
            continue
        if it.get("tg") in ("sent",) or parse_iso(it.get("due") or it["kickoff"]) <= now:
            continue  # déjà parti (X ou Telegram) : on ne touche pas
        log(f"♻️  {it['label']} : nouveau design → l'ancienne affiche est refaite")
        if dry_run:
            continue
        if it["status"] == "scheduled" and it.get("post_id"):
            try:
                buffer_delete_post(it["post_id"])
            except RuntimeError as e:
                log(f"   ⚠️ suppression Buffer impossible, on réessaie au prochain passage : {e}")
                continue
        old = ROOT / it["poster"] if it.get("poster") else None
        if old and old.exists():
            old.unlink()
        del items[key]
    if not dry_run:
        save_state(state)

    # 1) Changements : horaire modifié / match reporté ou annulé
    for m in matches:
        key = f"p{m['id']}"
        it = items.get(key)
        if not it or it["status"] in ("cancelled", "missed"):
            continue
        moved = abs((parse_iso(it["kickoff"]) - m["kickoff"]).total_seconds()) > 300
        off = m["status"] in ("POSTPONED", "CANCELLED", "SUSPENDED")
        if (moved or off) and it["status"] in ("poster_ready", "scheduled"):
            why = "reporté/annulé" if off else "horaire modifié"
            log(f"🔄 {it['label']} : {why} → ancien post retiré")
            if it["status"] == "scheduled" and it.get("post_id") and not dry_run:
                try:
                    buffer_delete_post(it["post_id"])
                except RuntimeError as e:
                    log(f"   ⚠️ suppression Buffer impossible : {e}")
            if off:
                it["status"] = "cancelled"
            elif not dry_run:
                del items[key]

    # 2) Nouvelles affiches
    for m in sorted(matches, key=lambda x: x["kickoff"]):
        label = f"{m['home']['short']}-{m['away']['short']}"
        candidates = []
        # avant-match
        if (m["status"] in ("TIMED", "SCHEDULED")
                and now + timedelta(minutes=MIN_LEAD_MIN) < m["kickoff"] <= now + timedelta(hours=LOOKAHEAD_H)
                and f"p{m['id']}" not in items):
            candidates.append("preview")
        # score final
        if (RESULT_POSTS and m["status"] == "FINISHED" and m["hs"] is not None and m["as_"] is not None
                and now - timedelta(hours=RESULT_MAX_AGE_H) < m["kickoff"] <= now
                and f"r{m['id']}" not in items):
            candidates.append("result")

        for kind in candidates:
            if created >= MAX_NEW_ITEMS_PER_RUN or images_today >= MAX_IMAGES_PER_DAY:
                log(f"⛔ Garde-fou coût atteint, {label} ({kind}) repoussé au prochain passage")
                continue
            key = ("p" if kind == "preview" else "r") + str(m["id"])
            with_image = kind == "preview" or RESULT_IMAGES
            rel = f"posters/{key}-{m['kickoff'].strftime('%Y%m%d')}.jpg" if with_image else None
            log(f"🆕 {label} [{m['comp_fr']}] {kind} — coup d'envoi {fr_date(m['kickoff'])} {fr_hour(m['kickoff'])}")
            if dry_run:
                log("   texte :\n      " + build_post_text(m, kind).replace("\n", "\n      "))
                due = due_for(kind, m["kickoff"], now)
                log(f"   publication : {due or 'immédiate'}  | affiche : {POSTER_MODE}")
                created += 1
                continue
            if with_image:
                try:
                    make_poster(m, kind, ROOT / rel)
                except Exception as e:  # noqa: BLE001 - une affiche ratée ne doit pas bloquer les autres
                    log(f"   ❌ création de l'affiche échouée : {e}")
                    continue
            items[key] = dict(kind=kind, match_id=m["id"], label=label, kickoff=iso(m["kickoff"]),
                              status="poster_ready", poster=rel, post_id=None, due=None,
                              created=iso(now), fails=0, design=DESIGN_VERSION)
            save_state(state)
            created += 1
            images_today += 1
            log(f"   ✅ affiche prête : {rel}")
    return created


# ============================================================
# LOGIQUE : PUBLISH
# ============================================================
def publish(raw_matches, state, now, dry_run=False):
    items = state["items"]
    by_id = {p["id"]: p for p in tracked_matches(raw_matches)}
    sent = 0
    for key, it in list(items.items()):
        if it["status"] != "poster_ready":
            continue
        kickoff = parse_iso(it["kickoff"])
        due = due_for(it["kind"], kickoff, now)
        if due == "SKIP":
            log(f"⌛ {it['label']} : trop tard pour l'avant-match, ignoré")
            it["status"] = "missed"
            continue
        m = by_id.get(it["match_id"])
        if not m:
            # match plus dans la fenêtre API : on reconstruit un minimum depuis l'état
            log(f"⚠️ {it['label']} : introuvable dans l'API ce passage, on réessaie plus tard")
            continue
        text = build_post_text(m, it["kind"])
        if dry_run:
            log(f"📤 [dry-run] {it['label']} → Buffer, {due or 'immédiat'}")
            continue
        try:
            url = None
            if it.get("poster"):
                url = image_url_for(it["poster"])
                if not wait_for_url(url):
                    raise RuntimeError(f"image pas encore accessible : {url}")
            post_id = buffer_create_post(text, url, alt_text(m, it["kind"]), due if due else None)
            it.update(status="scheduled", post_id=post_id, due=due)
            save_state(state)
            sent += 1
            log(f"📤 {it['label']} → Buffer OK ({'programmé ' + due if due else 'publié maintenant'})")
            time.sleep(1.5)
        except RuntimeError as e:
            it["fails"] = it.get("fails", 0) + 1
            if it["fails"] >= 3:
                it["status"] = "failed"
            save_state(state)
            log(f"❌ {it['label']} : {e}")
    return sent


# ============================================================
# NETTOYAGE
# ============================================================
def cleanup(state, now, dry_run=False):
    if dry_run:
        return
    cutoff = now - timedelta(days=KEEP_DAYS)
    for key, it in list(state["items"].items()):
        if parse_iso(it["kickoff"]) < cutoff:
            if it.get("poster"):
                p = ROOT / it["poster"]
                if p.exists():
                    p.unlink()
            if parse_iso(it["kickoff"]) < now - timedelta(days=30):
                del state["items"][key]
    save_state(state)


# ============================================================
# AFFICHE D'ESSAI
# ============================================================
def sample(raw_matches, now):
    ms = [m for m in tracked_matches(raw_matches) if m["kickoff"] > now and m["status"] in ("TIMED", "SCHEDULED")]
    if not ms:
        log("Aucun match à venir trouvé, affiche d'essai PSG - OM.")
        ms = [dict(id=0, kickoff=now + timedelta(hours=5), status="TIMED", comp_code="FL1",
                   comp_poster="LIGUE 1", comp_fr="Ligue 1", comp_tag="#Ligue1",
                   home=side({"id": 524, "name": "Paris Saint-Germain FC"}),
                   away=side({"id": 516, "name": "Olympique de Marseille"}), hs=None, as_=None)]
    m = sorted(ms, key=lambda x: x["kickoff"])[0]
    out = ROOT / "sample-poster.jpg"
    log(f"🎨 Affiche d'essai : {m['home']['short']} VS {m['away']['short']} (mode {POSTER_MODE})…")
    make_poster(m, "preview", out)
    log(f"✅ Enregistrée : {out}")
    log("Texte du post qui irait avec :\n\n" + build_post_text(m, "preview"))


# ============================================================
# MAIN
# ============================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="AccesMatch MatchDay")
    ap.add_argument("mode", nargs="?", choices=["prepare", "publish", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien (ni Buffer ni Telegram), affiche le plan")
    ap.add_argument("--sample", action="store_true", help="génère une seule affiche d'essai")
    ap.add_argument("--mock-file", help="(tests) fichier JSON de matchs à la place de l'API")
    ap.add_argument("--now", help="(tests) date/heure UTC simulée, ex 2026-10-10T07:00:00Z")
    args = ap.parse_args(argv)

    now = parse_iso(args.now) if args.now else now_utc()
    raw = fetch_matches(now, args.mock_file)
    log(f"📅 {iso(now)} — {len(raw)} matchs récupérés, {len(tracked_matches(raw))} concernent tes clubs")

    if args.sample:
        sample(raw, now)
        return

    state = load_state()
    if args.mode in ("prepare", "all"):
        n = prepare(raw, state, now, args.dry_run)
        log(f"➡️  {n} nouvelle(s) affiche(s)")
    if args.mode in ("publish", "all"):
        n = publish(raw, state, now, args.dry_run)
        log(f"➡️  {n} post(s) envoyé(s) à Buffer")
        n = telegram_step(raw, state, now, args.dry_run)
        log(f"➡️  {n} message(s) envoyé(s) sur Telegram")
    if args.mode == "all" or args.mode == "publish":
        cleanup(state, now, args.dry_run)
    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
