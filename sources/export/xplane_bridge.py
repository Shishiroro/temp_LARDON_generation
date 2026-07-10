"""
xplane_bridge.py — Interface avec X-Plane 12 pour le rendu automatise
=====================================================================
Communication UDP avec X-Plane pour injecter des poses camera
frame par frame et capturer des screenshots automatiquement.

Protocole UDP X-Plane :
  - DREF : set un dataref (float)
  - CMND : execute une commande X-Plane
  - RREF : request dataref (lecture)

Positionnement valide :
  - Pause sim + override planepath (l'avion est verrouille, plus de physique)
  - Paquet VEHS (lat/lon en double precision float64, angles en float32) —
    indispensable car les datarefs natifs sont en float32 et le bruit
    quantif. sur la latitude se voit dans le rendu en courte finale.
  - Les poses du JSON sont en lon/lat/alt geodesique (renderer-agnostique)

Capture :
  - mss (screen grab) cible sur la zone client de la fenetre X-Plane
  - Sortie JPEG (qualite 90) : compromis taille / latence pour les datasets longs

Convention pitch stockee : 90 = regard horizontal (level).
X-Plane attend : pitch 0 = level, negatif = nez en bas.
Conversion appliquee dans _convert_pose : xplane_pitch = pitch_stocke - 90.

Usage :
    from xplane_bridge import render_scenario, XPlaneConfig
    config = XPlaneConfig(xplane_dir="C:/X-Plane 12")
    render_scenario("runs/LFPO_24/poses_cam_export.json", "runs/LFPO_24/footage", config)
"""

import struct
import socket
import time
import json
import math
import ctypes
from pathlib import Path
from dataclasses import dataclass

import mss
import mss.tools
from PIL import Image

import platform

_IS_WINDOWS = platform.system() == "Windows"
_IS_LINUX = platform.system() == "Linux"

# Windows : win32gui pour gestion fenetres + DPI-aware
HAS_WIN32 = False
if _IS_WINDOWS:
    try:
        import win32gui
        HAS_WIN32 = True
        # Per-Monitor DPI Aware v2 (Windows 10 1703+), fallback sur v1
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except ImportError:
        pass

# Linux : python-xlib pour gestion fenetres (pip install python-xlib)
HAS_XLIB = False
if _IS_LINUX:
    try:
        from Xlib import display as _XlibDisplay  # noqa: F401
        HAS_XLIB = True
    except ImportError:
        pass

HAS_WINDOW_MGMT = HAS_WIN32 or HAS_XLIB

# Display Xlib (lazy, une seule connexion par process)
_xlib_disp = None


def _get_xlib_disp():
    global _xlib_disp
    if _xlib_disp is None:
        from Xlib import display as _XD
        _xlib_disp = _XD.Display()
    return _xlib_disp


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class XPlaneConfig:
    """Configuration de la connexion X-Plane."""
    host: str = "127.0.0.1"
    port: int = 49000               # Port UDP X-Plane (reception)
    xplane_dir: str = ""            # Repertoire d'installation X-Plane
    settle_time: float = 0.07       # Attente apres changement de pose (sec) — surcharge via XML param screenshot_duration
    window_width: int = 1024        # Largeur zone client X-Plane (carre)
    window_height: int = 1024       # Hauteur zone client X-Plane (carre)
    fov_h: float = 60.0             # FOV horizontal (reglages X-Plane, 60° comme LARD)
    fov_v: float = 60.0             # FOV vertical (= horizontal car fenetre carrée)


# ---------------------------------------------------------------------------
# Protocole UDP X-Plane
# ---------------------------------------------------------------------------

def _pack_dref(dref: str, value: float) -> bytes:
    """Empaquete un message DREF (set dataref)."""
    msg = b"DREF\x00"
    msg += struct.pack('<f', value)
    msg += dref.encode('utf-8').ljust(500, b'\x00')
    return msg


def _pack_cmnd(command: str) -> bytes:
    """Empaquete un message CMND (execute commande)."""
    msg = b"CMND\x00"
    msg += command.encode('utf-8') + b'\x00'
    return msg


