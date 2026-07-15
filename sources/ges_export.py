"""
ges_export.py — Generation du projet Google Earth Studio (.esp)
===============================================================
Pont renderer-agnostique -> GES : relit les poses (<scenario>.json) et
reconstruit le .esp via LARD GEODataset.create_scenario (branche LARD_V2).
On ne reimplemente pas le format GES : on reutilise le code LARD.

NB : le rendu GES lui-meme est EXTERNE au projet. Ici on ne produit que le
fichier .esp (le projet a ouvrir dans Google Earth Studio).

Ce module importe LARD a l'import (via lard_bridge) : l'appeler quand LARD
n'est pas installe leve une exception. Au 'generate', la generation du .esp
n'est PAS protegee (pas de try/except) : un echec interrompt la generation.
"""

import json
from pathlib import Path

# Importer lard_bridge declenche _ensure_lard_importable() (LARD sur sys.path).
from lard_bridge import _ensure_lard_importable, _lard_cwd, generate_frame_times

_ensure_lard_importable()
from src.geo.geo_dataset import GEODataset


def build_esp_from_poses(poses_json, out_esp, fov_vertical=30.0,
                         width=1024, height=1024):
    """Ecrit un .esp GES a partir d'un fichier de poses JSON.

    :param poses_json: chemin du <scenario>.json (poses camera)
    :param out_esp:    chemin du .esp a ecrire
    :return: chemin du .esp ecrit
    """
    poses_json = Path(poses_json)
    out_esp = Path(out_esp)
    data = json.loads(poses_json.read_text())
    poses = data.get("poses", [])
    if not poses:
        raise ValueError(f"poses vides dans {poses_json}")

    # poses -> flight_data (lon, lat, alt, yaw, pitch, roll) attendu par LARD.
    flight_data = [
        (p["lon"], p["lat"], p["alt_m"], p["heading"], p["pitch"], p["roll"])
        for p in poses
    ]
    fps = int(data.get("fps", 25))
    n_frames = len(flight_data)
    times = generate_frame_times(n_frames, fps)

    dataset = GEODataset(str(out_esp.parent), out_esp.stem)
    with _lard_cwd():  # CWD = LARD_ROOT pour data/template.json
        scenario, _ = dataset.create_scenario(
            flight_data,
            fov_vertical=fov_vertical,
            width=width, height=height,
            nb_frames=n_frames, fps=fps,
            times=times,
        )

    out_esp.write_text(json.dumps(scenario, indent=2))
    return str(out_esp)


def build_esp_for_scenario(scenario_dir, fov_vertical=30.0):
    """Genere <scenario>.esp dans scenario_dir depuis <scenario>.json."""
    scenario_dir = Path(scenario_dir)
    name = scenario_dir.name
    poses_json = scenario_dir / f"{name}.json"
    if not poses_json.exists():
        raise FileNotFoundError(f"Pas de {name}.json dans {scenario_dir}")
    return build_esp_from_poses(poses_json, scenario_dir / f"{name}.esp",
                                fov_vertical=fov_vertical)
