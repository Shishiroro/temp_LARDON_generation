"""injection_weather_test.py — Test rapide d'injection meteo X-Plane 12
======================================================================
Injecte UNIQUEMENT la meteo definie dans le XML actuellement selectionne
(sources/settings.xml -> template_file_name), sans lancer aucun scenario
ni rendre d'images.

Comportement :
  - Lit sources/settings.xml pour resoudre le XML actif.
  - Lit le XML TAF (par defaut moyenne (min+max)/2 par parametre).
  - Pause la sim + vue externe (pas de cockpit) via setup_view().
  - Teleporte l'avion / camera 100m au-dessus de la pose initiale stockee
    dans runs/<run>/poses_cam_export.json.
  - L'altitude cible est calculee a partir du JSON, pas de la pose
    courante du sim => le script est idempotent (relancer ne rajoute pas
    +100m supplementaires).
  - Injecte la meteo via le plugin XPPython3 (PI_weather.py).
  - La sim reste en pause apres l'injection : tu peux observer la scene
    directement dans X-Plane.

Usage :
  py injection_weather_test.py
  py injection_weather_test.py --run generation_01/LFPO_24
  py injection_weather_test.py --xplane-dir "C:/X-Plane 12" --alt-offset 200
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sources" / "export"))

from xplane_bridge import XPlaneConnection, XPlaneConfig, load_poses_json  # noqa: E402
from xplane_weather import (  # noqa: E402
    WeatherConfig, set_exchange_dir, check_plugin, inject_weather,

)


# ---------------------------------------------------------------------------
# Lecture XML TAF (template_file_name) -> WeatherConfig
# ---------------------------------------------------------------------------

def _param_mid(node, name):
    """Renvoie la moyenne (min+max)/2 d'un parametre TAF.

    Si min == max (cas usuel pour les profils figes) on retombe sur la
    valeur exacte. Pour les params samples (ex: cloud_margin_m), on prend
    le milieu de la plage pour avoir un comportement deterministe.
    """
    for p in node.findall("parameter"):
        if p.attrib.get("name") == name:
            mn = float(p.attrib["min"])
            mx = float(p.attrib["max"])
            return (mn + mx) / 2.0
    raise KeyError(f"parametre '{name}' manquant dans <node name='{node.attrib.get('name')}'>")


def read_weather_from_template(template_path: Path) -> WeatherConfig:
    tree = ET.parse(template_path)
    root = tree.getroot()
    weather_node = root.find(".//node[@name='weather']")
    settings_node = root.find(".//node[@name='settings']")
    if weather_node is None or settings_node is None:
        raise ValueError(f"node 'weather' ou 'settings' manquant dans {template_path}")
    # cloud_type est un enum (0..3) : on arrondit le milieu de plage a l'entier
    # le plus proche pour previsualiser un vrai type. Sinon une plage [0,3]
    # donne 1.5 -> affiche "2" (arrondi) mais injecte int(1.5)=1 (Stratus).
    return WeatherConfig(
        precip_rate=_param_mid(weather_node, "precip_rate"),
        cloud_type=round(_param_mid(weather_node, "cloud_type")),
        cloud_coverage=_param_mid(weather_node, "cloud_coverage"),
        cloud_thickness_m=_param_mid(weather_node, "cloud_thickness_m"),
        fog_visibility=_param_mid(weather_node, "fog_visibility"),
        temperature_c=_param_mid(weather_node, "temperature_c"),
        rain_scale=_param_mid(weather_node, "rain_scale"),
        cloud_margin_m=_param_mid(weather_node, "cloud_margin_m"),
        weather_effect_duration=_param_mid(weather_node, "weather_effect_duration"),
        time_of_day_h=_param_mid(settings_node, "time_of_day_h"),
        weather_zone_radius_nm=_param_mid(settings_node, "weather_zone_radius_nm"),
        load_texture_duration=_param_mid(settings_node, "load_texture_duration"),
    )


def resolve_template_path() -> Path:
    settings_path = ROOT / "sources" / "settings.xml"
    params = {p.attrib["name"]: p.attrib["value"]
              for p in ET.parse(settings_path).getroot()}
    return ROOT / "sources" / params["template_path"].rstrip("/") / params["template_file_name"]


# ---------------------------------------------------------------------------
# Resolution run par defaut (le plus recent)
# ---------------------------------------------------------------------------

def find_latest_run(runs_dir: Path) -> Path:
    """Trouve le run le plus recent (structure runs/<generation>/<ICAO_RWY>/).

    Recherche recursive : poses_cam_export.json vit desormais sous
    runs/<generation>/<run>/ (un dossier par batch, sous-dossier par scenario).
    """
    candidates = [p.parent for p in runs_dir.rglob("poses_cam_export.json")]
    if not candidates:
        raise FileNotFoundError(f"Aucun run avec poses_cam_export.json dans {runs_dir}")
    return max(candidates, key=lambda d: (d / "poses_cam_export.json").stat().st_mtime)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _settings = {p.attrib["name"]: p.attrib["value"]
                 for p in ET.parse(ROOT / "sources" / "settings.xml").getroot()}
    default_xp = _settings["xplane_dir"]
    parser.add_argument("--xplane-dir", type=str, default=default_xp,
                        help=f"Repertoire X-Plane 12 (defaut: {default_xp})")
    parser.add_argument("--run", type=str, default=None,
                        help="Chemin compose <generation>/<run> (ex: generation_01/LFPO_24) "
                             "ou le plus recent dans runs/ par defaut")
    parser.add_argument("--alt-offset", type=float, default=100.0,
                        help="Offset altitude au-dessus de la pose initiale (m, defaut: 100)")
    args = parser.parse_args()

    # 1. XML actif + meteo
    template_path = resolve_template_path()
    print(f"[XML] template      : {template_path.relative_to(ROOT)}")
    weather_cfg = read_weather_from_template(template_path)
    print(f"[XML] meteo         : precip={weather_cfg.precip_rate:.2f} "
          f"cloud_type={weather_cfg.cloud_type:.0f} coverage={weather_cfg.cloud_coverage:.2f} "
          f"vis={weather_cfg.fog_visibility:.0f}m temp={weather_cfg.temperature_c:.0f}C "
          f"rain_scale={weather_cfg.rain_scale:.1f}")

    # 2. Pose initiale (reference = JSON, donc idempotent)
    runs_dir = ROOT / "runs"
    run_dir = (runs_dir / args.run) if args.run else find_latest_run(runs_dir)
    if not run_dir.exists():
        print(f"[ERREUR] run introuvable : {run_dir}")
        return 1
    poses_data = load_poses_json(run_dir / "poses_cam_export.json")
    first = poses_data["poses"][0]
    target_lat = float(first["lat"])
    target_lon = float(first["lon"])
    target_alt = float(first["alt_m"]) + float(args.alt_offset)
    target_heading = float(first["heading"])
    target_pitch = float(first["pitch"]) - 90.0  # stocke (90=level) -> X-Plane (0=level)
    target_roll = float(first["roll"])
    print(f"[POSE] run          : {run_dir.relative_to(ROOT)}")
    print(f"[POSE] cible        : lat={target_lat:.6f} lon={target_lon:.6f} "
          f"alt={target_alt:.1f}m  (origin {first['alt_m']:.1f}m + {args.alt_offset:.0f}m)")

    # 3. Connexion X-Plane + setup vue (pause, override, no-cockpit, FOV)
    config = XPlaneConfig(xplane_dir=args.xplane_dir)
    conn = XPlaneConnection(config)
    if not conn.check_connection():
        print(f"[ERREUR] X-Plane injoignable ({config.host}:{config.port}).")
        return 1
    print(f"[XPLANE] connexion OK -> {config.host}:{config.port}")
    print(f"[XPLANE] setup_view (pause sim, override planepath, vue externe)...")
    conn.setup_view()

    # 4. Teleport
    print(f"[XPLANE] teleport pose initiale + {args.alt_offset:.0f}m")
    conn.set_camera_pose(target_lat, target_lon, target_alt,
                         target_heading, target_pitch, target_roll)

    # 5. Injection meteo
    set_exchange_dir(args.xplane_dir)
    if not check_plugin():
        print(f"[ERREUR] Plugin PI_weather.py inactif. Verifie XPPython3 dans X-Plane.")
        return 1
    aircraft_max_alt = max(float(p["alt_m"]) for p in poses_data["poses"]) + float(args.alt_offset)
    ok = inject_weather(weather_cfg, aircraft_max_alt_m=aircraft_max_alt,
                        latitude=target_lat, longitude=target_lon)
    if not ok:
        print(f"[ERREUR] injection meteo echouee")
        return 1

    print(f"[OK] meteo injectee, avion teleporte, sim en pause.")
    print(f"     Idempotent : relancer ce script remet l'avion exactement "
          f"a alt_origine+{args.alt_offset:.0f}m (pas de cumul).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
