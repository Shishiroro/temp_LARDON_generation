"""
PI_weather.py  —  XPPython3 plugin for LARD weather injection
========================================================================
Uses the official XPLMWeather API (X-Plane 12.0+) to inject weather.
Communicates with the LARD pipeline (sources/export/xplane_weather.py)
via JSON files exchanged in Resources/plugins/PythonPlugins/lard_exchange/.

Key implementation notes (XPLMWeather quirks discovered while building this):
  - cloud_type enum: 0=Cirrus, 1=Stratus, 2=Cumulus, 3=Cumulonimbus.
  - use_real_weather_bool is deprecated/read-only — use the change_mode
    dataref to switch between manual (0) and real-weather (7) modes.
  - getWeatherAtLocation returns radius_nm=0 and max_altitude_msl_ft=0;
    we MUST set them explicitly each call or XP ignores the injection.
  - isIncremental=False replaces all prior records (used both for set
    and for clear).
  - To force snow, write the same temperature on every temp_layers slot —
    otherwise XP blends with the +15°C layers and rain stays rain.

Installation:
  Copy this file to X-Plane 12/Resources/plugins/PythonPlugins/
  then reload via menu: Plugins > XPPython3 > Reload Scripts.

Protocol:
  Python pipeline writes weather_command.json  {seq, action, weather}
  Plugin reads, applies via XPLMWeather API, writes weather_status.json
  Sequence number guards against duplicate processing.
"""

import os
import json

try:
    import xp
