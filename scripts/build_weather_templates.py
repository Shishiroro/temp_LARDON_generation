"""
build_weather_templates.py — Generateur des XML de profils meteo
=================================================================
Lit base.xml et genere les 22 variantes de profil meteo dans
templates/<profil>/. Chaque variante = base.xml dont le bloc des
8 parametres meteo (entre @@WEATHER_BLOCK_START@@ et @@WEATHER_BLOCK_END@@)
est remplace par les valeurs du preset.

C'est du tooling BUILD-TIME : rien ici ne tourne pendant la generation TAF.
Au runtime, TAF lit simplement le XML pointe par sources/settings.xml
(parametre template_file_name, ex: value="rain/rain_heavy.xml").

Usage (depuis la racine du projet) :
    py scripts/build_weather_templates.py

Pour modifier un profil : editer la table PRESETS ci-dessous (ou
base.xml pour la trajectoire / les fautes), puis relancer ce script.
NE PAS editer les XML generes a la main : ils seront ecrases.
"""

from pathlib import Path

# Ce script vit dans scripts/ ; les templates sont dans sources/templates/.
ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "sources" / "templates"
BASE_FILE = TEMPLATES_DIR / "base.xml"

START_MARKER = "<!-- @@WEATHER_BLOCK_START@@ -->"
END_MARKER = "<!-- @@WEATHER_BLOCK_END@@ -->"

# Les 8 params meteo remplaces, dans l'ordre d'emission.
PARAM_ORDER = [
    "precip_rate", "cloud_type", "cloud_coverage", "cloud_thickness_m",
    "fog_visibility", "temperature_c", "rain_scale", "cloud_margin_m",
]

PARAM_COMMENT = {
    "precip_rate":       "Precipitation (0 = aucune ; >0 + cloud_type=-1 => Cb auto)",
    "cloud_type":        "-1=Cb auto, 0=Cirrus, 1=Stratus, 2=Cumulus, 3=Cumulonimbus",
    "cloud_coverage":    "Couverture nuageuse [0-1]",
    "cloud_thickness_m": "Epaisseur nuage (m) ; 0 => pas de particules",
    "fog_visibility":    "Visibilite (m)",
    "temperature_c":     "Temperature (C) ; < 0 => precip basculee en neige",
    "rain_scale":        "Taille gouttes (visuel) ; ignore si precip=0",
    "cloud_margin_m":    "Marge base nuages au-dessus de l'avion (m)",
}

# Valeurs meteo neutres par defaut (ciel degage). Chaque preset surcharge
# uniquement les params pertinents ; le reste reste a ces valeurs.
DEFAULTS = {
    "precip_rate":       (0, 0),
    "cloud_type":        (-1, -1),
    "cloud_coverage":    (0, 0),
    "cloud_thickness_m": (0, 0),
    "fog_visibility":    (50000, 50000),
    "temperature_c":     (15, 15),
    "rain_scale":        (1, 1),
    "cloud_margin_m":    (2000, 2000),
}

# Marge base nuages au-dessus de l'avion, par type (plage echantillonnee par TAF).
# Calquee sur l'altitude reelle de chaque genre de nuage.
MARGIN_CIRRUS  = (3000, 5000)  # cirrus = haute altitude
MARGIN_STRATUS = (150, 800)     # plafond bas realiste
MARGIN_CUMULUS = (600, 1800)
MARGIN_CB      = (600, 1400)

