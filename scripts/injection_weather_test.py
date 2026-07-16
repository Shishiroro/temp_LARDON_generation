"""injection_weather_test.py — Test rapide d'injection meteo X-Plane 12
======================================================================
Injecte UNIQUEMENT la meteo definie dans le XML actuellement selectionne
(sources/settings.xml -> template_file_name), sans lancer aucun scenario
ni rendre d'images.

Ne depend d'AUCUN scenario : on injecte la meteo sur la pose COURANTE de
l'avion (ton spawn sur la piste). Par defaut l'avion NE BOUGE PAS ; deux
arguments optionnels permettent de le decaler pour observer la scene.

Deux arguments (metres), relatifs a la position COURANTE, 0 par defaut :
    1er (vertical)    hauteur AU-DESSUS de la position courante
    2e  (horizontal)  recul DERRIERE la position courante (dans l'axe du cap)

Seule la POSITION change : le cap, le pitch et le roll courants sont conserves
tels quels, la vue ne bouge jamais.

NB : les offsets sont relatifs a la position COURANTE, donc relancer le script
deplace a nouveau l'avion depuis sa nouvelle position (les deplacements se
cumulent). Repositionner l'avion se fait a la main dans le sim.

La sim reste en pause apres l'injection : tu peux observer la scene
directement dans X-Plane.

Usage :
  py injection_weather_test.py                  # defaut : meteo seule, avion immobile
  py injection_weather_test.py 300 500          # <vertical> <horizontal>
  py injection_weather_test.py 400 0            # monte, sans reculer
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from pyproj import Geod

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT / "sources"))

from config import XPLANE_DIR  # noqa: E402
from xplane_bridge import XPlaneConnection, XPlaneConfig  # noqa: E402
from xplane_weather import (  # noqa: E402
    WeatherConfig, set_exchange_dir, check_plugin, inject_weather,
)

_GEOD = Geod(ellps="WGS84")


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
# Pose courante + deplacement
# ---------------------------------------------------------------------------

def read_current_pose(conn):
    """Pose courante de l'avion : (lat, lon, alt_m, heading, pitch, roll).

    Lue via RREF (float32) : precision ~1 m sur lat/lon, largement suffisant.
    A lire AVANT setup_view(), donc avant l'override de planepath.
    """
    lat = conn.read_dref("sim/flightmodel/position/latitude")
    lon = conn.read_dref("sim/flightmodel/position/longitude")
    alt = conn.read_dref("sim/flightmodel/position/elevation")
    hdg = conn.read_dref("sim/flightmodel/position/psi")      # cap vrai
    pitch = conn.read_dref("sim/flightmodel/position/theta")  # assiette
    roll = conn.read_dref("sim/flightmodel/position/phi")     # inclinaison
    if None in (lat, lon, alt, hdg, pitch, roll):
        raise RuntimeError(
            "Lecture de la pose X-Plane impossible (timeout RREF). "
            "Verifie que le sim tourne et qu'un avion est charge sur une piste."
        )
    return lat, lon, alt, hdg, pitch, roll


def offset_position(lat, lon, alt, heading, vertical_m, horizontal_m):
    """Recule de horizontal_m derriere la position et monte de vertical_m.

    Seule la POSITION change : le cap/pitch/roll sont conserves par l'appelant
    (on ne touche jamais a l'orientation de la vue).

    :return: (lat, lon, alt_m)
    """
    # Recul : on avance de horizontal_m dans l'azimut oppose au cap.
    lon2, lat2, _ = _GEOD.fwd(lon, lat, heading + 180.0, horizontal_m)
    return lat2, lon2, alt + vertical_m


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("vertical", nargs="?", type=float, default=0.0,
                        help="1er argument : hauteur au-dessus de la position "
                             "courante (m, defaut: 0 = pas de deplacement)")
    parser.add_argument("horizontal", nargs="?", type=float, default=0.0,
                        help="2e argument : recul derriere la position courante "
                             "(m, defaut: 0 = pas de deplacement)")
    parser.add_argument("--xplane-dir", type=str, default=XPLANE_DIR,
                        help=f"Repertoire X-Plane 12 (defaut: {XPLANE_DIR or '(non defini)'})")
    args = parser.parse_args()

    if args.vertical or args.horizontal:
        print(f"[ARGS] 1er = vertical   : {args.vertical:.0f} m au-dessus de la position courante")
        print(f"[ARGS] 2e  = horizontal : {args.horizontal:.0f} m de recul (axe du cap)")
        print(f"[ARGS] offsets relatifs a la position COURANTE : relancer deplace "
              f"a nouveau depuis la nouvelle position.")
    else:
        print(f"[ARGS] aucun deplacement (avion immobile). Pour bouger : "
              f"<vertical> <horizontal> en metres, ex. 300 500")

    # 1. XML actif + meteo
    template_path = resolve_template_path()
    print(f"[XML] template      : {template_path.relative_to(ROOT)}")
    weather_cfg = read_weather_from_template(template_path)
    print(f"[XML] meteo         : precip={weather_cfg.precip_rate:.2f} "
          f"cloud_type={weather_cfg.cloud_type:.0f} coverage={weather_cfg.cloud_coverage:.2f} "
          f"vis={weather_cfg.fog_visibility:.0f}m temp={weather_cfg.temperature_c:.0f}C "
          f"rain_scale={weather_cfg.rain_scale:.1f}")

    # 2. Connexion X-Plane
    config = XPlaneConfig(xplane_dir=args.xplane_dir)
    conn = XPlaneConnection(config)
    if not conn.check_connection():
        print(f"[ERREUR] X-Plane injoignable ({config.host}:{config.port}).")
        return 1
    print(f"[XPLANE] connexion OK -> {config.host}:{config.port}")

    # 3. Pose courante (AVANT setup_view : override planepath) -> pose decalee
    try:
        lat0, lon0, alt0, hdg, pitch, roll = read_current_pose(conn)
    except RuntimeError as e:
        print(f"[ERREUR] {e}")
        return 1
    lat, lon, alt = offset_position(lat0, lon0, alt0, hdg,
                                    args.vertical, args.horizontal)
    print(f"[POSE] courante     : lat={lat0:.6f} lon={lon0:.6f} alt={alt0:.1f}m "
          f"cap={hdg:.1f}deg pitch={pitch:.1f}deg roll={roll:.1f}deg")
    print(f"[POSE] cible        : +{args.vertical:.0f}m haut / -{args.horizontal:.0f}m recul "
          f"-> alt={alt:.1f}m (vue inchangee)")

    # 4. Setup vue (pause, override planepath, vue externe, FOV) puis teleport.
    #    On reinjecte le cap/pitch/roll courants : seule la position change.
    print(f"[XPLANE] setup_view (pause sim, override planepath, vue externe)...")
    conn.setup_view()
    print(f"[XPLANE] teleport (position seule, orientation conservee)")
    conn.set_camera_pose(lat, lon, alt, hdg, pitch, roll)

    # 5. Injection meteo (nuages places au-dessus de la camera)
    set_exchange_dir(args.xplane_dir)
    if not check_plugin():
        print(f"[ERREUR] Plugin PI_weather.py inactif. Verifie XPPython3 dans X-Plane.")
        return 1
    ok = inject_weather(weather_cfg, aircraft_max_alt_m=alt,
                        latitude=lat, longitude=lon)
    if not ok:
        print(f"[ERREUR] injection meteo echouee")
        return 1

    print(f"[OK] meteo injectee, avion deplace, sim en pause.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