# ---------------------------------------------------------------------------
# Fenetre X-Plane (Windows)
# ---------------------------------------------------------------------------

def find_xplane_window():
    """Trouve la fenetre X-Plane principale (la plus grande).

    :return: dict {hwnd, rect, title} ou None si introuvable.
             hwnd = HWND (Windows) ou window ID (Linux).
             rect = (left, top, right, bottom) en coords ecran.
    """
    if HAS_WIN32:
        return _find_xplane_window_win32()
    if HAS_XLIB:
        return _find_xplane_window_xlib()
    return None


# --- Windows impl ---

def _find_xplane_window_win32():
    candidates = []
    def callback(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if "X-Plane" in title and win32gui.IsWindowVisible(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            area = (rect[2] - rect[0]) * (rect[3] - rect[1])
            candidates.append({'hwnd': hwnd, 'rect': rect, 'title': title, 'area': area})
        return True
    win32gui.EnumWindows(callback, None)
    if not candidates:
        return None
    best = max(candidates, key=lambda c: c['area'])
    return {'hwnd': best['hwnd'], 'rect': best['rect'], 'title': best['title']}


def _get_client_rect_win32(hwnd):
    """Zone client (rendu pur, sans bordures ni barre titre) en coords ecran."""
    client = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (client[0], client[1]))
    right, bottom = win32gui.ClientToScreen(hwnd, (client[2], client[3]))
    return left, top, right, bottom


# --- Linux impl (python-xlib) ---

def _xlib_window_name(d, win):
    """Retourne le titre de la fenetre (tente _NET_WM_NAME puis WM_NAME)."""
    from Xlib import X
    try:
        atom = d.intern_atom('_NET_WM_NAME')
        utf8 = d.intern_atom('UTF8_STRING')
        prop = win.get_full_property(atom, utf8)
        if prop and prop.value:
            return prop.value.decode('utf-8', errors='replace')
    except Exception:
        pass
    try:
        name = win.get_wm_name()
        if name:
            return name
    except Exception:
        pass
    return None


def _find_xplane_window_xlib():
    """Trouve la fenetre X-Plane via python-xlib."""
    try:
        from Xlib import X
        d = _get_xlib_disp()
        root = d.screen().root
        # _NET_CLIENT_LIST = fenetres top-level (EWMH, supporte par tous les WM modernes)
        try:
            prop = root.get_full_property(d.intern_atom('_NET_CLIENT_LIST'), X.AnyPropertyType)
            wids = list(prop.value) if prop else []
        except Exception:
            wids = []
        if not wids:
            # Fallback : parcours recursif de l'arbre
            def _collect(win):
                ids = []
                try:
                    for child in win.query_tree().children:
                        ids.append(child.__resource_id)
                        ids.extend(_collect(child))
                except Exception:
                    pass
                return ids
            wids = _collect(root)
        candidates = []
        for wid in wids:
            try:
                win = d.create_resource_object('window', wid)
                name = _xlib_window_name(d, win)
                if not name or 'X-Plane' not in name:
                    continue
                geom = win.get_geometry()
                transl = win.translate_coords(root, 0, 0)
                x, y, w, h = transl.x, transl.y, geom.width, geom.height
                candidates.append({'hwnd': wid, 'rect': (x, y, x + w, y + h),
                                   'title': name, 'area': w * h})
            except Exception:
                continue
        if not candidates:
            return None
        best = max(candidates, key=lambda c: c['area'])
        return {'hwnd': best['hwnd'], 'rect': best['rect'], 'title': best['title']}
    except Exception:
        return None


def _get_client_rect_xlib(wid):
    """Zone client en coords ecran (Linux xlib — gere le reparenting WM)."""
    try:
        d = _get_xlib_disp()
        root = d.screen().root
        win = d.create_resource_object('window', wid)
        geom = win.get_geometry()
        # translate_coords donne les coords absolues du point (0,0) de win
        # dans le repere de root — c'est le coin haut-gauche de la zone client
        coords = root.translate_coords(win, 0, 0)
        x, y = coords.x, coords.y
        return x, y, x + geom.width, y + geom.height
    except Exception:
        return None


# --- Fonctions cross-platform ---

def _get_client_rect(hwnd):
    """Zone client en coords ecran (dispatch OS)."""
    if HAS_WIN32:
        return _get_client_rect_win32(hwnd)
    if HAS_XLIB:
        return _get_client_rect_xlib(hwnd)
    return None


def resize_xplane_window(width=1280, height=1024):
    """Redimensionne la fenetre X-Plane pour que la zone client fasse width x height."""
    info = find_xplane_window()
    if not info:
        print("  [XPLANE] Fenetre introuvable, resize impossible")
        return None
    hwnd = info['hwnd']

    if HAS_WIN32:
        full = win32gui.GetWindowRect(hwnd)
        client = _get_client_rect_win32(hwnd)
        border_w = (full[2] - full[0]) - (client[2] - client[0])
        border_h = (full[3] - full[1]) - (client[3] - client[1])
        win32gui.MoveWindow(
            hwnd, full[0], full[1],
            width + border_w, height + border_h, True,
        )
    elif HAS_XLIB:
        try:
            d = _get_xlib_disp()
            win = d.create_resource_object('window', hwnd)
            win.configure(x=0, y=0, width=width, height=height)
            d.sync()
        except Exception as e:
            print(f"  [XPLANE] Resize xlib failed: {e}")

    time.sleep(0.5)
    updated = find_xplane_window()
    if updated:
        info['rect'] = updated['rect']
    return info


def get_xplane_capture_region():
    """Retourne la region mss pour capturer la zone client X-Plane."""
    info = find_xplane_window()
    if not info:
        return None
    print(f"  [XPLANE] Fenetre trouvee: '{info['title']}' rect={info['rect']}")
    client = _get_client_rect(info['hwnd'])
    if not client:
        return None
    print(f"  [XPLANE] Client rect: left={client[0]} top={client[1]} "
          f"right={client[2]} bottom={client[3]}")
    region = {
        "left": client[0],
        "top": client[1],
        "width": client[2] - client[0],
        "height": client[3] - client[1],
    }
    # Validation : verifier que la region est dans les limites des moniteurs mss
    try:
        with mss.mss() as sct:
            all_mon = sct.monitors[0]  # moniteur virtuel = tout le desktop
            print(f"  [XPLANE] Desktop virtuel: {all_mon}")
    except Exception:
        pass
    return region


# ---------------------------------------------------------------------------
# Connexion X-Plane
# ---------------------------------------------------------------------------

class XPlaneConnection:
    """Connexion UDP vers X-Plane 12."""

    def __init__(self, config: XPlaneConfig = None):
        self.config = config or XPlaneConfig()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5.0)
        self.addr = (self.config.host, self.config.port)
        # Capture ecran
        self.sct = None
        self.capture_region = None

    def send_dref(self, dref: str, value: float):
        """Set un dataref X-Plane."""
        self.sock.sendto(_pack_dref(dref, value), self.addr)

    def send_command(self, command: str):
        """Execute une commande X-Plane."""
        self.sock.sendto(_pack_cmnd(command), self.addr)

    def read_dref(self, dref: str, idx: int = 0):
        """Lit un dataref X-Plane via RREF."""
        msg = b"RREF\x00"
        msg += struct.pack('<ii', 1, idx)
        msg += dref.encode('utf-8').ljust(400, b'\x00')
        self.sock.sendto(msg, self.addr)
        try:
            data, _ = self.sock.recvfrom(1024)
            val = struct.unpack('<f', data[9:13])[0] if len(data) >= 13 else None
        except socket.timeout:
            val = None
        # Stop stream
        msg2 = b"RREF\x00"
        msg2 += struct.pack('<ii', 0, idx)
        msg2 += dref.encode('utf-8').ljust(400, b'\x00')
        self.sock.sendto(msg2, self.addr)
        return val

    def check_connection(self) -> bool:
        """Verifie que X-Plane est joignable via RREF."""
        freq, idx = 1, 0
        dref = "sim/time/total_running_time_sec"
        msg = b"RREF\x00"
        msg += struct.pack('<ii', freq, idx)
        msg += dref.encode('utf-8').ljust(400, b'\x00')
        try:
            self.sock.sendto(msg, self.addr)
            self.sock.recvfrom(1024)
            msg_stop = b"RREF\x00"
            msg_stop += struct.pack('<ii', 0, idx)
            msg_stop += dref.encode('utf-8').ljust(400, b'\x00')
            self.sock.sendto(msg_stop, self.addr)
            return True
        except socket.timeout:
            return False

    def setup_view(self):
        """Configure X-Plane pour le rendu automatise.

        - Pause le simulateur
        - Override du chemin de vol
        - Zero velocites et accelerations
        - Vue forward with nothing (pas de cockpit)
        - Resize et cible la fenetre X-Plane pour capture mss
        """
        # Pause le sim
        self.send_dref("sim/time/paused", 1.0)
        time.sleep(0.3)

        # Override position avion
        self.send_dref("sim/operation/override/override_planepath[0]", 1.0)
        time.sleep(0.1)
        self.send_dref("sim/operation/override/override_flight_control", 1.0)
        self.send_dref("sim/operation/override/override_throttles", 1.0)
        time.sleep(0.1)

        # Zero velocites + accelerations + rotation rates
        for dref in [
            "sim/flightmodel/position/local_vx",
            "sim/flightmodel/position/local_vy",
            "sim/flightmodel/position/local_vz",
            "sim/flightmodel/position/local_ax",
            "sim/flightmodel/position/local_ay",
            "sim/flightmodel/position/local_az",
            "sim/flightmodel/position/P",
            "sim/flightmodel/position/Q",
            "sim/flightmodel/position/R",
        ]:
            self.send_dref(dref, 0.0)
        time.sleep(0.1)

        # Vue clean sans cockpit
        self.send_command("sim/view/forward_with_nothing")
        time.sleep(0.3)

        # Resize fenetre a la taille cible
        if HAS_WINDOW_MGMT:
            resize_xplane_window(self.config.window_width, self.config.window_height)
            print(f"  [XPLANE] Fenetre resizee -> {self.config.window_width}x{self.config.window_height}")

        # Fermer les panneaux/popups d'instruments susceptibles d'occulter le rendu
        # (vue forward_with_nothing les masque deja sur la plupart des airliners,
        #  ces commandes sont defensives pour les addons GA / cockpit personnalises)
        for cmd in (
            "sim/GPS/g1000n1_popup_close",
            "sim/instruments/map_close",
            "sim/instruments/popup_1_close",
            "sim/instruments/popup_2_close",
            "sim/instruments/popup_3_close",
        ):
            self.send_command(cmd)
        time.sleep(0.2)

        # Masquer tous les panneaux via le dataref show_popup
        for i in range(20):
            self.send_dref(f"sim/cockpit2/radios/indicators/show_popup[{i}]", 0.0)
        time.sleep(0.2)

        # Desactiver le mouse yoke (reticule au centre de l'ecran)
        self.send_dref("sim/operation/override/override_joystick", 1.0)
        self.send_dref("sim/joystick/mouse_is_yoke", 0.0)
        time.sleep(0.1)

        # Capture region : on relit la geometrie reelle apres le resize ci-dessus
        # (le WM peut imposer une taille legerement differente — bordures, decorations).
        if HAS_WINDOW_MGMT:
            self.capture_region = get_xplane_capture_region()
            if self.capture_region:
                print(f"  [XPLANE] Capture : {self.capture_region['width']}x"
                      f"{self.capture_region['height']}")
            self.sct = mss.mss()

        # --- FOV camera ---
        # Programmer le FOV via datarefs (methode LARD officielle).
        # Lire la taille reelle de la fenetre X-Plane pour adapter le FOV
        # si la fenetre est plus grande que la taille de crop desiree.
        actual_w = self.read_dref("sim/graphics/view/window_width", 30)
        actual_h = self.read_dref("sim/graphics/view/window_height", 31)
        desired_w = self.config.window_width
        desired_h = self.config.window_height

        if actual_w and actual_h:
            fact_w = actual_w / desired_w
            fact_h = actual_h / desired_h
            if fact_w < 1.0 or fact_h < 1.0:
                print(f"  [XPLANE] ATTENTION: fenetre ({actual_w:.0f}x{actual_h:.0f}) "
                      f"< taille desiree ({desired_w}x{desired_h})")
            # Adapter le FOV pour que le crop central ait le FOV desire
            # Formule tangent-based (geometriquement correcte) :
            #   prog_fov = 2 * atan(tan(desired_fov/2) * fact)
            # La formule lineaire (fact * fov) surestime le FOV de ~3° a fact=1.25,
            # ce qui decale la bbox GT vers l'exterieur (~5px/100px du centre).
            prog_fov_h = 2.0 * math.degrees(math.atan(
                math.tan(math.radians(self.config.fov_h / 2.0)) * fact_w))
            prog_fov_v = 2.0 * math.degrees(math.atan(
                math.tan(math.radians(self.config.fov_v / 2.0)) * fact_h))
            print(f"  [XPLANE] Fenetre reelle: {actual_w:.0f}x{actual_h:.0f}, "
                  f"fact={fact_w:.2f}x{fact_h:.2f}")
        else:
            prog_fov_h = self.config.fov_h
            prog_fov_v = self.config.fov_v

        # Activer FOV vertical independant (separe du horizontal)
        self.send_dref("sim/graphics/settings/non_proportional_vertical_FOV", 1.0)
        time.sleep(0.1)
        # Programmer les FOV
        self.send_dref("sim/graphics/view/field_of_view_deg", prog_fov_h)
        self.send_dref("sim/graphics/view/vertical_field_of_view_deg", prog_fov_v)
        time.sleep(0.2)

        # Verifier le readback
        readback_h = self.read_dref("sim/graphics/view/field_of_view_deg", 32)
        readback_v = self.read_dref("sim/graphics/view/vertical_field_of_view_deg", 33)
        if readback_h and readback_v:
            print(f"  [XPLANE] FOV programme: H={prog_fov_h:.1f}° V={prog_fov_v:.1f}°"
                  f" (readback: H={readback_h:.1f}° V={readback_v:.1f}°)")
        else:
            print(f"  [XPLANE] FOV programme: H={prog_fov_h:.1f}° V={prog_fov_v:.1f}°"
                  f" (readback timeout)")

    def send_posi(self, lat, lon, alt_m, heading, pitch, roll):
        """Positionne l'avion via VEHS (lat/lon en double precision float64).

        Protocole VEHS X-Plane : lat/lon/alt en double, angles en float32.
        On envoie le paquet 2x : X-Plane recale l'elevation au sol au 1er
        paquet (l'alt envoyee est ignoree), le 2e applique vraiment l'alt
        demandee. Astuce reprise du connecteur LARD officiel.
        """
        msg = struct.pack('<4sxidddfff', b'VEHS',
                          0,          # aircraft index (0 = ego)
                          lat,        # double (float64)
                          lon,        # double (float64)
                          alt_m,      # double (MSL metres)
                          heading,    # float (true heading deg)
                          pitch,      # float (deg, 0=level)
                          roll)       # float (deg)
        self.sock.sendto(msg, self.addr)
        self.sock.sendto(msg, self.addr)  # 2x pour l'elevation

    def set_camera_pose(self, lat, lon, alt_m, heading, pitch, roll):
        """Positionne la camera via VEHS (double precision lat/lon).

        Utilise le paquet VEHS pour envoyer lat/lon en float64,
        plus precis que les datarefs float32 ou la conversion locale.
        """
        self.send_posi(lat, lon, alt_m, heading, pitch, roll)
        # Zero velocites pour eviter le drift
        self.send_dref("sim/flightmodel/position/local_vx", 0.0)
        self.send_dref("sim/flightmodel/position/local_vy", 0.0)
        self.send_dref("sim/flightmodel/position/local_vz", 0.0)
        time.sleep(self.config.settle_time)

    def capture_frame(self, output_path):
        """Capture la fenetre X-Plane et crop au centre a la taille desiree.

        Si la fenetre est plus grande que window_width x window_height,
        on crop au centre pour que le FOV de l'image corresponde exactement
        au FOV desire (le FOV programme a ete adapte pour ca).

        :param output_path: chemin du fichier de sortie (.jpg)
        :return: True si capture reussie
        """
        if not self.sct or not self.capture_region:
            return False
        img = self.sct.grab(self.capture_region)
        pil_img = Image.frombytes("RGB", img.size, bytes(img.rgb))

        # Crop centre si la capture est plus grande que la taille desiree
        cap_w, cap_h = pil_img.size
        desired_w = self.config.window_width
        desired_h = self.config.window_height
        if cap_w > desired_w or cap_h > desired_h:
            left = (cap_w - desired_w) // 2
            top = (cap_h - desired_h) // 2
            pil_img = pil_img.crop((left, top, left + desired_w, top + desired_h))

        pil_img.save(str(output_path), quality=90)
        return True

    def close(self):
        """Ferme les sockets sans relacher les overrides (l'avion reste fige)."""
        if self.sct:
            self.sct.close()
        self.sock.close()


