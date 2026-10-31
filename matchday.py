#!/usr/bin/env python3
"""
AccesMatch MatchDay — affiches de match (logos officiels des clubs + VS) + posts Buffer automatiques.

Ce que fait le script a chaque passage (toutes les 30 min via GitHub Actions) :
  1. Interroge football-data.org : matchs des 10 clubs suivis (prochaines 49 h + resultats recents)
  2. Cree une affiche par match : logos officiels des deux clubs, VS, date, competition, heure (gratuit, sans IA)
  3. Telegram : envoie l'affiche 48 h avant le coup d'envoi (message : "teste avant le match pour obtenir ton acces")
     X : la MEME affiche (pied de page @monfootballfr, sans CTA) est postee 2 fois via Buffer sur monfootballfr :
         24 h avant le coup d'envoi puis 1 h avant, avec un texte de chauffe (noms de joueurs, vrais chiffres,
         question) et 3-4 hashtags. Ecrit par l'IA du community manager (cm.py), avec un modele de secours.
         Jamais sur un creneau video, 4 matchs max par jour.
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
import re
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

# Canal Buffer monfootballfr (id verifie via l'API Buffer). L'ancien canal @accesmatch (suspendu) n'existe plus.
BUFFER_CHANNEL_ID = os.environ.get("BUFFER_CHANNEL_ID", "6ade26c62f1f5ed648ca2250")
BUFFER_URL = "https://api.buffer.com"

TELEGRAM_URL = "t.me/accesmatch"

# Telegram : meme affiche envoyee dans ton canal, avec un bouton vers ton DM (prise d'accès)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip() or "@accesmatch"
# Ton lien de message prive (prise d'accès). A MODIFIER : https://t.me/TON_PSEUDO
TELEGRAM_DM_URL = os.environ.get("TELEGRAM_DM_URL") or "https://t.me/EliteTVSupport"
TELEGRAM_BUTTON_TEXT = "🧪 Tester avant le match"
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
POST_BEFORE_MIN = 48 * 60     # affiche annoncee 48 h avant le coup d'envoi (X + Telegram)
LOOKAHEAD_H = 49              # on prepare les matchs des 49 prochaines heures (48 h + 1 passage de marge)
MIN_LEAD_MIN = 10             # on ignore un match qui commence dans moins de 10 min
RESULT_MAX_AGE_H = 6          # score final poste seulement si le coup d'envoi date de moins de 6 h
MAX_NEW_ITEMS_PER_RUN = 30    # garde-fou : max 30 nouvelles affiches par passage (le rendu logos + VS est gratuit)
MAX_IMAGES_PER_DAY = 80       # garde-fou : max 80 affiches par jour (UTC) ; le rendu logos + VS est gratuit
KEEP_DAYS = 10                # on supprime les affiches de plus de 10 jours
POST_TO_X = True              # affiches J-24h + H-1 sur X (monfootballfr), sans CTA : 4 matchs max par jour
X_CLUBS = {524, 516, 81, 86, 57, 64, 65, 61, 66, 5}   # les 10 clubs de Telegram : PSG, OM, Barça, Real, Arsenal, Liverpool, City, Chelsea, United, Bayern
X_KINDS = ("xpreview", "x24", "x1")   # xpreview = ancien post unique 3 h avant (n'existe plus que dans l'historique)
X_J1_MIN = 24 * 60            # 1er post X : 24 h avant le coup d'envoi
X_H1_MIN = 60                 # 2e post X : 1 h avant le coup d'envoi (à ± 24 min si un créneau vidéo ou un autre post gêne)
X_J1_LOOKAHEAD_H = 49         # le post J-24h est programmé dans Buffer dès qu'il reste moins de 49 h (marge si GitHub est lent)
X_H1_LOOKAHEAD_H = 27         # le post H-1 est créé 27 h avant : ses chiffres restent frais
X_LATE_J1_MIN_H = 18          # passage tardif : un post J-24h en retard (match déplacé, GitHub lent) n'est publié que s'il reste 18 h au moins
X_MAX_PER_DAY = 4             # plafond de matchs avec affiches X par jour de match (2 posts chacun), les gros chocs d'abord
X_MIN_GAP_MIN = 8             # écart minimum entre deux affiches X
X_FOOTER = "@monfootballfr"   # pied de page des affiches X (celles de Telegram gardent @accesmatch)
X_VIDEO_SLOTS = ["09:15", "12:30", "18:30", "21:45"]   # créneaux vidéo (heure de Paris) : une affiche ne tombe jamais dessus
X_SLOT_MARGIN_MIN = 20        # écart minimum entre une affiche X et un créneau vidéo
X_QUIET_HOURS = ("00:30", "07:30")   # pas de post J-24h la nuit (heure de Paris)
X_TEXT_MAX = 225              # longueur max du texte (hors hashtags) pour tenir dans 280 caractères
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


def fr_day(dt_utc):
    """Date pour les textes de post : 'Mercredi 21 octobre' (fr_date reste en majuscules pour les affiches)."""
    return fr_date(dt_utc).capitalize()


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
                colors=None, landmark=None, stadium=None, tag=None, raw_short=short,
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
# TEXTE DU POST X (chauffe 24 h avant et 1 h avant le match)
# ============================================================
def gh_notice(msg):
    """Message lisible dans la page du run GitHub (les journaux complets ne sont pas téléchargeables)."""
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice::" + str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A"), flush=True)


_CM = None


def get_cm():
    """Le module du community manager (rédaction + relecture IA, liste des gros joueurs, hashtags) ou None."""
    global _CM
    if _CM is None:
        try:
            import cm
            _CM = cm
        except Exception as e:  # noqa: BLE001 - sans lui on retombe sur les textes de secours
            log(f"⚠️ module cm indisponible ({e}) : textes de secours")
            _CM = False
    return _CM or None


def disp(side_):
    """Nom du club tel qu'on l'écrit dans un post : PSG, OM, Real Madrid, Barça, Man City, Toulouse..."""
    if side_.get("raw_short"):
        return side_["raw_short"]
    return side_["short"] if side_["short"] in ("PSG", "OM") else side_["short"].title()


def when_word(kickoff, due):
    """Quand a lieu le match, vu depuis l'heure de publication : 'demain', 'ce soir', 'samedi 31'."""
    k, d = kickoff.astimezone(AUDIENCE_TZ), due.astimezone(AUDIENCE_TZ)
    diff = (k.date() - d.date()).days
    if diff == 0:
        return "ce soir" if k.hour >= 18 else ("cet après-midi" if k.hour >= 12 else "ce matin")
    if diff == 1:
        return "demain"
    return f"{JOURS[k.weekday()].lower()} {k.day}"


