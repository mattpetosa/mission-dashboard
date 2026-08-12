"""Bake a naked-eye star catalog + constellation stick figures into one JS asset.

Run once at build time (needs network + the packages below); the output is
committed as a static asset, so a visitor's browser never talks to a star
catalog service. Two independent, public data sources feed this, kept
separate on purpose:

  - Positions and magnitudes come from the Hipparcos catalog (ESA, 1997;
    public domain), fetched via `skyfield.data.hipparcos` -- real astrometry,
    not typed from memory.
  - Which stars belong to which constellation, and which pairs of them draw a
    recognizable stick figure, is *this file's* own editorial choice (the
    traditional shapes -- Big Dipper, Orion's belt, and so on), resolved to
    Hipparcos numbers via CDS's Sesame name resolver so the right star gets
    picked. No third-party stick-figure dataset is used or copied.

    pip install skyfield pandas
    python3 tools/build_stars.py
"""

import json
import re
import time
import urllib.parse
import urllib.request

from skyfield.api import load
from skyfield.data import hipparcos

OUT = "www/assets/stars.js"

SESAME = "https://cdsweb.u-strasbg.fr/cgi-bin/nph-sesame/-oI/SNV?"

# name -> (bayer designation for Sesame, display name or None for unlabeled)
# Each constellation: (display name, [(key, bayer, proper_name_or_None), ...], [(key, key), ...] lines)
CONSTELLATIONS = [
    ("Ursa Major", [
        ("dubhe", "alf UMa", "Dubhe"), ("merak", "bet UMa", "Merak"),
        ("phecda", "gam UMa", "Phecda"), ("megrez", "del UMa", "Megrez"),
        ("alioth", "eps UMa", "Alioth"), ("mizar", "zet UMa", "Mizar"),
        ("alkaid", "eta UMa", "Alkaid"),
    ], [("dubhe", "merak"), ("merak", "phecda"), ("phecda", "megrez"), ("megrez", "dubhe"),
        ("megrez", "alioth"), ("alioth", "mizar"), ("mizar", "alkaid")]),

    ("Ursa Minor", [
        ("polaris", "alf UMi", "Polaris"), ("yildun", "del UMi", None),
        ("epsumi", "eps UMi", None), ("zetumi", "zet UMi", None),
        ("kochab", "bet UMi", "Kochab"), ("pherkad", "gam UMi", "Pherkad"),
        ("etumi", "eta UMi", None),
    ], [("polaris", "yildun"), ("yildun", "epsumi"), ("epsumi", "zetumi"),
        ("zetumi", "kochab"), ("kochab", "pherkad"), ("zetumi", "etumi")]),

    ("Cassiopeia", [
        ("caph", "bet Cas", "Caph"), ("schedar", "alf Cas", "Schedar"),
        ("tsih", "gam Cas", "Tsih"), ("ruchbah", "del Cas", "Ruchbah"),
        ("segin", "eps Cas", "Segin"),
    ], [("caph", "schedar"), ("schedar", "tsih"), ("tsih", "ruchbah"), ("ruchbah", "segin")]),

    ("Orion", [
        ("betelgeuse", "alf Ori", "Betelgeuse"), ("bellatrix", "gam Ori", "Bellatrix"),
        ("mintaka", "del Ori", "Mintaka"), ("alnilam", "eps Ori", "Alnilam"),
        ("alnitak", "zet Ori", "Alnitak"), ("saiph", "kap Ori", "Saiph"),
        ("rigel", "bet Ori", "Rigel"),
    ], [("betelgeuse", "bellatrix"), ("bellatrix", "mintaka"), ("mintaka", "alnilam"),
        ("alnilam", "alnitak"), ("alnitak", "saiph"), ("saiph", "rigel"),
        ("rigel", "mintaka"), ("betelgeuse", "alnitak")]),

    ("Scorpius", [
        ("beta1sco", "bet1 Sco", None), ("delsco", "del Sco", "Dschubba"),
        ("pisco", "pi Sco", None), ("sigsco", "sig Sco", None),
        ("antares", "alf Sco", "Antares"), ("tausco", "tau Sco", None),
        ("epssco", "eps Sco", None), ("musco", "mu1 Sco", None),
        ("zetsco", "zet2 Sco", None), ("etasco", "eta Sco", None),
        ("thetasco", "tet Sco", "Sargas"), ("iotasco", "iot1 Sco", None),
        ("kapsco", "kap Sco", None), ("lamsco", "lam Sco", "Shaula"),
    ], [("beta1sco", "delsco"), ("delsco", "pisco"), ("pisco", "sigsco"),
        ("sigsco", "antares"), ("antares", "tausco"), ("tausco", "epssco"),
        ("epssco", "musco"), ("musco", "zetsco"), ("zetsco", "etasco"),
        ("etasco", "thetasco"), ("thetasco", "iotasco"), ("iotasco", "kapsco"),
        ("kapsco", "lamsco")]),

    ("Crux", [
        ("acrux", "alf Cru", "Acrux"), ("gacrux", "gam Cru", "Gacrux"),
        ("mimosa", "bet Cru", "Mimosa"), ("delcru", "del Cru", None),
    ], [("acrux", "gacrux"), ("mimosa", "delcru")]),

    ("Cygnus", [
        ("deneb", "alf Cyg", "Deneb"), ("sadr", "gam Cyg", "Sadr"),
        ("albireo", "bet Cyg", "Albireo"), ("delcyg", "del Cyg", None),
        ("epscyg", "eps Cyg", "Gienah"),
    ], [("deneb", "sadr"), ("sadr", "albireo"), ("delcyg", "sadr"), ("sadr", "epscyg")]),

    ("Leo", [
        ("regulus", "alf Leo", "Regulus"), ("etaleo", "eta Leo", None),
        ("algieba", "gam Leo", "Algieba"), ("zetleo", "zet Leo", "Adhafera"),
        ("mu leo", "mu Leo", "Rasalas"), ("epsleo", "eps Leo", "Ras Elased"),
        ("denebola", "bet Leo", "Denebola"), ("zosma", "del Leo", "Zosma"),
        ("chertan", "tet Leo", "Chertan"),
    ], [("epsleo", "mu leo"), ("mu leo", "zetleo"), ("zetleo", "algieba"),
        ("algieba", "etaleo"), ("etaleo", "regulus"),
        ("algieba", "zosma"), ("zosma", "chertan"), ("chertan", "denebola")]),

    ("Taurus", [
        ("aldebaran", "alf Tau", "Aldebaran"), ("elnath", "bet Tau", "Elnath"),
        ("zettau", "zet Tau", None), ("epstau", "eps Tau", "Ain"),
    ], [("epstau", "aldebaran"), ("aldebaran", "zettau"), ("zettau", "elnath")]),

    ("Gemini", [
        ("castor", "alf Gem", "Castor"), ("pollux", "bet Gem", "Pollux"),
        ("alhena", "gam Gem", "Alhena"), ("wasat", "del Gem", "Wasat"),
        ("mebsuta", "eps Gem", "Mebsuta"), ("tejat", "mu Gem", "Tejat"),
    ], [("castor", "pollux"), ("pollux", "wasat"), ("castor", "mebsuta"),
        ("wasat", "mebsuta"), ("mebsuta", "tejat"), ("tejat", "alhena")]),

    ("Canis Major", [
        ("sirius", "alf CMa", "Sirius"), ("mirzam", "bet CMa", "Mirzam"),
        ("wezen", "del CMa", "Wezen"), ("adhara", "eps CMa", "Adhara"),
        ("aludra", "eta CMa", "Aludra"),
    ], [("mirzam", "sirius"), ("sirius", "adhara"), ("adhara", "wezen"), ("wezen", "aludra")]),

    ("Lyra", [
        ("vega", "alf Lyr", "Vega"), ("sheliak", "bet Lyr", "Sheliak"),
        ("sulafat", "gam Lyr", "Sulafat"), ("dellyr", "del Lyr", None),
        ("zetlyr", "zet01 Lyr", None),
    ], [("vega", "zetlyr"), ("zetlyr", "sheliak"), ("sheliak", "sulafat"),
        ("sulafat", "dellyr"), ("dellyr", "zetlyr")]),

    ("Aquila", [
        ("altair", "alf Aql", "Altair"), ("tarazed", "gam Aql", "Tarazed"),
        ("alshain", "bet Aql", "Alshain"), ("delaql", "del Aql", None),
        ("lamaql", "lam Aql", None), ("zetaql", "zet Aql", None),
    ], [("tarazed", "altair"), ("altair", "alshain"), ("altair", "delaql"),
        ("delaql", "lamaql"), ("delaql", "zetaql")]),

    ("Bootes", [
        ("arcturus", "alf Boo", "Arcturus"), ("izar", "eps Boo", "Izar"),
        ("nekkar", "bet Boo", "Nekkar"), ("seginus", "gam Boo", "Seginus"),
        ("muphrid", "eta Boo", "Muphrid"), ("delboo", "del Boo", None),
    ], [("muphrid", "arcturus"), ("arcturus", "izar"), ("izar", "delboo"),
        ("delboo", "nekkar"), ("nekkar", "seginus"), ("seginus", "izar")]),

    ("Pegasus", [
        ("markab", "alf Peg", "Markab"), ("scheat", "bet Peg", "Scheat"),
        ("algenib", "gam Peg", "Algenib"), ("alpheratz", "alf And", "Alpheratz"),
    ], [("markab", "scheat"), ("scheat", "alpheratz"), ("alpheratz", "algenib"), ("algenib", "markab")]),

    ("Andromeda", [
        ("alpheratz2", "alf And", "Alpheratz"), ("mirach", "bet And", "Mirach"),
        ("almach", "gam And", "Almach"), ("delta and", "del And", None),
    ], [("alpheratz2", "delta and"), ("delta and", "mirach"), ("mirach", "almach")]),

    ("Perseus", [
        ("mirfak", "alf Per", "Mirfak"), ("algol", "bet Per", "Algol"),
        ("gamper", "gam Per", None), ("delper", "del Per", None),
        ("epsper", "eps Per", None), ("zetper", "zet Per", None),
        ("rhoper", "rho Per", None),
    ], [("algol", "rhoper"), ("algol", "mirfak"), ("mirfak", "gamper"),
        ("mirfak", "delper"), ("delper", "epsper"), ("epsper", "zetper")]),

    ("Auriga", [
        ("capella", "alf Aur", "Capella"), ("menkalinan", "bet Aur", "Menkalinan"),
        ("theaur", "tet Aur", None), ("iotaur", "iot Aur", None),
        ("delaur", "del Aur", None),
    ], [("capella", "delaur"), ("delaur", "iotaur"), ("iotaur", "theaur"),
        ("theaur", "menkalinan"), ("menkalinan", "capella")]),

    ("Canis Minor", [
        ("procyon", "alf CMi", "Procyon"), ("gomeisa", "bet CMi", "Gomeisa"),
    ], [("procyon", "gomeisa")]),

    ("Corona Borealis", [
        ("alphecca", "alf CrB", "Alphecca"), ("betcrb", "bet CrB", None),
        ("gamcrb", "gam CrB", None), ("delcrb", "del CrB", None),
        ("epscrb", "eps CrB", None), ("iotacrb", "iot CrB", None),
    ], [("betcrb", "alphecca"), ("alphecca", "gamcrb"), ("gamcrb", "delcrb"),
        ("delcrb", "epscrb"), ("epscrb", "iotacrb")]),

    ("Centaurus", [
        ("rigilkent", "alf01 Cen", "Rigil Kentaurus"), ("hadar", "bet Cen", "Hadar"),
    ], [("rigilkent", "hadar")]),
]