# ---------------------------------------------------------------------------
# Connexion UDP
# ---------------------------------------------------------------------------

def create_connection(config=None):
    """Cree une connexion UDP vers X-Plane.

    :param config: XPlaneConfig (optionnel)
    :return: XPlaneConnection
    """
    config = config or XPlaneConfig()
    print(f"  [XPLANE] Connexion UDP -> {config.host}:{config.port}")
    return XPlaneConnection(config)


def _convert_pose(lon, lat, alt, yaw, pitch_src, roll):
    """Convertit une pose stockee vers le format X-Plane.

    Format stocke : pitch 90 = regard horizontal (level).
    X-Plane : pitch 0 = level, negatif = nez en bas.

    :return: dict {lat, lon, alt_m, heading, pitch, roll}
    """
    return {
        "lat": float(lat),
        "lon": float(lon),
        "alt_m": float(alt),
        "heading": float(yaw),
        "pitch": float(pitch_src) - 90.0,
        "roll": float(roll),
    }


# ---------------------------------------------------------------------------
# Fichier poses_cam_export.json (format universel, independant du renderer)
# ---------------------------------------------------------------------------

def save_poses_json(flight_data, fps, scenario_name, output_path, ltp_alt=None,
                    trajectory_config=None):
    """Sauvegarde les poses dans un fichier JSON universel.

    :param flight_data: list de tuples (lon, lat, alt, yaw, pitch_ges, roll)
    :param fps: frames par seconde
    :param scenario_name: nom du scenario (ex: LFPO_24)
    :param output_path: chemin du fichier JSON de sortie
    :param ltp_alt: altitude MSL du LTP (seuil de piste) en metres, pour la meteo
    :param trajectory_config: dict optionnel des parametres trajectoire samples
        par TAF (along_track_distance_*, ground_speed_kts, turbulence_intensity,
        wind_*, stabilization_distance_m). Permet de reconstruire params_trace.xml
        a posteriori.
    :return: chemin du fichier cree
    """
    output_path = Path(output_path)

    poses = []
    for lon, lat, alt, yaw, pitch, roll in flight_data:
        poses.append({
            "lon": float(lon),
            "lat": float(lat),
            "alt_m": float(alt),
            "heading": float(yaw),
            "pitch": float(pitch),
            "roll": float(roll),
        })

    data = {
        "scenario_name": scenario_name,
        "n_frames": len(poses),
        "fps": fps,
        "ltp_alt": float(ltp_alt) if ltp_alt is not None else 0.0,
        "poses": poses,
    }
    if trajectory_config:
        data["trajectory"] = trajectory_config

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  .json poses -> {output_path}")
    return str(output_path)