def delta_word(kickoff, due):
    mins = round((kickoff - due).total_seconds() / 60)
    if 50 <= mins <= 70:
        return "dans 1 heure"
    if mins < 60:
        return f"dans {max(mins, 1)} minutes"
    return f"dans {mins // 60}h{mins % 60:02d}"


def auto_tag(side_):
    """Hashtag d'un club qu'on ne suit pas (Brighton, Toulouse...) à partir de son nom court."""
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9]+", side_.get("raw_short") or side_["short"].title())
             if w.lower() not in {"fc", "cf", "afc", "sc", "ac", "as", "rc", "cd", "ud", "sv", "fk", "us"}]
    tag = "".join(w[:1].upper() + w[1:] for w in words)
    if len(tag) > 16 and words:
        tag = words[0][:1].upper() + words[0][1:]
    return "#" + tag if tag else ""


def match_tags(m):
    """Les 3 hashtags de base d'un match : club, club, compétition (au moins 2 garantis)."""
    tags = []
    for s_ in (m["home"], m["away"]):
        t = s_["tag"] or auto_tag(s_)
        if t and t not in tags:
            tags.append(t)
    if m["comp_tag"] and m["comp_tag"] not in tags:
        tags.append(m["comp_tag"])
    return tags


def player_tag(post, names):
    """Hashtag du premier joueur cité dans le post (#Mbappé pour les stars connues, sinon #Gouiri, #JoãoPedro...)."""
    cm = get_cm()
    if cm:
        text = " " + re.sub(r"[^a-z0-9]+", " ", cm.norm(post)) + " "
        for words, tag in cm.TAGS_BY_KEYWORD:
            if cm.norm(words[0]) in cm.STAR_CANON and any(f" {w} " in text for w in words):
                return tag
    best = None
    for n in names:
        for cand in (n, n.split()[-1]):
            pos = post.find(cand)
            if pos >= 0 and len(cand) >= 4 and (best is None or pos < best[0]):
                best = (pos, cand)
    return "#" + re.sub(r"[^A-Za-zÀ-ÿ0-9]", "", best[1]) if best else None


