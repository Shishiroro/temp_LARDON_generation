"""
lard_bridge.py — Interface avec LARD (import uniquement, rien modifie)
======================================================================
Responsabilites :
    1. Obtenir la geometrie piste via compute_aiming_point
    2. Ecrire le fichier .yaml de sortie (format LARD, compatible export_labels)
    3. Appeler export_labels pour produire le CSV de ground truth

Note : export_labels lit des fichiers relativement au CWD,
       donc on change temporairement vers LARD_ROOT.
"""

import os
import sys
import json
import yaml
import uuid
import random
import numpy as np
from pathlib import Path
from contextlib import contextmanager
from dataclasses import asdict

from config import LARD_ROOT, PROJECT_ROOT


def _ensure_lard_importable():
    """Rend LARD importable (`from src....`) en ajoutant sa racine au sys.path.

    LARD n'est pas un package installable : son code s'importe lui-même en
    `from src....` et lit ses données relativement au CWD. On résout sa
    localisation via config (LARD_ROOT / paths.local.json) et on l'ajoute au
    sys.path ici, une seule fois — toute la connaissance de LARD est confinée
    à ce module (remplace le _paths.py global).
    """
    lard = str(LARD_ROOT)
    if lard not in sys.path:
        sys.path.insert(0, lard)


_ensure_lard_importable()

# DB LARD X-Plane (meme DB que le labeling LARD pour coherence trajectoire/GT)
RUNWAY_DB_XPLANE = str(LARD_ROOT / "data" / "runways_db_V2_XPlane.json")

# LARD (branche LARD_V2) — importe via _ensure_lard_importable() ci-dessus.
from src.geo.geo_dataset import compute_aiming_point
from src.geo.geo_utils import ecef2llh
from src.labeling.label_export import export_labels
from src.labeling.export_config import DatasetTypes


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

@contextmanager
def _lard_cwd():
    """Change temporairement le CWD vers LARD_ROOT (pour template.json)."""
    prev = os.getcwd()
    os.chdir(LARD_ROOT)
    try:
        yield
    finally:
        os.chdir(prev)


def get_runway_geometry(airport, runway):
    """
    Recupere la geometrie d'une piste (DB X-Plane).

    LARD convention :
      - LTP = (C+D)/2 = seuil de toucher (cote approche)
      - FPAP = (A+B)/2 = bout de piste (cote eloigne)
      - rwy_psi[0] = forward azimuth (LTP->FPAP) = vrai cap de la piste
      - rwy_psi[1] = back azimuth   (FPAP->LTP) = direction de l'approche

    :return: dict avec ltp_lat, ltp_lon, ltp_alt,
             runway_heading_deg, runway_back_azimuth_deg
    """
    # compute_aiming_point() exige une dist_m mais on ne veut que les sorties
    # geometriques (rwy_psi, ltp) ; on passe 0 et on ignore l'aiming point.
    # La vraie distance d'aiming est gardee dans trajectory_builder via
    # OUParams.dist_ap_m (altitude = -tan(alpha_v) * (distance + dist_ap_m)).
    _, _, rwy_psi, ltp, fpap = compute_aiming_point(
        RUNWAY_DB_XPLANE, airport, runway, 0.0
    )
    ltp_lat, ltp_lon, ltp_alt = ecef2llh(ltp[0], ltp[1], ltp[2])

    return {
        "ltp_lat": ltp_lat,
        "ltp_lon": ltp_lon,
        "ltp_alt": ltp_alt,
        "runway_heading_deg": rwy_psi[0],       # forward = vrai cap
        "runway_back_azimuth_deg": rwy_psi[1],  # back = direction approche
    }


# ---------------------------------------------------------------------------
# Generation de timestamps pour chaque frame (au format attendu par LARD)
# ---------------------------------------------------------------------------

