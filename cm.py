#!/usr/bin/env python3
"""
CM monfootballfr : community manager foot automatique (X, via Buffer), facon "grosse chaine foot" : info d'abord,
focus sur les grosses equipes et les gros joueurs (Mbappe, Yamal, Olise...), avec la ferveur d'un supporter.

Ce que fait le robot (a chaque passage, declenche toutes les 10 a 15 min) :
  1. REACTIONS : quand un match d'une grosse equipe (ou de Ligue des champions / Ligue 1) vient de finir,
     il ecrit une reaction a chaud (avec les buteurs si disponibles) et la programme dans les minutes qui suivent.
  2. BREAKING : une actu chaude (un gros joueur, une grosse equipe, publiee il y a moins de 4 h) part en quelques
     minutes, sans attendre un creneau.
  3. CRENEAUX : ~6 a 8 par jour. Selon l'heure : actu, chiffre du jour (meilleurs buteurs), anecdote "il y a N ans",
     programme / affiche du soir, recap des resultats, debat.
  3. GARDE-FOUS (aucun humain ne relit) : l'IA ne recoit QUE des faits (football-data + titres d'actu),
     le post est controle par le code (chiffres, insultes, liens, doublons) puis par une 2e IA "relectrice".
     Au moindre doute le post est jete : mieux vaut ne rien publier que publier une betise.
  4. Buffer : les posts sont programmes (jamais publies tout de suite), sans jamais toucher aux creneaux video.

Commandes :
  python cm.py                # execution normale (cron)
  python cm.py --dry-run      # comme la normale, mais n'envoie rien a Buffer et n'ecrit pas l'etat
  python cm.py --samples      # un echantillon de chaque type de post, sans rien programmer
  python cm.py --diag         # diagnostic : modeles OpenAI, flux d'actu, football-data

Interrupteur : "enabled": false dans cm_config.json (ou variable CM_DISABLED=1).
Variables : OPENAI_API_KEY, BUFFER_API_KEY, FOOTBALL_DATA_TOKEN (optionnelles : OPENAI_MODEL, OPENAI_MODEL_VERIFIER, BUFFER_CHANNEL_ID)
Codes de sortie : 0 = OK, 2 = erreur (cle OpenAI refusee, credit epuise...) -> email d'alerte GitHub
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "cm_config.json"
STATE_FILE = ROOT / "cm_state.json"

BUFFER_API_KEY = os.environ.get("BUFFER_API_KEY", "").strip()
BUFFER_URL = "https://api.buffer.com"
BUFFER_CHANNEL_ID = os.environ.get("BUFFER_CHANNEL_ID") or "6ade26c62f1f5ed648ca2250"   # monfootballfr
BUFFER_ORG_ID = os.environ.get("BUFFER_ORG_ID", "6ab1f781b91aadc0d8a2a8ca")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()

PARIS = ZoneInfo("Europe/Paris")
UA = {"User-Agent": "Mozilla/5.0 (compatible; monfootballfr-cm/1.0)"}

DEFAULT_CONFIG = {
    "enabled": True,
    "start_date": "2026-10-27",     # 1er jour de publication du compte (meme jour que la chauffe des videos)
    # chauffe progressive : [jours depuis le depart, creneaux de Paris] ; null = jusqu'a la fin
    "phases": [[14, ["08:05", "10:45", "14:00", "16:30", "20:15", "22:35"]],
               [30, ["08:05", "10:45", "14:00", "16:30", "19:15", "20:15", "22:35"]],
               [None, ["08:05", "10:45", "13:15", "15:00", "16:30", "19:15", "20:15", "22:35"]]],
    "max_reactions_per_day": 4,
    "max_breaking_per_day": 3,      # posts d'actu chaude hors creneaux
    "breaking_max_age_h": 4,        # une actu chaude doit avoir moins de 4 h
    "news_horizon_h": 3.5,          # un creneau plus lointain que ca n'utilise pas l'actu (elle serait deja froide)
    "lookahead_h": 8,               # on prepare un creneau au plus 8 h avant (baisser a 3 quand le declencheur est frequent)
    "model": "gpt-6-luna",          # redacteur des posts
    "verifier_model": "gpt-6.1-sol",  # relecteur (controle les faits avant programmation)
    "quiet_hours": ["00:30", "07:30"],
    "video_slots": ["09:15", "12:30", "18:30", "21:45"],
    "slot_margin_min": 30,          # jamais de post a moins de 30 min d'un autre post
}

MIN_LEAD_MIN = 10                   # un post doit etre programme au moins 10 min a l'avance
MAX_SLOT_TRIES = 3
MAX_POST_CHARS = 235                # hors hashtags
NEWS_MAX_AGE_H = 30
SIMILARITY_MAX = 0.55               # au-dela : trop proche d'un post recent
KEEP_POSTS = 300

COMPETITIONS = "FL1,CL,PL,PD,BL1,SA"
COMP_NAMES = {"FL1": "Ligue 1", "CL": "Ligue des champions", "PL": "Premier League", "PD": "Liga",
              "BL1": "Bundesliga", "SA": "Serie A"}
COMP_TAGS = {"FL1": "#Ligue1", "CL": "#LDC", "PL": "#PremierLeague", "PD": "#LaLiga", "BL1": "#Bundesliga",
             "SA": "#SerieA"}
# grosses equipes (ids football-data.org) : PSG, OM, Lyon, Monaco, Lille, Barca, Real, Atletico, Liverpool, City, Arsenal,
# Chelsea, Man United, Tottenham, Bayern, Dortmund, Leverkusen, Juventus, Inter, Milan, Napoli, Roma
X_CLUBS = {524, 516, 523, 548, 521, 81, 86, 78, 64, 65, 57, 61, 66, 73, 5, 4, 3, 109, 108, 98, 113, 100}

# mots-cles -> hashtag (sur les faits, pas sur le texte de l'IA)
TAGS_BY_KEYWORD = [
    (("mbappe",), "#Mbappé"), (("yamal",), "#Yamal"), (("olise",), "#Olise"), (("dembele",), "#Dembélé"),
    (("haaland",), "#Haaland"), (("vinicius", "vini jr"), "#Vinicius"), (("messi",), "#Messi"),
    (("ronaldo", "cr7"), "#Ronaldo"), (("bellingham",), "#Bellingham"), (("kane",), "#Kane"),
    (("salah",), "#Salah"), (("doue",), "#Doué"), (("cherki",), "#Cherki"), (("raphinha",), "#Raphinha"),
    (("paris saint-germain", "psg", "paris sg"), "#PSG"),
    (("marseille", "olympique de marseille"), "#OM"),
    (("barcelone", "barcelona", "barca"), "#FCBarcelona"),
    (("real madrid",), "#RealMadrid"),
    (("liverpool",), "#Liverpool"),
    (("manchester city", "man city"), "#ManCity"),
    (("bayern",), "#FCBayern"),
    (("manchester united", "man united"), "#MUFC"),
    (("arsenal",), "#Arsenal"),
    (("chelsea",), "#Chelsea"),
    (("atletico",), "#Atleti"),
    (("juventus",), "#Juve"),
    (("lyon",), "#OL"),
    (("monaco",), "#ASM"),
    (("lille",), "#LOSC"),
    (("equipe de france", "bleus"), "#EquipeDeFrance"),
]

# flux d'actu foot (les flux inaccessibles sont simplement ignores)
FEEDS = [
    ("L'Equipe", "https://dwh.lequipe.fr/api/edito/rss?path=/Football/"),
    ("RMC Sport", "https://rmcsport.bfmtv.com/rss/football/"),
    ("Foot Mercato", "https://www.footmercato.net/flux-rss"),
    ("Le Parisien", "https://feeds.leparisien.fr/leparisien/rss/sports/football"),
    ("Google Actualites", "https://news.google.com/rss/search?q=football+ligue+1+OR+PSG+OR+OM+when:1d&hl=fr&gl=FR&ceid=FR:fr"),
    # recherches ciblees sur les gros noms et les grosses equipes (Google Actualites renvoie "titre - media")
    ("Google Actualites", "https://news.google.com/rss/search?q=Mbapp%C3%A9+OR+Yamal+OR+Olise+OR+Demb%C3%A9l%C3%A9"
                          "+OR+Haaland+OR+Vinicius+OR+Messi+OR+Ronaldo+when:1d&hl=fr&gl=FR&ceid=FR:fr"),
    ("Google Actualites", "https://news.google.com/rss/search?q=%22Real+Madrid%22+OR+Bar%C3%A7a+OR+%22Manchester+City%22"
                          "+OR+Liverpool+OR+Bayern+OR+Arsenal+when:1d&hl=fr&gl=FR&ceid=FR:fr"),
    ("Google Actualites", "https://news.google.com/rss/search?q=%22Ligue+des+champions%22+OR+mercato+OR+%22Ballon+d%27Or%22"
                          "+when:1d&hl=fr&gl=FR&ceid=FR:fr"),
    ("20 Minutes", "https://www.20minutes.fr/feeds/rss-sport.xml"),
]

def norm(s):
    """minuscules sans accents (comparaisons)"""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def flat(s):
    """norm() + ponctuation d'apostrophes et tirets remplacee par des espaces (pour chercher des noms)."""
    return re.sub(r"[-'\u2019`]", " ", norm(s))