def x_tags_for(m, names=()):
    """Fonction(post) -> hashtags : les 3 de base (club, club, compétition) + le joueur cité, 4 au maximum."""
    base = match_tags(m)

    def f(post):
        tags = list(base)
        pt = player_tag(post, names)
        if pt and pt not in tags:
            tags.append(pt)
        tags = tags[:4]
        while len(tags) > 2 and len(post) + 2 + len(" ".join(tags)) > 276:     # 280 caractères max sur X
            tags.pop()
        return tags
    return f


def hashtags(m):
    return " ".join(match_tags(m))


# ---------- joueurs à mettre en avant (données réelles : meilleurs buteurs + gros noms de l'effectif) ----------
_SCORERS = {}


def fd_get(path, params=None):
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_TOKEN manquant")
    r = None
    for attempt in range(2):
        r = requests.get(f"https://api.football-data.org/v4{path}", params=params,
                         headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN}, timeout=30)
        if r.status_code == 429 and attempt == 0:
            log("⏳ football-data.org: limite atteinte, pause 60 s")
            time.sleep(60)
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"football-data {path} : HTTP {r.status_code} {r.text[:120]}")
    return r.json()


def comp_scorers(code):
    """Meilleurs buteurs d'une compétition (mis en cache pour le passage)."""
    if code not in _SCORERS:
        try:
            rows = fd_get(f"/competitions/{code}/scorers", {"limit": 60}).get("scorers") or []
        except RuntimeError as e:
            log(f"   buteurs {code} indisponibles ({e})")
            rows = []
        _SCORERS[code] = [r for r in rows if r.get("goals") is not None and (r.get("player") or {}).get("name")]
    return _SCORERS[code]


def star_names(team_id, state, now):
    """Gros noms de l'effectif (liste des stars du robot vidéo), gardés 10 jours dans state.json."""
    cm = get_cm()
    if not cm or not team_id:
        return []
    cache = state.setdefault("squads", {})
    c = cache.get(str(team_id))
    if c and parse_iso(c["t"]) > now - timedelta(days=10):
        return c["stars"]
    try:
        squad = fd_get(f"/teams/{team_id}").get("squad") or []
    except RuntimeError as e:
        log(f"   effectif {team_id} indisponible ({e})")
        return c["stars"] if c else []
    stars = [p["name"] for p in squad if p.get("name") and cm.stars_in(p["name"])]
    cache[str(team_id)] = {"t": iso(now), "stars": stars}
    return stars


def short_name(full):
    parts = full.split()
    return parts[-1] if len(parts) == 2 else full


def players_facts(m, state, now):
    """(faits, noms) : pour chaque équipe, ses meilleurs buteurs de la compétition (chiffres exacts) puis ses gros noms."""
    rows = comp_scorers(m["comp_code"]) if m["comp_code"] else []
    facts, names = [], []
    for s_ in (m["home"], m["away"]):
        picks = []
        for r in rows:
            if (r.get("team") or {}).get("id") == s_["id"] and len(picks) < 2:
                g = r["goals"]
                txt = f"{r['player']['name']} ({g} but{'s' if g > 1 else ''} en {m['comp_fr']} cette saison"
                if r.get("assists"):
                    txt += f", {r['assists']} passe{'s' if r['assists'] > 1 else ''} décisive{'s' if r['assists'] > 1 else ''}"
                picks.append((r["player"]["name"], txt + ")"))
        have = {n for n, _ in picks}
        for n in star_names(s_["id"], state, now):
            if n not in have and len(picks) < 3:
                picks.append((n, n))
        if picks:
            facts.append(f"Joueurs à suivre, {disp(s_)} : " + ", ".join(t for _, t in picks))
            names += [n for n, _ in picks]
    return facts, names


def x_facts(m, kind, due, state, now):
    h, a = m["home"], m["away"]
    k = m["kickoff"]
    facts = [f"Match : {h['long']} (à domicile) contre {a['long']}",
             f"Noms à utiliser dans le post : {disp(h)} et {disp(a)}",
             f"Compétition : {m['comp_fr']}"]
    if kind == "x1":
        facts += [f"Heure du coup d'envoi : {fr_hour(k)} (heure de Paris)", f"Le coup d'envoi est {delta_word(k, due)}"]
    else:
        facts.append(f"Quand : {when_word(k, due)} à {fr_hour(k)} (heure de Paris)")
    if h["tracked"] and a["tracked"]:
        facts.append("Choc entre deux très grosses équipes")
    pf, names = players_facts(m, state, now)
    return facts + pf, names


TONES = ["intensite maximale, phrases courtes et nerveuses", "complice de supporter, bon enfant",
         "soiree de gala, on annonce un gros morceau", "taquin et confiant, avec une petite vanne bon enfant",
         "tension d'avant-match, ca sent la poudre"]
