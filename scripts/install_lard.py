"""
install_lard.py — Clone et prepare LARD (dependance externe)
============================================================
LARD n'est pas un package pip : ce script le clone a la bonne version, installe
ses dependances, et enregistre son emplacement pour sources/config.py.

- Branche : LARD_V2 (celle qui contient src/geo/, requise par lard_bridge.py ;
  main = LARD_V1 casserait les imports `from src.geo...`).
- Emplacement : par defaut <racine_projet>/LARD, surchargeable via --dest
  (LARD peut etre n'importe ou sur la machine).
- Enregistrement : ecrit lard_dir dans paths.local.json a la racine.

Usage :
    py scripts/install_lard.py
    py scripts/install_lard.py --dest "D:/libs/LARD" --no-deps
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/deel-ai/LARD"
BRANCH = "LARD_V2"

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "paths.local.json"


def _run(cmd):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True)


def clone_lard(dest):
    if dest.exists() and any(dest.iterdir()):
        print(f"[LARD] Deja present : {dest} (clone saute)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--branch", BRANCH, "--single-branch", REPO_URL, str(dest)])
    print(f"[LARD] Clone OK ({BRANCH}) -> {dest}")


def install_deps(dest):
    req = dest / "requirements.txt"
    if not req.exists():
        print(f"[LARD] Pas de requirements.txt dans {dest}, skip deps")
        return
    _run([sys.executable, "-m", "pip", "install", "-r", str(req)])
    print("[LARD] Dependances installees")


def register_path(dest):
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    cfg["lard_dir"] = str(dest)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"[LARD] Chemin enregistre dans {CONFIG_FILE.name} (lard_dir={dest})")


def main():
    parser = argparse.ArgumentParser(
        description="Clone + prepare LARD (branche LARD_V2)")
    parser.add_argument("--dest", type=str, default=str(ROOT / "LARD"),
                        help="Dossier de destination (defaut: <racine>/LARD)")
    parser.add_argument("--no-deps", action="store_true",
                        help="Ne pas installer requirements.txt de LARD")
    args = parser.parse_args()

    dest = Path(args.dest).resolve()
    clone_lard(dest)
    if not args.no_deps:
        install_deps(dest)
    register_path(dest)
    print("\n[OK] LARD pret.")
    print('     Verifier : cd sources && python -c "from config import LARD_ROOT; print(LARD_ROOT)"')


if __name__ == "__main__":
    main()