def _load_stars():
    """Gros joueurs : meme liste que le robot video (video_priority.json) pour rester coherent."""
    stars, boost, aliases = [], [], {}
    try:
        d = json.loads((ROOT / "video_priority.json").read_text(encoding="utf-8"))
        stars, boost, aliases = d.get("stars", []), d.get("boost", []), d.get("aliases", {})
    except (OSError, json.JSONDecodeError):
        stars = ["mbappe", "yamal", "olise", "dembele", "haaland", "vinicius", "messi", "ronaldo", "bellingham"]
        boost = stars[:3]
    return stars, boost, aliases


_STARS, _BOOST, _ALIASES = _load_stars()
STAR_WORDS = sorted({norm(x) for x in _STARS} | {norm(k) for k in _ALIASES}, key=len, reverse=True)
STAR_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in STAR_WORDS) + r")\b")
STAR_CANON = {**{norm(x): norm(x) for x in _STARS}, **{norm(k): norm(v) for k, v in _ALIASES.items()}}
BOOST_SET = {norm(x) for x in _BOOST} | {"yamal", "olise"}
CLUB_RE = re.compile(r"\b(psg|paris saint germain|paris sg|marseille|om|real madrid|real|barca|barcelone|liverpool|"
                     r"manchester city|man city|manchester united|man united|arsenal|chelsea|bayern|atletico|"
                     r"juventus|juve|inter milan|inter de milan|ac milan|dortmund|tottenham|napoli|lyon|monaco|"
                     r"equipe de france|bleus|deschamps|clasico|classique)\b")
COMP_RE = re.compile(r"\b(ligue des champions|ligue 1|ballon d or|mercato|premier league|liga|bundesliga)\b")


def stars_in(text):
    """Gros joueurs cites dans un texte (noms canoniques, sans accent)."""
    return {STAR_CANON.get(m, m) for m in STAR_RE.findall(flat(text))}


def news_score(title):
    """Interet d'une actu : gros joueur = 2 (stars du moment = 3), grosse equipe = 1, competition/mercato = 0,5."""
    n = flat(title)
    stars = stars_in(title)
    score = sum(3 if st in BOOST_SET else 2 for st in stars)
    score += len(set(CLUB_RE.findall(n)))
    score += 0.5 * len(set(COMP_RE.findall(n)))
    return score


# sujets qu'on ne touche JAMAIS (actu sensible, paris sportifs, justice...) : normalises sans accents
SENSITIVE_RE = re.compile(
    r"\b(deces|decede\w*|mort\w*|mourir|deuil|hommage|obseques|cancer|suicide\w*|accident\w*|tragedie|drame\w*|"
    r"viol\w*|agress\w*|violence\w*|racis\w*|homophob\w*|antisemit\w*|terror\w*|attentat\w*|guerre\w*|"
    r"politique\w*|election\w*|proces|garde a vue|interpelle\w*|mis en examen|plainte\w*|dopage|dope\w*|"
    r"paris sportifs?|parieur\w*|pronostic\w*|cotes?|bookmaker\w*|projectile\w*|escroquer\w*|coma|hospitalis\w*|grave\w*|menace\w*|"
    r"insulte\w*|police|justice|tribunal|prison|drogue\w*|cannabis|deprime\w*|burn.?out)\b")

# mots interdits dans le post genere
BAD_RE = re.compile(
    r"\b(connard\w*|connasse\w*|salop\w*|pute\w*|encul\w*|merde\w*|putain\w*|nique\w*|fdp|ntm|batard\w*|"
    r"negre\w*|bougnoul\w*|mongol\w*|debile\w*|attarde\w*|handicape\w*|cancer|suicide\w*|mort|mourir|deces|tuer|"
    r"juif\w*|arabe\w*|noir\w*|singe\w*|nain)\b")

PRICES = {  # dollars par million de tokens (entree, sortie), page tarifs OpenAI d'octobre 2026
    "gpt-6-luna": (0.10, 0.50), "gpt-6.1-sol": (2.00, 10.00), "gpt-6-astra": (10.00, 50.00),
}
# redaction : modele le moins cher ; relecture : modele plus fiable (appel court, quelques centimes par mois)
WRITER_MODELS = ["gpt-6-luna", "gpt-6.1-sol"]
VERIFIER_MODELS = ["gpt-6.1-sol", "gpt-6-luna"]

STYLES = [
    "une question qui divise les supporters",
    "un avis tranche mais drole",
    "une petite vanne de supporter, bon enfant",
    "un enthousiasme sincere de passionne",
]

SYSTEM_PROMPT = (
    "Tu es la plume de @monfootballfr, grosse chaine X francophone d'actu foot, animee par des passionnes : "
    "l'efficacite d'une chaine d'info foot (type Foot Mercato, Actu Foot) et la ferveur d'un supporter. "
    "L'info d'abord, une reaction ou un avis ensuite. Phrases courtes et nettes, vocabulaire de supporter "
    "(ca claque, enorme, quelle masse), un peu d'humour, jamais de langue de bois ni de \"d'un cote... de l'autre\".\n"
    "Tu parles des grosses equipes et des gros joueurs (Mbappe, Yamal, Olise, Dembele, Haaland, Vinicius...). "
    "Toujours respectueux : jamais d'insulte, jamais de moquerie sur le physique, l'origine, la religion, une "
    "blessure, un deces ou une affaire judiciaire. On chambre avec humour, on ne rabaisse pas un joueur ou un club.\n"
    "REGLE D'OR : tu n'utilises QUE les FAITS fournis. Aucun score, chiffre, stat, transfert, blessure, citation, "
    "date ou heure qui n'est pas dans les faits. Si tu n'es pas sur, tu ne l'affirmes pas.\n"
    "Si l'info vient d'un media, cite-le (\"Selon L'Equipe\", \"D'apres RMC Sport\") et reformule avec tes propres mots "
    "(ne recopie jamais le titre). Une rumeur, une piste ou un \"pourrait\" reste au conditionnel, jamais presentee "
    "comme un fait etabli.\n"
    "Format : UN seul post de 80 a 230 caracteres, en francais, 2 emojis maximum (celui d'accroche compte), AUCUN "
    "hashtag (ils sont ajoutes ensuite), aucun lien, aucune @mention, pas de guillemets autour du post. "
    "Evite les formules creuses (\"alors, vous en pensez quoi ?\") : une question seulement si on te le demande.\n"
    "Reponds uniquement en JSON : {\"post\": \"...\"}"
)

