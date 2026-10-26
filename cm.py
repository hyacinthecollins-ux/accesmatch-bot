#!/usr/bin/env python3
"""
CM monfootballfr : community manager foot automatique (X, via Buffer), ton "pote passionne".

Ce que fait le robot (toutes les 30 min, dans GitHub Actions) :
  1. REACTIONS : quand un match d'un des 7 clubs suivis (ou de Ligue des champions / Ligue 1) vient de finir,
     il ecrit une reaction a chaud et la programme dans les minutes qui suivent.
  2. CRENEAUX : aux heures prevues (chauffe progressive du compte), il ecrit soit un avis sur l'actu du jour
     (flux de medias francais, avec "Selon <media>"), soit un debat lance a partir d'un classement.
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
Variables : OPENAI_API_KEY, BUFFER_API_KEY, FOOTBALL_DATA_TOKEN (optionnelles : OPENAI_MODEL, BUFFER_CHANNEL_ID)
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
    "phases": [[7, ["20:15"]], [14, ["16:30", "20:15"]], [None, ["10:45", "16:30", "20:15"]]],
    "max_reactions_per_day": 2,
    "model": "",                    # vide = choix automatique du modele economique disponible
    "quiet_hours": ["00:30", "07:30"],
    "video_slots": ["09:15", "12:30", "18:30", "21:45"],
    "slot_margin_min": 30,          # jamais de post a moins de 30 min d'un autre post
}

MIN_LEAD_MIN = 10                   # un post doit etre programme au moins 10 min a l'avance
SLOT_LOOKAHEAD_H = 8                # on prepare un creneau au plus 8 h avant (les crons GitHub sont parfois en retard)
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
X_CLUBS = {524, 516, 81, 86, 64, 65, 5}     # PSG, OM, Barca, Real, Liverpool, City, Bayern (ids football-data.org)

# mots-cles -> hashtag (sur les faits, pas sur le texte de l'IA)
TAGS_BY_KEYWORD = [
    (("paris saint-germain", "psg", "paris sg"), "#PSG"),
    (("marseille", "olympique de marseille"), "#OM"),
    (("barcelone", "barcelona", "barca"), "#FCBarcelona"),
    (("real madrid",), "#RealMadrid"),
    (("liverpool",), "#Liverpool"),
    (("manchester city", "man city"), "#ManCity"),
    (("bayern",), "#FCBayern"),
    (("lyon",), "#OL"),
    (("monaco",), "#ASM"),
    (("lille",), "#LOSC"),
    (("equipe de france", "bleus"), "#EquipeDeFrance"),
]

# flux d'actu foot (les flux inaccessibles sont simplement ignores)
FEEDS = [
    ("L'Equipe", "https://dwh.lequipe.fr/api/edito/rss?path=/Football/"),
    ("RMC Sport", "https://rmcsport.bfmtv.com/rss/football/"),
    ("So Foot", "https://www.sofoot.com/rss.xml"),
    ("Foot Mercato", "https://www.footmercato.net/flux-rss"),
    ("Le Parisien", "https://feeds.leparisien.fr/leparisien/rss/sports/football"),
    ("Google Actualites", "https://news.google.com/rss/search?q=football+ligue+1+OR+PSG+OR+OM+when:1d&hl=fr&gl=FR&ceid=FR:fr"),
]

WATCH_RE = re.compile(r"\b(psg|paris|marseille|om|barca|barcelone|real madrid|liverpool|manchester city|bayern|"
                      r"ligue 1|ligue des champions|mbappe|dembele|yamal|haaland|messi|ronaldo|vinicius|bellingham|"
                      r"bleus|deschamps|clasico|classique|olise|doue)\b")

# sujets qu'on ne touche JAMAIS (actu sensible, paris sportifs, justice...) : normalises sans accents
SENSITIVE_RE = re.compile(
    r"\b(deces|decede\w*|mort\w*|mourir|deuil|hommage|obseques|cancer|suicide\w*|accident\w*|tragedie|drame\w*|"
    r"viol\w*|agress\w*|violence\w*|racis\w*|homophob\w*|antisemit\w*|terror\w*|attentat\w*|guerre\w*|"
    r"politique\w*|election\w*|proces|garde a vue|interpelle\w*|mis en examen|plainte\w*|dopage|dope\w*|"
    r"paris sportifs?|pronostic\w*|cotes?|bookmaker\w*|escroquer\w*|coma|hospitalis\w*|grave\w*|menace\w*|"
    r"insulte\w*|police|justice|tribunal|prison|drogue\w*|cannabis|deprime\w*|burn.?out)\b")

# mots interdits dans le post genere
BAD_RE = re.compile(
    r"\b(connard\w*|connasse\w*|salop\w*|pute\w*|encul\w*|merde\w*|putain\w*|nique\w*|fdp|ntm|batard\w*|"
    r"negre\w*|bougnoul\w*|mongol\w*|debile\w*|attarde\w*|handicape\w*|cancer|suicide\w*|mort|mourir|deces|tuer|"
    r"juif\w*|arabe\w*|noir\w*|singe\w*|nain)\b")

PRICES = {  # dollars par million de tokens (entree, sortie) : indicatif, pour le journal de cout
    "gpt-6-luna": (0.10, 0.50), "gpt-5-mini": (0.25, 2.00), "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60), "gpt-5-nano": (0.05, 0.40),
}
MODEL_PREFERENCE = ["gpt-6-luna", "gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini", "gpt-5-nano"]

STYLES = [
    "une question qui divise les supporters",
    "un avis tranche mais drole",
    "une petite vanne de supporter, bon enfant",
    "un constat qui fait reflechir, avec une question a la fin",
    "un enthousiasme sincere de passionne",
]

SYSTEM_PROMPT = (
    "Tu es la plume de @monfootballfr, un compte X francophone de foot tenu par un pote passionne, pas par un "
    "media corporate.\n"
    "Ton : tutoiement, direct, drole, avis tranches mais toujours respectueux. Jamais d'insulte, jamais de moquerie "
    "sur le physique, l'origine, la religion, une blessure, un deces ou une affaire judiciaire. On chambre avec "
    "humour, on ne rabaisse pas un joueur ou un club.\n"
    "REGLE D'OR : tu n'utilises QUE les FAITS fournis. Aucun score, chiffre, stat, transfert, blessure, citation ou "
    "date qui n'est pas dans les faits. Si tu n'es pas sur, pose une question au lieu d'affirmer.\n"
    "Si l'info vient d'un media, ecris \"Selon <media>\" et reformule avec tes propres mots (ne recopie jamais le "
    "titre), sans presenter une rumeur comme un fait etabli.\n"
    "Format : UN seul post de 80 a 230 caracteres, en francais, 0 a 2 emojis, AUCUN hashtag (ils sont ajoutes "
    "ensuite), aucun lien, aucune @mention, pas de guillemets autour du post. Finis de preference par une question "
    "courte qui donne envie de repondre.\n"
    "Reponds uniquement en JSON : {\"post\": \"...\"}"
)

VERIFIER_PROMPT = (
    "Tu es le relecteur strict d'un compte de foot automatique. On te donne des FAITS et un POST. "
    "Reponds uniquement en JSON : {\"ok\": true ou false, \"raison\": \"...\"}.\n"
    "ok=false si : le post contient un chiffre, un nom, une stat, un transfert, une blessure ou une citation absents "
    "des FAITS ; il presente une rumeur comme un fait ; il contient une insulte ou une moquerie sur le physique, "
    "l'origine, la religion, une blessure, un deces ou une affaire judiciaire ; il contient un lien ou une @mention ; "
    "il est incomprehensible ou hors sujet. Sinon ok=true."
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
                 ("counter", 0), ("usage", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})):
        st.setdefault(k, v)
    return st


def save_state(st):
    st["posts"] = st["posts"][-KEEP_POSTS:]
    st["reacted"] = st["reacted"][-300:]
    st["used_news"] = st["used_news"][-600:]
    st["slots"] = dict(sorted(st["slots"].items())[-120:])
    st["slot_tries"] = dict(sorted(st["slot_tries"].items())[-120:])
    STATE_FILE.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def norm(s):
    """minuscules sans accents (comparaisons)"""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


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
_MODEL = {"name": None, "list": None}


def _openai_headers():
    if not OPENAI_API_KEY:
        raise Fatal("OPENAI_API_KEY manquant")
    return {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}


def list_models():
    """Liste des modeles du compte. Une cle restreinte (sans droit de lecture des modeles) donne une liste vide :
    ce n'est pas bloquant, on essaie alors les modeles habituels directement."""
    if _MODEL["list"] is None:
        r = requests.get("https://api.openai.com/v1/models", headers=_openai_headers(), timeout=30)
        if r.status_code == 401:
            raise Fatal(f"OpenAI refuse la cle (HTTP 401) : {r.text[:200]}")
        _MODEL["list"] = sorted(m["id"] for m in r.json().get("data", [])) if r.status_code == 200 else []
        _MODEL["list_status"] = r.status_code
    return _MODEL["list"]


