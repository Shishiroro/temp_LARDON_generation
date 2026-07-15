"""
install_taf.py — Clone et prepare TAF (dependance externe LAAS)
==============================================================
TAF n'est pas un package pip : ce script le clone et enregistre son emplacement
pour sources/config.py (miroir de install_lard.py).

- Depot : https://redmine.laas.fr/laas/taf.git (branche par defaut).
- Emplacement : par defaut <racine_projet>/taf, surchargeable via --dest
  (TAF peut etre n'importe ou sur la machine).
- Enregistrement : ecrit taf_dir dans paths.local.json a la racine.
- Dependances Python de TAF (z3-solver, numpy) : elles sont dans le
  requirements.txt du projet (TAF n'a pas de requirements.txt propre).

Usage :
    py scripts/install_taf.py
    py scripts/install_taf.py --dest "D:/libs/taf"
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://redmine.laas.fr/laas/taf.git"

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "paths.local.json"


def _run(cmd):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True)


def clone_taf(dest):
    if dest.exists() and any(dest.iterdir()):
        print(f"[TAF] Deja present : {dest} (clone saute)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", REPO_URL, str(dest)])
    print(f"[TAF] Clone OK -> {dest}")


def register_path(dest):
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    cfg["taf_dir"] = str(dest)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"[TAF] Chemin enregistre dans {CONFIG_FILE.name} (taf_dir={dest})")


def main():
    parser = argparse.ArgumentParser(
        description="Clone + prepare TAF (depot LAAS)")
    parser.add_argument("--dest", type=str, default=str(ROOT / "taf"),
                        help="Dossier de destination (defaut: <racine>/taf)")
    args = parser.parse_args()

    dest = Path(args.dest).resolve()
    clone_taf(dest)
    register_path(dest)
    print("\n[OK] TAF pret.")
    print("     Deps Python de TAF (z3-solver, numpy) : dans requirements.txt du projet.")
    print('     Verifier : cd sources && python -c "from config import TAF_SRC; print(TAF_SRC)"')


if __name__ == "__main__":
    main()