VERIFIER_PROMPT = (
    "Tu es le relecteur strict d'un compte de foot automatique. On te donne des FAITS et un POST. "
    "Reponds uniquement en JSON : {\"ok\": true ou false, \"raison\": \"...\"}.\n"
    "ok=false si : le post contient un chiffre, un nom, une stat, un score, un transfert, une blessure, une heure, une "
    "date ou une citation absents des FAITS ; il presente une rumeur comme un fait ; il attribue une info a un autre "
    "media que celui des FAITS ; il contient une insulte ou une moquerie sur le physique, l'origine, la religion, une "
    "blessure, un deces ou une affaire judiciaire ; il contient un lien ou une @mention ; il est incomprehensible ou "
    "hors sujet. Un style punchy, un emoji d'accroche (🚨 📊 📆 ⏱️ 🔥) et un avis de supporter sont normaux. "
    "Un chiffre ou un nom qui figure dans les FAITS est valide. Sinon ok=true."
)


def log(msg):
    print(msg, flush=True)


def _gh(kind, msg):
    if os.environ.get("GITHUB_ACTIONS"):
        msg = str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::{kind}::{msg}", flush=True)


def gh_notice(msg):
    _gh("notice", msg)


def gh_error(msg):
    _gh("error", msg)


class Fatal(Exception):
    """Probleme general (cle refusee, credit epuise) : on arrete tout et on alerte."""


# ============================================================
# CONFIG / ETAT
# ============================================================
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            log("config illisible, valeurs par defaut")
    return cfg


def load_state():
    st = {}
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("etat illisible, on repart de zero")
    for k, v in (("posts", []), ("slots", {}), ("slot_tries", {}), ("reacted", []), ("used_news", []),
                 ("used_titles", []), ("recent_stats", []), ("counter", 0), ("usage", {"by_model": {}})):
        st.setdefault(k, v)
    return st


def save_state(st):
    st["posts"] = st["posts"][-KEEP_POSTS:]
    st["reacted"] = st["reacted"][-300:]
    st["used_news"] = st["used_news"][-600:]
    st["used_titles"] = st["used_titles"][-120:]
    st["recent_stats"] = st["recent_stats"][-30:]
    st["slots"] = dict(sorted(st["slots"].items())[-120:])
    st["slot_tries"] = dict(sorted(st["slot_tries"].items())[-120:])
    STATE_FILE.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def hhmm_to_dt(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=PARIS).astimezone(timezone.utc)


def phase_slots(day, cfg):
    start = date.fromisoformat(cfg["start_date"])
    n = (day - start).days
    if n < 0:
        return []
    for until, slots in cfg["phases"]:
        if until is None or n < until:
            return slots
    return cfg["phases"][-1][1]


# ============================================================
# OPENAI
# ============================================================
_OK = {}            # role -> modele qui a repondu
_PARAMS = {}        # modele -> reglages qui ont marche (noms de parametres acceptes)
_FAILS = []         # modeles refuses (pour le diagnostic)


def _openai_headers():
    if not OPENAI_API_KEY:
        raise Fatal("OPENAI_API_KEY manquant")
    return {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}


def models_for(role, cfg):
    """Modeles a essayer pour un role ("writer" ou "verifier"), le modele choisi d'abord, puis les secours."""
    key, env, base = (("verifier_model", "OPENAI_MODEL_VERIFIER", VERIFIER_MODELS) if role == "verifier"
                      else ("model", "OPENAI_MODEL", WRITER_MODELS))
    forced = (os.environ.get(env) or cfg.get(key) or "").strip()
    return ([forced] if forced else []) + [m for m in base if m != forced]


def usage_cost(usage):
    total = 0.0
    for m, u in (usage.get("by_model") or {}).items():
        p = PRICES.get(m)
        if p:
            total += (u["in"] * p[0] + u["out"] * p[1]) / 1e6
    return total


def usage_text(usage):
    parts = [f"{m}: {u['calls']} appels, {u['in']} tokens in, {u['out']} out" for m, u in (usage.get("by_model") or {}).items()]
    return " | ".join(parts) + f" | ~{usage_cost(usage):.4f} $ cumules"


def llm_json(messages, cfg, state, role="writer", max_tokens=2500):
    """Appel OpenAI (chat completions) qui renvoie un dict JSON. S'adapte aux parametres refuses par un modele
    (renomme ou retire ce que l'erreur 400 designe) et retient les reglages qui marchent pour les appels suivants."""
    models = [_OK[role]] if role in _OK else models_for(role, cfg)
    last_err = None
    for model in models:
        prm = _PARAMS.setdefault(model, {"extra": {"reasoning_effort": "low", "response_format": {"type": "json_object"}},
                                         "tokens_param": "max_completion_tokens"})
        body = {"model": model, "messages": messages, prm["tokens_param"]: max_tokens, **prm["extra"]}
        for _ in range(6):
            r = requests.post("https://api.openai.com/v1/chat/completions", headers=_openai_headers(),
                              json=body, timeout=120)
            if r.status_code == 200:
                j = r.json()
                u = j.get("usage") or {}
                mu = state["usage"].setdefault("by_model", {}).setdefault(model, {"calls": 0, "in": 0, "out": 0})
                mu["calls"] += 1
                mu["in"] += u.get("prompt_tokens", 0)
                mu["out"] += u.get("completion_tokens", 0)
                _OK[role] = model
                prm["extra"] = {k: v for k, v in body.items() if k in ("reasoning_effort", "response_format", "temperature")}
                prm["tokens_param"] = "max_tokens" if "max_tokens" in body else "max_completion_tokens"
                txt = (j["choices"][0]["message"].get("content") or "").strip()
                m = re.search(r"\{.*\}", txt, re.S)
                if not m:
                    raise ValueError(f"reponse sans JSON : {txt[:120]!r} (fin: {j['choices'][0].get('finish_reason')})")
                return json.loads(m.group(0))
            msg = r.text[:600]
            if r.status_code == 403 and ("access to model" in msg or "model_not_found" in msg or "does not exist" in msg):
                last_err = f"{model} : acces refuse a ce modele"
                _FAILS.append(f"{model}: {msg[:120]}")
                break
            if r.status_code in (401, 403):
                raise Fatal(f"OpenAI refuse la cle (HTTP {r.status_code}) : {msg[:300]}")
            if r.status_code == 402 or (r.status_code == 429 and ("insufficient_quota" in msg or "billing" in msg.lower())):
                raise Fatal("Credit OpenAI epuise (insufficient_quota) : recharge sur platform.openai.com")
            if r.status_code == 429:
                time.sleep(20)
                continue
            if r.status_code == 404 or "model_not_found" in msg or "does not exist" in msg:
                last_err = f"modele {model} indisponible"
                _FAILS.append(f"{model}: {msg[:120]}")
                break
            if r.status_code == 400:   # parametre non supporte par ce modele : on le retire ou on le renomme
                changed = False
                if "max_completion_tokens" in msg and "max_completion_tokens" in body:
                    body["max_tokens"] = body.pop("max_completion_tokens")
                    changed = True
                elif "max_tokens" in msg and "max_tokens" in body:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                    changed = True
                else:
                    for p in ("reasoning_effort", "response_format", "temperature"):
                        if p in msg and p in body:
                            body.pop(p)
                            changed = True
                            break
                if changed:
                    log(f"   (OpenAI : reglage ajuste pour {model} : {msg[:110]})")
                    continue
            last_err = f"{model} HTTP {r.status_code} {msg[:200]}"
            break
        else:
            last_err = last_err or f"{model} : trop d'essais"
    raise RuntimeError(f"OpenAI : aucun modele n'a repondu ({last_err})")


