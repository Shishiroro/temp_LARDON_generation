"""
Export.py — Point d'entree TAF + Phase 2 (generation d'images + GT)
====================================================================
Deux fonctions independantes hebergees dans le meme fichier :

  1. export(root_node, path)
     Callback TAF appele pendant la generation z3. Ecrit .yaml + JSON
     configs dans output/test_artifact_X (temporaire).

  2. render_run(run_dir, xplane_dir)
     Orchestrateur Phase 2 appele par run_pipeline.py sur chaque run_dir.
     Seul point d'entree pour produire la donnee brute :
       step_render -> step_faults -> step_ground_truth
     TAF n'invoque jamais cette fonction.

Chaine TAF : root_node -> TrajectoryConfig -> build_trajectory -> .yaml
Chaine pipeline : run_dir -> step_render -> step_faults -> step_ground_truth
"""

import sys
import math
import xml.etree.ElementTree as ET
from pathlib import Path

# Bootstrap sys.path via sources/_paths.py (parent dossier de sources/export/)
_PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))
import _paths  # noqa: F401

from trajectory_builder import TrajectoryConfig, OUParams, build_trajectory
from lard_bridge import get_runway_geometry, export_scenario
from xplane_bridge import save_poses_json
from sensor_faults import (
    FaultConfig, KNOWN_FAULT_TYPES, validate_faults, save_fault_profile,
)
from xplane_weather import (
    WeatherConfig, validate_weather, has_weather, save_weather_profile,
)

 
def _read_param(node, name):
    """Lit un parametre TAF depuis un node (instance 0)."""
    return node.get_parameter_n(name).values[0]