except ImportError:
    raise RuntimeError("PI_weather requires XPPython3")


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

            # Weather mode control (replaces deprecated use_real_weather_bool)
            self.dr_change_mode = xp.findDataRef("sim/weather/region/change_mode")

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
    # Weather injection via XPLMWeather API
    # ------------------------------------------------------------------

    def _get_aircraft_pos(self):
        """Return (lat, lon, elev_msl) of the aircraft."""
        lat = xp.getDatad(self.dr_lat)
        lon = xp.getDatad(self.dr_lon)
        elev = xp.getDataf(self.dr_elev)
        return lat, lon, elev

    def _apply_weather(self, weather):
        """Inject weather at the aircraft's current position.

        Cloud type enum (XPLMWeather API / new):
          0 = Cirrus
          1 = Stratus
          2 = Cumulus
          3 = Cumulonimbus
        """
        # change_mode dataref: 0..6 = manual modes, 7 = real weather (online METAR).
        # We must switch out of mode 7 or XP overwrites our injection from METAR.
        try:
            xp.setDatai(self.dr_change_mode, 0)
        except Exception:
            pass

        lat, lon, elev = self._get_aircraft_pos()

        # Build weather info from current state
        info = xp.getWeatherAtLocation(lat, lon, elev)

        # -- Clouds (layer 0) --
        # Si cloud_type est present dans les params, on controle les nuages.
        # Sinon on ne touche pas aux cloud layers — XP12 gere tout seul
        # (ex: nuages auto generes avec la pluie).
        cloud_type = None
        cloud_coverage = 0.0
        cloud_base = 0.0
        cloud_top = 0.0
        if "cloud_type" in weather:
            cloud_type = float(weather["cloud_type"])
            cloud_coverage = float(weather.get("cloud_coverage", 0.0))
            cloud_base = float(weather.get("cloud_base_msl", 1000.0))
            cloud_top = float(weather.get("cloud_top_msl", 3000.0))

            info.cloud_layers[0].cloud_type = cloud_type
            info.cloud_layers[0].coverage = cloud_coverage
            info.cloud_layers[0].alt_base = cloud_base
            info.cloud_layers[0].alt_top = cloud_top

            # Clear other cloud layers
            for i in range(1, len(info.cloud_layers)):
                info.cloud_layers[i].cloud_type = 0.0
                info.cloud_layers[i].coverage = 0.0

        # -- Precipitation --
        info.precip_rate = float(weather.get("precip_rate", 0.0))

        # -- Visibility --
        info.visibility = float(weather.get("visibility_m", 50000.0))

        # -- Temperature (for snow: < 0°C) --
        if "temperature_c" in weather:
            temp = float(weather["temperature_c"])
            info.temperature_alt = temp
            # Forcer toutes les couches de temperature pour que XP12
            # utilise cette temperature a toutes les altitudes.
            # Sans ca, seule temperature_alt est modifiee et les autres
            # couches restent a +15°C → XP12 blend et pas de neige.
            try:
                for i in range(len(info.temp_layers)):
                    info.temp_layers[i] = temp
                for i in range(len(info.dewp_layers)):
                    info.dewp_layers[i] = temp - 2.0  # dewpoint legèrement sous temp
            except (AttributeError, TypeError):
                pass  # temp_layers pas disponible (XP12 < 12.3)

        # -- Coverage radius and altitude cap --
        # CRITICAL: getWeatherAtLocation returns 0 for these fields,
        # so we MUST always set them explicitly
        info.radius_nm = float(weather.get("radius_nm", 50.0))
        info.max_altitude_msl_ft = float(weather.get("max_alt_ft", 30000.0))

        # -- Time of day --
        # time_of_day_h is already in UTC (the pipeline converts local hour ->
        # UTC via timezonefinder + pytz in xplane_weather.local_hour_to_zulu).
        # zulu_time_sec is XP's dataref for "seconds since midnight UTC".
        if "time_of_day_h" in weather:
            hour = float(weather["time_of_day_h"])
            try:
                xp.setDatai(self.dr_use_system_time, 0)
            except Exception:
                pass
            xp.setDataf(self.dr_zulu_time, hour * 3600.0)

        # Apply weather — isIncremental=False to replace all prior records
        with xp.weatherUpdateContext(isIncremental=False, updateImmediately=True):
            xp.setWeatherAtLocation(lat, lon, elev, info)

        # -- Rain drop scale (private dataref, hors XPLMWeather) --
        if "rain_scale" in weather and self.dr_rain_scale is not None:
            try:
                xp.setDataf(self.dr_rain_scale, float(weather["rain_scale"]))
            except Exception as e:
                xp.log(f"LARD Weather v2: rain_scale error: {e}")

        time_str = f" time={weather['time_of_day_h']}h" if "time_of_day_h" in weather else ""
        cloud_str = "auto (XP12)" if cloud_type is None else f"type={cloud_type:.0f} cov={cloud_coverage:.1f} {cloud_base:.0f}-{cloud_top:.0f}m"
        xp.log(f"LARD Weather v2: SET clouds=[{cloud_str}] "
               f"precip={info.precip_rate:.2f} "
               f"vis={info.visibility:.0f}m radius={info.radius_nm:.0f}nm"
               f"{time_str} at ({lat:.4f}, {lon:.4f})")

        # Readback : verifier ce que XP12 a reellement applique
        try:
            rb = xp.getWeatherAtLocation(lat, lon, elev)
            for i in range(min(3, len(rb.cloud_layers))):
                cl = rb.cloud_layers[i]
                xp.log(f"LARD Weather v2: READBACK layer[{i}] "
                       f"type={cl.cloud_type:.0f} cov={cl.coverage:.2f} "
                       f"base={cl.alt_base:.0f}m top={cl.alt_top:.0f}m")
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
        """Reset to default weather.

        Calling each step in isolation is not enough — XP's weather state
        survives across our plugin's life unless we also force a regen
        and switch back to real-weather mode.
        """
        # 1) Entering an isIncremental=False context and exiting without any
        #    setWeatherAtLocation call wipes all records we previously injected.
        with xp.weatherUpdateContext(isIncremental=False, updateImmediately=True):
            pass

        # 2) Force XP to rebuild the weather grid from scratch (otherwise the
        #    cleared cells stay "manual but empty" — clouds linger visually).
        try:
            xp.commandOnce(self.cmd_regen_weather)
        except Exception:
            pass

        # 3) Hand control back to METAR-driven real weather.
        try:
            xp.setDatai(self.dr_change_mode, 7)
        except Exception:
            pass

        # 4) Restore system time (we forced time_of_day_h while injecting).
        try:
            xp.setDatai(self.dr_use_system_time, 1)
        except Exception:
            pass

        xp.log("LARD Weather v2: CLEARED (regen + real weather + system time)")

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