# ============================================================
# DONNEES : football-data.org
# ============================================================
def fd_get(path, params=None):
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_TOKEN manquant")
    for attempt in range(2):
        r = requests.get(f"https://api.football-data.org/v4{path}", params=params,
                         headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN}, timeout=30)
        if r.status_code == 429 and attempt == 0:
            time.sleep(60)
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"football-data {path} : HTTP {r.status_code} {r.text[:150]}")
    return r.json()


def recent_finished(now, hours=8):
    d_from = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    d_to = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    data = fd_get("/matches", {"dateFrom": d_from, "dateTo": d_to, "competitions": COMPETITIONS})
    out = []
    for m in data.get("matches", []):
        if m.get("status") != "FINISHED":
            continue
        ko = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        if timedelta(0) < now - ko < timedelta(hours=hours):
            out.append(m)
    return out


def reaction_priority(m):
    ids = {m["homeTeam"].get("id"), m["awayTeam"].get("id")}
    code = (m.get("competition") or {}).get("code")
    if ids & X_CLUBS:
        return 0
    if code == "CL":
        return 1
    if code == "FL1":
        return 2
    return 9


def team_name(t):
    return t.get("shortName") or t.get("name") or "?"


def match_facts(m):
    ft = (m.get("score") or {}).get("fullTime") or {}
    ht = (m.get("score") or {}).get("halfTime") or {}
    h, a = team_name(m["homeTeam"]), team_name(m["awayTeam"])
    comp = COMP_NAMES.get((m.get("competition") or {}).get("code"), (m.get("competition") or {}).get("name", ""))
    facts = [f"Match termine ({comp}) : {h} {ft.get('home')}-{ft.get('away')} {a}"]
    if ht.get("home") is not None:
        facts.append(f"Score a la mi-temps : {ht.get('home')}-{ht.get('away')}")
    if m.get("matchday"):
        facts.append(f"Journee {m['matchday']}")
    hs, as_ = ft.get("home"), ft.get("away")
    if hs is not None and as_ is not None:
        facts.append("Match nul" if hs == as_ else f"Victoire de {h if hs > as_ else a}")
    return facts


def standings_facts(code, top=6):
    data = fd_get(f"/competitions/{code}/standings")
    table = None
    for s in data.get("standings", []):
        if s.get("type") == "TOTAL" and s.get("table"):
            table = s["table"]
            break
    if not table:
        return None
    md = (data.get("season") or {}).get("currentMatchday")
    rows = [f"{r['position']}. {team_name(r['team'])} {r['points']} pts (diff. {r['goalDifference']:+d}, "
            f"{r['playedGames']} matchs)" for r in table[:top]]
    return [f"Classement {COMP_NAMES.get(code, code)}" + (f" apres {md} journees" if md else "") + " :"] + rows


# ============================================================
# DONNEES : actu (flux RSS)
# ============================================================
def _text(el):
    return html.unescape(re.sub(r"<[^>]+>", " ", (el.text or "") if el is not None else "")).strip()


