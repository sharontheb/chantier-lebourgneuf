#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regenere index.html a partir du tableau Monday.

   Tourne dans GitHub Actions. Toute la configuration Monday (jeton, tableau,
   groupes, colonnes) vient de la variable d'environnement MONDAY_CONFIG, un
   JSON garde en secret du depot : rien de tout cela n'apparait dans le code
   publie.

   MONDAY_CONFIG = {
     "token": "...", "board": 123, "groupes": ["group_x"],
     "cols_item": ["avancement", "echeances", "fournisseur"],
     "cols_sub":  ["avancement", "echeances", "fournisseur"],
     "journal": 456,                 # element dont les mises a jour font le fil
     "journal_ignore": ["789"]       # mises a jour a ne pas publier (mode d'emploi)
   }
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

RACINE = os.path.dirname(os.path.abspath(__file__))
DEPOT = os.path.dirname(RACINE)
GABARIT = os.path.join(RACINE, "gabarit.html")
SORTIE = os.path.join(DEPOT, "index.html")
MEDIAS = os.path.join(DEPOT, "medias")

API = "https://api.monday.com/v2"
# Quebec : UTC-5 de novembre a mars, periode du chantier
TZ = timezone(timedelta(hours=-5))

MAX_MEDIA = 25 * 1024 * 1024      # 25 Mo par fichier
MAX_MESSAGES = 60
IMAGES = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"}
VIDEOS = {"mp4", "mov", "m4v", "webm"}
TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "gif": "image/gif", "webp": "image/webp", "heic": "image/heic",
         "heif": "image/heif", "mp4": "video/mp4", "mov": "video/quicktime",
         "m4v": "video/x-m4v", "webm": "video/webm"}


def config():
    brut = os.environ.get("MONDAY_CONFIG", "").strip()
    if not brut:
        sys.exit("MONDAY_CONFIG manquant : ajouter le secret au depot.")
    try:
        c = json.loads(brut)
    except ValueError as e:
        sys.exit("MONDAY_CONFIG illisible : %s" % e)
    for champ in ("token", "board", "groupes", "cols_item", "cols_sub"):
        if not c.get(champ):
            sys.exit("MONDAY_CONFIG : champ %s manquant." % champ)
    return c


