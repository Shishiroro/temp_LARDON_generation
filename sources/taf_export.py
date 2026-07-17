"""
taf_export.py — Point d'entree TAF (Phase 1 uniquement)
=======================================================
Ce fichier est le POINT D'EXTENSION de TAF : taf_generate.run() le copie en
taf/src/Export.py et en sources/Export.py, et taf/src/Tree.py y lie export()
AU CHARGEMENT (`from Export import export`, pendant `import Taf`).

  export(root_node, path)
      Callback appele par z3 une fois par test case. Lit les parametres du
      scenario, calcule la trajectoire, et ecrit dans `path` (sas TAF,
      output/taf/...) : .yaml + poses camera + profils fautes/meteo.

Chaine : root_node -> TrajectoryConfig -> build_trajectory -> .yaml + poses

La Phase 2 (rendu, fautes, GT) vit dans render.py : TAF ne l'appelle jamais et
les deux moities ne partageaient aucun symbole.
"""

import math
import xml.etree.ElementTree as ET
from pathlib import Path

from trajectory_builder import (
    TrajectoryConfig,
    OUParams,
    build_trajectory
)
from lard_bridge import get_runway_geometry, export_scenario
from xplane_bridge import save_poses_json
from sensor_faults import (
    FaultConfig,
    KNOWN_FAULT_TYPES,
    validate_faults,
    save_fault_profile,
)
from xplane_weather import (
    WeatherConfig,
    validate_weather,
    has_weather,
    save_weather_profile,
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
    # Sauvegarde dans poses_cam_export.json : sert a remplir la colonne
    # `weather` du metadata.csv (cote pipeline, metadata.py).
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