X_INSTR = {
    "x24": ("Ecris le post de chauffe 24 h avant le match (il accompagne l'affiche du match) : accroche \"🔥\" ou \"🚨\", "
            "annonce le match avec la formulation de date indiquee dans les faits (\"Quand\") et l'heure, cite 2 ou 3 joueurs "
            "des faits, avec leur chiffre exact s'il y en a un. On ne connait pas les compositions : un joueur est \"a "
            "suivre\", \"attendu\" ou \"a surveiller\", JAMAIS annonce titulaire ni forcement present. Monte la pression "
            "avec une phrase qui claque (sans rien inventer), puis termine par une question courte qui fait reagir "
            "(ton score ? ton buteur ? qui gagne ?). N'ecris jamais le mot \"pronostic\". Evite les formules usees "
            "(\"la tension monte\", \"ca promet\", \"a suivre de pres\") et ecris les noms de clubs comme dans les faits "
            "(jamais en majuscules)."),
    "x1": ("Ecris le post de chauffe a 1 h du coup d'envoi (il accompagne l'affiche du match) : accroche \"⏰\" ou \"🚨\", "
           "annonce que le coup d'envoi est dans le delai indique dans les faits, avec l'heure, cite 2 ou 3 joueurs des "
           "faits, avec leur chiffre exact s'il y en a un. On ne connait pas les compositions : un joueur est \"a suivre\", "
           "\"attendu\" ou \"a surveiller\", JAMAIS annonce titulaire ni forcement present. Ambiance d'avant-match, "
           "tension qui monte, puis termine par une question courte qui fait reagir (ton score ? ton buteur ? "
           "qui gagne ?). N'ecris jamais le mot \"pronostic\". Evite les formules usees (\"la tension monte\", "
           "\"ca promet\", \"a suivre de pres\") et ecris les noms de clubs comme dans les faits (jamais en majuscules)."),
}


def fallback_text(m, kind, due, names):
    """Texte de secours (sans IA) : que des infos exactes, avec les noms de joueurs quand on en a."""
    title = f"{disp(m['home'])} × {disp(m['away'])}"
    k = m["kickoff"]
    rng = random.Random(f"{m['id']}-{kind}")
    who = [short_name(n) for n in dict.fromkeys(names)][:3]
    eyes = ""
    if who:
        eyes = "Les yeux sur " + (", ".join(who[:-1]) + " et " + who[-1] if len(who) > 1 else who[0]) + ". "
    if kind == "x24":
        head = rng.choice([
            f"🔥 {title} {when_word(k, due)} à {fr_hour(k)} !",
            f"🚨 {title} : rendez-vous {when_word(k, due)} à {fr_hour(k)} !",
            f"⚽ {m['comp_fr']} : {title} {when_word(k, due)} à {fr_hour(k)} !"])
        tail = rng.choice(["Ton score ? 👇", "Qui l'emporte ? 👇", "Ton buteur ? 👇"])
        return f"{head}\n\n{eyes}{tail}".strip()
    head = rng.choice([
        f"⏰ Coup d'envoi {delta_word(k, due)} : {title} !",
        f"🚨 {title} {delta_word(k, due)} ({m['comp_fr']}) !",
        f"🔥 C'est bientôt : {title} {delta_word(k, due)} !"])
    tail = rng.choice(["Ton score ? 👇", "Qui plante en premier ? 👇", "Ton buteur ? 👇"])
    return f"{head}\n\n{eyes}{tail}".strip()


def build_post_text(m, kind, due, state, now=None, quiet=False):
    """Texte final du post X : rédigé et relu par l'IA du community manager à partir de faits réels ;
    texte de secours si l'IA est indisponible ou refuse. Hashtags toujours ajoutés (3 à 4)."""
    now = now or now_utc()
    facts, names = x_facts(m, kind, due, state, now)
    tags = x_tags_for(m, names)
    text = None
    cm = get_cm()
    if cm and OPENAI_API_KEY:
        hs = {"usage": {"by_model": {}}, "counter": int(m["id"]) % 97,
              "posts": [{"text": t} for t in state.get("x_texts", [])[-12:]]}
        try:
            tone = TONES[(int(m["id"]) + (kind == "x1")) % len(TONES)]
            text = cm.generate("x_" + kind, X_INSTR[kind] + f" Tonalite : {tone}.", facts, cm.load_config(), hs,
                               comp_code=m["comp_code"],
                               ending="", tags=tags, max_chars=X_TEXT_MAX)
        except Exception as e:  # noqa: BLE001 - jamais bloquer l'affiche : on prend le texte de secours
            log(f"   ⚠️ rédaction IA impossible ({type(e).__name__}: {str(e)[:120]}) : texte de secours")
        u = state.setdefault("llm_calls", 0)
        state["llm_calls"] = u + sum(v.get("calls", 0) for v in hs["usage"]["by_model"].values())
    if text:
        state["llm_posts"] = state.get("llm_posts", 0) + 1
        src = "IA"
    else:
        post = fallback_text(m, kind, due, names)
        tg = tags(post)
        text = (post + "\n\n" + " ".join(tg)).strip()
        src = "secours"
    if not quiet:
        state["x_texts"] = (state.get("x_texts", []) + [text.split("\n\n")[0]])[-30:]
    return text, src


