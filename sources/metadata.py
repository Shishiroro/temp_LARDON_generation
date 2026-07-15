"""
metadata.py — Construction du metadata.csv d'un scenario (dataset)
==================================================================
Reprend le MEME schema que l'ancien build_dataset (notebook) : verite terrain
LARD (passthrough) + colonnes derivees (scenario, airport, runway, time,
time_fps, weather, image). Produit/mis a jour a chaque export.

Le metadata.csv est ecrit dans dataset/<sim>/<airport_runway>/<scenario>/ et
son chemin `image` est relatif au scenario ("images/<fichier>").
"""

import csv
import json
import re
from pathlib import Path

from scenario import list_images, airport_runway_from_scenario
from runway_utils import reciprocal_runway

# Colonnes du metadata.csv — alignees sur l'ancien build_dataset / CSV LARD.
META_COLS = [
    "height", "width", "type",
    "scenario", "airport", "runway",
    "time", "time_fps", "weather",
    "yaw", "pitch", "roll",
    "slant_distance", "along_track_distance", "height_above_runway",
    "lateral_path_angle", "vertical_path_angle",
    "x_TR", "y_TR", "x_TL", "y_TL", "x_BL", "y_BL", "x_BR", "y_BR",
    "image",
]

# Colonnes copiees telles quelles depuis le CSV GT LARD.
LARD_PASSTHROUGH = [
    "height", "width",
    "airport", "runway",
    "yaw", "pitch", "roll",
    "slant_distance", "along_track_distance", "height_above_runway",
    "lateral_path_angle", "vertical_path_angle",
    "x_TR", "y_TR", "x_TL", "y_TL", "x_BL", "y_BL", "x_BR", "y_BR",
]


def _norm_rwy(rwy):
    """Normalise un numero de piste pour comparaison ('08L' -> '8L', '29' -> '29')."""
    m = re.match(r"^(\d{1,2})([LRC]?)$", str(rwy).strip())
    return f"{int(m.group(1))}{m.group(2)}" if m else str(rwy).strip()


def _load_lard_rows(gt_csv, target_runway):
    """Lignes GT LARD (delim ';') de la piste cible, dans l'ordre des frames.

    LARD emet une ligne par piste visible dans le cone camera (souvent
    plusieurs pistes par frame). On ne garde que la piste du scenario (+ sa
    reciproque, meme bande physique), en preservant l'ordre d'ecriture = ordre
    des poses = ordre des frames. La liste retournee a donc une ligne par frame.
    """
    keep = {_norm_rwy(target_runway), _norm_rwy(reciprocal_runway(target_runway))}
    rows = []
    with open(gt_csv, newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if _norm_rwy(row.get("runway", "")) in keep:
                rows.append(row)
    return rows


def _poses_json(scenario_dir):
    """Le <scenario>.json (poses), en excluant fault_/weather_profile.json."""
    for p in Path(scenario_dir).glob("*.json"):
        if p.name not in ("fault_profile.json", "weather_profile.json"):
            return p
    return None


def _read_template_name(scenario_dir):
    """trajectory.template_file_name depuis <scenario>.json ('' si absent)."""
    pj = _poses_json(scenario_dir)
    if not pj:
        return ""
    try:
        data = json.loads(pj.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    return str((data.get("trajectory") or {}).get("template_file_name", ""))


def _load_time_of_day(scenario_dir):
    """weather.time_of_day_h depuis weather_profile.json ('' si absent)."""
    wp = Path(scenario_dir) / "weather_profile.json"
    if not wp.exists():
        return ""
    try:
        data = json.loads(wp.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    return data.get("weather", {}).get("time_of_day_h", "")


def _weather_label(template_file_name):
    """'rain/rain_heavy.xml' -> 'rain_heavy', '' -> ''."""
    return Path(template_file_name).stem if template_file_name else ""


def _format_time(time_lard, time_of_day_h):
    """date LARD 'YYYY-MM-DD HH:MM:SS' + heure X-Plane -> 'DD/MM/YYYY HH:MM'."""
    if not time_lard:
        return ""
    try:
        y, m, d = time_lard.split(" ")[0].split("-")
        date_fmt = f"{d}/{m}/{y}"
    except (ValueError, IndexError):
        return ""
    if time_of_day_h in ("", None):
        return date_fmt
    try:
        h = float(time_of_day_h)
    except (TypeError, ValueError):
        return date_fmt
    hh = int(h) % 24
    mm = int(round((h - int(h)) * 60)) % 60
    return f"{date_fmt} {hh:02d}:{mm:02d}"


def _format_time_fps(time_lard):
    """'YYYY-MM-DD HH:MM:SS' -> 'HH:MM:SS'."""
    if not time_lard or " " not in time_lard:
        return ""
    return time_lard.split(" ", 1)[1]


def build_metadata_csv(scenario_dir, images_dir, gt_csv, simulator, out_csv):
    """Ecrit metadata.csv (schema META_COLS) : GT LARD + colonnes derivees.

    :param scenario_dir: scenarios/<batch>/<scenario_name>/ (yaml/json/profils)
    :param images_dir:   dataset .../images/
    :param gt_csv:       CSV GT LARD brut (produit par lard_bridge)
    :param simulator:    'xplane' | 'GES' (colonne 'type')
    :param out_csv:      chemin du metadata.csv a ecrire
    :return: chemin du metadata.csv
    """
    scenario_dir = Path(scenario_dir)
    scenario_name = scenario_dir.name
    icao_rwy = airport_runway_from_scenario(scenario_name)   # KPDX_10L
    airport_def, _, runway_def = icao_rwy.partition("_")

    lard_rows = _load_lard_rows(gt_csv, runway_def)
    time_of_day = _load_time_of_day(scenario_dir)
    weather = _weather_label(_read_template_name(scenario_dir))

    images = list_images(images_dir)

    # Association frame -> ligne GT. LARD ecrit une ligne par frame (apres
    # filtrage sur la piste), dans l'ordre des poses = ordre des frames.
    # Si LARD a rempli la colonne 'image' (images trouvees dans footage/), on
    # matche par nom ; sinon (mode with_images=False) on aligne par index.
    by_name = {Path(r["image"]).name: r for r in lard_rows if r.get("image", "").strip()}
    use_names = len(by_name) == len(lard_rows) and lard_rows
    if len(lard_rows) != len(images):
        print(f"  [metadata] ATTENTION : {len(lard_rows)} lignes GT (piste "
              f"{runway_def}) pour {len(images)} images — jointure best-effort")

    rows = []
    for idx, img in enumerate(images):
        base = {c: "" for c in META_COLS}
        if use_names:
            lard = by_name.get(img.name, {})
        else:
            lard = lard_rows[idx] if idx < len(lard_rows) else {}
        for k in LARD_PASSTHROUGH:
            if k in lard:
                base[k] = lard[k]
        base["type"] = simulator
        base["scenario"] = scenario_name
        base["airport"] = base["airport"] or airport_def
        base["runway"] = base["runway"] or runway_def
        base["time"] = _format_time(lard.get("time", ""), time_of_day)
        base["time_fps"] = _format_time_fps(lard.get("time", ""))
        base["weather"] = weather
        base["image"] = f"images/{img.name}"
        rows.append(base)

    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"  metadata.csv -> {out_csv} ({len(rows)} lignes)")
    return str(out_csv)