def fetch_feed(name, url):
    r = requests.get(url, headers=UA, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    root = ET.fromstring(r.content)
    items = []
    for it in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
        title = _text(it.find("title")) or _text(it.find("{http://www.w3.org/2005/Atom}title"))
        when = (_text(it.find("pubDate")) or _text(it.find("{http://www.w3.org/2005/Atom}updated"))
                or _text(it.find("{http://purl.org/dc/elements/1.1/}date")))
        ts = None
        try:
            ts = parsedate_to_datetime(when) if when else None
        except (TypeError, ValueError):
            try:
                ts = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                ts = None
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        source = name
        if name == "Google Actualites" and " - " in title:
            title, source = title.rsplit(" - ", 1)
        summary = ""
        if name != "Google Actualites":
            summary = re.sub(r"\s+", " ", _text(it.find("description")) or _text(it.find("{http://www.w3.org/2005/Atom}summary"))).strip()
            if summary.lower().startswith(title.lower()[:40]) or len(summary) < 50:
                summary = ""
        if title:
            items.append({"title": re.sub(r"\s+", " ", title).strip(), "source": source.strip(), "ts": ts,
                          "summary": summary[:280]})
    return items


def news_items(now):
    """Titres d'actu foot recents et surs, du plus pertinent au moins pertinent."""
    seen, out, report = set(), [], []
    for name, url in FEEDS:
        try:
            items = fetch_feed(name, url)
        except Exception as e:                          # un flux en panne ne doit jamais bloquer le robot
            report.append(f"{name}: KO ({str(e)[:40]})")
            continue
        report.append(f"{name}: {len(items)}")
        for it in items:
            key = hashlib.sha1(norm(it["title"]).encode()).hexdigest()[:12]
            if key in seen:
                continue
            seen.add(key)
            if it["ts"] and now - it["ts"] > timedelta(hours=NEWS_MAX_AGE_H):
                continue
            n = norm(it["title"])
            if SENSITIVE_RE.search(n) or len(it["title"]) < 25 or n.startswith(("direct", "live", "en direct")):
                continue
            score = news_score(it["title"])
            age_h = (now - it["ts"]).total_seconds() / 3600 if it["ts"] else 12
            it.update(key=key, score=score, age_h=age_h)
            out.append(it)
    out.sort(key=lambda it: -(it["score"] - 0.2 * min(it["age_h"], 24)))      # gros noms d'abord, puis le plus frais
    return out, report


def news_facts(it):
    facts = [f"Source : {it['source']}", f"Titre publie par {it['source']} : {it['title']}",
             f"Publie il y a environ {max(1, round(it['age_h']))} h"]
    if it.get("summary"):
        facts.append(f"Resume de l'article ({it['source']}) : {it['summary']}")
    return facts


# ============================================================
# GENERATION + GARDE-FOUS
# ============================================================
def numbers_in(s):
    return set(re.findall(r"\d+", s or ""))


def tokens(s):
    return {w for w in re.findall(r"[a-z0-9]{4,}", norm(s))}


def too_similar(post, state):
    a = tokens(post)
    for p in state["posts"][-30:]:
        b = tokens(p.get("text", ""))
        if a and b and len(a & b) / len(a | b) >= SIMILARITY_MAX:
            return True
    return False


def local_check(post, facts, state):
    """Controle par le code. Renvoie la raison du refus, ou None si le post est correct."""
    if not post:
        return "post vide"
    if len(post) < 40 or len(post) > MAX_POST_CHARS:
        return f"longueur {len(post)}"
    if re.search(r"https?:|www\.|\.com\b|\.fr\b|@|#", post):
        return "lien, mention ou hashtag"
    if BAD_RE.search(norm(post)):
        return "mot interdit"
    if SENSITIVE_RE.search(norm(post)):
        return "sujet sensible"
    allowed = numbers_in(" ".join(facts)) | {"1", "2"}
    extra = numbers_in(post) - allowed
    if extra:
        return f"chiffre absent des faits : {sorted(extra)}"
    if too_similar(post, state):
        return "trop proche d'un post recent"
    return None


def verify(post, facts, cfg, state):
    out = llm_json([{"role": "system", "content": VERIFIER_PROMPT},
                    {"role": "user", "content": "FAITS :\n" + "\n".join(f"- {f}" for f in facts)
                     + f"\n\nPOST :\n{post}"}], cfg, state, role="verifier", max_tokens=1500)
    return bool(out.get("ok")), str(out.get("raison", ""))[:160]


def hashtags_for(post, comp_code=None):
    """Hashtags d'apres ce que le post cite vraiment (joueurs d'abord, puis clubs, puis la competition)."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", norm(post)) + " "
    tags = []
    for words, tag in TAGS_BY_KEYWORD:
        if any(f" {w} " in text or f" {w}s " in text for w in words) and tag not in tags:
            tags.append(tag)
    if comp_code in COMP_TAGS and COMP_TAGS[comp_code] not in tags:
        tags.append(COMP_TAGS[comp_code])
    return tags[:2]


_STATUS = []        # raisons "rien programme" du passage (affichees dans l'annotation GitHub)


def note(msg):
    log(msg)
    _STATUS.append(msg.strip())


_REJECTS = []       # derniers refus (affiches par --samples pour comprendre pourquoi un post est jete)


def generate(kind, instruction, facts, cfg, state, comp_code=None):
    """Un post valide (texte final avec hashtags) ou None. Jamais d'exception pour un simple refus de contenu."""
    ending = ("Termine par une question courte qui donne envie de repondre."
              if kind == "debate" or (state["counter"] % 3 == 0 and kind in ("news", "breaking", "reaction", "stat"))
              else "Pas de question finale : termine sur une phrase qui claque.")
    style = f"\nStyle du jour : {STYLES[state['counter'] % len(STYLES)]}." if kind in ("debate", "news") else ""
    user = (f"{instruction}\n{ending}{style}\n\nFAITS :\n" + "\n".join(f"- {f}" for f in facts))
    for attempt in range(2):
        try:
            out = llm_json([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
                           cfg, state)
        except (ValueError, json.JSONDecodeError) as e:
            log(f"   reponse inexploitable : {e}")
            continue
        post = re.sub(r"\s+", " ", str(out.get("post", ""))).strip().strip('"').strip("«» ").strip()
        why = local_check(post, facts, state)
        if why:
            log(f"   essai {attempt + 1} refuse ({why}) : {post[:90]}")
            _REJECTS.append(f"{why} : {post[:110]}")
            continue
        try:
            ok, raison = verify(post, facts, cfg, state)
        except (ValueError, json.JSONDecodeError) as e:
            log(f"   relecture inexploitable ({e}) : post jete par prudence")
            continue
        if not ok:
            log(f"   essai {attempt + 1} refuse par la relecture ({raison}) : {post[:90]}")
            _REJECTS.append(f"relecture ({raison}) : {post[:110]}")
            continue
        tags = hashtags_for(post, comp_code)
        return (post + ("\n\n" + " ".join(tags) if tags else "")).strip()
    return None


INSTR = {
    "reaction": "Ecris la reaction a chaud, juste apres le coup de sifflet final : accroche \"⏱️\", le score exact, "
                "puis ton avis de passionne (cite un buteur ou un fait du match seulement s'il est dans les faits).",
    "news": "Ecris un post d'actu sur cette info : va droit au fait en une ou deux phrases, cite le media "
            "(\"Selon ...\"), reformule avec tes mots, puis une courte reaction de supporter.",
    "breaking": "Ecris un post d'actu CHAUDE facon chaine info foot : commence par \"🚨\", l'info en une phrase nette, "
                "le media cite (\"Selon ...\"), conditionnel si c'est une piste ou une rumeur, puis une courte "
                "reaction qui claque.",
    "stat": "Ecris le post \"chiffre du jour\" : accroche \"📊\", mets en avant le joueur indique avec ses chiffres "
            "EXACTS des faits, puis un commentaire qui claque. Aucune comparaison, aucun record, aucun chiffre "
            "qui n'est pas dans les faits.",
    "history": "Ecris une anecdote \"il y a N ans\" : accroche \"📆\", raconte le fait en une ou deux phrases avec les "
               "infos exactes des faits, finis sur un clin d'oeil de supporter. N'ajoute aucun detail absent des faits.",
    "preview": "Ecris le post \"au programme\" : accroche \"🔥\" ou \"⚽\", les matchs avec leur heure (heure de Paris, "
               "telle qu'indiquee dans les faits), puis une phrase d'envie.",
    "recap": "Ecris le recap des resultats : accroche \"📋\", les scores exacts des faits, puis un mini avis de passionne.",
    "debate": "Lance un debat de supporters a partir de ce classement (n'invente aucune prediction chiffree).",
}


# ============================================================
# BUFFER
# ============================================================
SCHEDULED_Q = """
query Scheduled($input: PostsInput!, $after: String) {
  posts(first: 100, after: $after, input: $input) {
    edges { node { id dueAt } }
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


def gql(query, variables):
    if not BUFFER_API_KEY:
        raise Fatal("BUFFER_API_KEY manquant")
    headers = {"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"}
    r = None
    for attempt in range(2):
        r = requests.post(BUFFER_URL, headers=headers, json={"query": query, "variables": variables}, timeout=90)
        if r.status_code == 429 and attempt == 0:
            time.sleep(min(int(r.headers.get("Retry-After", "30") or 30), 120))
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"Buffer HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(f"Buffer GraphQL: {json.dumps(j['errors'])[:400]}")
    return j["data"]


def scheduled_times():
    """Heures (UTC) de TOUS les posts programmes du compte (videos, affiches, posts CM)."""
    after, out = None, []
    while True:
        inp = {"organizationId": BUFFER_ORG_ID, "filter": {"channelIds": [BUFFER_CHANNEL_ID], "status": ["scheduled"]}}
        data = gql(SCHEDULED_Q, {"input": inp, "after": after})["posts"]
        for e in data.get("edges") or []:
            due = e["node"].get("dueAt")
            if due:
                out.append(datetime.fromisoformat(due.replace("Z", "+00:00")))
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return out


def create_text_post(text, due_dt):
    inp = {"channelId": BUFFER_CHANNEL_ID, "schedulingType": "automatic", "mode": "customScheduled",
           "dueAt": due_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "text": text}
    res = gql(CREATE_POST_Q, {"input": inp})["createPost"]
    if res["__typename"] != "PostActionSuccess":
        raise RuntimeError(f"Buffer a refuse le post : {res.get('__typename')} - {res.get('message')}")
    return res["post"]["id"]


def in_quiet_hours(dt_utc, cfg):
    local = dt_utc.astimezone(PARIS).strftime("%H:%M")
    a, b = cfg["quiet_hours"]
    return (local >= a or local < b) if a > b else (a <= local < b)


def free_time(dt, cfg, busy, shifts=(0, 10, 20, 30, 40, 50, 60)):
    """Premier horaire libre a partir de dt (decalages en minutes vers plus tard) : a plus de slot_margin_min de
    tout autre post Buffer ET de tout creneau video theorique, hors heures calmes. None si rien n'est libre."""
    margin = timedelta(minutes=cfg["slot_margin_min"])
    for s in shifts:
        t = dt + timedelta(minutes=s)
        if in_quiet_hours(t, cfg):
            continue
        day = t.astimezone(PARIS).date()
        video = [hhmm_to_dt(d, h) for d in (day - timedelta(days=1), day, day + timedelta(days=1))
                 for h in cfg["video_slots"]]
        if all(abs(t - o) >= margin for o in busy) and all(abs(t - v) >= margin for v in video):
            return t
    return None


# ============================================================
# LOGIQUE PRINCIPALE
# ============================================================
def record(state, kind, text, due, post_id, source=""):
    state["posts"].append({"id": post_id, "kind": kind, "text": text, "due": due.strftime("%Y-%m-%dT%H:%MZ"),
                           "day": due.astimezone(PARIS).date().isoformat(), "source": source,
                           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    state["counter"] += 1


def posts_on(state, day_iso):
    return sum(1 for p in state["posts"] if p.get("day") == day_iso)


def run_reactions(now, cfg, state, busy, dry):
    today = now.astimezone(PARIS).date()
    if today < date.fromisoformat(cfg["start_date"]):
        return 0
    made = sum(1 for p in state["posts"] if p["kind"] == "reaction" and p["day"] == today.isoformat())
    if made >= cfg["max_reactions_per_day"]:
        return 0
    cands = [m for m in recent_finished(now) if str(m["id"]) not in state["reacted"] and reaction_priority(m) < 9]
    cands.sort(key=reaction_priority)
    created = 0
    for m in cands[: cfg["max_reactions_per_day"] - made]:
        facts = match_facts(m) + goal_facts(m)
        log(f"[reaction] {facts[0]}")
        state["reacted"].append(str(m["id"]))      # on ne retente jamais le meme match (cout)
        text = generate("reaction", INSTR["reaction"], facts, cfg, state, (m.get("competition") or {}).get("code"))
        if not text:
            log("   aucun post valide : on passe")
            continue
        due = free_time(now + timedelta(minutes=MIN_LEAD_MIN), cfg, busy, shifts=(0, 5, 10, 15, 20, 30, 40, 50, 60))
        if not due:
            log("   aucun horaire libre : on passe")
            continue
        created += publish(state, "reaction", text, due, busy, dry, facts[0])
    return created


STAR_DAILY_CAP = 2                  # au plus 2 posts par jour sur le meme joueur


def same_story(title, state):
    a = tokens(title)
    for t in state["used_titles"][-80:]:
        b = tokens(t)
        if a and b and len(a & b) / len(a | b) >= 0.4:
            return True
    return False


def star_counts_today(state, day_iso):
    counts = {}
    for p in state["posts"]:
        if p.get("day") == day_iso:
            for st in stars_in(p.get("text", "")):
                counts[st] = counts.get(st, 0) + 1
    return counts


def pick_news(now, state, min_score=1.0, max_age_h=None, day_iso=None):
    """Meilleure actu pas encore utilisee : gros joueur / grosse equipe, pas une histoire deja racontee,
    pas un joueur deja cite 2 fois aujourd'hui."""
    items, report = news_items(now)
    used = set(state["used_news"])
    counts = star_counts_today(state, day_iso or now.astimezone(PARIS).date().isoformat())
    for it in items:
        if it["key"] in used or it["score"] < min_score:
            continue
        if max_age_h is not None and it["age_h"] > max_age_h:
            continue
        if same_story(it["title"], state):
            continue
        if any(counts.get(st, 0) >= STAR_DAILY_CAP for st in stars_in(it["title"])):
            continue
        return it, report
    return None, report


# ---------- chiffre du jour : meilleurs buteurs (football-data) ----------
STAT_COMPS = ["PD", "FL1", "PL", "CL", "BL1", "SA"]


def scorers_facts(code, state):
    """(faits, joueur mis en avant) pour le classement des buteurs d'une competition, ou (None, None)."""
    data = fd_get(f"/competitions/{code}/scorers", {"limit": 10})
    rows = [r for r in (data.get("scorers") or []) if r.get("goals") is not None and (r.get("player") or {}).get("name")]
    if not rows:
        return None, None
    recent = set(state["recent_stats"][-8:])
    pick = None
    for r in rows:
        name = r["player"]["name"]
        if stars_in(name) and norm(name) not in recent:
            pick = r
            break
    if pick is None:
        for r in rows[:3]:
            if norm(r["player"]["name"]) not in recent and (r.get("team") or {}).get("id") in X_CLUBS:
                pick = r
                break
    if pick is None:
        return None, None

    def line(i, r):
        g, a, pm = r["goals"], r.get("assists"), r.get("playedMatches")
        out = f"{i}. {r['player']['name']} ({team_name(r.get('team') or {})}) : {g} but" + ("s" if g > 1 else "")
        if a:
            out += f", {a} passe" + ("s" if a > 1 else "") + " decisive" + ("s" if a > 1 else "")
        if pm:
            out += f" en {pm} match" + ("s" if pm > 1 else "")
        return out

    comp = COMP_NAMES.get(code, code)
    idx = rows.index(pick) + 1
    facts = [f"Joueur a mettre en avant : {pick['player']['name']} ({team_name(pick.get('team') or {})}), "
             f"{idx}e du classement des buteurs de {comp}",
             f"Classement des buteurs {comp} (saison en cours) :"] + [line(i, r) for i, r in enumerate(rows[:6], 1)]
    if idx > 6:
        facts.append(line(idx, pick))
    return facts, norm(pick["player"]["name"])


# ---------- anecdote : "il y a N ans" (Wikipedia, evenements du jour) ----------
MOIS = ["", "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre",
        "novembre", "decembre"]
FOOT_EVENT_RE = re.compile(r"\b(football|footballeur|coupe du monde|ligue des champions|ballon d or|championnat d europe|"
                           r"euro \d{4}|psg|paris saint germain|olympique de marseille|real madrid|fc barcelone|"
                           r"barcelone|manchester united|manchester city|liverpool|bayern|juventus|equipe de france|"
                           r"ligue 1|premier league|coupe de france|ligue europa|coupe de l uefa)\b")
OTHER_SPORT_RE = re.compile(r"\b(rugby|cricket|ski|handball|basket\w*|volley\w*|hockey|football americain|football australien|"
                            r"football gaelique|football canadien|futsal|tennis|cyclisme|tour de france)\b")


def history_facts(now, state):
    """(cle, faits) pour une anecdote football survenue le meme jour d'une annee passee, ou (None, None)."""
    d = now.astimezone(PARIS).date()
    r = requests.get(f"https://fr.wikipedia.org/api/rest_v1/feed/onthisday/events/{d.month:02d}/{d.day:02d}",
                     headers=UA, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Wikipedia HTTP {r.status_code}")
    used = set(state["used_news"])
    cands = []
    for ev in r.json().get("events", []):
        text, year = (ev.get("text") or "").strip(), ev.get("year")
        n = flat(text)
        if not text or not isinstance(year, int) or d.year - year < 1:
            continue
        if not FOOT_EVENT_RE.search(n) or OTHER_SPORT_RE.search(n) or SENSITIVE_RE.search(norm(text)):
            continue
        key = "h" + hashlib.sha1(f"{d.month}-{d.day}-{year}-{text}".encode()).hexdigest()[:10]
        if key in used:
            continue
        cands.append((year, key, text))
    if not cands:
        return None, None
    year, key, text = sorted(cands, reverse=True)[state["counter"] % len(cands)]
    n_years = d.year - year
    return key, [f"Aujourd'hui, {d.day} {MOIS[d.month]}", f"Il y a {n_years} ans, en {year} (meme jour) : {text}",
                 f"Ecart : {n_years} ans"]


# ---------- programme du jour / affiche du soir, recap des resultats ----------
def local_dt(m):
    return datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(PARIS)


def is_big(m):
    ids = {m["homeTeam"].get("id"), m["awayTeam"].get("id")}
    return bool(ids & X_CLUBS) or (m.get("competition") or {}).get("code") == "CL"


def fixtures_facts(now, whole_day):
    """Matchs a venir aujourd'hui (grosses equipes + Ligue des champions) : tout le jour, ou ceux qui commencent
    dans plus de 20 min. None s'il n'y en a pas."""
    today = now.astimezone(PARIS).date()
    data = fd_get("/matches", {"dateFrom": (today - timedelta(days=1)).isoformat(),
                               "dateTo": (today + timedelta(days=1)).isoformat(), "competitions": COMPETITIONS})
    ms = []
    for m in data.get("matches", []):
        if m.get("status") not in ("TIMED", "SCHEDULED") or not is_big(m):
            continue
        ko = local_dt(m)
        if ko.date() != today or (not whole_day and ko.astimezone(timezone.utc) < now + timedelta(minutes=20)):
            continue
        ms.append((reaction_priority(m), ko, m))
    if not ms:
        return None
    ms.sort(key=lambda x: (x[0], x[1]))
    top = sorted(ms[:4], key=lambda x: x[1])
    facts = [f"Programme du jour ({today.day} {MOIS[today.month]}), heures de Paris :"]
    for _, ko, m in top:
        comp = COMP_NAMES.get((m.get("competition") or {}).get("code"), "")
        facts.append(f"{ko.hour}h{ko.minute:02d} : {team_name(m['homeTeam'])} - {team_name(m['awayTeam'])} ({comp})")
    return facts


def results_facts(now):
    today = now.astimezone(PARIS).date()
    ms = [m for m in recent_finished(now, hours=20) if is_big(m) and local_dt(m).date() == today]
    if len(ms) < 2:
        return None
    ms.sort(key=lambda m: (reaction_priority(m), local_dt(m)))
    facts = [f"Resultats du jour ({today.day} {MOIS[today.month]}) :"]
    for m in ms[:5]:
        ft = (m.get("score") or {}).get("fullTime") or {}
        comp = COMP_NAMES.get((m.get("competition") or {}).get("code"), "")
        facts.append(f"{team_name(m['homeTeam'])} {ft.get('home')}-{ft.get('away')} {team_name(m['awayTeam'])} ({comp})")
    return facts


def goal_facts(m):
    """Buteurs du match si football-data les donne (sinon liste vide : le post se passe de buteurs)."""
    try:
        d = fd_get(f"/matches/{m['id']}")
    except RuntimeError:
        return []
    parts = []
    for g in (d.get("goals") or [])[:8]:
        who, mn = (g.get("scorer") or {}).get("name"), g.get("minute")
        if who and mn is not None:
            parts.append(f"{who} {mn}e")
    return ["Buteurs : " + ", ".join(parts)] if parts else []


DEBATE_COMPS = ["FL1", "CL", "PL", "PD"]

# type de post prefere selon l'heure du creneau (le suivant prend le relais si le premier n'a rien de bon)
SLOT_KINDS = {
    "08:05": ["preview", "news", "stat", "history"],
    "10:45": ["news", "stat", "history", "debate"],
    "13:15": ["news", "stat", "history", "debate"],
    "14:00": ["history", "stat", "news", "debate"],
    "15:00": ["stat", "news", "history", "debate"],
    "16:30": ["news", "stat", "debate", "history"],
    "19:15": ["preview", "news", "stat", "debate"],
    "20:15": ["preview", "news", "debate", "stat"],
    "22:35": ["recap", "news", "debate", "stat"],
}
DEFAULT_KINDS = ["news", "stat", "preview", "debate", "history"]


def build(kind, now, cfg, state, dry, hhmm="20:15", day_iso=None):
    """Prepare les faits d'un type de post. Renvoie {"facts", "instr", "code", "source", "after"} ou None."""
    if kind in ("news", "breaking"):
        it, _ = pick_news(now, state, min_score=2.0 if kind == "breaking" else 1.0,
                          max_age_h=cfg["breaking_max_age_h"] if kind == "breaking" else None, day_iso=day_iso)
        if not it:
            return None
        log(f"[{kind}] {it['source']} : {it['title'][:90]} (score {it['score']:.1f}, {it['age_h']:.0f} h)")
        if not dry:
            state["used_news"].append(it["key"])
            state["used_titles"].append(it["title"])
        return {"facts": news_facts(it), "instr": kind, "code": None, "source": it["source"], "after": None}
    if kind == "stat":
        offset = state["counter"] % len(STAT_COMPS)
        for code in (STAT_COMPS[offset:] + STAT_COMPS[:offset])[:3]:
            try:
                facts, who = scorers_facts(code, state)
            except RuntimeError as e:
                log(f"   buteurs {code} indisponibles ({e})")
                continue
            if facts:
                log(f"[stat] {COMP_NAMES[code]} : {facts[0]}")
                return {"facts": facts, "instr": "stat", "code": code, "source": f"football-data {code}",
                        "after": (lambda who=who: state["recent_stats"].append(who)) if not dry else None}
        return None
    if kind == "history":
        try:
            key, facts = history_facts(now, state)
        except (RuntimeError, requests.RequestException, ValueError) as e:
            log(f"   anecdote indisponible ({e})")
            return None
        if not facts:
            return None
        log(f"[anecdote] {facts[1][:100]}")
        if not dry:
            state["used_news"].append(key)
        return {"facts": facts, "instr": "history", "code": None, "source": "Wikipedia", "after": None}
    if kind == "preview":
        try:
            facts = fixtures_facts(now, whole_day=int(hhmm.split(":")[0]) < 12)
        except RuntimeError as e:
            log(f"   programme indisponible ({e})")
            return None
        if not facts:
            return None
        log(f"[programme] {facts[1]}")
        return {"facts": facts, "instr": "preview", "code": None, "source": "football-data", "after": None}
    if kind == "recap":
        try:
            facts = results_facts(now)
        except RuntimeError as e:
            log(f"   resultats indisponibles ({e})")
            return None
        if not facts:
            return None
        log(f"[recap] {facts[1]}")
        return {"facts": facts, "instr": "recap", "code": None, "source": "football-data", "after": None}
    if kind == "debate":
        n = state["counter"] % len(DEBATE_COMPS)
        for code in DEBATE_COMPS[n:] + DEBATE_COMPS[:n]:
            try:
                facts = standings_facts(code)
            except RuntimeError as e:
                log(f"   classement {code} indisponible ({e})")
                continue
            if facts:
                log(f"[debat] {COMP_NAMES[code]}")
                return {"facts": facts, "instr": "debate", "code": code, "source": f"football-data {code}", "after": None}
        return None
    return None


def make_slot_post(now, cfg, state, dry, hhmm="20:15", hours_ahead=0.0, day_iso=None):
    """Genere le post d'un creneau : essaie les types preferes pour cette heure, dans l'ordre."""
    kinds = list(SLOT_KINDS.get(hhmm, DEFAULT_KINDS))
    if hours_ahead > cfg["news_horizon_h"]:               # creneau lointain : l'actu serait deja froide
        kinds = [k for k in kinds if k != "news"] + ["news"]
    last = state["posts"][-1]["kind"] if state["posts"] else None
    if len(kinds) > 1 and kinds[0] == last:                # pas deux fois le meme type d'affilee
        kinds = kinds[1:] + kinds[:1]
    for kind in kinds:
        b = build(kind, now, cfg, state, dry, hhmm, day_iso)
        if not b:
            continue
        text = generate(b["instr"], INSTR[b["instr"]], b["facts"], cfg, state, b["code"])
        if text:
            if b["after"]:
                b["after"]()
            return kind, text, b["source"]
    return None, None, None


BREAKING_GAP_MIN = 60               # au moins 1 h entre deux posts "breaking"


def run_breaking(now, cfg, state, busy, dry):
    """Actu chaude (gros joueur / grosse equipe, moins de 4 h) : on la poste en quelques minutes, sans attendre un creneau."""
    today = now.astimezone(PARIS).date()
    if today < date.fromisoformat(cfg["start_date"]) or cfg["max_breaking_per_day"] <= 0:
        return 0
    if in_quiet_hours(now + timedelta(minutes=MIN_LEAD_MIN), cfg):
        return 0
    mine = [p for p in state["posts"] if p["kind"] == "breaking"]
    if sum(1 for p in mine if p["day"] == today.isoformat()) >= cfg["max_breaking_per_day"]:
        note("breaking : plafond du jour atteint")
        return 0
    if mine:
        last_at = datetime.strptime(mine[-1]["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now - last_at < timedelta(minutes=BREAKING_GAP_MIN):
            note("breaking : dernier il y a moins d'1 h")
            return 0
    due = free_time(now + timedelta(minutes=MIN_LEAD_MIN), cfg, busy, shifts=(0, 5, 10, 15, 20, 30, 40, 50, 60))
    if not due:
        note("breaking : aucun horaire libre dans l'heure (autres posts trop proches)")
        return 0
    b = build("breaking", now, cfg, state, dry)
    if not b:
        note("breaking : aucune actu chaude exploitable (score >= 2, < 4 h, pas deja traitee)")
        return 0
    text = generate("breaking", INSTR["breaking"], b["facts"], cfg, state)
    if not text:
        note("breaking : post refuse par les garde-fous (" + (_REJECTS[-1][:140] if _REJECTS else "?") + ")")
        return 0
    return publish(state, "breaking", text, due, busy, dry, b["source"])


def publish(state, kind, text, due, busy, dry, source=""):
    local = due.astimezone(PARIS).strftime("%a %d/%m %H:%M")
    if dry:
        log(f"   [simulation] {local} [{kind}] {text}")
        gh_notice(f"[CM simulation] {local} [{kind}] " + text.replace("\n", " | "))
        busy.append(due)
        return 1
    post_id = create_text_post(text, due)
    record(state, kind, text, due, post_id, source)
    busy.append(due)
    log(f"   programme {local} [{kind}] : {text}")
    return 1


def run_slots(now, cfg, state, busy, dry):
    today = now.astimezone(PARIS).date()
    created = 0
    for day in (today, today + timedelta(days=1)):
        for hhmm in phase_slots(day, cfg):
            key = f"{day.isoformat()}T{hhmm}"
            dt = hhmm_to_dt(day, hhmm)
            if key in state["slots"] or not (now + timedelta(minutes=MIN_LEAD_MIN) <= dt <= now + timedelta(hours=cfg["lookahead_h"])):
                continue
            if state["slot_tries"].get(key, 0) >= MAX_SLOT_TRIES:
                continue
            due = free_time(dt, cfg, busy)
            if not due:
                log(f"creneau {key} : aucun horaire libre, on passe")
                state["slots"][key] = "skipped"
                continue
            state["slot_tries"][key] = state["slot_tries"].get(key, 0) + 1
            log(f"creneau {key} (essai {state['slot_tries'][key]})")
            kind, text, source = make_slot_post(now, cfg, state, dry, hhmm=hhmm,
                                                hours_ahead=(dt - now).total_seconds() / 3600, day_iso=day.isoformat())
            if not text:
                log("   aucun post valide pour ce creneau, nouvel essai au prochain passage")
                continue
            created += publish(state, kind, text, due, busy, dry, source)
            if not dry:
                state["slots"][key] = "done"
    return created


def run(dry=False):
    cfg = load_config()
    if not cfg["enabled"] or os.environ.get("CM_DISABLED"):
        log("CM desactive (cm_config.json : enabled=false)")
        return 0
    state = load_state()
    now = datetime.now(timezone.utc)
    busy = scheduled_times()
    created = 0
    try:
        created += run_reactions(now, cfg, state, busy, dry)
        created += run_breaking(now, cfg, state, busy, dry)
        created += run_slots(now, cfg, state, busy, dry)
    except Fatal as e:
        gh_error(f"CM arrete : {e}")
        log(f"ERREUR : {e}")
        if not dry:
            save_state(state)
        return 2
    gh_notice(f"[CM] {'simulation : ' if dry else ''}{created} post(s) | {usage_text(state['usage'])}"
              + ("\n" + "\n".join(_STATUS[:8]) if _STATUS else ""))
    log(f"{created} post(s) | usage cumule : {usage_text(state['usage'])}")
    if not dry:
        save_state(state)
    return 0


def samples():
    """Un echantillon de chaque type de post, SANS rien programmer ni ecrire d'etat."""
    cfg = load_config()
    state = load_state()
    now = datetime.now(timezone.utc)
    lines = []
    try:
        ms = sorted([m for m in recent_finished(now, hours=72) if reaction_priority(m) < 9], key=reaction_priority)[:1]
        for m in ms:
            facts = match_facts(m) + goal_facts(m)
            t = generate("reaction", INSTR["reaction"], facts, cfg, state, (m.get("competition") or {}).get("code"))
            lines.append(f"REACTION [{' | '.join(facts)[:160]}] -> {t}")
        for kind in ("breaking", "news", "stat", "history", "preview", "recap", "debate"):
            state["counter"] += 1
            b = build(kind, now, cfg, state, True, hhmm="08:05" if kind == "preview" else "20:15")
            if not b:
                lines.append(f"{kind.upper()} : rien de disponible maintenant")
                continue
            t = generate(b["instr"], INSTR[b["instr"]], b["facts"], cfg, state, b["code"])
            lines.append(f"{kind.upper()} [{' | '.join(b['facts'])[:170]}] -> {t}")
            if not t and _REJECTS:
                lines.append("   refus : " + " || ".join(_REJECTS[-2:]))
    except Fatal as e:
        gh_error(f"CM : {e}")
        return 2
    lines.append(f"USAGE : {usage_text(state['usage'])}")
    for ln in lines:
        log(ln.replace("\n", " | "))
    for i in range(0, len(lines), 4):          # annotations de taille raisonnable
        gh_notice("[CM echantillons]\n" + "\n".join(ln.replace("\n", " | ") for ln in lines[i:i + 4]))
    return 0


def diag():
    cfg = load_config()
    state = load_state()
    notes = []
    for role in ("writer", "verifier"):
        try:
            notes.append(f"Modeles {role} (ordre d'essai) : {models_for(role, cfg)}")
            out = llm_json([{"role": "user", "content": "Reponds en JSON : {\"ok\": true, \"mot\": \"but\"}"}],
                           cfg, state, role=role, max_tokens=500)
            notes.append(f"  test {role} : {out} | modele retenu : {_OK.get(role)}")
        except Fatal as e:
            notes.append(f"  {role} : ERREUR FATALE {e}")
        except Exception as e:
            notes.append(f"  {role} : ERREUR {e}")
    if _FAILS:
        notes.append("Modeles refuses : " + " ; ".join(_FAILS))
    notes.append("Usage du test : " + usage_text(state["usage"]))
    now = datetime.now(timezone.utc)
    items, report = news_items(now)
    notes.append("Flux d'actu : " + " | ".join(report) + f" | exploitables : {len(items)}")
    for it in items[:6]:
        notes.append(f"  actu : [{it['source']}] {it['title'][:100]} (score {it['score']:.1f}, {it['age_h']:.0f} h)")
    notes.append(f"  actus chaudes (score >= 2, < {cfg['breaking_max_age_h']} h) : "
                 f"{sum(1 for it in items if it['score'] >= 2 and it['age_h'] <= cfg['breaking_max_age_h'])}")
    for label, fn in (("buteurs PD", lambda: scorers_facts("PD", state)[0]),
                      ("anecdote du jour", lambda: history_facts(now, state)[1]),
                      ("programme du jour", lambda: fixtures_facts(now, True)),
                      ("resultats du jour", lambda: results_facts(now))):
        try:
            r = fn()
            notes.append(f"{label} : " + (" | ".join(r)[:200] if r else "rien"))
        except Exception as e:
            notes.append(f"{label} : ERREUR {str(e)[:120]}")
    try:
        ms = recent_finished(now, hours=72)
        notes.append(f"football-data : {len(ms)} matchs termines (72 h) | " + " ; ".join(match_facts(m)[0] for m in ms[:4]))
        st = standings_facts("FL1", 3)
        notes.append("Classement L1 : " + (" ".join(st) if st else "indisponible"))
    except Exception as e:
        notes.append(f"football-data : ERREUR {e}")
    try:
        notes.append(f"Buffer : {len(scheduled_times())} posts programmes sur le canal")
    except Exception as e:
        notes.append(f"Buffer : ERREUR {e}")
    notes.append(f"Config : enabled={cfg['enabled']} depart={cfg['start_date']} phases={cfg['phases']}")
    for n in notes:
        log(n)
    gh_notice("[CM diagnostic]\n" + "\n".join(notes))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="CM monfootballfr")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--samples", action="store_true")
    ap.add_argument("--diag", action="store_true")
    args = ap.parse_args(argv)
    try:
        code = diag() if args.diag else samples() if args.samples else run(dry=args.dry_run)
    except Fatal as e:
        gh_error(f"CM : {e}")
        log(f"ERREUR : {e}")
        code = 2
    except (RuntimeError, requests.RequestException) as e:
        gh_error(f"CM : {e}")
        log(f"ERREUR : {e}")
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
