"""
AccesMatch — rendu d'affiche "logos officiels + VS" (sans IA, gratuit, toujours identique).

Les logos des clubs viennent de football-data.org (champ "crest" de chaque équipe).
Ils sont téléchargés une seule fois puis gardés dans le dossier crests/ du dépôt.
Si un logo est introuvable, une pastille avec les initiales du club le remplace (l'affiche sort quand même).
"""
import colorsys
import io
import os
from functools import lru_cache
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1350                      # format 4:5, bien affiché dans le fil X et sur Telegram
ROOT = Path(__file__).resolve().parent
FONT_PATHS = [ROOT / "fonts" / "Poppins-Bold.ttf",
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
              Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")]
CREST_DIR = Path(os.environ.get("CREST_DIR") or ROOT / "crests")

GOLD_TOP = (255, 241, 176)
GOLD_BOTTOM = (245, 176, 20)
BG_TOP = (4, 7, 16)
BG_BOTTOM = (12, 20, 44)
FOOTER = "@accesmatch"


# ------------------------------------------------------------
# Polices
# ------------------------------------------------------------
@lru_cache(maxsize=64)
def font(size):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def fit_font(text, max_w, start, minimum=26):
    size = start
    while size > minimum and font(size).getlength(text) > max_w:
        size -= 2
    return font(size)


def spaced_width(text, fnt, spacing):
    return sum(fnt.getlength(c) for c in text) + spacing * max(len(text) - 1, 0)


def draw_spaced(draw, text, fnt, cx, cy, spacing, fill):
    x = cx - spaced_width(text, fnt, spacing) / 2
    for c in text:
        draw.text((x, cy), c, font=fnt, fill=fill, anchor="lm")
        x += fnt.getlength(c) + spacing


# ------------------------------------------------------------
# Logos
# ------------------------------------------------------------
def _decode(data, url):
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        return Image.open(io.BytesIO(data)).convert("RGBA")
    if url.lower().endswith(".svg") or b"<svg" in data[:3000].lower():
        try:
            import cairosvg
            png = cairosvg.svg2png(bytestring=data, output_width=700)
            return Image.open(io.BytesIO(png)).convert("RGBA")
        except Exception:
            return None
    return None


def _trim(img):
    box = img.split()[3].getbbox()
    return img.crop(box) if box else img


def load_crest(team):
    """team = {"id": int|None, "crest_urls": [..], "short": "PSG"} → image RGBA ou None."""
    tid = team.get("id")
    cache = CREST_DIR / f"{tid}.png" if tid else None
    if cache and cache.exists():
        try:
            return _trim(Image.open(cache).convert("RGBA"))
        except Exception:
            pass
    for url in team.get("crest_urls") or []:
        if not url:
            continue
        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (accesmatch-bot)"})
            if r.status_code != 200 or not r.content:
                continue
            img = _decode(r.content, url)
            if img is None:
                continue
            img = _trim(img)
            if cache:
                CREST_DIR.mkdir(parents=True, exist_ok=True)
                img.save(cache)
            return img
        except Exception:
            continue
    return None


def monogram(team, color):
    """Pastille de secours avec les initiales du club."""
    size = 600
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([10, 10, size - 10, size - 10], fill=color + (255,), outline=(255, 255, 255, 255), width=18)
    words = [w for w in (team.get("short") or "?").replace("-", " ").split() if w]
    letters = ("".join(w[0] for w in words[:3]) if len(words) > 1 else (words[0][:3] if words else "?")).upper()
    d.text((size / 2, size / 2), letters, font=fit_font(letters, 420, 240), fill=(255, 255, 255, 255), anchor="mm")
    return img


def dominant_color(img):
    """Couleur marquante du logo (pour l'éclairage de l'affiche)."""
    small = img.copy()
    small.thumbnail((64, 64))
    buckets = {}
    for r, g, b, a in small.getdata():
        if a < 200:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.35 or v < 0.25 or v > 0.97:
            continue
        key = int(h * 24) % 24
        w = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        w[0] += s
        w[1] += h * s
        w[2] += v * s
        w[3] += 1
    if not buckets:
        return (40, 90, 220)
    best = max(buckets.values(), key=lambda w: w[0])
    h, v = best[1] / best[0], best[2] / best[0]
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, 0.85), max(0.75, min(1.0, v)))
    return (int(r * 255), int(g * 255), int(b * 255))


# ------------------------------------------------------------
# Briques graphiques
# ------------------------------------------------------------
def vertical_gradient(size, top, bottom):
    mask = Image.linear_gradient("L").resize(size)
    return Image.composite(Image.new("RGB", size, bottom), Image.new("RGB", size, top), mask)


def glow(canvas, center, radius, color, alpha):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    cx, cy = center
    ImageDraw.Draw(layer).ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color + (alpha,))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius * 0.5)))


def streaks(canvas):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x, w, a in ((-100, 70, 14), (160, 28, 10), (700, 90, 12), (930, 36, 14), (420, 18, 8)):
        d.polygon([(x + 420, 0), (x + 420 + w, 0), (x - 380 + w, H), (x - 380, H)], fill=(255, 255, 255, a))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(3)))