def alt_text(m, kind):
    if kind in ("preview", "xpreview", "x24", "x1"):
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
    """Cree l'affiche (logos officiels + VS) ou, si POSTER_MODE=ai, la version OpenAI.
    kind : preview / result (Telegram, pied de page @accesmatch) ou x24 / x1 (X, pied de page @monfootballfr)."""
    base = "preview" if kind in ("preview",) + X_KINDS else "result"
    if POSTER_MODE == "ai":
        prompt = build_preview_prompt(m) if base == "preview" else build_result_prompt(m)
        generate_image(prompt, quality_for(m), out_path)
        return
    h, a = m["home"], m["away"]
    poster.render_poster(
        out_path, base,
        {"id": h.get("id"), "short": h["short"], "crest_urls": h.get("crest_urls")},
        {"id": a.get("id"), "short": a["short"], "crest_urls": a.get("crest_urls")},
        fr_date(m["kickoff"]), m["comp_poster"],
        time_text=fr_hour(m["kickoff"]).upper(),
        score=(m["hs"], m["as_"]) if base == "result" else None,
        footer=X_FOOTER if kind in X_KINDS else None)


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
    "⚽ <b>{m}</b>\n{c} · <b>{d}</b> à <b>{h}</b> (heure de Paris)\n\n🧪 Teste avant le match pour obtenir ton accès : une fois le match lancé, plus de test.\n📺 Direct, HD, sans stream qui coupe.\n\nÉcris-moi en privé 👇",
    "🔥 <b>{m}</b> — <b>{d}</b> à <b>{h}</b>\n{c}\n\nFais ton test <b>avant</b> le match pour obtenir ton accès, pas pendant 🔓\nClique sur le bouton et écris-moi 👇",
    "🚨 <b>{m}</b>\n{c} · <b>{d}</b> à <b>{h}</b> (heure de Paris)\n\nN'attends pas le dernier moment : teste dès maintenant pour obtenir ton accès avant le début du match 🧪\nÉcris-moi en privé 👇",
]
TG_RESULT_TEMPLATES = [
    "🏁 <b>SCORE FINAL</b> : {s}\n\nTu l'as raté ? Pour le prochain, teste <b>avant</b> le match pour obtenir ton accès 🔓\nÉcris-moi en privé 👇",
    "📣 Terminé : <b>{s}</b>\n\nLe prochain match en direct ? Teste avant qu'il commence pour obtenir ton accès 📺\nClique sur le bouton et écris-moi 👇",
]


def build_telegram_text(m, kind):
    rng = random.Random(f"tg-{m['id']}-{kind}")
    if kind == "preview":
        title = f"{m['home']['short']} × {m['away']['short']}"
        return rng.choice(TG_PREVIEW_TEMPLATES).format(m=title, d=fr_day(m["kickoff"]), h=fr_hour(m["kickoff"]),
                                                       c=m["comp_fr"])
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
    """Envoie dans le canal Telegram : avant-match au meme moment que le post Buffer (48 h avant), score final tout de suite.
    Independant de Buffer : un post X en echec ne bloque plus l'annonce Telegram."""
    if not dry_run and not telegram_ready():
        if TELEGRAM_BOT_TOKEN:
            log("ℹ️ Telegram : TELEGRAM_DM_URL pas encore renseigné dans matchday.py, étape ignorée")
        return 0
    by_id = {p["id"]: p for p in tracked_matches(raw_matches)}
    sent = 0
    for key, it in list(state["items"].items()):
        if it["kind"] in X_KINDS or it.get("tg") in ("sent", "skip", "failed") or it["status"] == "cancelled":
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
def is_x_match(m):
    return m["home"].get("id") in X_CLUBS or m["away"].get("id") in X_CLUBS


