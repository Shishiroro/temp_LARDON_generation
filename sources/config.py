"""
config.py — Resolution centralisee des chemins externes (LARD, TAF, X-Plane)
============================================================================
Unique source de verite pour localiser les dependances externes, en
remplacement de _paths.py (qui bricolait le sys.path global et lisait
settings.xml).

Resolution, pour chaque chemin :
    1. Fichier de config local `paths.local.json` a la racine du projet
    2. Valeur par defaut (LARD / taf clones a la racine ; xplane vide)

LARD et TAF peuvent etre N'IMPORTE OU sur la machine : indiquer leur chemin
(absolu) dans paths.local.json. Voir paths.local.json.example.

IMPORTANT : ce module n'a AUCUN effet de bord a l'import — il ne modifie pas
sys.path. L'insertion de LARD dans sys.path est confinee a lard_bridge
(_ensure_lard_importable) ; celle de TAF a taf_generate (taf/src).

LARD n'est pas un package installable (app Streamlit, code importe en
`from src....`, donnees lues relativement au CWD) : on ne peut donc que
resoudre sa localisation ici, pas le `pip install`.
"""

import json
from pathlib import Path

# config.py vit dans sources/ -> racine du projet = parent
SOURCES_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SOURCES_DIR.parent

_CONFIG_FILE = PROJECT_ROOT / "paths.local.json"


def _load_config_file():
    """Charge paths.local.json (dict vide si absent ou invalide)."""
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


_FILE_CFG = _load_config_file()


def _resolve(cfg_key, default):
    """paths.local.json[cfg_key] -> defaut.

    Un chemin relatif est resolu par rapport a PROJECT_ROOT ; un chemin absolu
    est pris tel quel (LARD/TAF peuvent etre n'importe ou sur la machine).
    """
    val = str(_FILE_CFG.get(cfg_key) or "").strip()
    if not val:
        return Path(default)
    p = Path(val)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


# --- Dependances externes ---
LARD_ROOT = _resolve("lard_dir", PROJECT_ROOT / "LARD")
TAF_ROOT = _resolve("taf_dir", PROJECT_ROOT / "taf")
TAF_SRC = TAF_ROOT / "src"

# X-Plane : chaine vide par defaut (sert uniquement a localiser le plugin meteo).
XPLANE_DIR = str(_FILE_CFG.get("xplane_dir") or "").strip()
