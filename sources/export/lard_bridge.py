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

# Bootstrap sys.path via sources/_paths.py (parent de sources/export/)
_PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))
import _paths  # noqa: F401

LARD_ROOT = _paths.LARD_ROOT
PROJECT_ROOT = _paths.ROOT
# DB LARD X-Plane (meme DB que le labeling LARD pour coherence trajectoire/GT)
RUNWAY_DB_XPLANE = str(LARD_ROOT / "data" / "runways_db_V2_XPlane.json")

from src.geo.geo_dataset import compute_aiming_point
from src.geo.geo_utils import ecef2llh
from src.labeling.label_export import export_labels
from src.labeling.export_config import DatasetTypes
from runway import reciprocal_runway, runway_from_run_name


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


def generate_labels_csv(yaml_path, dataset_dir, csv_name=None):
    """
    Genere le .csv ground truth LARD a partir du .yaml et des images dans footage/.
    A appeler APRES avoir recupere les images X-Plane.

    :param yaml_path: chemin vers le .yaml du scenario
    :param dataset_dir: dossier contenant footage/ avec les images
    :param csv_name: nom du fichier CSV (defaut: <yaml_stem>_labels.csv)
    """
    yaml_path = Path(yaml_path).resolve()
    dataset_dir = Path(dataset_dir).resolve()

    if csv_name is None:
        csv_name = f"{yaml_path.stem}_labels.csv"
    csv_file = dataset_dir / csv_name
    footage_dir = dataset_dir / "footage"

    # LARD recopie les images dans `out_images_dir` (typiquement exported_images/)
    # avant de generer le CSV. On veut juste pointer ce CSV sur les images deja
    # presentes dans footage/, sans duplication.
    # Astuce : on passe out_images_dir = footage_dir et on monkey-patch shutil
    # dans le module label_export pour qu'une copie src->dst identique soit
    # un no-op au lieu d'une SameFileError. Le patch est local au module et
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
                export_dir=dataset_dir,
                out_labels_file=csv_file,
                out_images_dir=footage_dir,
            )
    finally:
        _le.shutil = _orig_shutil

    print(f"  .csv GT -> {csv_file}")
    return str(csv_file)


# ---------------------------------------------------------------------------
# Generation GT pour un run + annotation visuelle
# ---------------------------------------------------------------------------

def generate_gt(run_dir):
    """Genere le CSV ground truth LARD pour un run.

    Lit run_dir/<stem>.yaml et les images dans run_dir/footage/, ecrit
    run_dir/<stem>_labels.csv.

    :return: Path du CSV genere
    """
    from pathlib import Path
    run_dir = Path(run_dir)
    yamls = list(run_dir.glob("*.yaml"))
    if not yamls:
        raise FileNotFoundError(f"Pas de .yaml dans {run_dir}")
    yaml_path = yamls[0]

    print(f"\n  [GT] Generation CSV pour {run_dir.name}...")

    csv_file = generate_labels_csv(
        yaml_path=str(yaml_path),
        dataset_dir=str(run_dir),
    )
    return Path(csv_file)


def load_gt_corners(csv_path, runway=None):
    """Charge un *_labels.csv LARD et retourne {image_name: [[(x,y) x 4], ...]}.

    Si runway fourni, filtre sur cette piste + son reciprocal (meme bande physique).
    Sinon retourne toutes les pistes.

    Coordonnees retournees en float (cast a int au besoin par l'appelant).
    """
    import csv as csvmod

    keep = None
    if runway:
        def _norm(r):
            return str(r).lstrip("0") or "0"
        keep = {_norm(runway), _norm(reciprocal_runway(runway))}

    out = {}
    with open(csv_path, "r") as f:
        reader = csvmod.DictReader(f, delimiter=";")
        for row in reader:
            if keep is not None:
                rwy = str(row.get("runway", "")).lstrip("0") or "0"
                if rwy not in keep:
                    continue
            fname = Path(row["image"]).name
            corners = [
                (float(row["x_TR"]), float(row["y_TR"])),
                (float(row["x_TL"]), float(row["y_TL"])),
                (float(row["x_BL"]), float(row["y_BL"])),
                (float(row["x_BR"]), float(row["y_BR"])),
            ]
            out.setdefault(fname, []).append(corners)
    return out


def annotate_gt(run_dir, csv_path=None, runway=None, max_images=0,
                out_dir=None, prefix="gt_"):
    """Dessine les bbox GT LARD sur les images dans `out_dir`.

    :param run_dir: dossier du run (contient footage/ + <stem>_labels.csv)
    :param csv_path: CSV GT (defaut: auto-detecte run_dir/*_labels.csv)
    :param runway: filtre une piste (defaut: extrait du nom du run).
                   Le reciprocal est aussi accepte (meme bande physique).
    :param max_images: 0 = toutes
    :param out_dir: dossier de sortie (defaut: run_dir/annotated_lard/)
    :param prefix: prefixe des fichiers de sortie (defaut: "gt_")
    """
    from PIL import Image, ImageDraw, ImageFont

    run_dir = Path(run_dir)
    footage_dir = run_dir / "footage"
    out_dir = Path(out_dir) if out_dir is not None else run_dir / "annotated_lard"
    out_dir.mkdir(parents=True, exist_ok=True)

    if csv_path is None:
        csvs = list(run_dir.glob("*_labels.csv"))
        if not csvs:
            raise FileNotFoundError(f"Pas de *_labels.csv dans {run_dir}")
        csv_path = csvs[0]

    target_runway = runway or runway_from_run_name(run_dir.name)

    # Police lisible (arial sur Windows, DejaVu/Liberation sur Linux)
    font = None
    for ttf in ["arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]:
        try:
            font = ImageFont.truetype(ttf, 20)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    # Reutilise le parser commun, cast en int pour PIL
    raw_entries = load_gt_corners(csv_path, runway=target_runway)
    entries = {
        fname: [
            {"corners": tuple((int(x), int(y)) for x, y in corners),
             "runway": target_runway or "?"}
            for corners in corners_list
        ]
        for fname, corners_list in raw_entries.items()
    }

    COLORS = ["cyan", "red", "yellow", "lime", "magenta", "orange"]
    runway_colors = {}
    color_idx = 0
    processed = 0

    # Affichage : la piste d'approche (pas le reciprocal LARD)
    display_runway = target_runway

    for filename in sorted(entries.keys()):
        if max_images > 0 and processed >= max_images:
            break
        img_path = footage_dir / filename
        if not img_path.exists():
            continue

        img = Image.open(img_path).convert("RGB")
        draw = ImageDraw.Draw(img)

        for entry in entries[filename]:
            rwy = display_runway or entry["runway"]
            if rwy not in runway_colors:
                runway_colors[rwy] = COLORS[color_idx % len(COLORS)]
                color_idx += 1
            color = runway_colors[rwy]
            c = entry["corners"]
            for i in range(4):
                draw.line([c[i], c[(i + 1) % 4]], fill=color, width=1)
            cx = sum(p[0] for p in c) // 4
            cy = min(p[1] for p in c) - 25
            bbox = draw.textbbox((cx, cy), rwy, font=font)
            draw.rectangle([bbox[0] - 2, bbox[1] - 2, bbox[2] + 2, bbox[3] + 2], fill="black")
            draw.text((cx, cy), rwy, fill=color, font=font)

        img.save(out_dir / f"{prefix}{filename}")
        processed += 1

    print(f"  [GT-VIS] {processed} images annotees dans {out_dir.name}/")