def candidate_models(cfg):
    forced = os.environ.get("OPENAI_MODEL") or cfg.get("model")
    if forced:
        return [forced]
    avail = set(list_models())
    if not avail:
        return list(MODEL_PREFERENCE)
    ordered = [m for m in MODEL_PREFERENCE if m in avail]
    if not ordered:
        ordered = [m for m in sorted(avail) if re.search(r"(luna|mini|nano)", m) and "audio" not in m
                   and "realtime" not in m and "transcribe" not in m and "tts" not in m][:3]
    return ordered or list(MODEL_PREFERENCE)


def llm_json(messages, cfg, state, max_tokens=2500):
    """Appel OpenAI (chat completions) qui renvoie un dict JSON. S'adapte aux parametres refuses par un modele
    (renomme ou retire ce que l'erreur 400 designe) et retient les reglages qui marchent pour les appels suivants."""
    models = [_MODEL["name"]] if _MODEL["name"] else candidate_models(cfg)
    last_err = None
    for model in models:
        extra = _MODEL.get("params") or {"reasoning_effort": "low", "response_format": {"type": "json_object"}}
        body = {"model": model, "messages": messages, "max_completion_tokens": max_tokens, **extra}
        if _MODEL.get("tokens_param") == "max_tokens":
            body["max_tokens"] = body.pop("max_completion_tokens")
        for _ in range(6):
            r = requests.post("https://api.openai.com/v1/chat/completions", headers=_openai_headers(),
                              json=body, timeout=120)
            if r.status_code == 200:
                j = r.json()
                u = j.get("usage") or {}
                st = state["usage"]
                st["calls"] += 1
                st["prompt_tokens"] += u.get("prompt_tokens", 0)
                st["completion_tokens"] += u.get("completion_tokens", 0)
                st["model"] = model
                _MODEL["name"] = model
                _MODEL["params"] = {k: v for k, v in body.items()
                                    if k in ("reasoning_effort", "response_format", "temperature")}
                _MODEL["tokens_param"] = "max_tokens" if "max_tokens" in body else "max_completion_tokens"
                txt = (j["choices"][0]["message"].get("content") or "").strip()
                m = re.search(r"\{.*\}", txt, re.S)
                if not m:
                    raise ValueError(f"reponse sans JSON : {txt[:120]!r} (fin: {j['choices'][0].get('finish_reason')})")
                return json.loads(m.group(0))
            msg = r.text[:600]
            if r.status_code == 403 and ("access to model" in msg or "model_not_found" in msg or "does not exist" in msg):
                last_err = f"{model} : acces refuse a ce modele"
                _MODEL.setdefault("fails", []).append(f"{model}: {msg[:120]}")
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
                _MODEL.setdefault("fails", []).append(f"{model}: {msg[:120]}")
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
        if title:
            items.append({"title": re.sub(r"\s+", " ", title).strip(), "source": source.strip(), "ts": ts})
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
            score = len(set(WATCH_RE.findall(n)))
            age_h = (now - it["ts"]).total_seconds() / 3600 if it["ts"] else 12
            it.update(key=key, score=score, age_h=age_h)
            out.append(it)
    out.sort(key=lambda it: (-min(it["score"], 3), it["age_h"]))
    return out, report