# PRESETS : (sous_dossier, nom_fichier) -> surcharges {param: (min, max)}
# Tout param absent garde sa valeur DEFAULTS.
PRESETS = {
    # --- Brouillard 
    ("fog", "fog_light"):    {"fog_visibility": (7000, 10000)},
    ("fog", "fog_moderate"): {"fog_visibility": (4000, 7000)},
    ("fog", "fog_heavy"):    {"fog_visibility": (1000, 4000)},

    # --- Nuages : precip=0, cloud_type fixe, coverage/thickness par intensite ---
    # Cirrus (cloud_type=0)
    ("clouds", "cirrus_light"):    {"cloud_type": (0, 0), "cloud_coverage": (0.3, 0.5), "cloud_thickness_m": (600, 600),   "cloud_margin_m": MARGIN_CIRRUS},
    ("clouds", "cirrus_moderate"): {"cloud_type": (0, 0), "cloud_coverage": (0.5, 0.7), "cloud_thickness_m": (1000, 1000), "cloud_margin_m": MARGIN_CIRRUS},
    ("clouds", "cirrus_heavy"):    {"cloud_type": (0, 0), "cloud_coverage": (0.7, 1.0), "cloud_thickness_m": (1500, 1500), "cloud_margin_m": MARGIN_CIRRUS},
    # Stratus (cloud_type=1)
    ("clouds", "stratus_light"):    {"cloud_type": (1, 1), "cloud_coverage": (0.3, 0.3), "cloud_thickness_m": (300, 300), "cloud_margin_m": MARGIN_STRATUS},
    ("clouds", "stratus_moderate"): {"cloud_type": (1, 1), "cloud_coverage": (0.7, 0.7), "cloud_thickness_m": (500, 500), "cloud_margin_m": MARGIN_STRATUS},
    ("clouds", "stratus_heavy"):    {"cloud_type": (1, 1), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (800, 800), "cloud_margin_m": MARGIN_STRATUS},
    # Cumulus (cloud_type=2)
    ("clouds", "cumulus_light"):    {"cloud_type": (2, 2), "cloud_coverage": (0.3, 0.3), "cloud_thickness_m": (1000, 1000), "cloud_margin_m": MARGIN_CUMULUS},
    ("clouds", "cumulus_moderate"): {"cloud_type": (2, 2), "cloud_coverage": (0.7, 0.7), "cloud_thickness_m": (2000, 2000), "cloud_margin_m": MARGIN_CUMULUS},
    ("clouds", "cumulus_heavy"):    {"cloud_type": (2, 2), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (3000, 3000), "cloud_margin_m": MARGIN_CUMULUS},
    # Cumulonimbus (cloud_type=3)
    ("clouds", "cumulonimbus_light"):    {"cloud_type": (3, 3), "cloud_coverage": (0.3, 0.3), "cloud_thickness_m": (6000, 6000),   "cloud_margin_m": MARGIN_CB},
    ("clouds", "cumulonimbus_moderate"): {"cloud_type": (3, 3), "cloud_coverage": (0.7, 0.7), "cloud_thickness_m": (8000, 8000),   "cloud_margin_m": MARGIN_CB},
    ("clouds", "cumulonimbus_heavy"):    {"cloud_type": (3, 3), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (10000, 10000), "cloud_margin_m": MARGIN_CB},


    # --- Pluie : precip + Cumulonimbus auto (cloud_type=-1) ---
    ("rain", "rain_light"):    {"precip_rate": (1.0, 1.0), "cloud_type": (1, 3), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (500, 500), "temperature_c": (15, 15), "rain_scale": (1.0, 2.0), "cloud_margin_m": (2000, 5000)},
    ("rain", "rain_moderate"): {"precip_rate": (1.0, 1.0), "cloud_type": (1, 3), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (500, 500), "temperature_c": (15, 15), "rain_scale": (2.0, 4.0), "cloud_margin_m": (500, 1800)},
    ("rain", "rain_heavy"):    {"precip_rate": (1.0, 1.0), "cloud_type": (3, 3), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (10000, 10000), "temperature_c": (15, 15), "rain_scale": (4.0, 5.0), "cloud_margin_m": (300, 500)},


    # --- Neige : precip + T < 0 (XP12 bascule les particules en neige) ---
    # NB : temperature plafonnee a -2 C (et non 0) pour garantir la neige.
    ("snow", "snow_light"):    {"precip_rate": (1.0, 1.0), "cloud_type": (1, 3), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (500, 500), "temperature_c": (-15, -15), "rain_scale": (1.0, 2.0), "cloud_margin_m": (2000, 5000)},
    ("snow", "snow_moderate"): {"precip_rate": (1.0, 1.0), "cloud_type": (1, 3), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (500, 500), "temperature_c": (-15, -15), "rain_scale": (2.0, 4.0), "cloud_margin_m": (500, 1800)},
    ("snow", "snow_heavy"):    {"precip_rate": (1.0, 1.0), "cloud_type": (3, 3), "cloud_coverage": (1.0, 1.0), "cloud_thickness_m": (10000, 10000), "temperature_c": (-15, -15), "rain_scale": (4.0, 5.0), "cloud_margin_m": (300, 500)},

  
    ("clear", "clear"): {"cloud_type": (0, 0), "cloud_thickness_m": (10, 10), }, #OK
}


def _fmt(v):
    """Formate un nombre pour XML : entier sans decimale, sinon flottant court."""
    return f"{v:g}"


def build_weather_block(overrides):
    """Construit le bloc XML indente des 8 params meteo pour un preset."""
    vals = dict(DEFAULTS)
    vals.update(overrides)
    lines = []
    for name in PARAM_ORDER:
        lo, hi = vals[name]
        lines.append(f"            <!-- {PARAM_COMMENT[name]} -->")
        prefix = f'            <parameter name="{name}"'
        lines.append(
            f'{prefix.ljust(50)}type="real" '
            f'min="{_fmt(lo)}" max="{_fmt(hi)}"/>'
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def _banner(profile):
    """Bandeau d'avertissement insere en tete des fichiers generes."""
    return (
        "<!-- ============================================================\n"
        "     FICHIER GENERE par scripts/build_weather_templates.py\n"
        f"     Profil : {profile}   |   NE PAS EDITER A LA MAIN\n"
        "     Source : base.xml + table PRESETS du generateur.\n"
        "     Regenerer : py scripts/build_weather_templates.py\n"
        "     ============================================================ -->\n"
    )


def main():
    if not BASE_FILE.exists():
        raise SystemExit(f"base.xml introuvable : {BASE_FILE}")

    base = BASE_FILE.read_text(encoding="utf-8")
    if START_MARKER not in base or END_MARKER not in base:
        raise SystemExit(
            "Marqueurs @@WEATHER_BLOCK_START@@ / @@WEATHER_BLOCK_END@@ "
            "absents de base.xml"
        )

    before, _, rest = base.partition(START_MARKER)
    _, _, after = rest.partition(END_MARKER)
    first_nl = before.index("\n") + 1  # apres la ligne <?xml ... ?>

    count = 0
    for (subdir, fname), overrides in PRESETS.items():
        block = build_weather_block(overrides)
        head = before[:first_nl] + _banner(f"{subdir}/{fname}") + before[first_nl:]
        content = (
            head + START_MARKER + "\n\n" + block
            + "\n\n            " + END_MARKER + after
        )
        out_dir = TEMPLATES_DIR / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{fname}.xml").write_text(content, encoding="utf-8")
        count += 1
        print(f"  genere  {subdir}/{fname}.xml")

    print(f"\n{count} templates meteo generes dans {TEMPLATES_DIR}")


if __name__ == "__main__":
    main()
