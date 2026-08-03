"""
PI_weather.py  —  XPPython3 plugin for LARD weather injection
========================================================================
Injects weather through the sim/weather/region/* datarefs (documented and
writable, see Resources/plugins/DataRefs.txt), NOT through the XPLMWeather
API. Communicates with the LARD pipeline (sources/xplane_weather.py) via
JSON files exchanged in Resources/plugins/PythonPlugins/lard_exchange/.

WHY NOT XPLMWeather / setWeatherAtLocation (measured 2026-07-17, XP 12.4.2)
--------------------------------------------------------------------------
That API is flagged EXPERIMENTAL by Laminar and it SILENTLY IGNORES
info.visibility: we asked for 500 m and the sim rendered 21 km, every time.
Three theories were tested against the sim and all three are FALSE:
  - "XP12 derives visibility from the temperature/dewpoint spread": swept the
    spread from 2.0 C down to 0.0 C (fully saturated air). Visibility never
    moved off ~21 km. Humidity does NOT drive visibility.
  - "info.age must be 0 for the report to have weight": getWeatherAtLocation
    already returns age=0. No effect.
  - "change_mode=0 means Rapidly Improving, so the sim burns the fog off":
    change_mode=3 (Static) changes nothing.
Fog has in fact NEVER worked in this project, in any version — it is a missing
feature, not a regression. Do not "restore" anything from git history.

THE RECIPE THAT WORKS (verified on screen: fog + rain at KPDX)
-------------------------------------------------------------
  1. change_mode = 3 (Static) — see enum below, 0 is NOT a neutral value.
  2. write the region datarefs (visibility in STATUTE MILES, not meters).
  3. update_immediately = 1 — applies everything EXCEPT clouds.
  4. sim/operation/regen_weather — THE missing piece. Without it the sim
     never rebuilds its weather grid: the dataref holds the value we wrote
     but sim/weather/aircraft/* and the render stay on the old one.
Result: 500 m requested -> 500 m effective, to the meter.

THE TWO SOURCES ARE EXCLUSIVE
-----------------------------
sim/weather/region/weather_source: 0=Preset, 1=Real, 2=Controlpad, 3=Plugin.
regen_weather switches the sim to Preset, and from that moment X-Plane ignores
every plugin record. So "fog via datarefs + the rest via setWeatherAtLocation"
CANNOT work (measured: fog OK, clouds and rain dead). That mix is what made the
earlier attempt look like it "broke the other profiles". Everything goes
through the datarefs, or nothing does.

STATE IS GLOBAL AND PERSISTS ACROSS SCENARIOS
---------------------------------------------
Unlike an XPLMWeather record, these datarefs are sim-wide and survive into the
next scenario. Hence the rule: EVERY parameter is written on EVERY injection,
including the defaults and including clear skies. A key we skip is not "the
default", it is "whatever the previous scenario left". Never make a write
conditional on the value being non-default.

Installation:
  py scripts/install_weather_plugin.py
  then reload via menu: Plugins > XPPython3 > Reload Scripts.

Protocol:
  Python pipeline writes weather_command.json  {seq, action, weather}
  Plugin reads, applies, writes weather_status.json
  Sequence number guards against duplicate processing.
"""

import os
import json

try:
    import xp
except ImportError:
    raise RuntimeError("PI_weather requires XPPython3")


# ---------------------------------------------------------------------------
# Constantes meteo
# ---------------------------------------------------------------------------

# sim/weather/region/visibility_reported_sm est en MILES TERRESTRES ; tout le
# reste du pipeline (XML, WeatherConfig, metadata) raisonne en METRES.
# La conversion se fait ICI et nulle part ailleurs.
METERS_PER_STATUTE_MILE = 1609.344

# Ecart temperature <-> point de rosee (C), sur les 13 couches atmo.
# NB : ne pilote PAS le brouillard (mesure : aucun effet sur la visibilite,
# meme a 0 = air sature). C'est juste une humidite realiste par defaut.
DEWPOINT_SPREAD_C = 2.0

# Couches des datarefs atmospheriques region (float[13], cf atmosphere_alt_levels_m)
ATMOSPHERE_LAYERS = 13