def x_priority(m):
    """Importance d'un match pour le plafond du jour : +2 choc entre deux clubs X_CLUBS, +1 Ligue des champions,
    +1 si PSG ou OM (public francophone)."""
    ids = {m["home"].get("id"), m["away"].get("id")}
    return ((2 if len(ids & X_CLUBS) == 2 else 0) + (1 if m["comp_code"] == "CL" else 0)
            + (1 if ids & {524, 516} else 0))


def x_selection(matches, items):
    """Ids des matchs qui ont droit aux affiches X (J-24h + H-1) : par jour de match (heure de Paris), au plus
    X_MAX_PER_DAY matchs, les plus importants d'abord. Les matchs déjà pris en charge comptent dans le plafond."""
    done, used = set(), {}
    for it in items.values():
        if it["kind"] in X_KINDS and it["status"] != "cancelled":
            done.add(it["match_id"])
            d = parse_iso(it["kickoff"]).astimezone(AUDIENCE_TZ).date()
            used.setdefault(d, set()).add(it["match_id"])
    by_day = {}
    for m in matches:
        if m["status"] in ("TIMED", "SCHEDULED") and is_x_match(m) and m["id"] not in done:
            by_day.setdefault(m["kickoff"].astimezone(AUDIENCE_TZ).date(), []).append(m)
    keep = set(done)
    for d, ms in by_day.items():
        ms.sort(key=lambda m: (-x_priority(m), m["kickoff"]))
        keep.update(m["id"] for m in ms[:max(0, X_MAX_PER_DAY - len(used.get(d, ())))])
    return keep


def in_quiet_hours(dt_utc):
    local = dt_utc.astimezone(AUDIENCE_TZ).strftime("%H:%M")
    a, b = X_QUIET_HOURS
    return (local >= a or local < b) if a > b else (a <= local < b)


