"""
trajectory_builder.py — Generation de trajectoires d'approche realistes
=======================================================================
Migration depuis LARDv2/LARD/src/trajectory/sequence_generator.py
vers un module decouple, style fonctions pures + dataclasses.

Pipeline :
    1. Timeline spatiale (frames uniformes en distance, dt variable)
    2. Processus Ornstein-Uhlenbeck a dt variable (5 canaux)
    3. Convergence finale (deviations → 0 pres de la piste)
    4. Crab angle (correction vent)
    5. Assemblage des poses (lon, lat, alt, yaw, pitch, roll)
"""

import numpy as np
import pyproj
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

KTS_TO_MS = 0.514444


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class TrajectoryConfig:
    """Parametres utilisateur (viennent du XML via TAF)."""
    fps: int = 5
    along_track_distance_start: float = 3000.0
    along_track_distance_end: float = 280.0
    ground_speed_kts: float = 137.0
    correlation_time_s: float = 4.0   # auto-calcule par Export.py (Dryden)
    turbulence_intensity: float = 0.3
    wind_speed_kts: float = 0.0
    wind_direction_deg: float = 0.0
    stabilization_distance_m: float = 800.0


@dataclass
class OUParams:
    """Hyperparametres OU (dans le code, pas dans le XML)."""
    alpha_v_deg: float = -3.0       # angle vertical moyen (glide slope ~3°, pente de descente standard ILS)
    alpha_h_deg: float = 0.0        # angle horizontal moyen (axe piste = l'avion vise la piste)
    pitch_deg: float = -4.0         # pitch moyen (assiette negative en approche)
    yaw_deg: float = 0.0            # yaw moyen (deviation)
    roll_deg: float = 0.0           # roll moyen (ailes a plat)

    std_alpha_v_deg: float = 0.25   # ecart-type vertical (~half-dot glide slope)
    std_alpha_h_deg: float = 0.35   # ecart-type horizontal (~half-dot localizer, ±15m a 3km)
    std_yaw_deg: float = 1.2        # ecart-type yaw (approche stabilisee A320)
    std_pitch_deg: float = 0.7      # ecart-type pitch (assiette quasi fixe sur glide)
    std_roll_deg: float = 2.5       # ecart-type roll (+ couplage lateral ajoute apres OU)

    dist_ap_m: float = 300.0        # distance aiming point depuis LTP, ou l'avion vise (touchdown ~300m apres seuil)

    # Offset initial (degrade avec la convergence finale)
    alpha_h_offset_deg: float = 5.0   # ex: 2.0 = 2° lateral au start
    alpha_v_offset_deg: float = 5.0   # ex: 0.5 = 0.5° au-dessus du glide

# ---------------------------------------------------------------------------
# Timeline spatiale
# ---------------------------------------------------------------------------

def compute_spatial_timeline(cfg: TrajectoryConfig):
    """Genere N frames uniformement espacees en distance.

    A vitesse constante (cas actuel), dt est aussi constant — la structure
    dt_array reste un vecteur pour permettre une future deceleration variable
    sans changer le reste du pipeline.

    :return: (distances_m, dt_array)
        distances_m : ndarray (n_frames,), decroissant (start -> end)
        dt_array    : ndarray (n_frames-1,) en secondes
    """
    speed_ms = cfg.ground_speed_kts * KTS_TO_MS
    total_dist = cfg.along_track_distance_start - cfg.along_track_distance_end
    duration = total_dist / speed_ms
    n_frames = max(int(round(duration * cfg.fps)), 2)

    distances_m = np.linspace(cfg.along_track_distance_start, cfg.along_track_distance_end, n_frames)
    spatial_step = abs(distances_m[1] - distances_m[0]) if n_frames > 1 else 0.0

    dt_array = np.full(n_frames - 1, spatial_step / speed_ms)

    return distances_m, dt_array


# ---------------------------------------------------------------------------
# Processus Ornstein-Uhlenbeck a dt variable
# ---------------------------------------------------------------------------

