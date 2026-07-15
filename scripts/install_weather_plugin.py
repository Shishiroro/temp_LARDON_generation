"""
install_weather_plugin.py — Installe le plugin meteo dans X-Plane 12
====================================================================
Copie XPlanePlugin/PI_weather.py dans le dossier des plugins Python de
X-Plane 12 :

    <X-Plane 12>/Resources/plugins/PythonPlugins/PI_weather.py

Le dossier PythonPlugins/ est cree s'il n'existe pas.

Pre-requis : XPPython3 doit deja etre installe dans
<X-Plane 12>/Resources/plugins/ (voir README).

Usage (depuis la racine du projet) :
    py scripts/install_weather_plugin.py                       # lit XPLANE_DIR (config)
    py scripts/install_weather_plugin.py --xplane-dir "D:/X-Plane 12"

Apres la copie, recharger dans le simulateur :
    Plugins > XPPython3 > Reload Scripts
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SRC = ROOT / "XPlanePlugin" / "PI_weather.py"

# config appli (XPLANE_DIR) resolue depuis sources/config.py
sys.path.insert(0, str(ROOT / "sources"))
from config import XPLANE_DIR


def install(xplane_dir):
    if not PLUGIN_SRC.exists():
        raise SystemExit(f"[ERREUR] Plugin introuvable : {PLUGIN_SRC}")

    xp = Path(xplane_dir)
    plugins_dir = xp / "Resources" / "plugins"
    if not plugins_dir.exists():
        raise SystemExit(
            f"[ERREUR] Dossier plugins X-Plane introuvable : {plugins_dir}\n"
            f"         Verifier le chemin X-Plane 12 fourni."
        )
    if not (plugins_dir / "XPPython3").exists():
        print("[ATTENTION] XPPython3 ne semble pas installe dans "
              f"{plugins_dir}.\n            Le plugin meteo ne fonctionnera "
              "pas sans XPPython3 (voir README).")

    dest_dir = plugins_dir / "PythonPlugins"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / PLUGIN_SRC.name
    shutil.copy2(PLUGIN_SRC, dest)

    print(f"[OK] {PLUGIN_SRC.name} copie vers :\n     {dest}")
    print("\nRecharger depuis le simulateur : "
          "Plugins > XPPython3 > Reload Scripts")


def main():
    parser = argparse.ArgumentParser(
        description="Installe PI_weather.py dans X-Plane 12 (PythonPlugins/)")
    parser.add_argument(
        "--xplane-dir", type=str, default=None,
        help="Repertoire X-Plane 12 (defaut: XPLANE_DIR via config.py)")
    args = parser.parse_args()

    xplane_dir = args.xplane_dir or XPLANE_DIR
    if not xplane_dir:
        sys.exit("[ERREUR] Aucun repertoire X-Plane : definir XPLANE_DIR "
                 "(env ou paths.local.json) ou passer --xplane-dir.")

    install(xplane_dir)


if __name__ == "__main__":
    main()
