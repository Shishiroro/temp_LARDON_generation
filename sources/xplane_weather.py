"""
xplane_weather.py — Effets meteo X-Plane 12 via XPPython3 (v2, per-scenario)
=============================================================================
Gere la configuration et injection des effets meteo dans X-Plane 12
via le plugin PI_weather.py (XPPython3 / XPLMWeather API).

La meteo est injectee UNE FOIS avant le rendu d'un scenario (pas per-frame).
Deux delais distincts sont appliques avant la capture, configurables via XML :
  - load_texture_duration   : chargement des textures de la zone teleportee
    + stabilisation GPU des nuages. Vitesse sim normale, applique a TOUS
    les scenarios.
  - weather_effect_duration : accumulation des effets sol (flaques de pluie,
    couche de neige) en sim acceleree 8x. Applique uniquement si
    precip_rate > 0 ; mettre 0 pour de la pluie sans accumulation.

Parametres directs XP12 (plus de severity/from_pct/to_pct) :
  - precip_rate     : taux precipitation [0, 1]
  - cloud_type      : enum XPLMWeather (0=Cirrus, 1=Stratus, 2=Cumulus, 3=Cumulonimbus, -1=off)
  - cloud_coverage  : couverture nuageuse [0, 1]
  - fog_visibility  : visibilite en metres [500, 50000]
  - temperature_c   : temperature Celsius [-30, 45] — < 0 = neige
  - time_of_day_h   : heure locale [0, 24]

Communication par fichiers JSON :
  Python ecrit weather_command.json → plugin lit, applique, ecrit weather_status.json
"""

import os
import json
import time
from dataclasses import dataclass, asdict, fields
from datetime import datetime
from pathlib import Path

from timezonefinder import TimezoneFinder
import pytz

# Singleton — l'instanciation de TimezoneFinder est couteuse (chargement
# des polygones de fuseaux horaires), on la fait une seule fois au chargement
# du module.
_TZ_FINDER = TimezoneFinder()


# Delais par defaut (fallback) — les valeurs effectives sont lues depuis
# WeatherConfig (parametres XML load_texture_duration / weather_effect_duration).
DEFAULT_LOAD_TEXTURE_DURATION = 10.0    # chargement textures + stabilisation nuages GPU
DEFAULT_WEATHER_EFFECT_DURATION = 8.0   # accumulation flaques/neige (sim acceleree)

# Acceleration sim pendant la phase d'accumulation des effets sol (flaques,
# neige). La sim tourne a SIM_SPEED_BOOST puis revient a 1x.
# Ex: 8x pendant 8s reel = ~64s de meteo simulee.
SIM_SPEED_BOOST = 8


@dataclass
class WeatherConfig:
    """Configuration meteo X-Plane per-scenario (genere par TAF)."""
    precip_rate: float = 0.0
    cloud_type: float = -1.0         # -1 = pas de nuages
    cloud_coverage: float = 0.0
    cloud_margin_m: float = 200.0    # marge au-dessus de l'avion pour base nuages
    cloud_thickness_m: float = 2000.0  # epaisseur du nuage (alt_top = alt_base + thickness)
    fog_visibility: float = 50000.0  # visibilite en metres (brouillard si < 50000)
    temperature_c: float = 15.0
    time_of_day_h: float = 12.0
    rain_scale: float = 1.0          # taille visuelle des gouttes (sim/private/controls/rain/scale)
    weather_zone_radius_nm: float = 50.0  # rayon de la zone meteo XPLMWeather (nm ; 1 nm = 1.852 km)
    load_texture_duration: float = DEFAULT_LOAD_TEXTURE_DURATION      # delai chargement textures + stabilisation nuages (s, vitesse normale)
    weather_effect_duration: float = DEFAULT_WEATHER_EFFECT_DURATION  # delai accumulation flaques/neige (s, sim 8x ; ignore si precip=0)