def generate_ou_process(n_steps, dt_array, correlation_time, std, mean=0.0,
                        conv_factors=None, sim_rate_hz=500):
    """
    Processus OU discret avec pas de temps variables et convergence integree.

    x[i+1] = mean + (x[i] - mean) * exp(-dt/tau) + sigma_d * N(0,1)
    ou sigma_d = std_i * sqrt(1 - exp(-2*dt/tau))

    Si conv_factors est fourni, le std varie par pas (std_i = std * conv[i]).
    Le processus OU converge alors naturellement vers mean car le bruit
    injecte diminue progressivement, tout en gardant la correlation temporelle.

    La simulation interne tourne a une frequence fixe (sim_rate_hz) independante
    du fps demande. Cela garantit que le meme seed produit la meme trajectoire
    sous-jacente quel que soit le fps (10, 20, 30...).

    :param n_steps: nombre de pas (frames de sortie)
    :param dt_array: array de dt (n_steps-1,)
    :param correlation_time: tau du processus OU (secondes)
    :param std: ecart-type stationnaire de base
    :param mean: moyenne de reversion
    :param conv_factors: ndarray (n_steps,) facteurs de convergence [0,1]
    :param sim_rate_hz: frequence interne de simulation (Hz). Fixe = meme
                        nombre de tirages aleatoires pour la meme duree.
    :return: ndarray (n_steps,)
    """
    if n_steps <= 0:
        return np.array([])

    tau = max(correlation_time, 1e-6)

    # Temps cumule des frames de sortie. dt_array est suppose dimensionne
    # (n_steps - 1,) — on tronque/pad defensivement pour rester robuste.
    dt_for_frames = dt_array[: n_steps - 1] if n_steps > 1 else np.empty(0)
    frame_times = np.empty(n_steps)
    frame_times[0] = 0.0
    if n_steps > 1:
        np.cumsum(dt_for_frames, out=frame_times[1:])
    total_duration = frame_times[-1]

    # Grille interne a frequence fixe (linspace -> dt_j constant)
    n_micro = max(int(round(total_duration * sim_rate_hz)), 1) + 1
    micro_times = np.linspace(0.0, total_duration, n_micro)

    # Interpoler conv_factors sur la grille interne
    if conv_factors is not None:
        conv_micro = np.interp(micro_times, frame_times, conv_factors)
    else:
        conv_micro = None

    # dt_j est constant sur le linspace -> on hoiste decay / sigma_d_base
    dt_j = (total_duration / (n_micro - 1)) if n_micro > 1 else 0.0
    decay = np.exp(-dt_j / tau)
    sigma_d_base = np.sqrt(max(1.0 - np.exp(-2.0 * dt_j / tau), 0.0))

    # Pre-tirage de toutes les normales (1 par pas, ordre preserve = meme seed -> meme trajectoire)
    noise = np.random.randn(n_micro)

    # OU discret : recurrence Markov, boucle Python necessaire mais legere
    x_micro = np.empty(n_micro)
    std_0 = std * (conv_micro[0] if conv_micro is not None else 1.0)
    x_micro[0] = mean + std_0 * noise[0]
    if conv_micro is not None:
        for j in range(1, n_micro):
            std_j = std * conv_micro[j]
            x_micro[j] = mean + (x_micro[j - 1] - mean) * decay + std_j * sigma_d_base * noise[j]
    else:
        sigma_d = std * sigma_d_base
        for j in range(1, n_micro):
            x_micro[j] = mean + (x_micro[j - 1] - mean) * decay + sigma_d * noise[j]

    # Interpoler aux temps des frames de sortie
    return np.interp(frame_times, micro_times, x_micro)


# ---------------------------------------------------------------------------
# Convergence finale
# ---------------------------------------------------------------------------