def export(root_node, path):
    """
    Point d'entree TAF. Appele pour chaque test_artifact genere.

    :param root_node: Node racine de l'arbre TAF (apres generation z3)
    :param path: chemin du dossier test_artifact courant
    """
    scenario_node = root_node.get_child_n("scenario")
    trajectory_node = scenario_node.get_child_n("trajectory")
    weather_node = scenario_node.get_child_n("weather")
    settings_node = scenario_node.get_child_n("settings")
    faults_node = scenario_node.get_child_n("faults")

    # --- Trajectoire ---
    fps = int(_read_param(trajectory_node, "fps"))
    along_track_distance_start = float(_read_param(trajectory_node, "along_track_distance_start"))
    along_track_distance_end = float(_read_param(trajectory_node, "along_track_distance_end"))
    ground_speed_kts = float(_read_param(trajectory_node, "ground_speed_kts"))
    turbulence_intensity = float(_read_param(trajectory_node, "turbulence_intensity"))
    wind_speed_kts = float(_read_param(trajectory_node, "wind_speed_kts"))
    wind_direction_deg = float(_read_param(trajectory_node, "wind_direction_deg"))
    stabilization_distance_m = float(_read_param(trajectory_node, "stabilization_distance_m"))

    # airport_runway : format "ICAO_RWY" (ex: "LFPO_06")
    airport_runway = str(_read_param(trajectory_node, "airport_runway"))
    if "_" not in airport_runway:
        raise ValueError(f"Format airport_runway invalide : '{airport_runway}' (attendu: ICAO_RWY)")
    airport, runway = airport_runway.split("_", 1)

    # --- Fautes capteur (severity > 0 = actif) ---
    # XML : start_pct + duration_pct (configurables par faute)
    # Interne : FaultConfig garde from_pct/to_pct ; to_pct = start + duration
    faults = []
    for fault_type in sorted(KNOWN_FAULT_TYPES):
        fault_node = faults_node.get_child_n(fault_type)
        severity = float(_read_param(fault_node, "severity"))
        if severity > 0:
            start_pct = float(_read_param(fault_node, "start_pct"))
            duration_pct = float(_read_param(fault_node, "duration_pct"))
            from_pct = start_pct
            to_pct = min(start_pct + duration_pct, 100.0)
            extra = {}
            if fault_type == "channel_swap":
                extra["order"] = [int(_read_param(fault_node, f"c{i}")) for i in range(3)]
            faults.append(FaultConfig(fault_type, severity, from_pct, to_pct, extra))

    if faults:
        validate_faults(faults)
        fault_str = ", ".join(f"{f.fault_type}({f.severity:.2f})[{f.from_pct:.0f}-{f.to_pct:.0f}%]"
                              for f in faults)
        print(f"[Export] Fautes capteur : {fault_str}")

    # --- Meteo X-Plane (per-scenario) ---
    # Les params meteo sont lus tels quels depuis le XML actif (base.xml
    # ou une variante de profil generee dans templates/<profil>/). Le choix du
    # profil se fait en pointant settings.xml sur le bon XML, pas dans le code.
    weather_cfg = WeatherConfig(
        precip_rate=float(_read_param(weather_node, "precip_rate")),
        cloud_type=float(_read_param(weather_node, "cloud_type")),
        cloud_coverage=float(_read_param(weather_node, "cloud_coverage")),
        cloud_margin_m=float(_read_param(weather_node, "cloud_margin_m")),
        cloud_thickness_m=float(_read_param(weather_node, "cloud_thickness_m")),
        fog_visibility=float(_read_param(weather_node, "fog_visibility")),
        temperature_c=float(_read_param(weather_node, "temperature_c")),
        time_of_day_h=float(_read_param(settings_node, "time_of_day_h")),
        rain_scale=float(_read_param(weather_node, "rain_scale")),
        weather_zone_radius_nm=float(_read_param(settings_node, "weather_zone_radius_nm")),
        load_texture_duration=float(_read_param(settings_node, "load_texture_duration")),
        weather_effect_duration=float(_read_param(weather_node, "weather_effect_duration")),
    )

    # Delai d'attente apres chaque teleport camera, avant la capture (en s).
    # A regler par machine : plus le GPU est lent a charger les textures, plus
    # il faut augmenter cette valeur pour eviter les flashs gris a l'ecran.
    # Independant de la meteo ; relu cote rendu via poses_cam_export.json
    # (champ trajectory.screenshot_duration) -> XPlaneConfig.settle_time.
    screenshot_duration = float(_read_param(settings_node, "screenshot_duration"))

    if has_weather(weather_cfg):
        validate_weather(weather_cfg)
        parts = []
        if weather_cfg.precip_rate > 0:
            parts.append(f"precip={weather_cfg.precip_rate:.2f}")
        if weather_cfg.cloud_type >= 0:
            parts.append(f"cloud_type={weather_cfg.cloud_type:.0f} cov={weather_cfg.cloud_coverage:.1f}")
        if weather_cfg.fog_visibility < 50000:
            parts.append(f"vis={weather_cfg.fog_visibility:.0f}m")
        if weather_cfg.temperature_c < 0:
            parts.append(f"temp={weather_cfg.temperature_c:.0f}C")
        if weather_cfg.time_of_day_h != 12.0:
            parts.append(f"heure={weather_cfg.time_of_day_h:.1f}h")
        print(f"[Export] Meteo : {', '.join(parts)}")

    # --- Calcul auto de tau (Dryden simplifie) ---
    # Modele turbulence atmospherique : tau ~= h / V, ou h est l'altitude
    # geometrique en debut de segment (deduite du glide slope 3°) et V la
    # vitesse sol. Plus on est haut/lent, plus la turbulence est correlee.
    h_m = along_track_distance_start * math.tan(math.radians(3.0))
    speed_ms = ground_speed_kts * 0.514444  # 1 kt = 0.514444 m/s
    correlation_time_s = h_m / speed_ms

    # --- Construire les configs ---
    cfg = TrajectoryConfig(
        fps=fps,
        along_track_distance_start=along_track_distance_start,
        along_track_distance_end=along_track_distance_end,
        ground_speed_kts=ground_speed_kts,
        correlation_time_s=correlation_time_s,
        turbulence_intensity=turbulence_intensity,
        wind_speed_kts=wind_speed_kts,
        wind_direction_deg=wind_direction_deg,
        stabilization_distance_m=stabilization_distance_m,
    )
    ou = OUParams(alpha_h_offset_deg=0, alpha_v_offset_deg=0)

    # --- Geometrie piste ---
    rwy = get_runway_geometry(airport, runway)

    # --- Generer la trajectoire ---
    print(f"[Export] {airport}/{runway} | fps={fps} | "
          f"dist=[{along_track_distance_start:.0f}-{along_track_distance_end:.0f}m] | "
          f"wind={wind_speed_kts:.0f}kts@{wind_direction_deg:.0f}deg")

    flight_data, _, _ = build_trajectory(
        cfg, ou,
        ltp_lat=rwy["ltp_lat"],
        ltp_lon=rwy["ltp_lon"],
        ltp_alt=rwy["ltp_alt"],
        runway_heading_deg=rwy["runway_heading_deg"],
        runway_back_azimuth_deg=rwy["runway_back_azimuth_deg"],
    )

    # --- Exporter .yaml (format LARD) ---
    weather_arg = weather_cfg if has_weather(weather_cfg) else None
    scenario_name = f"{airport}_{runway}"
    export_scenario(
        flight_data, cfg, ou,
        airport, runway,
        output_dir=path,
        scenario_name=scenario_name,
        faults=faults,
        weather=weather_arg,
    )

    # Nom du template XML actif (lu depuis settings.xml, CWD = sources/).
    # Sauvegarde dans poses_cam_export.json pour que le notebook puisse
    # remplir la colonne `weather` du metadata.csv.
    template_file_name = ""
    try:
        settings_tree = ET.parse("settings.xml")
        for p in settings_tree.getroot():
            if p.attrib.get("name") == "template_file_name":
                template_file_name = p.attrib.get("value", "")
                break
    except (ET.ParseError, OSError):
        pass

    # --- Sauver poses_cam_export.json (format X-Plane, coords locales) ---
    save_poses_json(
        flight_data, cfg.fps, scenario_name,
        output_path=Path(path) / "poses_cam_export.json",
        ltp_alt=rwy["ltp_alt"],
        trajectory_config={
            "along_track_distance_start": along_track_distance_start,
            "along_track_distance_end": along_track_distance_end,
            "ground_speed_kts": ground_speed_kts,
            "turbulence_intensity": turbulence_intensity,
            "wind_speed_kts": wind_speed_kts,
            "wind_direction_deg": wind_direction_deg,
            "stabilization_distance_m": stabilization_distance_m,
            "airport_runway": f"{airport}_{runway}",
            "screenshot_duration": screenshot_duration,
            "template_file_name": template_file_name,
        },
    )

    # --- Sauver le profil de fautes (si actif) ---
    if faults:
        save_fault_profile(
            faults,
            n_frames=len(flight_data),
            output_path=Path(path) / "fault_profile.json",
        )

    # --- Sauver le profil meteo (si actif) ---
    if has_weather(weather_cfg):
        save_weather_profile(
            weather_cfg,
            output_path=Path(path) / "weather_profile.json",
        )