def has_weather(config):
    """Retourne True si la config modifie la meteo par rapport au defaut."""
    return (config.precip_rate > 0
            or config.cloud_type >= 0
            or config.fog_visibility < 50000
            or config.temperature_c < 0
            or config.time_of_day_h != 12.0)


def validate_weather(config):
    """Valide les parametres meteo."""
    if not 0.0 <= config.precip_rate <= 1.0:
        raise ValueError(f"precip_rate={config.precip_rate} hors [0, 1]")
    if config.cloud_type not in (-1, 0, 1, 2, 3) and not (-1 <= config.cloud_type <= 3):
        raise ValueError(f"cloud_type={config.cloud_type} hors [-1, 3]")
    if not 0.0 <= config.cloud_coverage <= 1.0:
        raise ValueError(f"cloud_coverage={config.cloud_coverage} hors [0, 1]")
    if not 500 <= config.fog_visibility <= 50000:
        raise ValueError(f"fog_visibility={config.fog_visibility} hors [500, 50000]")
    if not -30 <= config.temperature_c <= 45:
        raise ValueError(f"temperature_c={config.temperature_c} hors [-30, 45]")
    if not 0 <= config.time_of_day_h <= 24:
        raise ValueError(f"time_of_day_h={config.time_of_day_h} hors [0, 24]")
    if not 0.1 <= config.rain_scale <= 5.0:
        raise ValueError(f"rain_scale={config.rain_scale} hors [0.1, 5.0]")
    if not 1.0 <= config.weather_zone_radius_nm <= 10000.0:
        raise ValueError(f"weather_zone_radius_nm={config.weather_zone_radius_nm} hors [1, 10000]")
    if not 0.0 <= config.load_texture_duration <= 60.0:
        raise ValueError(f"load_texture_duration={config.load_texture_duration} hors [0, 60]")
    if not 0.0 <= config.weather_effect_duration <= 60.0:
        raise ValueError(f"weather_effect_duration={config.weather_effect_duration} hors [0, 60]")


def local_hour_to_zulu(local_hour, lat, lon, date=None):
    """Convertit heure civile locale en UTC via le fuseau politique reel.

    Utilise timezonefinder pour identifier le fuseau de la position (lat, lon)
    puis pytz pour resoudre l'offset UTC a la date donnee (gere DST).

    Fallback longitude/15 si timezonefinder ne trouve pas de fuseau (ocean,
    cas exotique).

    :param local_hour: heure civile locale [0, 24]
    :param lat: latitude (degres)
    :param lon: longitude (degres, negatif = ouest)
    :param date: datetime pour resolution DST (defaut: datetime.now())
    :return: heure UTC [0, 24)
    """
    if date is None:
        date = datetime.now()
    tz_name = _TZ_FINDER.timezone_at(lat=lat, lng=lon)
    if tz_name is None:
        # Pas de fuseau politique (ocean, polaire) : on retombe sur le fuseau
        # solaire ~longitude/15. Approximation suffisante hors zones DST.
        return (local_hour - lon / 15.0) % 24.0
    tz = pytz.timezone(tz_name)
    utc_offset_h = tz.utcoffset(date).total_seconds() / 3600
    return (local_hour - utc_offset_h) % 24.0