def compute_convergence_factors(distances_m, along_track_distance_end,
                                stabilization_distance_m,
                                exponent=0.5, residual=0.08):
    """
    Facteurs de convergence : 1.0 loin, → residual pres de la piste.

    conv(d) = residual + (1 - residual) * clamp((d - end) / (stab - end), 0, 1) ^ exponent

    Le residual (defaut 8%) empeche les deviations de tomber a zero,
    gardant un bruit realiste meme en courte finale.

    :return: ndarray (len(distances_m),) dans [residual, 1]
    """
    factors = np.ones(len(distances_m))
    if stabilization_distance_m <= along_track_distance_end:
        return factors

    mask = distances_m < stabilization_distance_m
    ratio = np.clip(
        (distances_m[mask] - along_track_distance_end) / (stabilization_distance_m - along_track_distance_end),
        0.0, 1.0,
    )
    factors[mask] = residual + (1.0 - residual) * ratio ** exponent

    return factors


# ---------------------------------------------------------------------------
# Crab angle
# ---------------------------------------------------------------------------

def compute_crab_angle(wind_speed_kts, wind_direction_deg,
                       runway_heading_deg, groundspeed_kts,
                       pilot_correction=0.30):
    """Angle de crabe pour compenser le vent traversier (avec correction pilote).

    L'avion pointe le nez face au vent pour que la trajectoire reste alignee
    sur l'axe piste. En pratique, le pilote ne maintient pas le crab pur :
    `pilot_correction` modelise cette fraction corrigee (0 = full crab nominal,
    1 = avion parfaitement aligne par defaut).

    :return: crab angle en degres (signe = sens de la correction)
    """
    if wind_speed_kts <= 0 or groundspeed_kts <= 0:
        return 0.0

    relative_wind_deg = wind_direction_deg - runway_heading_deg
    crosswind_kts = wind_speed_kts * np.sin(np.deg2rad(relative_wind_deg))
    ratio = np.clip(crosswind_kts / groundspeed_kts, -1.0, 1.0)
    crab_deg = np.rad2deg(np.arcsin(ratio))
    return crab_deg * (1.0 - pilot_correction)


# ---------------------------------------------------------------------------
# Generation de trajectoire complete
# ---------------------------------------------------------------------------