def load_poses_json(path):
    """Charge un fichier poses_cam_export.json.

    :return: dict {scenario_name, n_frames, fps, poses: [{lon,lat,alt_m,heading,pitch,roll}]}
    """
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Rendu d'un scenario complet
# ---------------------------------------------------------------------------

def render_scenario(poses_path, output_dir, config=None, weather_profile_path=None):
    """Rend un scenario complet via X-Plane.

    Lit le fichier poses_cam_export.json, injecte chaque pose dans X-Plane,
    capture la fenetre via mss, et sauve les images en JPEG.

    :param poses_path: chemin vers le fichier poses_cam_export.json
    :param output_dir: dossier de sortie pour les images (footage/)
    :param config: XPlaneConfig (optionnel)
    :param weather_profile_path: chemin vers weather_profile.json (optionnel)
    :return: Path du dossier de sortie
    """
    config = config or XPlaneConfig()
    data = load_poses_json(poses_path)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scenario_name = data["scenario_name"]
    n_frames = data["n_frames"]

    print(f"  [XPLANE] Rendu de {scenario_name} ({n_frames} frames)...")

    conn = create_connection(config)

    try:
        if not conn.check_connection():
            raise ConnectionError(
                "Impossible de joindre X-Plane. "
                "Verifier que X-Plane 12 est lance et ecoute sur "
                f"{config.host}:{config.port}"
            )

        conn.setup_view()

        # Teleporter l'avion a la premiere pose AVANT la meteo
        # pour que X-Plane charge les textures de la zone pendant la stabilisation
        first_pose = data["poses"][0]
        xp_first = _convert_pose(
            first_pose["lon"], first_pose["lat"], first_pose["alt_m"],
            first_pose["heading"], first_pose["pitch"], first_pose["roll"],
        )
        conn.set_camera_pose(
            xp_first["lat"], xp_first["lon"], xp_first["alt_m"],
            xp_first["heading"], xp_first["pitch"], xp_first["roll"],
        )

        # Injecter la meteo (per-scenario, une seule fois) APRES teleportation,
        # afin que X-Plane charge les textures de la zone pendant la stabilisation
        # (load_texture_duration / weather_effect_duration, lus depuis le XML).
        #
        # IMPORTANT : on unpause la sim avant l'injection. X-Plane ne spawn pas
        # de particules (pluie/neige) tant que la sim est pausee. Les overrides
        # planepath / flight_control / throttles restent a 1.0 -> l'avion reste
        # fige meme si la sim tourne (seul le moteur graphique avance).
        weather_active = False
        weather_status = "no_weather"  # no_weather | ok | plugin_timeout | inject_failed
        if weather_profile_path and Path(weather_profile_path).exists():
            from xplane_weather import (
                load_weather_profile, inject_weather, set_exchange_dir, check_plugin,
            )
            weather_cfg = load_weather_profile(weather_profile_path)
            if weather_cfg:
                if config.xplane_dir:
                    set_exchange_dir(config.xplane_dir)
                    if check_plugin():
                        print(f"  [XPLANE] Plugin XPPython3 weather OK")
                        max_alt_m = max(p["alt_m"] for p in data["poses"])
                        first_lat = data["poses"][0]["lat"]
                        first_lon = data["poses"][0]["lon"]
                        # Unpause pour que les particules meteo (pluie/neige) spawn pendant le settle
                        conn.send_dref("sim/time/paused", 0.0)
                        weather_active = inject_weather(
                            weather_cfg, aircraft_max_alt_m=max_alt_m,
                            latitude=first_lat, longitude=first_lon,
                        )
                        weather_status = "ok" if weather_active else "inject_failed"
                    else:
                        print(f"  [XPLANE] ATTENTION: plugin XPPython3 ne repond pas — meteo ignoree")
                        weather_status = "plugin_timeout"

        # Si la meteo est active, on garde la sim qui tourne pendant la capture :
        # sinon X-Plane fige le moteur de particules et la pluie/neige disparait
        # apres quelques frames. override_planepath verrouille deja la position.
        if weather_active:
            conn.send_dref("sim/time/paused", 0.0)

        render_w = conn.capture_region["width"] if conn.capture_region else config.window_width
        render_h = conn.capture_region["height"] if conn.capture_region else config.window_height
        print(f"  [XPLANE] Render config : {render_w}x{render_h}"
              f" FOV {config.fov_h}x{config.fov_v}°  weather={weather_status}")

        t_start = time.perf_counter()

        # Padding identique a LARD : img_digits = len(str(n_frames - 1))
        img_digits = len(str(n_frames - 1))

        for i, pose in enumerate(data["poses"]):
            xp = _convert_pose(
                pose["lon"], pose["lat"], pose["alt_m"],
                pose["heading"], pose["pitch"], pose["roll"],
            )

            conn.set_camera_pose(
                xp["lat"], xp["lon"], xp["alt_m"],
                xp["heading"], xp["pitch"], xp["roll"],
            )

            # Nommage 0-based, padding LARD : {name}_{i:0Nd}.jpg
            dst = output_dir / f"{scenario_name}_{str(i).zfill(img_digits)}.jpg"
            conn.capture_frame(dst)

            if (i + 1) % 50 == 0 or (i + 1) == n_frames:
                elapsed = time.perf_counter() - t_start
                rate = (i + 1) / elapsed
                print(f"  [XPLANE] {i + 1}/{n_frames} frames | "
                      f"capture {rate:.1f} img/s | {elapsed:.0f}s")

        n_rendered = len(list(output_dir.glob("*.jpg")))
        total = time.perf_counter() - t_start
        print(f"  [XPLANE] {n_rendered} images dans {output_dir} ({total:.0f}s)")

        if n_rendered < n_frames:
            print(f"  [XPLANE] ATTENTION : {n_frames - n_rendered} frames manquantes")

        # Geler l'avion sur la derniere position pour eviter un crash au sol
        conn.send_dref("sim/time/paused", 1.0)
        conn.send_dref("sim/flightmodel/position/local_vx", 0.0)
        conn.send_dref("sim/flightmodel/position/local_vy", 0.0)
        conn.send_dref("sim/flightmodel/position/local_vz", 0.0)
        # override_planepath[0] reste a 1.0 (pas de release)

    finally:
        # Pas de clear meteo ici — le prochain set_weather (isIncremental=False)
        # ecrase tout. Le clear final est fait par run_pipeline.py apres tous les scenarios.
        conn.close()

    return output_dir