def build_plugin_command(config, aircraft_max_alt_m=200.0, latitude=0.0, longitude=0.0):
    """Construit les parametres JSON pour PI_weather.py.

    Traduit WeatherConfig en params attendus par le plugin.

    :param config: WeatherConfig
    :param aircraft_max_alt_m: altitude max de l'avion dans le scenario (MSL, metres).
        Les nuages sont places au-dessus avec une marge de config.cloud_margin_m.
    :param latitude: latitude de l'aeroport (pour conversion heure locale → UTC via fuseau politique)
    :param longitude: longitude de l'aeroport (pour conversion heure locale → UTC)
    """
    visibility = config.fog_visibility

    # NB : la cle "visibility_m" est le nom attendu par le plugin XPPython3
    # (protocole JSON), distinct du champ XML/WeatherConfig "fog_visibility".
    params = {
        "precip_rate": config.precip_rate,
        "visibility_m": visibility,
        "radius_nm": config.weather_zone_radius_nm,
        "max_alt_ft": 30000.0,
    }

    # Temperature
    if config.temperature_c != 15.0:
        params["temperature_c"] = config.temperature_c

    # Heure du jour — conversion locale → UTC via fuseau politique (timezonefinder + pytz)
    zulu_h = local_hour_to_zulu(config.time_of_day_h, latitude, longitude)
    params["time_of_day_h"] = zulu_h

    # Position des nuages : on les place dynamiquement au-dessus de l'avion
    # (alt_max + cloud_margin_m) plutot qu'a une altitude absolue, pour eviter
    # qu'ils soient sous l'avion sur les approches montagneuses.
    #
    # Fallback `cloud_thickness_m <= 0` UNIQUEMENT : un nuage avec base==top
    # est un nuage degenere pour XPLMWeather et ne genere AUCUNE particule
    # (bug "pluie invisible"). Si l'utilisateur met 0 dans le XML on substitue
    # une epaisseur par defaut coherente avec le genre. Toute valeur positive
    # (meme petite, ex: 50 m) est respectee telle quelle — on ne plancher pas.
    THICKNESS_DEFAULT_BY_TYPE = {0: 1000, 1: 500, 2: 2000, 3: 6000}  # Cirrus/Stratus/Cumulus/Cb
    cloud_base = aircraft_max_alt_m + config.cloud_margin_m
    if config.cloud_type >= 0:
        # Nuages manuels demandes par l'utilisateur
        thickness = config.cloud_thickness_m
        if thickness <= 0:
            thickness = THICKNESS_DEFAULT_BY_TYPE.get(int(config.cloud_type), 2000)
        params["cloud_type"] = config.cloud_type
        params["cloud_coverage"] = config.cloud_coverage
        params["cloud_base_msl"] = cloud_base
        params["cloud_top_msl"] = cloud_base + thickness
    elif config.precip_rate > 0:
        # Pluie sans nuages manuels : forcer Cumulonimbus
        # XP12 ne genere pas ses propres nuages via l'API setWeatherAtLocation,
        # contrairement a l'interface utilisateur
        thickness = config.cloud_thickness_m if config.cloud_thickness_m > 0 else THICKNESS_DEFAULT_BY_TYPE[3]
        params["cloud_type"] = 3.0   # Cumulonimbus
        params["cloud_coverage"] = 1.0
        params["cloud_base_msl"] = cloud_base
        params["cloud_top_msl"] = cloud_base + thickness

    # Taille des gouttes (dataref prive, ignore si non supporte)
    if config.rain_scale != 1.0:
        params["rain_scale"] = config.rain_scale

    return params


def save_weather_profile(config, output_path):
    """Sauvegarde le profil meteo dans un fichier JSON."""
    output_path = Path(output_path)
    profile = {
        "version": 2,
        "mode": "per_scenario",
        "weather": asdict(config),
    }
    with open(output_path, "w") as f:
        json.dump(profile, f, indent=2)
    print(f"  .json weather -> {output_path}")
    return str(output_path)


def load_weather_profile(profile_path):
    """Charge un profil meteo depuis un fichier JSON."""
    with open(profile_path, "r") as f:
        profile = json.load(f)

    # Support v1 (ancien format per-frame) : ignorer
    if profile.get("version", 1) < 2:
        print(f"  [WEATHER] Ancien format v1 ignore : {profile_path}")
        return None

    # Filtre les champs inconnus (ex: rain_intensity supprime) pour rester
    # compat avec les anciens weather_profile.json
    weather = profile["weather"]
    valid_fields = {f.name for f in fields(WeatherConfig)}
    filtered = {k: v for k, v in weather.items() if k in valid_fields}
    return WeatherConfig(**filtered)