def appeler(c, requete):
    corps = json.dumps({"query": requete}).encode("utf-8")
    req = urllib.request.Request(API, data=corps, method="POST", headers={
        "Authorization": c["token"],
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        rep = json.loads(r.read().decode("utf-8"))
    if rep.get("errors"):
        sys.exit("Monday a refuse la requete : %s" % json.dumps(rep["errors"])[:400])
    return rep.get("data") or {}


def interroger(c):
    """Une requete GraphQL, les seules colonnes utiles."""
    requete = """
    query {
      boards(ids: [%d]) {
        groups(ids: %s) {
          items_page(limit: 100) {
            items {
              id
              name
              column_values(ids: %s) { id text }
              subitems {
                name
                column_values(ids: %s) { id text }
              }
            }
          }
        }
      }
    }""" % (int(c["board"]),
            json.dumps(c["groupes"]),
            json.dumps(c["cols_item"]),
            json.dumps(c["cols_sub"]))

    boards = appeler(c, requete).get("boards") or []
    if not boards:
        sys.exit("Monday n'a retourne aucun tableau.")
    items = []
    for g in boards[0].get("groups") or []:
        items.extend(((g.get("items_page") or {}).get("items")) or [])
    if not items:
        sys.exit("Monday n'a retourne aucun element : rien n'est publie.")
    return items


def val(colonnes, cle):
    for cv in colonnes or []:
        if cv.get("id") == cle:
            t = cv.get("text")
            return t if isinstance(t, str) else ""
    return ""


def filtrer(items, c):
    """Reecrit en quatre champs neutres : n, a, d, f, plus s(ous-travaux).

    Ni id, ni url, ni id de colonne, ni mise a jour ne traversent. Les lots
    marques REFERENCE sont de l'archive interne et ne sortent pas.
    """
    av, dates, fourn = c["cols_item"]
    s_av, s_dates, s_fourn = c["cols_sub"]
    journal = str(c.get("journal") or "")
    sortie = []
    for it in items:
        if journal and str(it.get("id") or "") == journal:
            continue  # le journal alimente le fil, ce n'est pas un lot
        cv = it.get("column_values")
        etat = val(cv, av)
        if etat.strip().upper() in ("REFERENCE", "RÉFÉRENCE"):
            continue
        subs = []
        for s in it.get("subitems") or []:
            sv = s.get("column_values")
            subs.append({
                "n": s.get("name") or "",
                "a": val(sv, s_av),
                "d": val(sv, s_dates),
                "f": val(sv, s_fourn),
            })
        sortie.append({
            "n": it.get("name") or "",
            "a": etat,
            "d": val(cv, dates),
            "f": val(cv, fourn),
            "s": subs,
        })
    return {"items": sortie}


def horodatage(s):
    """'2026-11-23T22:41:07Z' -> millisecondes."""
    try:
        d = datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def rapatrier(url, chemin):
    """Telecharge un media de Monday, sans depasser la taille permise."""
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            taille = int(r.headers.get("Content-Length") or 0)
            if taille > MAX_MEDIA:
                return False
            donnees = r.read(MAX_MEDIA + 1)
    except Exception as e:
        print("  media ignore (%s)" % type(e).__name__)
        return False
    if len(donnees) > MAX_MEDIA:
        return False
    with open(chemin, "wb") as fh:
        fh.write(donnees)
    return True


def le_fil(c):
    """Les mises a jour de l'element Journal deviennent le fil de la page.

    Les photos et videos sont rapatriees dans medias/ : les liens Monday
    expirent, pas les fichiers du depot.
    """
    journal = c.get("journal")
    if not journal:
        return []
    ignores = {str(x) for x in (c.get("journal_ignore") or [])}

    data = appeler(c, """
    query {
      items(ids: [%d]) {
        updates(limit: %d) {
          id
          text_body
          created_at
          creator { name }
          assets { id public_url file_extension }
        }
      }
    }""" % (int(journal), MAX_MESSAGES))

    items = data.get("items") or []
    if not items:
        return []

    os.makedirs(MEDIAS, exist_ok=True)
    gardes = set()
    messages = []

    for u in items[0].get("updates") or []:
        if str(u.get("id") or "") in ignores:
            continue
        texte = (u.get("text_body") or "").strip()
        meds = []
        for a in u.get("assets") or []:
            ext = (a.get("file_extension") or "").lstrip(".").lower()
            if ext not in IMAGES and ext not in VIDEOS:
                continue
            aid = re.sub(r"[^0-9A-Za-z]", "", str(a.get("id") or ""))
            if not aid:
                continue
            nom = "%s.%s" % (aid, ext)
            chemin = os.path.join(MEDIAS, nom)
            if not os.path.exists(chemin):
                url = a.get("public_url")
                if not url or not rapatrier(url, chemin):
                    continue
            gardes.add(nom)
            meds.append({"src": "medias/" + nom, "type": TYPES.get(ext, "")})
        if not texte and not meds:
            continue
        messages.append({
            "qui": ((u.get("creator") or {}).get("name") or "Sharon").strip(),
            "texte": texte,
            "at": horodatage(u.get("created_at")),
            "medias": meds,
        })

    # menage : ce qui n'est plus publie ne reste pas dans le depot
    if os.path.isdir(MEDIAS):
        for nom in os.listdir(MEDIAS):
            if nom.startswith(".") or nom in gardes:
                continue
            try:
                os.remove(os.path.join(MEDIAS, nom))
            except OSError:
                pass

    messages.sort(key=lambda m: m["at"], reverse=True)
    return messages[:MAX_MESSAGES]


def pour_script(valeur):
    return (json.dumps(valeur, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c")
            .replace(" ", "\\u2028")
            .replace(" ", "\\u2029"))


def injecter(page, nom, valeur):
    motif = re.compile(r"/\*INJECT_" + nom + r"\*/.*?/\*FIN\*/", re.S)
    if not motif.search(page):
        sys.exit("marqueur INJECT_%s absent du gabarit" % nom)
    return motif.sub(lambda _: "/*INJECT_%s*/%s/*FIN*/" % (nom, pour_script(valeur)),
                     page, count=1)


def main():
    c = config()
    donnees = filtrer(interroger(c), c)

    with open(GABARIT, encoding="utf-8") as fh:
        page = fh.read()
    fil = le_fil(c)

    maintenant = datetime.now(TZ)
    page = injecter(page, "DONNEES", donnees)

    # Garde-fou : rien de confidentiel ne doit sortir du calendrier. Il porte
    # sur les donnees Monday, pas sur le fil : ce que Sharon ecrit lui-meme
    # dans le journal, il le publie sciemment, et un message ne doit jamais
    # bloquer la mise a jour du calendrier.
    interdits = [
        (r"\$\s?\d[\d\s,.]{2,}|\d[\d\s]{2,}[,.]\d{2}\s*\$", "un montant"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "une adresse courriel"),
        (r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b", "un numero de telephone"),
        (r"(?i)boards/\d{6,}|pulses/\d{6,}", "un lien Monday"),
    ]
    for motif, quoi in interdits:
        m = re.search(motif, page)
        if m:
            sys.exit("ARRET : le calendrier contient %s (%r). Rien n'est publie."
                     % (quoi, m.group(0)[:40]))

    # signale sans bloquer ce qui ressemble a une indiscretion dans le fil
    for msg in fil:
        for motif, quoi in interdits[:3]:
            if re.search(motif, msg["texte"]):
                print("  ATTENTION : un message du fil contient %s" % quoi)
                break

    page = injecter(page, "NOUVELLES", fil)
    page = injecter(page, "GENERE", int(maintenant.timestamp() * 1000))

    with open(SORTIE, "w", encoding="utf-8") as fh:
        fh.write(page)

    lots = len(donnees["items"])
    travaux = sum(len(i["s"]) or 1 for i in donnees["items"])
    print("index.html : %d lots, %d travaux, %d message(s), %.1f Ko, %s"
          % (lots, travaux, len(fil), len(page.encode()) / 1024,
             maintenant.strftime("%Y-%m-%d %H:%M")))


if __name__ == "__main__":
    main()