def gradient_text(canvas, text, fnt, center, top=GOLD_TOP, bottom=GOLD_BOTTOM, shadow=True, stroke=0):
    cx, cy = center
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).text((cx, cy), text, font=fnt, fill=255, anchor="mm", stroke_width=stroke)
    box = mask.getbbox()
    if not box:
        return
    if shadow:
        sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sh.paste((0, 0, 0, 170), (0, 12), mask)
        canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)))
    grad = vertical_gradient((canvas.width, box[3] - box[1]), top, bottom)
    fill = Image.new("RGB", canvas.size, bottom)
    fill.paste(grad, (0, box[1]))
    canvas.paste(fill, (0, 0), mask)


def place_crest(canvas, img, center, box, color):
    cx, cy = center
    # disque derrière le logo (permet de voir aussi les logos sombres)
    disc = box + 70
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([cx - disc / 2, cy - disc / 2, cx + disc / 2, cy + disc / 2],
              fill=(255, 255, 255, 26), outline=color + (150,), width=5)
    canvas.alpha_composite(layer)
    crest = img.copy()
    crest.thumbnail((box, box), Image.LANCZOS)
    x, y = int(cx - crest.width / 2), int(cy - crest.height / 2)
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    alpha = crest.split()[3].point(lambda v: int(v * 0.6))
    shadow.paste((0, 0, 0, 255), (x, y + 14), alpha)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))
    canvas.alpha_composite(crest, (x, y))


# ------------------------------------------------------------
# Affiche
# ------------------------------------------------------------
def render_poster(out_path, kind, home, away, date_text, comp_text, time_text=None, score=None):
    """
    kind : "preview" (logos + VS + heure) ou "result" (logos + score final).
    home / away : {"id", "short", "crest_urls"}.
    date_text : "SAMEDI 10 OCTOBRE" — comp_text : "PREMIER LEAGUE" — time_text : "20H45" — score : (2, 1)
    """
    sides = []
    for team in (home, away):
        img = load_crest(team)
        color = dominant_color(img) if img is not None else (40, 90, 220)
        if img is None:
            if not os.environ.get("POSTER_ALLOW_FALLBACK"):
                # Pas de logo officiel = pas d'affiche (on réessaie au prochain passage, 30 min plus tard)
                raise RuntimeError(f"logo officiel introuvable pour {team.get('short')} (id {team.get('id')})")
            print(f"⚠️ logo introuvable pour {team.get('short')}, pastille de secours utilisée", flush=True)
            img = monogram(team, color)
        sides.append((img, color))
    (home_img, home_col), (away_img, away_col) = sides

    canvas = vertical_gradient((W, H), BG_TOP, BG_BOTTOM).convert("RGBA")
    glow(canvas, (250, 540), 420, home_col, 150)
    glow(canvas, (830, 540), 420, away_col, 150)
    glow(canvas, (540, 1040), 520, (245, 176, 20), 38)
    streaks(canvas)

    d = ImageDraw.Draw(canvas)

    # --- haut : date + compétition
    draw_spaced(d, date_text, font(44), W / 2, 112, 5, (255, 255, 255, 235))
    comp_font = fit_font(comp_text, 700, 36)
    cw = spaced_width(comp_text, comp_font, 7) + 90
    pill = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle([W / 2 - cw / 2, 168, W / 2 + cw / 2, 238], radius=35,
                                           fill=(255, 255, 255, 16), outline=GOLD_BOTTOM + (255,), width=3)
    canvas.alpha_composite(pill)
    d = ImageDraw.Draw(canvas)
    draw_spaced(d, comp_text, comp_font, W / 2, 203, 7, GOLD_TOP + (255,))

    # --- logos
    place_crest(canvas, home_img, (250, 540), 290, home_col)
    place_crest(canvas, away_img, (830, 540), 290, away_col)
    d = ImageDraw.Draw(canvas)
    gradient_text(canvas, "VS", font(130), (540, 545), stroke=3)

    # --- noms
    d = ImageDraw.Draw(canvas)
    for name, cx in ((home["short"], 250), (away["short"], 830)):
        f = fit_font(name, 410, 56)
        d.text((cx, 800), name, font=f, fill=(255, 255, 255, 255), anchor="mm")
        d.rounded_rectangle([cx - 40, 842, cx + 40, 849], radius=3, fill=GOLD_BOTTOM + (255,))

    # --- bas : heure ou score
    if kind == "preview":
        label, hero = "COUP D'ENVOI", (time_text or "").upper()
        sub = "HEURE DE PARIS"
        hero_font = font(200)
    else:
        label, hero = "SCORE FINAL", f"{score[0]} - {score[1]}"
        sub = ""
        hero_font = font(230)
    draw_spaced(d, label, font(36), W / 2, 930, 10, (200, 210, 235, 255))
    gradient_text(canvas, hero, hero_font, (540, 1075), stroke=3)
    d = ImageDraw.Draw(canvas)
    if sub:
        draw_spaced(d, sub, font(28), W / 2, 1195, 8, (150, 165, 200, 255))

    # --- pied
    d.line([(390, 1262), (690, 1262)], fill=(255, 255, 255, 50), width=2)
    draw_spaced(d, FOOTER, font(34), W / 2, 1305, 4, (170, 182, 210, 255))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92, optimize=True)
    return out_path