# ---------------------------------------------------------------------------
# Communication avec le plugin XPPython3 (PI_weather.py)
# ---------------------------------------------------------------------------

DEFAULT_EXCHANGE_DIR = None
_seq = 0


def set_exchange_dir(xplane_dir):
    """Configure le dossier d'echange pour la communication avec le plugin."""
    global DEFAULT_EXCHANGE_DIR
    DEFAULT_EXCHANGE_DIR = os.path.join(
        xplane_dir, "Resources", "plugins", "PythonPlugins", "lard_exchange"
    )
    os.makedirs(DEFAULT_EXCHANGE_DIR, exist_ok=True)


def _send_weather_command(action, weather=None, timeout=5.0, retries=2, **extra_fields):
    """Envoie une commande au plugin PI_weather.py et attend l'ack.

    :param retries: nombre de tentatives supplementaires si timeout (0 = pas de retry)
    :param extra_fields: champs supplementaires a inclure dans la commande JSON
    """
    global _seq

    if DEFAULT_EXCHANGE_DIR is None:
        raise RuntimeError("Exchange dir not set. Call set_exchange_dir(xplane_dir) first.")

    for attempt in range(1 + retries):
        _seq += 1
        seq = _seq

        cmd_file = os.path.join(DEFAULT_EXCHANGE_DIR, "weather_command.json")
        sts_file = os.path.join(DEFAULT_EXCHANGE_DIR, "weather_status.json")

        cmd = {"seq": seq, "action": action}
        if weather:
            cmd["weather"] = weather
        cmd.update(extra_fields)

        # Ecriture atomique : on serialise dans un .tmp puis on os.replace.
        # os.replace est atomique sur POSIX et autorise l'ecrasement sur Windows
        # (contrairement a os.rename). Le plugin XPPython3 voit donc soit
        # l'ancien fichier complet, soit le nouveau complet — jamais un JSON
        # tronque. La boucle de retry couvre le cas ou le plugin a le fichier
        # ouvert en lecture au meme instant (sharing violation sur Windows).
        tmp = cmd_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cmd, f)
        for _ in range(20):
            try:
                os.replace(tmp, cmd_file)
                break
            except OSError:
                time.sleep(0.05)

        # Attente ack
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(sts_file, "r") as f:
                    status = json.load(f)
                if status.get("ack_seq") == seq:
                    if not status.get("ok"):
                        print(f"  [WEATHER] Plugin error: {status.get('error')}")
                    return status
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
            time.sleep(0.05)

        if attempt < retries:
            print(f"  [WEATHER] Timeout (tentative {attempt+1}/{1+retries}) — retry dans 3s...")
            time.sleep(3.0)

    print(f"  [WEATHER] Timeout — plugin ne repond pas apres {1+retries} tentatives (seq={seq})")
    return None


def check_plugin():
    """Verifie que le plugin XPPython3 est actif."""
    result = _send_weather_command("noop", timeout=10.0)
    return result is not None and result.get("ok", False)