def build_trajectory(cfg: TrajectoryConfig, ou: OUParams,
                     ltp_lat, ltp_lon, ltp_alt,
                     runway_heading_deg, runway_back_azimuth_deg):
    """
    Genere une trajectoire d'approche complete.

    :param cfg: parametres utilisateur (du XML)
    :param ou: hyperparametres OU (du code)
    :param ltp_lat, ltp_lon, ltp_alt: position LTP de la piste
    :param runway_heading_deg: azimut LTP→FPAP (cap camera)
    :param runway_back_azimuth_deg: azimut FPAP→LTP (positionnement avion)
    :return: list de tuples (lon, lat, alt, yaw, pitch, roll)
    """
    # --- Timeline spatiale ---
    distances_m, dt_array = compute_spatial_timeline(cfg)
    n_frames = len(distances_m)

    # --- Scaling turbulence ---
    # turb=0 → scale=0.1 (quasi ILS), turb=0.5 → scale=0.55, turb=1 → scale=1.0
    turb_scale = 0.1 + 0.9 * max(0.0, cfg.turbulence_intensity)

    # --- Convergence (integree dans l'OU) ---
    conv = compute_convergence_factors(
        distances_m, cfg.along_track_distance_end, cfg.stabilization_distance_m
    )

    # --- OU deviations par canal (avec convergence integree) ---
    # Le std diminue progressivement via conv_factors, ce qui fait
    # converger le processus OU naturellement vers 0 sans "snap".
    #
    # Tau differencies par type de canal :
    #   - Position (alpha_h, alpha_v) : tau long (~5x) car l'avion a de l'inertie,
    #     il ne change pas de direction instantanement. Donne des derives lentes.
    #   - Attitude (yaw, pitch, roll) : tau court (Dryden) car les surfaces de
    #     controle reagissent vite. Donne des oscillations rapides realistes.
    tau_attitude = cfg.correlation_time_s           # ~2s (Dryden)
    tau_position = cfg.correlation_time_s * 5.0     # ~10s (derive lente)

    def _ou(std, tau, mean=0.0):
        return generate_ou_process(
            n_steps=n_frames,
            dt_array=dt_array,
            correlation_time=tau,
            std=std * turb_scale,
            mean=mean,
            conv_factors=conv,
        )

    alpha_v_dev = _ou(ou.std_alpha_v_deg, tau_position)
    alpha_h_dev = _ou(ou.std_alpha_h_deg, tau_position)
    yaw_dev     = _ou(ou.std_yaw_deg, tau_attitude)
    pitch_dev   = _ou(ou.std_pitch_deg, tau_attitude)
    roll_dev    = _ou(ou.std_roll_deg, tau_attitude)

    # Offset initial qui s'estompe avec conv (1.0 au start, ~0.08 a la piste)
    alpha_h_dev += ou.alpha_h_offset_deg * conv
    alpha_v_dev += ou.alpha_v_offset_deg * conv

    # --- Rate limiting sur les canaux angulaires ---
    # Empeche les sauts brusques frame-a-frame (l'avion ne peut pas
    # tourner instantanement). Meme principe que max_climb_rate pour l'altitude.
    # Valeurs en deg/s : roll ~5, yaw ~3, pitch ~2 (realiste approche).
    max_roll_rate_ds = 5.0    # deg/s
    max_yaw_rate_ds = 3.0     # deg/s
    max_pitch_rate_ds = 2.0   # deg/s
    dt_frame = dt_array[0]  # constant (vitesse constante)

    def _rate_limit(signal, max_rate_ds):
        max_delta = max_rate_ds * dt_frame
        for i in range(1, len(signal)):
            delta = signal[i] - signal[i - 1]
            if abs(delta) > max_delta:
                signal[i] = signal[i - 1] + np.sign(delta) * max_delta
        return signal

    max_alpha_h_rate_ds = 1.5   # deg/s — deviation laterale (localizer)
    max_alpha_v_rate_ds = 0.5   # deg/s — deviation verticale (glide slope)

    alpha_h_dev = _rate_limit(alpha_h_dev, max_alpha_h_rate_ds)
    alpha_v_dev = _rate_limit(alpha_v_dev, max_alpha_v_rate_ds)
    yaw_dev     = _rate_limit(yaw_dev, max_yaw_rate_ds)
    pitch_dev   = _rate_limit(pitch_dev, max_pitch_rate_ds)

    # --- Couplage roll-lateral : l'avion penche dans le sens de sa deviation ---
    # Pour eviter les oscillations parasites, on couple le roll a la derivee de
    # alpha_h filtree par un EMA passe-bas (la derivee brute introduisait des
    # micro-oscillations a haute frequence sur les trajectoires de test).
    # Gain ~2.0 : 1 deg/s de deviation laterale -> 2 deg de roll.
    roll_lateral_gain = 2.0
    # tau_filter=0.615s : equivalent fps-aware d'un EMA fixe (alpha=0.15 a fps=10).
    # ema_alpha = 1 - exp(-dt/tau_filter) garantit le meme lissage quel que soit le fps.
    tau_filter_s = 0.615
    ema_alpha = 1.0 - np.exp(-dt_frame / tau_filter_s)
    d_alpha_h = np.diff(alpha_h_dev, prepend=alpha_h_dev[0]) / dt_frame  # derivee deg/s
    # Filtre EMA passe-bas sur la derivee (recurrence Markov -> boucle Python
    # necessaire, mais sur quelques centaines de frames ca reste negligeable).
    d_alpha_h_filtered = np.zeros_like(d_alpha_h)
    d_alpha_h_filtered[0] = d_alpha_h[0]
    for i in range(1, len(d_alpha_h)):
        d_alpha_h_filtered[i] = ema_alpha * d_alpha_h[i] + (1 - ema_alpha) * d_alpha_h_filtered[i - 1]
    roll_dev += roll_lateral_gain * d_alpha_h_filtered

    roll_dev    = _rate_limit(roll_dev, max_roll_rate_ds)

    # --- Crab angle avec de-crab en finale ---
    # Le pilote maintient le crab pendant l'approche puis l'enleve
    # (coup de palonnier) dans les derniers 300m du segment.
    # Plein crab a decrab_start, 0 a along_track_distance_end (convergence lineaire).
    crab_angle = compute_crab_angle(
        cfg.wind_speed_kts, cfg.wind_direction_deg,
        runway_heading_deg, cfg.ground_speed_kts
    )
    decrab_start_m = cfg.along_track_distance_end + 300.0
    ratio_decrab = np.clip((distances_m - cfg.along_track_distance_end) / 300.0, 0.0, 1.0)
    crab_angles = np.where(distances_m < decrab_start_m, crab_angle * ratio_decrab, crab_angle)

    # --- Calcul altitudes brutes (vectorise) ---
    alpha_v = np.clip(ou.alpha_v_deg + alpha_v_dev, -6.0, -0.5)  # toujours en descente
    raw_alts = ltp_alt + (-np.tan(np.deg2rad(alpha_v)) * (distances_m + ou.dist_ap_m))
    raw_alts = np.maximum(raw_alts, ltp_alt + 1.0)

    # Enforce quasi-monotone descent : on autorise de petites remontees
    # mais on plafonne le taux de montee a une valeur physique realiste.
    # ~1.5 m/s ≈ 300 ft/min : limite haute d'une rafale verticale en approche.
    # Converti en par-frame via dt reel (depend de vitesse + fps).
    max_climb_rate_ms = 1.5
    max_descent_rate_ms = 5.0   # ~1000 ft/min : descente max realiste en approche
    max_climb_per_frame = max_climb_rate_ms * dt_frame
    max_descent_per_frame = max_descent_rate_ms * dt_frame
    for i in range(1, n_frames):
        if raw_alts[i] > raw_alts[i - 1] + max_climb_per_frame:
            raw_alts[i] = raw_alts[i - 1] + max_climb_per_frame
        elif raw_alts[i] < raw_alts[i - 1] - max_descent_per_frame:
            raw_alts[i] = raw_alts[i - 1] - max_descent_per_frame

    # --- Assemblage des poses (vectorise) ---
    # Position horizontale : projeter depuis LTP le long du back azimuth
    # (derriere le seuil, loin de la piste — convention LARD : LTP = C/D).
    # pyproj.Geod.fwd accepte des arrays -> 1 seul appel pour toutes les frames.
    g = pyproj.Geod(ellps='WGS84')
    azimuths = runway_back_azimuth_deg + ou.alpha_h_deg + alpha_h_dev
    ltp_lons = np.full(n_frames, ltp_lon)
    ltp_lats = np.full(n_frames, ltp_lat)
    lons, lats, _ = g.fwd(ltp_lons, ltp_lats, azimuths, distances_m, radians=False)

    # Camera regarde vers FPAP : runway_heading_deg = azimut LTP -> FPAP,
    # donc l'avion (qui arrive depuis l'arriere du seuil) regarde droit dans
    # l'axe piste. On y ajoute le crab (correction vent) et le yaw_dev (bruit).
    yaws    = runway_heading_deg + crab_angles + yaw_dev
    # Convention pitch stockee : 90 = level. xplane_bridge soustrait 90 a la lecture.
    pitches = 90.0 + ou.pitch_deg + pitch_dev
    rolls   = ou.roll_deg + roll_dev

    flight_data = list(zip(lons.tolist(), lats.tolist(), raw_alts.tolist(),
                           yaws.tolist(), pitches.tolist(), rolls.tolist()))

    return flight_data, distances_m, ltp_alt