def news_facts(it):
    return [f"Source : {it['source']}", f"Titre publie par {it['source']} : {it['title']}",
            f"Publie il y a environ {max(1, round(it['age_h']))} h"]


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
                     + f"\n\nPOST :\n{post}"}], cfg, state, max_tokens=800)
    return bool(out.get("ok")), str(out.get("raison", ""))[:160]


def hashtags_for(facts, comp_code=None):
    text = norm(" ".join(facts))
    tags = []
    for words, tag in TAGS_BY_KEYWORD:
        if any(w in text for w in words) and tag not in tags:
            tags.append(tag)
    if comp_code in COMP_TAGS and COMP_TAGS[comp_code] not in tags:
        tags.append(COMP_TAGS[comp_code])
    return tags[:2]


def generate(kind, instruction, facts, cfg, state, comp_code=None):
    """Un post valide (texte final avec hashtags) ou None. Jamais d'exception pour un simple refus de contenu."""
    style = STYLES[state["counter"] % len(STYLES)]
    user = (f"{instruction}\nStyle du jour : {style}.\n\nFAITS :\n" + "\n".join(f"- {f}" for f in facts))
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
            continue
        try:
            ok, raison = verify(post, facts, cfg, state)
        except (ValueError, json.JSONDecodeError) as e:
            log(f"   relecture inexploitable ({e}) : post jete par prudence")
            continue
        if not ok:
            log(f"   essai {attempt + 1} refuse par la relecture ({raison}) : {post[:90]}")
            continue
        tags = hashtags_for(facts, comp_code)
        return (post + ("\n\n" + " ".join(tags) if tags else "")).strip()
    return None