# Bright single stars with no drawn stick figure -- still labeled.
STANDALONE = [
    ("canopus", "alf Car", "Canopus"),
    ("achernar", "alf Eri", "Achernar"),
    ("fomalhaut", "alf PsA", "Fomalhaut"),
    ("spica", "alf Vir", "Spica"),
]

FILLER_COUNT = 200  # additional unlabeled bright stars, padded up to this total


def resolve_hip(bayer):
    url = SESAME + urllib.parse.quote(bayer)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                text = r.read().decode()
            m = re.search(r"^%I HIP (\d+)", text, re.M)
            return int(m.group(1)) if m else None
        except Exception:
            time.sleep(1.5)
    return None


def main():
    print("loading Hipparcos catalog...")
    with load.open(hipparcos.URL) as f:
        df = hipparcos.load_dataframe(f)

    stars = []          # list of dicts: name, ra, dec, mag
    key_to_index = {}
    resolved_hips = set()

    def add_star(key, bayer, proper_name):
        hip = resolve_hip(bayer)
        if hip is None or hip not in df.index:
            print(f"  WARN: could not resolve {bayer!r} ({proper_name}) -- skipped")
            return
        row = df.loc[hip]
        idx = len(stars)
        stars.append({
            "name": proper_name,
            "ra": round(float(row["ra_degrees"]), 4),
            "dec": round(float(row["dec_degrees"]), 4),
            "mag": round(float(row["magnitude"]), 2),
        })
        key_to_index[key] = idx
        resolved_hips.add(hip)

    constellations_out = []
    for cname, members, lines in CONSTELLATIONS:
        print(f"resolving {cname} ({len(members)} stars)...")
        for key, bayer, proper in members:
            if key not in key_to_index:
                add_star(key, bayer, proper)
        line_idx = [[key_to_index[a], key_to_index[b]] for a, b in lines
                    if a in key_to_index and b in key_to_index]
        if line_idx:
            constellations_out.append({"name": cname, "lines": line_idx})

    print(f"resolving {len(STANDALONE)} standalone named stars...")
    for key, bayer, proper in STANDALONE:
        add_star(key, bayer, proper)

    print(f"padding with brightest unlabeled stars up to {FILLER_COUNT} total...")
    brightest = df.sort_values("magnitude")
    for hip, row in brightest.iterrows():
        if len(stars) >= FILLER_COUNT:
            break
        if hip in resolved_hips:
            continue
        if not (-90 <= row["dec_degrees"] <= 90):
            continue
        stars.append({
            "name": None,
            "ra": round(float(row["ra_degrees"]), 4),
            "dec": round(float(row["dec_degrees"]), 4),
            "mag": round(float(row["magnitude"]), 2),
        })

    payload = {"stars": stars, "constellations": constellations_out}
    js = (
        "// Generated by tools/build_stars.py -- do not hand-edit.\n"
        "// Positions/magnitudes: ESA Hipparcos catalog (1997, public domain).\n"
        "// Constellation groupings/lines: this project's own traditional-shape choices.\n"
        "window.STAR_CATALOG = " + json.dumps(payload, separators=(",", ":")) + ";\n"
    )
    with open(OUT, "w") as f:
        f.write(js)
    print(f"wrote {OUT}: {len(stars)} stars, {len(constellations_out)} constellations")


if __name__ == "__main__":
    main()