def generate_frame_times(n_frames, fps, seed=None):
    """
    Genere un timestamp par frame (date/heure aleatoire, increment 1/fps).

    LARD attend un dict {year, month, day, hour, minute, second} par frame.
    On choisit une date/heure de base aleatoire puis on incremente.

    :param seed: si fourni, la date/heure de base est deterministe (utile pour
                 reproduire un meme CSV GT a partir du meme scenario).
    """
    rng = random.Random(seed)
    base_year = rng.randint(2020, 2025)
    base_month = rng.randint(1, 12)
    base_day = rng.randint(1, 28)
    base_hour = rng.randint(9, 16)  # heures de plein jour (evite crepuscule)
    base_minute = rng.randint(0, 59)
    base_second = rng.randint(0, 59)

    dt_frame = 1.0 / fps
    times = []
    for i in range(n_frames):
        elapsed = i * dt_frame
        sec_total = base_second + elapsed
        minute_total = base_minute + int(sec_total // 60)
        second = int(sec_total % 60)
        hour_total = base_hour + int(minute_total // 60)
        minute = int(minute_total % 60)
        hour = int(hour_total % 24)

        times.append({
            "year": base_year,
            "month": base_month,
            "day": base_day,
            "hour": hour,
            "minute": minute,
            "second": second,
        })

    return times


# ---------------------------------------------------------------------------
# Export .yaml + poses
# ---------------------------------------------------------------------------

def export_scenario(flight_data, cfg, ou_params, airport, runway,
                    output_dir, scenario_name="scenario", faults=None,
                    weather=None):
    """
    Exporte le .yaml d'un scenario au format LARD.

    Ce .yaml est fidele au format LARD (poses, image, trajectory)
    pour etre compatible avec export_labels() de LARD.

    :param flight_data: list de tuples (lon, lat, alt, yaw, pitch, roll)
    :param cfg: TrajectoryConfig (parametres utilisateur)
    :param ou_params: OUParams (hyperparametres)
    :param airport: code ICAO
    :param runway: identifiant piste
    :param output_dir: dossier de sortie
    :param scenario_name: nom de base des fichiers
    :param faults: liste de FaultConfig (optionnel)
    :param weather: WeatherConfig (optionnel)
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    n_frames = len(flight_data)
    times = generate_frame_times(n_frames, cfg.fps)

    # --- Params image (X-Plane : 1024x1024, FOV 60°) ---
    img_width = 1024
    img_height = 1024
    fov_x = 60.0
    f_focal = img_height / 2.0 / np.tan(np.deg2rad(fov_x / 2.0))
    fov_y = round(float(2 * np.rad2deg(np.arctan2(img_width / 2.0, f_focal))), 6)
    watermark_height = 0

    # --- .yaml au format LARD (compatible export_labels) ---
    def _to_python(val):
        """Convertit numpy scalaires en types Python natifs pour YAML."""
        if hasattr(val, 'item'):
            return val.item()
        if isinstance(val, np.integer):
            return int(val)
        if isinstance(val, np.floating):
            return float(val)
        return val

    # Construire la liste poses au format LARD exact :
    # {uuid, airport, runway, pose: [lon, lat, alt, yaw, pitch, roll], time: {...}}
    poses = []
    for i, fd in enumerate(flight_data):
        poses.append({
            'uuid': str(uuid.uuid4()),
            'airport': airport,
            'runway': runway,
            'pose': [_to_python(v) for v in fd],
            'time': times[i],
        })

    # Structure yaml LARD (asdict(ScenarioContent) dans write_scenario.py)
    yaml_content = {
        'airports_runways': {airport: [runway]},
        'image': {
            'height': img_height,
            'width': img_width,
            'fov_x': fov_x,
            'fov_y': fov_y,
            'watermark_height': watermark_height,
        },
        'poses': poses,
        'runways_database': 'data/filtered_runways_database_Final.json',
        'trajectory': {
            'sample_number': n_frames,
            'dist_ap_m': ou_params.dist_ap_m,
            'max_distance_m': cfg.along_track_distance_start,
            'min_distance_m': cfg.along_track_distance_end,
            'alpha_h_deg': ou_params.alpha_h_deg,
            'std_alpha_h_deg': ou_params.std_alpha_h_deg,
            'alpha_h_distrib': 'normal',
            'alpha_v_deg': ou_params.alpha_v_deg,
            'std_alpha_v_deg': ou_params.std_alpha_v_deg,
            'alpha_v_distrib': 'normal',
            'yaw_deg': ou_params.yaw_deg,
            'std_yaw_deg': ou_params.std_yaw_deg,
            'yaw_distrib': 'normal',
            'pitch_deg': ou_params.pitch_deg,
            'std_pitch_deg': ou_params.std_pitch_deg,
            'pitch_distrib': 'normal',
            'roll_deg': ou_params.roll_deg,
            'std_roll_deg': ou_params.std_roll_deg,
            'roll_distrib': 'normal',
            'use_ODD': False,
        },
    }

    # --- Fautes capteur (optionnel) ---
    if faults:
        yaml_content['sensor_faults'] = [asdict(f) for f in faults]

    # --- Effets meteo X-Plane (optionnel) ---
    if weather:
        yaml_content['xplane_weather'] = asdict(weather)

    yaml_file = output_path / f"{scenario_name}.yaml"
    with open(yaml_file, "w") as f:
        yaml.dump(yaml_content, f, sort_keys=False, default_flow_style=False)

    print(f"  .yaml -> {yaml_file}")

    return str(yaml_file)


def generate_labels_csv(yaml_path, out_dir, images_dir, csv_name="metadata.csv"):
    """
    Genere le CSV verite terrain LARD a partir du .yaml et des images.
    A appeler APRES avoir recupere les images du simulateur.

    :param yaml_path: chemin vers le .yaml du scenario
    :param out_dir: dossier de sortie du CSV (dataset .../<scenario>/)
    :param images_dir: dossier contenant les images (dataset .../images/)
    :param csv_name: nom du fichier CSV (defaut: metadata.csv)
    """
    yaml_path = Path(yaml_path).resolve()
    out_dir = Path(out_dir).resolve()
    images_dir = Path(images_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_file = out_dir / csv_name

    # LARD recopie les images dans `out_images_dir` avant de generer le CSV.
    # On pointe out_images_dir sur images/ (images deja presentes) et on
    # monkey-patch shutil dans label_export pour qu'une copie src->dst
    # identique soit un no-op au lieu d'une SameFileError. Patch local,
    # restaure dans le finally en sortie.
    import src.labeling.label_export as _le
    import shutil as _real_shutil

    class _SmartShutil:
        @staticmethod
        def copy(src, dst, *a, **kw):
            if Path(src).resolve() == Path(dst).resolve():
                return dst
            return _real_shutil.copy(src, dst, *a, **kw)

    _orig_shutil = _le.shutil
    _le.shutil = _SmartShutil
    # Mute les print() verbeux de LARD (Labelling Pose / ORIENTATION / not labelled)
    # en redirigeant stdout vers un buffer jete a la fin du with (rien n'est ecrit).
    import contextlib, io
    try:
        with _lard_cwd(), contextlib.redirect_stdout(io.StringIO()):
            export_labels(
                dataset_type=DatasetTypes.XPLANE,
                yaml_scenario_path=yaml_path,
                export_dir=out_dir,
                out_labels_file=csv_file,
                out_images_dir=images_dir,
            )
    finally:
        _le.shutil = _orig_shutil

    print(f"  metadata.csv (GT) -> {csv_file}")
    return str(csv_file)


# ---------------------------------------------------------------------------
# Generation GT pour un run + annotation visuelle
# ---------------------------------------------------------------------------

def generate_gt_csv(scenario_dir, out_dir, images_dir):
    """Genere le CSV verite terrain LARD brut (labels_lard.csv) pour un scenario.

    Lit le .yaml de scenario_dir et les images de images_dir, ecrit
    out_dir/labels_lard.csv. Ce CSV brut est ensuite enrichi en metadata.csv
    par metadata.build_metadata_csv.

    :param scenario_dir: dossier scenarios/<batch>/<scenario_name>/ (contient le .yaml)
    :param out_dir: dossier dataset .../<scenario_name>/ (sortie du CSV)
    :param images_dir: dossier des images (dataset .../images/)
    :return: Path du CSV GT brut
    """
    scenario_dir = Path(scenario_dir)
    yamls = list(scenario_dir.glob("*.yaml"))
    if not yamls:
        raise FileNotFoundError(f"Pas de .yaml dans {scenario_dir}")

    print(f"\n  [GT] labels LARD pour {scenario_dir.name}...")
    csv_file = generate_labels_csv(
        yaml_path=str(yamls[0]), out_dir=str(out_dir), images_dir=str(images_dir),
        csv_name="labels_lard.csv",
    )
    return Path(csv_file)