INSTR = {
    "reaction": "Ecris la reaction a chaud d'un supporter juste apres la fin de ce match.",
    "news": "Ecris l'avis d'un supporter sur cette actu (cite la source avec \"Selon ...\", reformule, pose une question).",
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
        facts = match_facts(m)
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


def pick_news(now, state):
    items, report = news_items(now)
    used = set(state["used_news"])
    for it in items:
        if it["key"] not in used:
            return it, report
    return None, report


DEBATE_COMPS = ["FL1", "CL", "PL", "PD"]


def make_slot_post(now, cfg, state, dry):
    """Choisit le type de post du creneau (alternance actu / debat, avec repli) et le genere."""
    order = ["news", "debate"] if state["counter"] % 2 == 0 else ["debate", "news"]
    for kind in order:
        if kind == "news":
            it, _ = pick_news(now, state)
            if not it:
                continue
            log(f"[actu] {it['source']} : {it['title'][:90]}")
            if not dry:
                state["used_news"].append(it["key"])
            facts = news_facts(it)
            text = generate("news", INSTR["news"], facts, cfg, state)
            if text:
                return kind, text, it["source"]
        else:
            comps = DEBATE_COMPS[state["counter"] % len(DEBATE_COMPS):] + DEBATE_COMPS[:state["counter"] % len(DEBATE_COMPS)]
            for code in comps:
                try:
                    facts = standings_facts(code)
                except RuntimeError as e:
                    log(f"   classement {code} indisponible ({e})")
                    continue
                if not facts:
                    continue
                log(f"[debat] {COMP_NAMES[code]}")
                text = generate("debate", INSTR["debate"], facts, cfg, state, code)
                if text:
                    return kind, text, f"football-data {code}"
                break
    return None, None, None


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
            if key in state["slots"] or not (now + timedelta(minutes=MIN_LEAD_MIN) <= dt <= now + timedelta(hours=SLOT_LOOKAHEAD_H)):
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
            kind, text, source = make_slot_post(now, cfg, state, dry)
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
        created += run_slots(now, cfg, state, busy, dry)
    except Fatal as e:
        gh_error(f"CM arrete : {e}")
        log(f"ERREUR : {e}")
        if not dry:
            save_state(state)
        return 2
    u = state["usage"]
    price = PRICES.get(u.get("model", ""), None)
    cost = f" (~{(u['prompt_tokens'] * price[0] + u['completion_tokens'] * price[1]) / 1e6:.3f} $ cumules)" if price else ""
    gh_notice(f"[CM] {'simulation : ' if dry else ''}{created} post(s) | {u['calls']} appels IA cumules, "
              f"modele {u.get('model', '?')}{cost}")
    log(f"{created} post(s) | usage cumule : {u}")
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
        ms = sorted([m for m in recent_finished(now, hours=72) if reaction_priority(m) < 9], key=reaction_priority)[:2]
        for m in ms:
            facts = match_facts(m)
            t = generate("reaction", INSTR["reaction"], facts, cfg, state, (m.get("competition") or {}).get("code"))
            lines.append(f"REACTION [{facts[0]}] -> {t}")
        items, report = news_items(now)
        lines.append("FLUX : " + " | ".join(report))
        for it in items[:2]:
            t = generate("news", INSTR["news"], news_facts(it), cfg, state)
            lines.append(f"ACTU [{it['source']} : {it['title'][:80]}] -> {t}")
        for code in ("FL1", "CL"):
            facts = standings_facts(code)
            if facts:
                t = generate("debate", INSTR["debate"], facts, cfg, state, code)
                lines.append(f"DEBAT [{COMP_NAMES[code]}] -> {t}")
    except Fatal as e:
        gh_error(f"CM : {e}")
        return 2
    u = state["usage"]
    lines.append(f"USAGE : {u}")
    for ln in lines:
        log(ln.replace("\n", " | "))
    gh_notice("[CM echantillons]\n" + "\n".join(ln.replace("\n", " | ") for ln in lines))
    return 0


def diag():
    cfg = load_config()
    state = load_state()
    notes = []
    try:
        models = list_models()
        notes.append(f"OpenAI liste des modeles : HTTP {_MODEL.get('list_status')} | {len(models)} modeles | gpt* : "
                     + ", ".join(m for m in models if m.startswith("gpt"))[:700])
        cands = candidate_models(cfg)
        notes.append(f"Modeles essayes dans l'ordre : {cands}")
        out = llm_json([{"role": "user", "content": "Reponds en JSON : {\"ok\": true, \"mot\": \"but\"}"}], cfg, state, 300)
        notes.append(f"Test IA : {out} | modele retenu : {_MODEL['name']} | usage : {state['usage']}")
        if _MODEL.get("fails"):
            notes.append("Modeles refuses : " + " ; ".join(_MODEL["fails"]))
    except Fatal as e:
        notes.append(f"OpenAI : ERREUR FATALE {e}")
    except Exception as e:
        notes.append(f"OpenAI : ERREUR {e}")
    now = datetime.now(timezone.utc)
    items, report = news_items(now)
    notes.append("Flux d'actu : " + " | ".join(report) + f" | exploitables : {len(items)}")
    for it in items[:4]:
        notes.append(f"  actu : [{it['source']}] {it['title'][:100]} (score {it['score']}, {it['age_h']:.0f} h)")
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
