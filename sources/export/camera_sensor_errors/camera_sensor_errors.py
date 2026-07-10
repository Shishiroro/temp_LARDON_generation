"""
camera_sensor_errors.py — Simulation de fautes capteur camera embarquee avion.

Applique des degradations realistes a des images X-Plane avant inference YOLOv8,
pour evaluer la robustesse du modele.

Importe par sources/export/sensor_faults.py — pas de CLI standalone, le
pipeline (run_pipeline.py) pilote tout via les fault_profile.json TAF.

Usage:
    from camera_sensor_errors import apply_errors, ERROR_REGISTRY
    img_degraded = apply_errors(img, ["gaussian_noise", "vignetting"], severity=0.5)
"""

from typing import Optional, Callable

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Fonctions de degradation individuelles
# ---------------------------------------------------------------------------
# Chaque fonction prend (image BGR uint8, severity 0-1) et retourne image BGR uint8.

def gaussian_noise(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Bruit gaussien capteur (amplifie en basse lumiere)."""
    sigma = 5 + severity * 45  # 5-50
    noise = np.random.normal(0, sigma, img.shape).astype(np.float32)
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def shot_noise(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Shot noise (bruit photonique, plus visible dans les zones sombres)."""
    scale = max(0.05, 1.0 - severity * 0.95)  # 1.0 → 0.05
    noisy = np.random.poisson(img.astype(np.float32) * scale) / scale
    return np.clip(noisy, 0, 255).astype(np.uint8)


def salt_pepper(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Pixels morts / chauds (sel & poivre)."""
    ratio = 0.001 + severity * 0.029  # 0.1% - 3%
    out = img.copy()
    n_pixels = img.shape[0] * img.shape[1]
    n_salt = int(n_pixels * ratio / 2)
    n_pepper = int(n_pixels * ratio / 2)
    # Salt (pixels blancs)
    ys = np.random.randint(0, img.shape[0], n_salt)
    xs = np.random.randint(0, img.shape[1], n_salt)
    out[ys, xs] = 255
    # Pepper (pixels noirs)
    ys = np.random.randint(0, img.shape[0], n_pepper)
    xs = np.random.randint(0, img.shape[1], n_pepper)
    out[ys, xs] = 0
    return out


def dead_pixels(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Pixels morts persistants (clusters fixes, defaut capteur)."""
    n_clusters = int(1 + severity * 15)
    out = img.copy()
    h, w = img.shape[:2]
    for _ in range(n_clusters):
        cy, cx = np.random.randint(0, h), np.random.randint(0, w)
        r = np.random.randint(1, 4)
        y1, y2 = max(0, cy - r), min(h, cy + r + 1)
        x1, x2 = max(0, cx - r), min(w, cx + r + 1)
        # Pixel mort = noir ou bloque a une couleur
        color = 0 if np.random.random() < 0.7 else np.random.randint(200, 256)
        out[y1:y2, x1:x2] = color
    return out


def motion_blur(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Flou de bouge (vibrations structure avion)."""
    ksize = int(3 + severity * 22)  # 3 - 25 pixels
    if ksize % 2 == 0:
        ksize += 1
    # Direction aleatoire
    angle = np.random.uniform(0, 180)
    kernel = np.zeros((ksize, ksize), dtype=np.float32)
    center = ksize // 2
    cos_a, sin_a = np.cos(np.radians(angle)), np.sin(np.radians(angle))
    for i in range(ksize):
        offset = i - center
        x = int(round(center + offset * cos_a))
        y = int(round(center + offset * sin_a))
        if 0 <= x < ksize and 0 <= y < ksize:
            kernel[y, x] = 1.0
    kernel /= kernel.sum() + 1e-8
    return cv2.filter2D(img, -1, kernel)


def defocus_blur(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Flou de mise au point (defocus)."""
    ksize = int(3 + severity * 18)  # 3 - 21
    if ksize % 2 == 0:
        ksize += 1
    return cv2.GaussianBlur(img, (ksize, ksize), 0)


def glass_blur(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Flou type verre depoli : delegue a albumentations.GlassBlur (mode 'fast').

    Algorithme : permutations locales de pixels + flou gaussien (Hendrycks).
    Simule un objectif sale/gele/condense ou une vitre depolie.
    Mapping severity -> params albumentations (subtle -> medium -> strong) :
      sigma       : 0.4 -> 1.0   (defaut alb : 0.7)
      max_delta   : 2   -> 6     (defaut alb : 4, deplacement max en pixels)
      iterations  : 1   -> 3     (defaut alb : 2)
    """
    import logging
    logging.getLogger("albumentations").setLevel(logging.WARNING)
    import albumentations as A
    sigma = 0.4 + severity * 0.6
    max_delta = int(2 + severity * 4)
    iterations = int(1 + severity * 2)
    # albumentations attend RGB ; notre pipeline est BGR (cv2)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    transform = A.GlassBlur(
        sigma=sigma,
        max_delta=max_delta,
        iterations=iterations,
        mode="fast",
        p=1.0,
    )
    blurred_rgb = transform(image=rgb)["image"]
    return cv2.cvtColor(blurred_rgb, cv2.COLOR_RGB2BGR)


def overexposure(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Surexposition (soleil face, auto-exposure lag)."""
    factor = 1.3 + severity * 1.7  # 1.3 - 3.0
    return np.clip(img.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def underexposure(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Sous-exposition (ombre, transition luminosite rapide)."""
    factor = max(0.1, 0.7 - severity * 0.6)  # 0.7 → 0.1
    return np.clip(img.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def vignetting(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Vignettage optique (assombrissement bords)."""
    h, w = img.shape[:2]
    Y, X = np.ogrid[:h, :w]
    cy, cx = h / 2, w / 2
    max_dist = np.sqrt(cx**2 + cy**2)
    dist = np.sqrt((X - cx)**2 + (Y - cy)**2) / max_dist
    strength = 0.3 + severity * 0.6  # 0.3 - 0.9
    mask = 1.0 - strength * (dist ** 2)
    mask = np.clip(mask, 0, 1).astype(np.float32)
    return np.clip(img.astype(np.float32) * mask[..., None], 0, 255).astype(np.uint8)


def chromatic_aberration(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Aberration chromatique (decalage R/B aux bords)."""
    shift = int(1 + severity * 6)  # 1 - 7 pixels
    h, w = img.shape[:2]
    b, g, r = cv2.split(img)
    # Decaler R vers l'exterieur, B vers l'interieur
    M_r = np.float32([[1, 0, shift], [0, 1, shift]])
    M_b = np.float32([[1, 0, -shift], [0, 1, -shift]])
    r_shifted = cv2.warpAffine(r, M_r, (w, h), borderMode=cv2.BORDER_REFLECT)
    b_shifted = cv2.warpAffine(b, M_b, (w, h), borderMode=cv2.BORDER_REFLECT)
    return cv2.merge([b_shifted, g, r_shifted])


def radial_distortion(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Distorsion radiale (barrel/pincushion)."""
    h, w = img.shape[:2]
    k1 = (severity - 0.5) * 0.8  # -0.4 (pincushion) a +0.4 (barrel)
    fx = fy = max(w, h)
    cx_cam, cy_cam = w / 2, h / 2
    camera_matrix = np.array([[fx, 0, cx_cam], [0, fy, cy_cam], [0, 0, 1]], dtype=np.float32)
    dist_coeffs = np.array([k1, 0, 0, 0, 0], dtype=np.float32)
    return cv2.undistort(img, camera_matrix, dist_coeffs)


def lens_flare(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Lens flare simplifie (tache lumineuse)."""
    out = img.astype(np.float32)
    h, w = img.shape[:2]
    # Position aleatoire dans le tiers superieur (soleil)
    cx = np.random.randint(w // 4, 3 * w // 4)
    cy = np.random.randint(0, h // 3)
    radius = int((0.1 + severity * 0.3) * max(w, h))
    intensity = 100 + severity * 155
    Y, X = np.ogrid[:h, :w]
    dist = np.sqrt((X - cx)**2 + (Y - cy)**2).astype(np.float32)
    mask = np.clip(1.0 - dist / radius, 0, 1) ** 2
    out += mask[..., None] * intensity
    return np.clip(out, 0, 255).astype(np.uint8)


def banding(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Banding horizontal (interference electrique, readout noise)."""
    h, w = img.shape[:2]
    n_bands = int(5 + severity * 30)
    amplitude = 10 + severity * 40
    out = img.astype(np.float32)
    for _ in range(n_bands):
        y = np.random.randint(0, h)
        thickness = np.random.randint(1, 4)
        y2 = min(h, y + thickness)
        offset = np.random.uniform(-amplitude, amplitude)
        out[y:y2, :] += offset
    return np.clip(out, 0, 255).astype(np.uint8)


def rolling_shutter(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Effet rolling shutter (skew horizontal, vibrations haute freq)."""
    h, w = img.shape[:2]
    max_shift = int(2 + severity * 15)  # pixels de skew max
    out = np.zeros_like(img)
    # Sinusoide pour simuler vibration
    freq = np.random.uniform(1, 4)
    phase = np.random.uniform(0, 2 * np.pi)
    for y in range(h):
        shift = int(max_shift * np.sin(2 * np.pi * freq * y / h + phase))
        if shift > 0:
            out[y, shift:] = img[y, :w - shift]
            out[y, :shift] = img[y, 0:1]
        elif shift < 0:
            out[y, :w + shift] = img[y, -shift:]
            out[y, w + shift:] = img[y, -1:]
        else:
            out[y] = img[y]
    return out


def jpeg_artifacts(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Artefacts de compression JPEG (transmission video degradee)."""
    quality = int(max(5, 80 - severity * 75))  # 80 → 5
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    _, buf = cv2.imencode('.jpg', img, encode_param)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def color_shift(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Derive de balance des blancs / temperature couleur."""
    out = img.astype(np.float32)
    shifts = np.random.uniform(-30 * severity, 30 * severity, 3)
    out += shifts[None, None, :]
    return np.clip(out, 0, 255).astype(np.uint8)


def condensation(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Condensation / givre partiel sur objectif (flou non-uniforme + voile)."""
    h, w = img.shape[:2]
    # Voile semi-transparent
    haze = np.full_like(img, 220, dtype=np.float32)
    # Masque : plus opaque en peripherie (condensation bords)
    Y, X = np.ogrid[:h, :w]
    cy, cx = h / 2, w / 2
    max_dist = np.sqrt(cx**2 + cy**2)
    dist = np.sqrt((X - cx)**2 + (Y - cy)**2) / max_dist
    alpha = severity * 0.7 * np.clip(dist * 1.5, 0, 1)
    # Flou local
    ksize = int(5 + severity * 20)
    if ksize % 2 == 0:
        ksize += 1
    blurred = cv2.GaussianBlur(img, (ksize, ksize), 0).astype(np.float32)
    out = img.astype(np.float32)
    a = alpha[..., None]
    out = out * (1 - a) + (blurred * 0.5 + haze * 0.5) * a
    return np.clip(out, 0, 255).astype(np.uint8)


def dirt_on_lens(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Salete / gouttes sur objectif (taches floues localisees)."""
    out = img.copy()
    h, w = img.shape[:2]
    n_spots = int(3 + severity * 15)
    for _ in range(n_spots):
        cy = np.random.randint(0, h)
        cx = np.random.randint(0, w)
        radius = int(10 + severity * 40 * np.random.uniform(0.5, 1.5))
        # Extraire ROI
        y1, y2 = max(0, cy - radius), min(h, cy + radius)
        x1, x2 = max(0, cx - radius), min(w, cx + radius)
        roi = out[y1:y2, x1:x2]
        if roi.size == 0:
            continue
        ksize = max(3, radius // 2)
        if ksize % 2 == 0:
            ksize += 1
        blurred_roi = cv2.GaussianBlur(roi, (ksize, ksize), 0)
        # Masque circulaire avec bords doux
        ry, rx = y2 - y1, x2 - x1
        Y_roi, X_roi = np.ogrid[:ry, :rx]
        center_y, center_x = (cy - y1), (cx - x1)
        d = np.sqrt((X_roi - center_x)**2 + (Y_roi - center_y)**2).astype(np.float32)
        mask = np.clip(1.0 - d / radius, 0, 1)[..., None]
        # Teinte brunâtre pour la salete
        tint = np.array([[[140, 160, 170]]], dtype=np.float32)
        dirty = blurred_roi.astype(np.float32) * 0.6 + tint * 0.4
        out[y1:y2, x1:x2] = np.clip(
            roi.astype(np.float32) * (1 - mask * 0.7) + dirty * mask * 0.7,
            0, 255
        ).astype(np.uint8)
    return out


def droplets(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Gouttelettes / eclaboussures sur la lentille (albumentations.Spatter, mode 'rain').

    Seul gauss_sigma depend de severity (0 -> 2.0, 1 -> 5.0, plage du site
    albumentations). Tous les autres parametres restent aux defauts du site.
    """
    import logging
    logging.getLogger("albumentations").setLevel(logging.WARNING)
    import albumentations as A
    gauss_sigma = 2.0 + severity * 3.0  # 2.0 -> 5.0
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    transform = A.Spatter(
        mean=(0.65, 0.65),
        std=(0.3, 0.3),
        gauss_sigma=(gauss_sigma, gauss_sigma),
        cutout_threshold=(0.68, 0.68),
        intensity=(0.6, 0.6),
        mode="rain",
        color=None,
        p=1.0,
    )
    spattered_rgb = transform(image=rgb)["image"]
    return cv2.cvtColor(spattered_rgb, cv2.COLOR_RGB2BGR)


def snow(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Neige : delegue a albumentations.RandomSnow (methode bleach).

    Algorithme HLS threshold + brighten, code de reference upstream.
    severity : 0 = leger (seuil eleve, peu de pixels touches)
               1 = lourd (seuil bas, blanchiment massif)
    Mapping severity -> params albumentations :
      snow_point        : 0.30 -> 0.05  (plage [0.05, 0.30])
      brightness_coeff  : 2.0  -> 3.0   (plage [2, 3])
    """
    import logging
    logging.getLogger("albumentations").setLevel(logging.WARNING)
    import albumentations as A
    snow_point = 0.30 - severity * 0.25
    brightness_coeff = 2.0 + severity * 1.0
    # albumentations attend RGB ; notre pipeline est BGR (cv2)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    transform = A.RandomSnow(
        snow_point_range=(snow_point, snow_point),
        brightness_coeff=brightness_coeff,
        p=1.0,
    )
    snowy_rgb = transform(image=rgb)["image"]
    return cv2.cvtColor(snowy_rgb, cv2.COLOR_RGB2BGR)


def fog(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Brouillard (Hendrycks). Voile blanc progressif, perte de contraste."""
    h, w = img.shape[:2]
    out = img.astype(np.float32)
    # Intensite du voile
    alpha = 0.2 + severity * 0.6  # 0.2 - 0.8
    # Gradient vertical (plus dense en bas = plus loin sur la piste)
    gradient = np.linspace(0.3, 1.0, h).reshape(h, 1, 1).astype(np.float32)
    fog_layer = np.full_like(out, 240)  # blanc legerement gris
    out = out * (1 - alpha * gradient) + fog_layer * alpha * gradient
    return np.clip(out, 0, 255).astype(np.uint8)


def zoom_blur(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Flou radial / zoom (Hendrycks). Simule l'avancement rapide vers la piste."""
    h, w = img.shape[:2]
    n_steps = int(3 + severity * 12)  # 3 - 15 etapes de zoom
    zoom_factor = 1.0 + severity * 0.15  # 1.0 - 1.15
    out = np.zeros_like(img, dtype=np.float32)
    cx, cy = w / 2, h / 2
    for i in range(n_steps):
        t = i / max(n_steps - 1, 1)
        scale = 1.0 + (zoom_factor - 1.0) * t
        M = np.float32([
            [scale, 0, cx * (1 - scale)],
            [0, scale, cy * (1 - scale)]
        ])
        zoomed = cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REFLECT)
        out += zoomed.astype(np.float32)
    out /= n_steps
    return np.clip(out, 0, 255).astype(np.uint8)


def contrast(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Perte/exces de contraste (Hendrycks). Haze atmospherique ou reglage auto."""
    # severity 0 = contraste normal, 0.5 = reduit, 1.0 = tres reduit
    factor = max(0.1, 1.0 - severity * 0.85)  # 1.0 → 0.15
    mean = img.astype(np.float32).mean()
    out = mean + (img.astype(np.float32) - mean) * factor
    return np.clip(out, 0, 255).astype(np.uint8)


def pixelate(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    """Pixelisation (Hendrycks). Transmission video basse resolution."""
    h, w = img.shape[:2]
    # Facteur de reduction : 0.5 (leger) a 0.05 (extreme)
    scale = max(0.05, 0.5 - severity * 0.45)
    small_w, small_h = max(1, int(w * scale)), max(1, int(h * scale))
    small = cv2.resize(img, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


def channel_swap(img: np.ndarray, severity: float = 0.5,
                 order: Optional[list] = None) -> np.ndarray:
    """Permutation des canaux couleur (bug demosaicing / cable RGB inverse).

    severity < 0.5 : permutation dans l'espace BGR natif.
    severity >= 0.5 : permutation dans l'espace HSV (decalage de teinte plus violent).
    `order` : permutation explicite [c0, c1, c2] (ex: [0, 2, 1]). Si None, on utilise
    une permutation fixe (inversion BGR<->RGB). PAS de tirage aleatoire : le swap doit
    rester constant sur toute la duree de l'erreur (sinon il change frame a frame, la
    fonction etant appelee independamment par image). La variete entre scenarios vient
    du sampling TAF de c0/c1/c2 (cf. Export.py), pas d'un hasard par frame.
    """
    if order is None:
        order = [2, 1, 0]
    if severity < 0.5:
        return img[:, :, list(order)].copy()
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    swapped_hsv = hsv[:, :, list(order)]
    return cv2.cvtColor(swapped_hsv, cv2.COLOR_HSV2BGR)


# ---------------------------------------------------------------------------
# Registre des erreurs
# ---------------------------------------------------------------------------

ERROR_REGISTRY: dict[str, Callable] = {
    # Bruit
    "gaussian_noise": gaussian_noise,
    "shot_noise": shot_noise,
    "salt_pepper": salt_pepper,
    "dead_pixels": dead_pixels,
    # Flou
    "motion_blur": motion_blur,
    "defocus_blur": defocus_blur,
    "glass_blur": glass_blur,
    "rolling_shutter": rolling_shutter,
    # Exposition
    "overexposure": overexposure,
    "underexposure": underexposure,
    # Optique
    "vignetting": vignetting,
    "chromatic_aberration": chromatic_aberration,
    "radial_distortion": radial_distortion,
    "lens_flare": lens_flare,
    # Defauts capteur
    "banding": banding,
    # Compression
    "jpeg_artifacts": jpeg_artifacts,
    # Couleur
    "color_shift": color_shift,
    "channel_swap": channel_swap,
    # Environnement
    "condensation": condensation,
    "dirt_on_lens": dirt_on_lens,
    "droplets": droplets,
    # Atmospherique / transmission
    "fog": fog,
    "snow": snow,
    "zoom_blur": zoom_blur,
    "contrast": contrast,
    "pixelate": pixelate,
}


# ---------------------------------------------------------------------------
# API principale
# ---------------------------------------------------------------------------

def apply_errors(
    img: np.ndarray,
    errors: list[str],
    severity: float = 0.5,
    severities: Optional[dict[str, float]] = None,
    extras: Optional[dict[str, dict]] = None,
) -> np.ndarray:
    """Applique une liste d'erreurs a une image.

    Args:
        img: Image BGR uint8.
        errors: Liste de noms d'erreurs (cles de ERROR_REGISTRY).
        severity: Severite globale par defaut (0-1).
        severities: Dict optionnel {nom_erreur: severite} pour override par erreur.
        extras: Dict optionnel {nom_erreur: {kwarg: value}} pour params custom
                (ex: {"channel_swap": {"order": [0, 2, 1]}}). Les kwargs non
                supportes par la fonction cible sont silencieusement ignores.

    Returns:
        Image degradee BGR uint8.
    """
    import inspect
    result = img.copy()
    for name in errors:
        if name not in ERROR_REGISTRY:
            print(f"[WARN] Erreur inconnue ignoree: {name}")
            continue
        s = severity
        if severities and name in severities:
            s = severities[name]
        fn = ERROR_REGISTRY[name]
        kwargs = {}
        if extras and name in extras:
            sig_params = inspect.signature(fn).parameters
            kwargs = {k: v for k, v in extras[name].items() if k in sig_params}
        result = fn(result, s, **kwargs)
    return result