def after_quiet(dt_utc):
    """Si dt_utc tombe dans les heures calmes, premier instant calme-fini (07:35 à Paris) ; sinon dt_utc."""
    if not in_quiet_hours(dt_utc):
        return dt_utc
    local = dt_utc.astimezone(AUDIENCE_TZ)
    hh, mm = map(int, X_QUIET_HOURS[1].split(":"))
    end = local.replace(hour=hh, minute=mm + 5, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.astimezone(timezone.utc)


def slot_clash(dt_utc, taken):
    """True si dt_utc tombe trop près d'un créneau vidéo ou d'une autre affiche X déjà programmée."""
    if any(abs(dt_utc - t) < timedelta(minutes=X_MIN_GAP_MIN) for t in taken):
        return True
    margin = timedelta(minutes=X_SLOT_MARGIN_MIN)
    local = dt_utc.astimezone(AUDIENCE_TZ)
    for day in (-1, 0, 1):
        for hhmm in X_VIDEO_SLOTS:
            hh, mm = map(int, hhmm.split(":"))
            slot = (local + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if abs(local - slot) < margin:
                return True
    return False


def x_due(kind, kickoff, now, taken):
    """Heure de publication (datetime UTC) d'une affiche X, ou None si c'est trop tard / impossible.
    x24 : 24 h avant (puis jusqu'à +56 min plus tard si besoin) ; x1 : 1 h avant, à ± 24 min si un créneau vidéo gêne."""
    soon = now + timedelta(minutes=3)
    if kind == "x24":
        ideal = kickoff - timedelta(minutes=X_J1_MIN)
        if ideal < soon:
            if kickoff - now < timedelta(hours=X_LATE_J1_MIN_H):
                return None                       # trop tard pour une annonce "J-24h" : le post H-1 suffit
            ideal = soon
        late = ideal == soon
        ideal = after_quiet(ideal)
        for shift in ((0, 40, 80, 120, 160, 200, 240) if late else (0, 8, 16, 24, 32, 40, 48, 56, 90, 120, 180)):
            t = ideal + timedelta(minutes=shift)
            if t < soon or in_quiet_hours(t) or slot_clash(t, taken):
                continue
            if t > kickoff - timedelta(hours=4):
                break
            return t
        return None
    ideal = kickoff - timedelta(minutes=X_H1_MIN)
    for shift in (0, -8, 8, -16, 16, -24, 24):
        t = ideal + timedelta(minutes=shift)
        if t < soon:
            continue
        if t > kickoff - timedelta(minutes=10) or slot_clash(t, taken):
            continue
        return t
    if kickoff > now + timedelta(minutes=25):     # affiche tardive mais encore utile
        for shift in range(0, 40, 8):
            t = soon + timedelta(minutes=shift)
            if t <= kickoff - timedelta(minutes=10) and not slot_clash(t, taken):
                return t
    return None


def x_taken(items):
    """Heures (UTC) des affiches X déjà programmées dans Buffer."""
    out = []
    for it in items.values():
        if it["kind"] in X_KINDS and it["status"] == "scheduled" and it.get("due"):
            out.append(parse_iso(it["due"]))
    return out


def due_for(item_kind, kickoff, now):
    """Telegram : heure de publication (iso), ou None pour 'maintenant'. 'SKIP' si trop tard."""
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

    # 0b) Ancien format X (un seul post 3 h avant) : remplacé par les posts J-24h et H-1
    for key, it in list(items.items()):
        if not POST_TO_X or it["kind"] != "xpreview" or it["status"] not in ("poster_ready", "scheduled"):
            continue
        if it["status"] == "scheduled" and parse_iso(it.get("due") or it["kickoff"]) <= now + timedelta(minutes=5):
            continue  # déjà parti
        log(f"♻️  {it['label']} : ancien post X unique → remplacé par J-24h + H-1")
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
    for m, key in ((m, f"{pre}{m['id']}") for m in matches for pre in ("p", "x", "a", "h")):
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
    x_ok = x_selection(matches, items) if POST_TO_X else set()
    for m in sorted(matches, key=lambda x: x["kickoff"]):
        label = f"{m['home']['short']}-{m['away']['short']}"
        candidates = []
        # avant-match
        if (m["status"] in ("TIMED", "SCHEDULED")
                and now + timedelta(minutes=MIN_LEAD_MIN) < m["kickoff"] <= now + timedelta(hours=LOOKAHEAD_H)
                and f"p{m['id']}" not in items):
            candidates.append("preview")
        # affiches X : J-24h puis H-1 (10 clubs, plafond de matchs par jour)
        if POST_TO_X and m["id"] in x_ok and m["status"] in ("TIMED", "SCHEDULED"):
            taken = x_taken(items)
            if (now < m["kickoff"] <= now + timedelta(hours=X_J1_LOOKAHEAD_H) and f"a{m['id']}" not in items
                    and x_due("x24", m["kickoff"], now, taken)):
                candidates.append("x24")
            if (now < m["kickoff"] <= now + timedelta(hours=X_H1_LOOKAHEAD_H) and f"h{m['id']}" not in items
                    and x_due("x1", m["kickoff"], now, taken)):
                candidates.append("x1")
        # score final
        if (RESULT_POSTS and m["status"] == "FINISHED" and m["hs"] is not None and m["as_"] is not None
                and now - timedelta(hours=RESULT_MAX_AGE_H) < m["kickoff"] <= now
                and f"r{m['id']}" not in items):
            candidates.append("result")

        for kind in candidates:
            if created >= MAX_NEW_ITEMS_PER_RUN or images_today >= MAX_IMAGES_PER_DAY:
                log(f"⛔ Garde-fou coût atteint, {label} ({kind}) repoussé au prochain passage")
                continue
            key = {"preview": "p", "result": "r", "x24": "a", "x1": "h"}[kind] + str(m["id"])
            with_image = kind in ("preview",) + X_KINDS or RESULT_IMAGES
            stem = f"x{m['id']}" if kind in X_KINDS else key        # J-24h et H-1 partagent la même affiche
            rel = f"posters/{stem}-{m['kickoff'].strftime('%Y%m%d')}.jpg" if with_image else None
            log(f"🆕 {label} [{m['comp_fr']}] {kind} — coup d'envoi {fr_date(m['kickoff'])} {fr_hour(m['kickoff'])}")
            if dry_run:
                if kind in X_KINDS:
                    due_dt = x_due(kind, m["kickoff"], now, x_taken(items))
                    names = []
                    txt = fallback_text(m, kind, due_dt, names) + "\n\n" + hashtags(m)
                    log(f"   publication X/Buffer : {iso(due_dt)}  | affiche : {POSTER_MODE}")
                else:
                    txt = build_telegram_text(m, kind)
                    log(f"   publication Telegram : {due_for(kind, m['kickoff'], now) or 'immédiate'}  | affiche : {POSTER_MODE}")
                log("   texte :\n      " + txt.replace("\n", "\n      "))
                created += 1
                continue
            if with_image:
                twin = items.get("h" + str(m["id"]) if kind == "x24" else "a" + str(m["id"])) if kind in X_KINDS else None
                reuse = bool(twin and twin["status"] != "cancelled" and twin["kickoff"] == iso(m["kickoff"])
                             and twin.get("design") == DESIGN_VERSION and (ROOT / rel).exists())
                if not reuse:
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
    if not POST_TO_X:
        log("ℹ️ Affiches X désactivées (POST_TO_X = False) : rien n'est envoyé à Buffer")
        return 0
    items = state["items"]
    by_id = {p["id"]: p for p in tracked_matches(raw_matches)}
    taken = x_taken(items)
    sent = 0
    todo = sorted((it for it in items.values() if it["kind"] in ("x24", "x1") and it["status"] == "poster_ready"),
                  key=lambda it: it["kickoff"])
    for it in todo:       # les affiches Telegram (preview / result) ne passent pas par Buffer
        kickoff = parse_iso(it["kickoff"])
        due = x_due(it["kind"], kickoff, now, taken)
        if due is None:
            log(f"⌛ {it['label']} ({it['kind']}) : trop tard ou aucun horaire libre, ignoré")
            it["status"] = "missed"
            continue
        m = by_id.get(it["match_id"])
        if not m:
            # match plus dans la fenêtre API : on réessaie plus tard
            log(f"⚠️ {it['label']} : introuvable dans l'API ce passage, on réessaie plus tard")
            continue
        if dry_run:
            text = fallback_text(m, it["kind"], due, []) + "\n\n" + hashtags(m)
            log(f"📤 [dry-run] {it['label']} ({it['kind']}) → Buffer, {iso(due)}\n      " + text.replace("\n", "\n      "))
            taken.append(due)
            continue
        try:
            text, src = build_post_text(m, it["kind"], due, state, now)
            url = None
            if it.get("poster"):
                url = image_url_for(it["poster"])
                if not wait_for_url(url):
                    raise RuntimeError(f"image pas encore accessible : {url}")
            post_id = buffer_create_post(text, url, alt_text(m, it["kind"]), iso(due))
            it.update(status="scheduled", post_id=post_id, due=iso(due), text=text, text_src=src)
            taken.append(due)
            save_state(state)
            sent += 1
            log(f"📤 {it['label']} ({it['kind']}) → Buffer OK, programmé {iso(due)} (texte {src})")
            gh_notice(f"[affiche X] {it['label']} {'J-24h' if it['kind'] == 'x24' else 'H-1'} programmée "
                      f"{due.astimezone(AUDIENCE_TZ).strftime('%d/%m %Hh%M')} (Paris), texte {src} :\n{text}")
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
# AFFICHE D'ESSAI + TEST DES TEXTES X
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
    make_poster(m, "x24", out)
    log(f"✅ Enregistrée : {out}")
    due = m["kickoff"] - timedelta(minutes=X_J1_MIN)
    log("Texte du post qui irait avec :\n\n" + build_post_text(m, "x24", due, {"items": {}}, now, quiet=True)[0])


def xtest(raw_matches, now, n=3):
    """Écrit (pour de vrai, avec l'IA et les vraies données) les posts J-24h et H-1 des prochains matchs, sans rien
    programmer ni modifier : pour juger la qualité des textes. Résultat dans la page du run."""
    state = load_state()
    ms = sorted((m for m in tracked_matches(raw_matches) if m["kickoff"] > now and m["status"] in ("TIMED", "SCHEDULED")),
                key=lambda m: (-x_priority(m), m["kickoff"]))[:n]
    if not ms:
        gh_notice("[test affiches X] aucun match à venir dans la fenêtre")
        return
    for m in ms:
        for kind, minutes in (("x24", X_J1_MIN), ("x1", X_H1_MIN)):
            due = m["kickoff"] - timedelta(minutes=minutes)
            text, src = build_post_text(m, kind, due, state, now, quiet=True)
            facts, names = x_facts(m, kind, due, state, now)
            gh_notice(f"[test X {kind}] {m['home']['short']}-{m['away']['short']} ({src}, {len(text)} car.)\n{text}\n"
                      f"-- joueurs trouvés : {', '.join(names) or 'aucun'}")
    log(f"IA : {state.get('llm_calls', 0)} appels")


# ============================================================
# MAIN
# ============================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="AccesMatch MatchDay")
    ap.add_argument("mode", nargs="?", choices=["prepare", "publish", "all", "xtest"], default="all")
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
    if args.mode == "xtest":
        xtest(raw, now)
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