# ===========================================================================
# Phase 2 : Generation d'images + GT (post-TAF, par run_dir)
# ---------------------------------------------------------------------------
# Les fonctions ci-dessous sont appelees apres que TAF a fini sa generation
# et que les runs ont ete deplaces dans runs/<name>/.
# TAF n'utilise QUE export() ci-dessus, jamais ce qui suit.
# Export.render_run est le SEUL point d'entree pour produire la donnee brute :
#   images (footage/, degraded/) + ground truth LARD (*_labels.csv).
# ===========================================================================

def step_render(run_dir, xplane_dir):
    """Rendu X-Plane 12 (meteo injectee a l'interieur si profil present).

    :return: True si images presentes apres rendu, False sinon
    """
    from runs import has_images
    from xplane_bridge import render_xplane_run

    run_dir = Path(run_dir)

    if not render_xplane_run(run_dir, xplane_dir or ""):
        print(f"  [Image] Echec rendu X-Plane pour {run_dir.name}")
        return False

    if not has_images(run_dir):
        print(f"  [Image] Pas d'images dans footage/ pour {run_dir.name}")
        return False

    return True


def step_faults(run_dir):
    """Applique les fautes capteur si fault_profile.json present (no-op sinon).

    Les exceptions sont logees mais ne bloquent pas le pipeline : la presence
    de fautes est optionnelle.
    """
    from sensor_faults import apply_faults

    try:
        apply_faults(run_dir)
    except Exception as e:
        print(f"  [Image] FAULTS ERREUR : {e}")


def step_ground_truth(run_dir):
    """Genere le CSV GT LARD (projection 3D->2D des coins piste).

    Pure geometrie (offline, pas besoin de X-Plane), mais necessite que
    footage/ existe car LARD parcourt les images pour produire le CSV.
    Skip si <name>_labels.csv existe deja (idempotent comme step_render).

    :return: True si CSV present apres l'etape (genere ou existant), False sinon
    """
    from lard_bridge import generate_gt

    run_dir = Path(run_dir)
    if list(run_dir.glob("*_labels.csv")):
        print(f"  [Image] GT deja present, skip")
        return True

    try:
        generate_gt(run_dir)
    except Exception as e:
        print(f"  [Image] GT ERREUR : {e}")
        return bool(list(run_dir.glob("*_labels.csv")))

    return True


def render_run(run_dir, xplane_dir):
    """Phase 2 : seule fonction qui produit la donnee brute d'un run.

    Enchaine sur run_dir (deja existant dans runs/, avec ses configs JSON) :
      1. step_render          -> footage/*.jpg          (X-Plane + meteo)
      2. step_faults          -> degraded/*.jpg         (si fault_profile.json)
      3. step_ground_truth    -> <name>_labels.csv      (GT LARD)

    Le banc d'evaluation (evaluation/) consomme ces sorties (images + GT) ;
    il ne genere plus de GT lui-meme.

    :param run_dir: dossier du run dans runs/
    :param xplane_dir: chemin vers X-Plane 12
    :return: True si rendu reussi, False sinon
    """
    run_dir = Path(run_dir)
    print(f"\n  [Image] Rendu + fautes + GT pour {run_dir.name}")

    if not step_render(run_dir, xplane_dir):
        return False

    step_faults(run_dir)
    step_ground_truth(run_dir)

    return True