def render_xplane_run(run_dir, xplane_dir):
    """Rend les images d'un run via X-Plane 12.

    Lit run_dir/poses_cam_export.json (+ weather_profile.json si present),
    sauve les images dans run_dir/footage/. Skip si footage/ contient deja
    des images.

    Renomme depuis `render_run` pour eviter la collision avec
    Export.render_run (qui orchestre la Phase 2 entiere : rendu + fautes + GT).

    :param run_dir: dossier du run
    :param xplane_dir: chemin du repertoire X-Plane 12
    :return: True si les images ont ete rendues (ou existaient deja), False sinon
    """
    run_dir = Path(run_dir)
    poses_file = run_dir / "poses_cam_export.json"
    footage_dir = run_dir / "footage"

    if not poses_file.exists():
        print(f"  [XPLANE] Pas de poses_cam_export.json pour {run_dir.name}")
        return False

    # sources/ est ajoute en sys.path par run_pipeline.py et Export.py
    from runs import list_images
    imgs = list_images(footage_dir)
    if imgs:
        print(f"  [XPLANE] footage/ existe deja ({len(imgs)} images), skip rendu")
        return True

    print(f"\n  [XPLANE] Rendu de {run_dir.name}...")

    # Charge le poses JSON pour recuperer screenshot_duration (delai apres
    # teleport camera). Si absent (anciens runs), conserve le defaut XPlaneConfig.
    with open(poses_file) as f:
        poses_data = json.load(f)
    pose_settle = (poses_data.get("trajectory") or {}).get("screenshot_duration")

    config = XPlaneConfig(xplane_dir=xplane_dir)
    if pose_settle is not None:
        config.settle_time = float(pose_settle)
    weather_file = run_dir / "weather_profile.json"
    weather_path = str(weather_file) if weather_file.exists() else None

    render_scenario(str(poses_file), str(footage_dir), config,
                    weather_profile_path=weather_path)
    return True
