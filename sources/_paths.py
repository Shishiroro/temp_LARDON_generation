"""
_paths.py — Bootstrap sys.path centralise pour LARD-LAAS-TAF
=============================================================
Import simple : `import _paths` ajoute tous les dossiers necessaires au
sys.path (idempotent).

Modules entry-points cote usine (run_pipeline, Generate, Export copie par TAF)
inserent ce fichier en tete de leurs imports. Le banc d'evaluation (evaluation/)
le reutilise via son propre bootstrap (evaluation/__init__.py).
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# sources/_paths.py -> sources/ -> racine
PROJECT_DIR = Path(__file__).resolve().parent
ROOT = PROJECT_DIR.parent
EXPORT_DIR = PROJECT_DIR / "export"


def _resolve_dir(value, default):
    """Resout un chemin de settings.xml : vide -> defaut ; relatif -> / ROOT."""
    if not value or not value.strip():
        return default
    p = Path(value.strip())
    return p if p.is_absolute() else (ROOT / p)


# Chemins LARD/ et taf/ : surcharges optionnelles via sources/settings.xml
# (parametres lard_dir / taf_dir). Vide => clones a la racine (./LARD, ./taf).
_lard_dir, _taf_dir = "", ""
_settings = PROJECT_DIR / "settings.xml"
if _settings.exists():
    try:
        for _p in ET.parse(_settings).getroot():
            _name = _p.attrib.get("name")
            if _name == "lard_dir":
                _lard_dir = _p.attrib.get("value", "")
            elif _name == "taf_dir":
                _taf_dir = _p.attrib.get("value", "")
    except ET.ParseError:
        pass

LARD_ROOT = _resolve_dir(_lard_dir, ROOT / "LARD")
TAF_ROOT = _resolve_dir(_taf_dir, ROOT / "taf")
TAF_SRC = TAF_ROOT / "src"

# NB : l'evaluation (yolo/eval) n'est PAS ajoutee ici — c'est le bootstrap du
# package evaluation/ (evaluation/__init__.py) qui s'en charge. sources/ ne
# connait pas evaluation/ (dependance unidirectionnelle evaluation -> sources).
_PATHS = (ROOT, PROJECT_DIR, EXPORT_DIR, LARD_ROOT)

for _p in _PATHS:
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)