def inject_weather(config, aircraft_max_alt_m=200.0, latitude=0.0, longitude=0.0):
    """Injecte la meteo dans X-Plane et attend la stabilisation.

    Appeler UNE FOIS avant le rendu du scenario.

    :param config: WeatherConfig
    :param aircraft_max_alt_m: altitude max de l'avion (MSL, metres)
    :param latitude: latitude de l'aeroport (pour conversion heure locale → UTC via fuseau politique)
    :param longitude: longitude de l'aeroport (pour conversion heure locale → UTC)
    :return: True si l'injection a reussi
    """
    if not has_weather(config):
        print(f"  [WEATHER] Pas de meteo active, skip")
        return True

    params = build_plugin_command(
        config, aircraft_max_alt_m=aircraft_max_alt_m,
        latitude=latitude, longitude=longitude,
    )
    result = _send_weather_command("set_weather", weather=params)

    if not result or not result.get("ok"):
        print(f"  [WEATHER] ECHEC injection meteo")
        return False

    # Log
    parts = []
    if config.precip_rate > 0:
        parts.append(f"precip={config.precip_rate:.2f}")
    if config.cloud_type >= 0:
        cloud_names = {0: "Cirrus", 1: "Stratus", 2: "Cumulus", 3: "Cumulonimbus"}
        parts.append(f"clouds={cloud_names.get(int(config.cloud_type), '?')}"
                     f"({config.cloud_coverage:.0%})")
    if config.fog_visibility < 50000:
        parts.append(f"vis={config.fog_visibility:.0f}m")
    if config.temperature_c < 0:
        parts.append(f"temp={config.temperature_c:.0f}C (neige)")
    if config.time_of_day_h != 12.0:
        zulu_h = local_hour_to_zulu(config.time_of_day_h, latitude, longitude)
        parts.append(f"heure={config.time_of_day_h:.0f}h local -> {zulu_h:.1f}h UTC")
    parts.append(f"avion_max={aircraft_max_alt_m:.0f}m")
    if config.cloud_type >= 0:
        cloud_base = aircraft_max_alt_m + config.cloud_margin_m
        parts.append(f"nuages={cloud_base:.0f}-{cloud_base + config.cloud_thickness_m:.0f}m MSL")

    print(f"  [WEATHER] Injecte : {', '.join(parts)}")

    # Phase 1 — chargement des textures de la zone teleportee + stabilisation
    # GPU des nuages. Wall-clock : la vitesse sim n'accelere pas le streaming
    # des textures. Toujours applique, meme sans precipitation.
    load_texture_duration = float(config.load_texture_duration)
    if load_texture_duration > 0:
        print(f"  [WEATHER] Chargement textures + stabilisation nuages "
              f"{load_texture_duration:.1f}s...")
        time.sleep(load_texture_duration)

    # Phase 2 — accumulation des effets sol (flaques de pluie, couche de neige).
    # Sim acceleree pour simuler plusieurs minutes de meteo en quelques secondes
    # reelles. Applique uniquement si precip active ET duree > 0 (0 = pas
    # d'accumulation : la pluie tombe mais la piste reste seche).
    weather_effect_duration = float(config.weather_effect_duration)
    if config.precip_rate > 0 and weather_effect_duration > 0:
        print(f"  [WEATHER] Accumulation flaques/neige {weather_effect_duration:.1f}s")
        set_sim_speed(SIM_SPEED_BOOST)
        time.sleep(weather_effect_duration)
        set_sim_speed(1)

    print(f"  [WEATHER] Stabilisation terminee")
    return True


def set_sim_speed(speed):
    """Change la vitesse de simulation X-Plane (1=normal, 2=2x, ..., 16=max)."""
    if DEFAULT_EXCHANGE_DIR is None:
        return False
    result = _send_weather_command("set_sim_speed", timeout=3.0, retries=0, speed=int(speed))
    return result is not None and result.get("ok", False)


def reset_weather():
    """Remet la meteo par defaut (ciel clair).

    Suppose que set_exchange_dir() a deja ete appele. Pour un appel
    defensif depuis l'orchestrateur, voir reset_if_active().
    """
    result = _send_weather_command("clear_weather")
    if result and result.get("ok"):
        print(f"  [WEATHER] Clear OK")
    else:
        print(f"  [WEATHER] Clear ECHEC")


def reset_if_active(xplane_dir):
    """Clear meteo defensif, no-op si xplane_dir vide ou plugin absent.

    Wrapper haut-niveau pour l'orchestrateur : configure le exchange dir,
    appelle reset_weather(), ignore les exceptions silencieusement.
    """
    if not xplane_dir:
        return
    try:
        set_exchange_dir(xplane_dir)
        reset_weather()
    except Exception:
        pass