# Couches nuageuses region (float[3])
CLOUD_LAYERS = 3

# change_mode : TENDANCE appliquee par le sim, pas un simple "manuel vs reel".
#   0 = Rapidly Improving ... 3 = Static ... 6 = Rapidly Deteriorating
#   7 = Using Real Weather (METAR en ligne : ecraserait notre injection)
# 3 est le seul mode qui preserve ce qu'on ecrit. Ecrire 0 en croyant qu'il
# veut dire "pas de meteo reelle" demande au sim d'ameliorer la meteo.
CHANGE_MODE_STATIC = 3
CHANGE_MODE_REAL_WEATHER = 7

# Sentinelle "pas de brouillard" (cf xplane_weather.has_weather / WeatherConfig).
NO_FOG_VISIBILITY_M = 50000.0


# ---------------------------------------------------------------------------
# Exchange directory
# ---------------------------------------------------------------------------
EXCHANGE_DIR = None
CMD_FILE = None
STS_FILE = None
STS_TMP = None


class PythonInterface:

    def __init__(self):
        self.Name = "LARD Weather v2"
        self.Sig = "lard.weather.xppython3"
        self.Desc = "LARD weather injection via XPLMWeather API (v2)"
        self.flight_loop_id = None
        self.last_ack_seq = -1
        # Dataref handles
        self.dr_lat = None
        self.dr_lon = None
        self.dr_elev = None
        self.dr_change_mode = None
        self.dr_rain_scale = None   # sim/private/controls/rain/scale (taille gouttes)
        self.dr_sim_speed = None    # sim/time/sim_speed (multiplicateur vitesse sim)
        # Command handle for regen_weather
        self.cmd_regen_weather = None

    # ------------------------------------------------------------------
    # Plugin lifecycle
    # ------------------------------------------------------------------

    def XPluginStart(self):
        global EXCHANGE_DIR, CMD_FILE, STS_FILE, STS_TMP
        try:
            EXCHANGE_DIR = os.path.join(
                xp.getSystemPath(),
                "Resources", "plugins", "PythonPlugins", "lard_exchange"
            )
            CMD_FILE = os.path.join(EXCHANGE_DIR, "weather_command.json")
            STS_FILE = os.path.join(EXCHANGE_DIR, "weather_status.json")
            STS_TMP = os.path.join(EXCHANGE_DIR, "weather_status.tmp")
            os.makedirs(EXCHANGE_DIR, exist_ok=True)
            for f in (CMD_FILE, STS_FILE, STS_TMP):
                try:
                    os.remove(f)
                except FileNotFoundError:
                    pass
        except Exception as e:
            xp.log(f"LARD Weather: start error: {e}")
        return self.Name, self.Sig, self.Desc

    def XPluginEnable(self):
        try:
            # Position datarefs
            self.dr_lat = xp.findDataRef("sim/flightmodel/position/latitude")
            self.dr_lon = xp.findDataRef("sim/flightmodel/position/longitude")
            self.dr_elev = xp.findDataRef("sim/flightmodel/position/elevation")

            # --- Meteo : datarefs region (la base modifiable, cf DataRefs.txt) ---
            # use_real_weather_bool est deprecie/read-only : c'est change_mode
            # qui commande.
            self.dr_change_mode = xp.findDataRef("sim/weather/region/change_mode")
            # ATTENTION : en MILES TERRESTRES (le reste du pipeline est en metres).
            self.dr_visibility_sm = xp.findDataRef(
                "sim/weather/region/visibility_reported_sm")
            # Pluie : controle DIRECT, sans nuage. C'est un gain sur l'API
            # XPLMWeather, qui exigeait un cloud_type >= 0 comme source.
            self.dr_rain_percent = xp.findDataRef("sim/weather/region/rain_percent")
            # Nuages : float[3] -> setDatavf obligatoire (setDataf ne marche pas).
            self.dr_cloud_type = xp.findDataRef("sim/weather/region/cloud_type")
            self.dr_cloud_coverage = xp.findDataRef(
                "sim/weather/region/cloud_coverage_percent")
            self.dr_cloud_base = xp.findDataRef("sim/weather/region/cloud_base_msl_m")
            self.dr_cloud_tops = xp.findDataRef("sim/weather/region/cloud_tops_msl_m")
            # Atmosphere : float[13]
            self.dr_temps_aloft = xp.findDataRef(
                "sim/weather/region/temperatures_aloft_deg_c")
            self.dr_dewpoint = xp.findDataRef("sim/weather/region/dewpoint_deg_c")
            self.dr_sealevel_temp = xp.findDataRef(
                "sim/weather/region/sealevel_temperature_c")
            # Altitudes des 13 couches. atmosphere_alt_levels_m est READ-ONLY et
            # donne les altitudes de reference ; temperature_altitude_msl_m est
            # writable et dit A QUELLES altitudes s'appliquent temperatures_aloft.
            # On ne l'a jamais ecrit : si le tableau est a zero, nos temperatures
            # sont peut-etre posees a une altitude qui n'existe pas.
            self.dr_atmo_levels = xp.findDataRef(
                "sim/weather/region/atmosphere_alt_levels_m")
            self.dr_temp_altitude = xp.findDataRef(
                "sim/weather/region/temperature_altitude_msl_m")
            # Mesure de la neige au point CAMERA (read-only) : la verite terrain,
            # au lieu de deduire la neige de la temperature.
            self.dr_view_temp = xp.findDataRef("sim/weather/view/temperature_C")
            self.dr_view_snow = xp.findDataRef("sim/weather/view/snow_ratio")
            self.dr_view_rain = xp.findDataRef("sim/weather/view/rain_ratio")
            # Applique tout SAUF les nuages (cf DataRefs.txt) sans attendre le
            # prochain cycle (60 s).
            self.dr_update_now = xp.findDataRef("sim/weather/region/update_immediately")
            # Diagnostic : 0=Preset 1=Real 2=Controlpad 3=Plugin
            self.dr_weather_source = xp.findDataRef("sim/weather/region/weather_source")
            # Visibilite REELLEMENT rendue (apres protection framerate) : c'est
            # la seule mesure fiable de ce que la camera voit.
            self.dr_vis_effective = xp.findDataRef(
                "sim/graphics/view/visibility_effective_m")

            # Time of day
            self.dr_zulu_time = xp.findDataRef("sim/time/zulu_time_sec")
            self.dr_use_system_time = xp.findDataRef("sim/time/use_system_time")

            # Rain drop scale (private dataref, may not exist on all XP12 versions)
            try:
                self.dr_rain_scale = xp.findDataRef("sim/private/controls/rain/scale")
            except Exception:
                self.dr_rain_scale = None

            # Sim speed (for accelerating weather accumulation)
            self.dr_sim_speed = xp.findDataRef("sim/time/sim_speed")

            # Regen weather command
            self.cmd_regen_weather = xp.findCommand("sim/operation/regen_weather")

            # Flight loop — phase=0 (before flight model)
            self.flight_loop_id = xp.createFlightLoop(self._tick, phase=0)
            xp.scheduleFlightLoop(self.flight_loop_id, interval=-5)
            xp.log(f"LARD Weather v2: enabled — exchange dir: {EXCHANGE_DIR}")
        except Exception as e:
            xp.log(f"LARD Weather v2: enable error: {e}")
        return 1

    def XPluginDisable(self):
        try:
            if self.flight_loop_id:
                xp.destroyFlightLoop(self.flight_loop_id)
                self.flight_loop_id = None
        except Exception as e:
            xp.log(f"LARD Weather v2: disable error: {e}")
        xp.log("LARD Weather v2: disabled")

    def XPluginStop(self):
        pass

    def XPluginReceiveMessage(self, inFrom, inMsg, inParam):
        pass

    # ------------------------------------------------------------------
    # Flight loop callback
    # ------------------------------------------------------------------

    def _tick(self, sinceLast, sinceFlightLoop, counter, refCon):
        try:
            self._process_command()
        except Exception as e:
            xp.log(f"LARD Weather v2: tick error: {e}")
        return -5  # ~5 Hz

    def _process_command(self):
        if CMD_FILE is None:
            return

        try:
            with open(CMD_FILE, "r") as f:
                cmd = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return

        seq = cmd.get("seq")
        if seq is None or seq == self.last_ack_seq:
            return

        action = cmd.get("action", "noop")
        status = {"ack_seq": seq, "ok": True}

        try:
            if action == "set_weather":
                self._apply_weather(cmd.get("weather", {}))
            elif action == "clear_weather":
                self._clear_weather()
            elif action == "set_sim_speed":
                self._set_sim_speed(cmd.get("speed", 1))
            elif action == "noop":
                pass
            else:
                status["ok"] = False
                status["error"] = f"unknown action: {action}"
        except Exception as e:
            status["ok"] = False
            status["error"] = str(e)
            xp.log(f"LARD Weather v2: action error: {e}")

        self._write_status(status)
        self.last_ack_seq = seq

    # ------------------------------------------------------------------
    # Weather injection via the sim/weather/region/* datarefs
    # ------------------------------------------------------------------

    def _get_aircraft_pos(self):
        """Return (lat, lon, elev_msl) of the aircraft — for logging only.

        The region datarefs describe the weather AROUND the aircraft: unlike
        setWeatherAtLocation there is no lat/lon, no radius_nm and no
        max_altitude_msl_ft to provide. The weather follows the aircraft, which
        suits us: we teleport between scenarios and can no longer fall outside
        an injected zone.
        """
        return (xp.getDatad(self.dr_lat), xp.getDatad(self.dr_lon),
                xp.getDataf(self.dr_elev))

    def _set_layers(self, dref, value, count):
        """Write the same value on every layer of a float[] dataref.

        setDataf does NOT work on array datarefs — it silently does nothing.
        setDatavf is mandatory.
        """
        xp.setDatavf(dref, [float(value)] * count, 0, count)

    def _apply_xplm_only(self, weather, lat, lon, elev):
        """Ancien comportement : setWeatherAtLocation SEUL, zero dataref region.

        Sert a tester si le mode Preset (donc le verrou sur la temperature) est
        declenche par le simple fait d'ecrire un dataref region. C'est le code
        qui produisait la neige avant la migration.
        """
        temp = float(weather.get("temperature_c", 15.0))
        spread = float(weather.get("dewpoint_spread_c", DEWPOINT_SPREAD_C))
        info = xp.getWeatherAtLocation(lat, lon, elev)
        cloud_type = float(weather.get("cloud_type", -1.0))
        if cloud_type >= 0:
            info.cloud_layers[0].cloud_type = cloud_type
            info.cloud_layers[0].coverage = float(weather.get("cloud_coverage", 0.0))
            info.cloud_layers[0].alt_base = float(weather.get("cloud_base_msl", 1000.0))
            info.cloud_layers[0].alt_top = float(weather.get("cloud_top_msl", 3000.0))
        else:
            info.cloud_layers[0].cloud_type = 0.0
            info.cloud_layers[0].coverage = 0.0
        for i in range(1, len(info.cloud_layers)):
            info.cloud_layers[i].cloud_type = 0.0
            info.cloud_layers[i].coverage = 0.0
        info.precip_rate = float(weather.get("precip_rate", 0.0))
        info.visibility = float(weather.get("visibility_m", NO_FOG_VISIBILITY_M))
        info.temperature_alt = temp
        info.dewpoint_alt = temp - spread
        for i in range(len(info.temp_layers)):
            info.temp_layers[i] = temp
        for i in range(len(info.dewp_layers)):
            info.dewp_layers[i] = temp - spread
        info.radius_nm = float(weather.get("radius_nm", 50.0))
        info.max_altitude_msl_ft = float(weather.get("max_alt_ft", 30000.0))
        with xp.weatherUpdateContext(isIncremental=False, updateImmediately=True):
            xp.setWeatherAtLocation(lat, lon, elev, info)
        xp.log(f"LARD Weather v2: PURE_XPLM temp={temp:.1f}C "
               f"precip={info.precip_rate:.2f} vis={info.visibility:.0f}m "
               f"source={xp.getDatai(self.dr_weather_source)} "
               f"sealevel={xp.getDataf(self.dr_sealevel_temp):.1f}C "
               f"view_temp={xp.getDataf(self.dr_view_temp):.1f}C")

    def _apply_weather(self, weather):
        """Apply the weather described by `weather` (JSON from the pipeline).

        EVERY parameter is written on EVERY call, defaults included: these
        datarefs are global sim state and a value we skip is inherited from the
        previous scenario, not reset to a default.

        Cloud type enum: 0=Cirrus, 1=Stratus, 2=Cumulus, 3=Cumulonimbus.
        """
        lat, lon, elev = self._get_aircraft_pos()

        # weather_api = "pure_xplm" : n'ecrit AUCUN dataref region, uniquement
        # setWeatherAtLocation — c'est l'ancien comportement, celui qui faisait
        # la neige. Hypothese testee ici : c'est le fait de TOUCHER aux datarefs
        # region qui force le sim en mode Preset, lequel verrouille la
        # temperature. Si elle est juste, brouillard et neige sont exclusifs et
        # le choix doit se faire par scenario.
        api = weather.get("weather_api", "datarefs")
        if api == "pure_xplm":
            self._apply_xplm_only(weather, lat, lon, elev)
            return

        # setWeatherAtLocation D'ABORD, datarefs region ENSUITE.
        # Ecrire un dataref region verrouille la temperature ; il faut donc
        # qu'elle soit deja a la bonne valeur AVANT. Sans ca elle reste figee sur
        # celle du scenario precedent : un scenario pluie a +15 C juste apres un
        # scenario neige rendait de la NEIGE tout en annoncant 15 C dans la
        # metadata — GT fausse mais plausible, le pire mode de defaillance.
        self._apply_xplm_only(weather, lat, lon, elev)

        # -- 1. Tendance : Static, sinon le sim fait deriver ce qu'on ecrit --
        xp.setDatai(self.dr_change_mode,
                    int(weather.get("change_mode", CHANGE_MODE_STATIC)))

        # -- 2. Visibilite / brouillard --
        # Le pipeline parle en METRES, le dataref en MILES TERRESTRES.
        vis_m = float(weather.get("visibility_m", NO_FOG_VISIBILITY_M))
        xp.setDataf(self.dr_visibility_sm, vis_m / METERS_PER_STATUTE_MILE)

        # -- 3. Pluie --
        # rain_percent pilote la pluie DIRECTEMENT, sans nuage : contrairement a
        # l'API XPLMWeather, precip_rate > 0 avec cloud_type = -1 donne bien de
        # la pluie. La cle JSON garde le nom du parametre XML (precip_rate).
        rain = float(weather.get("precip_rate", 0.0))
        xp.setDataf(self.dr_rain_percent, rain)

        # -- 4. Temperature / point de rosee (float[13]) --
        # L'ecart n'a AUCUN effet sur la visibilite (mesure : balayage 2.0 -> 0.0
        # C, visibilite inchangee). Il n'est la que pour une humidite realiste.
        # Ne pas le rebrancher sur le brouillard : ce n'est pas le bon levier.
        temp = float(weather.get("temperature_c", 15.0))
        spread = float(weather.get("dewpoint_spread_c", DEWPOINT_SPREAD_C))
        # Ecrites APRES le regen (cf plus bas) : le regen reconstruit
        # l'atmosphere depuis le preset et ecrase toute valeur ecrite avant.

        # -- 5. Nuages (float[3]) --
        # cloud_type = -1 (ou cle absente) => aucun nuage : on ECRIT la couverture
        # a 0 au lieu de ne rien faire, sinon les nuages du scenario precedent
        # restent en place.
        cloud_type = float(weather.get("cloud_type", -1.0))
        cloud_coverage = float(weather.get("cloud_coverage", 0.0))
        cloud_base = float(weather.get("cloud_base_msl", 1000.0))
        cloud_top = float(weather.get("cloud_top_msl", 3000.0))
        if cloud_type >= 0:
            types = [cloud_type] + [0.0] * (CLOUD_LAYERS - 1)
            covs = [cloud_coverage] + [0.0] * (CLOUD_LAYERS - 1)
            bases = [cloud_base] + [0.0] * (CLOUD_LAYERS - 1)
            tops = [cloud_top] + [0.0] * (CLOUD_LAYERS - 1)
        else:
            types = [0.0] * CLOUD_LAYERS
            covs = [0.0] * CLOUD_LAYERS
            bases = [0.0] * CLOUD_LAYERS
            tops = [0.0] * CLOUD_LAYERS
        # Valeurs seulement preparees ici : l'ecriture se fait APRES le regen.

        # -- 6. Heure du jour --
        # time_of_day_h est deja en UTC (conversion locale -> UTC faite cote
        # pipeline par xplane_weather.local_hour_to_zulu).
        if "time_of_day_h" in weather:
            xp.setDatai(self.dr_use_system_time, 0)
            xp.setDataf(self.dr_zulu_time, float(weather["time_of_day_h"]) * 3600.0)

        # -- 7. Application --
        # update_immediately applique tout SAUF les nuages ; regen_weather force
        # le sim a reconstruire sa grille meteo. Sans regen, les datarefs gardent
        # notre valeur mais le rendu reste sur l'ancienne : c'est LE piege qui a
        # fait croire pendant des mois que le brouillard etait impossible.
        xp.setDatai(self.dr_update_now, 1)
        # weather_api : "datarefs" (defaut) = regen -> brouillard exact, mais le
        # sim bascule en mode Preset qui VERROUILLE la temperature (pas de neige).
        #              "xplm" = pas de regen -> on reste en mode Plugin, la
        # temperature repond (neige possible) mais la visibilite est ignoree.
        # regen_weather est ce qui bascule la source, et weather_source est
        # READ-ONLY : on ne peut pas revenir en arriere dans le meme scenario.
        api = weather.get("weather_api", "datarefs")
        if api != "xplm":
            xp.commandOnce(self.cmd_regen_weather)

        # Ce qui doit etre ecrit APRES le regen : le regen reconstruit l'etat
        # depuis le preset, donc il ECRASE tout ce qu'on a pose avant. Seule la
        # visibilite survit (le regen la prend en entree) — d'ou son ecriture
        # plus haut. Temperature et nuages, eux, sont recalcules : ecrits avant,
        # ils sont perdus (mesure : temperature_c=-10 ressortait a +15, donc
        # aucune neige possible).
        # Les altitudes AVANT les temperatures : temperatures_aloft_deg_c n'a de
        # sens que rapporte a temperature_altitude_msl_m. On reprend les
        # altitudes de reference du sim (atmosphere_alt_levels_m, read-only).
        levels = []
        xp.getDatavf(self.dr_atmo_levels, levels, 0, ATMOSPHERE_LAYERS)
        if levels:
            xp.setDatavf(self.dr_temp_altitude, levels, 0, len(levels))
        self._set_layers(self.dr_temps_aloft, temp, ATMOSPHERE_LAYERS)
        self._set_layers(self.dr_dewpoint, temp - spread, ATMOSPHERE_LAYERS)
        xp.setDataf(self.dr_sealevel_temp, temp)

        xp.setDatavf(self.dr_cloud_type, types, 0, CLOUD_LAYERS)
        xp.setDatavf(self.dr_cloud_coverage, covs, 0, CLOUD_LAYERS)
        xp.setDatavf(self.dr_cloud_base, bases, 0, CLOUD_LAYERS)
        xp.setDatavf(self.dr_cloud_tops, tops, 0, CLOUD_LAYERS)

        # Re-appliquer : update_immediately ne vaut que pour le cycle en cours.
        xp.setDatai(self.dr_update_now, 1)

        # -- 7bis. Nuages et precipitation via l'API XPLMWeather --
        # Les deux API se COMPLETENT, elles ne s'excluent pas (mesure) :
        #   - les datarefs region cloud_* sont inertes ici (mode Preset) mais
        #     visibility_reported_sm y est exact au metre ;
        #   - setWeatherAtLocation ignore info.visibility mais rend les nuages
        #     et la pluie instantanement.
        # D'ou ce partage : visibilite par les datarefs (ci-dessus), nuages et
        # pluie par l'API (ici). Cet appel doit venir APRES le regen, sinon le
        # regen efface le record.
        # Effet de bord assume : l'API reecrit aussi la visibilite et le sim
        # melange les deux -> ~0.5 a 4 % d'ecart sur la consigne de brouillard
        # (500 m -> 489/521 m ; 20000 m -> 19891 m). Largement dans la tolerance.
        info = xp.getWeatherAtLocation(lat, lon, elev)
        if cloud_type >= 0:
            info.cloud_layers[0].cloud_type = cloud_type
            info.cloud_layers[0].coverage = cloud_coverage
            info.cloud_layers[0].alt_base = cloud_base
            info.cloud_layers[0].alt_top = cloud_top
        else:
            info.cloud_layers[0].cloud_type = 0.0
            info.cloud_layers[0].coverage = 0.0
        for i in range(1, len(info.cloud_layers)):
            info.cloud_layers[i].cloud_type = 0.0
            info.cloud_layers[i].coverage = 0.0
        info.precip_rate = rain
        info.visibility = vis_m
        info.temperature_alt = temp
        info.dewpoint_alt = temp - spread
        # radius_nm / max_altitude_msl_ft valent 0 au retour de
        # getWeatherAtLocation : sans ecriture explicite, XP ignore l'injection.
        info.radius_nm = float(weather.get("radius_nm", 50.0))
        info.max_altitude_msl_ft = float(weather.get("max_alt_ft", 30000.0))
        with xp.weatherUpdateContext(isIncremental=False, updateImmediately=True):
            xp.setWeatherAtLocation(lat, lon, elev, info)

        # -- 8. Taille des gouttes (dataref prive, hors modele region) --
        if "rain_scale" in weather and self.dr_rain_scale is not None:
            try:
                xp.setDataf(self.dr_rain_scale, float(weather["rain_scale"]))
            except Exception as e:
                xp.log(f"LARD Weather v2: rain_scale error: {e}")

        cloud_str = ("aucun" if cloud_type < 0 else
                     f"type={cloud_type:.0f} cov={cloud_coverage:.2f} "
                     f"{cloud_base:.0f}-{cloud_top:.0f}m")
        time_str = (f" time={weather['time_of_day_h']:.1f}hZ"
                    if "time_of_day_h" in weather else "")
        xp.log(f"LARD Weather v2: SET vis={vis_m:.0f}m rain={rain:.2f} "
               f"temp={temp:.1f}C nuages=[{cloud_str}]{time_str} "
               f"at ({lat:.4f}, {lon:.4f})")

        # PAS de readback de la visibilite ici : le sim met plusieurs secondes a
        # converger, donc une lecture immediate renvoie l'etat du scenario
        # PRECEDENT (vu en test : "demande=500m effective=14997m", ou 14997 etait
        # l'ancienne valeur). C'est un piege a diagnostic, pas une mesure.
        # Pour verifier une injection : lire sim/graphics/view/visibility_effective_m
        # APRES la stabilisation (cf inject_weather cote pipeline).
        # weather_source est instantane, lui, et dit quelle source pilote :
        # 0=Preset (etat normal ici, du au regen) 3=Plugin.
        try:
            # Relecture des TABLEAUX : impossible en UDP (RREF renvoie 0.00 sur
            # les float[] et m'a fait croire trois fois que l'ecriture echouait).
            # getDatavf cote plugin est le seul moyen de savoir si setDatavf a
            # atteint sa cible.
            rb_temps, rb_dewp, rb_alt, rb_cov = [], [], [], []
            xp.getDatavf(self.dr_temps_aloft, rb_temps, 0, 3)
            xp.getDatavf(self.dr_dewpoint, rb_dewp, 0, 3)
            xp.getDatavf(self.dr_temp_altitude, rb_alt, 0, 3)
            xp.getDatavf(self.dr_cloud_coverage, rb_cov, 0, CLOUD_LAYERS)
            xp.log(f"LARD Weather v2: ARRAYS temps_aloft[0:3]={rb_temps} "
                   f"dewp[0:3]={rb_dewp} temp_alt[0:3]={rb_alt} cov={rb_cov}")
            xp.log(f"LARD Weather v2: SCALARS sealevel="
                   f"{xp.getDataf(self.dr_sealevel_temp):.1f}C "
                   f"view_temp={xp.getDataf(self.dr_view_temp):.1f}C "
                   f"view_snow={xp.getDataf(self.dr_view_snow):.2f} "
                   f"view_rain={xp.getDataf(self.dr_view_rain):.2f} "
                   f"source={xp.getDatai(self.dr_weather_source)} "
                   f"(demande temp={temp:.1f}C)")
        except Exception as e:
            xp.log(f"LARD Weather v2: readback error: {e}")

    def _set_sim_speed(self, speed):
        """Set simulation speed multiplier (1=normal, 2=2x, 4=4x, etc.).

        Essaie d'abord le dataref sim/time/sim_speed,
        puis fallback sur les commandes sim_speed_up/down.
        """
        speed = max(1, min(int(speed), 16))  # clamp [1, 16]

        # Methode 1 : dataref direct
        try:
            current = xp.getDatai(self.dr_sim_speed)
            xp.setDatai(self.dr_sim_speed, speed)
            after = xp.getDatai(self.dr_sim_speed)
            if after == speed:
                xp.log(f"LARD Weather v2: sim_speed {current}x -> {speed}x (dataref)")
                return
            xp.log(f"LARD Weather v2: dataref write ignored ({after} != {speed}), fallback commands")
        except Exception as e:
            xp.log(f"LARD Weather v2: dataref sim_speed error: {e}, fallback commands")

        # Methode 2 : commandes sim_speed_up / sim_speed_down
        try:
            cmd_up = xp.findCommand("sim/operation/sim_speed_up")
            cmd_down = xp.findCommand("sim/operation/sim_speed_down")
            # D'abord revenir a 1x
            for _ in range(8):
                xp.commandOnce(cmd_down)
            # Puis monter au niveau voulu (chaque appui double la vitesse)
            import math
            steps = int(math.log2(speed)) if speed > 1 else 0
            for _ in range(steps):
                xp.commandOnce(cmd_up)
            xp.log(f"LARD Weather v2: sim_speed -> {speed}x (commands, {steps} steps up)")
        except Exception as e:
            xp.log(f"LARD Weather v2: sim_speed command error: {e}")

    def _clear_weather(self):
        """Hand the weather back to X-Plane (real METAR weather).

        Called between runs, not between scenarios: a scenario does not need a
        clear, since _apply_weather rewrites every dataref anyway.

        There is no plugin record to erase any more (we no longer use
        setWeatherAtLocation), so this is just: stop forcing, let the sim take
        over again.
        """
        # 1) Ciel degage + pas de pluie, pour ne rien laisser trainer si le
        #    passage en meteo reelle echoue.
        xp.setDataf(self.dr_visibility_sm, NO_FOG_VISIBILITY_M / METERS_PER_STATUTE_MILE)
        xp.setDataf(self.dr_rain_percent, 0.0)
        for dref in (self.dr_cloud_type, self.dr_cloud_coverage,
                     self.dr_cloud_base, self.dr_cloud_tops):
            xp.setDatavf(dref, [0.0] * CLOUD_LAYERS, 0, CLOUD_LAYERS)

        # 2) Rendre la main au METAR en ligne.
        xp.setDatai(self.dr_change_mode, CHANGE_MODE_REAL_WEATHER)

        # 3) Reconstruire la grille, sinon le sim reste sur notre etat force.
        xp.setDatai(self.dr_update_now, 1)
        xp.commandOnce(self.cmd_regen_weather)

        # 4) Rendre la main a l'heure systeme (forcee par time_of_day_h).
        xp.setDatai(self.dr_use_system_time, 1)

        xp.log("LARD Weather v2: CLEARED (real weather + regen + system time)")

    # ------------------------------------------------------------------
    # Status file I/O
    # ------------------------------------------------------------------

    def _write_status(self, status):
        try:
            with open(STS_TMP, "w") as f:
                json.dump(status, f)
            if os.path.exists(STS_FILE):
                os.remove(STS_FILE)
            os.rename(STS_TMP, STS_FILE)
        except Exception as e:
            xp.log(f"LARD Weather v2: write error: {e}")
