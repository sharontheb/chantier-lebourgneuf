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
     "cols_sub":  ["avancement", "echeances", "fournisseur"]
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
NOUVELLES = os.path.join(RACINE, "nouvelles.json")

API = "https://api.monday.com/v2"
# Quebec : UTC-5 de novembre a mars, periode du chantier
TZ = timezone(timedelta(hours=-5))


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


def interroger(c):
    """Une requete GraphQL, les seules colonnes utiles."""
    requete = """
    query {
      boards(ids: [%d]) {
        groups(ids: %s) {
          items_page(limit: 100) {
            items {
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

    boards = (rep.get("data") or {}).get("boards") or []
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
    sortie = []
    for it in items:
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


def nouvelles_propres(brut):
    out = []
    for m in brut or []:
        if not isinstance(m, dict):
            continue
        meds = []
        for x in m.get("medias") or []:
            if not isinstance(x, dict):
                continue
            src = x.get("src") if isinstance(x.get("src"), str) else ""
            if not re.fullmatch(r"medias/[A-Za-z0-9._-]{1,120}", src):
                continue
            meds.append({"src": src, "type": x.get("type") or ""})
        try:
            at = int(m.get("at") or 0)
        except (TypeError, ValueError):
            at = 0
        t = (m.get("texte") or "").strip()
        if not t and not meds:
            continue
        out.append({"qui": (m.get("qui") or "").strip() or "Sharon",
                    "texte": t, "at": at, "medias": meds})
    out.sort(key=lambda m: m["at"], reverse=True)
    return out[:60]


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
    try:
        with open(NOUVELLES, encoding="utf-8") as fh:
            fil = nouvelles_propres(json.load(fh))
    except (IOError, OSError, ValueError):
        fil = []

    maintenant = datetime.now(TZ)
    page = injecter(page, "DONNEES", donnees)
    page = injecter(page, "NOUVELLES", fil)
    page = injecter(page, "GENERE", int(maintenant.timestamp() * 1000))

    # garde-fou : rien de confidentiel ne doit atteindre la page publique
    interdits = [
        (r"\$\s?\d[\d\s,.]{2,}|\d[\d\s]{2,}[,.]\d{2}\s*\$", "un montant"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "une adresse courriel"),
        (r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b", "un numero de telephone"),
        (r"(?i)boards/\d{6,}|pulses/\d{6,}", "un lien Monday"),
    ]
    for motif, quoi in interdits:
        m = re.search(motif, page)
        if m:
            sys.exit("ARRET : la page contient %s (%r). Rien n'est publie."
                     % (quoi, m.group(0)[:40]))

    with open(SORTIE, "w", encoding="utf-8") as fh:
        fh.write(page)

    lots = len(donnees["items"])
    travaux = sum(len(i["s"]) or 1 for i in donnees["items"])
    print("index.html : %d lots, %d travaux, %d message(s), %.1f Ko, %s"
          % (lots, travaux, len(fil), len(page.encode()) / 1024,
             maintenant.strftime("%Y-%m-%d %H:%M")))


if __name__ == "__main__":
    main()
