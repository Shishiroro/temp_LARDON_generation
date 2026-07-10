"""
sensor_faults.py — Fautes capteur camera (post-traitement OpenCV)
=================================================================
Gere la configuration, validation et application des fautes capteur
hardware (bruit, flou, exposition, optique, artefacts).

Chaque faute a :
  - fault_type : type de degradation (gaussian_noise, dead_pixels, etc.)
  - severity   : degre de severite [0.0, 1.0]
  - from_pct   : debut d'application en % de la trajectoire [0, 100]
  - to_pct     : fin d'application en % de la trajectoire [0, 100]
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


# 26 types de fautes capteur hardware
KNOWN_FAULT_TYPES = {
    "gaussian_noise", "shot_noise", "salt_pepper", "dead_pixels",
    "motion_blur", "defocus_blur", "glass_blur", "rolling_shutter",
    "overexposure", "underexposure",
    "vignetting", "chromatic_aberration", "radial_distortion", "lens_flare",
    "banding", "jpeg_artifacts", "color_shift", "channel_swap",
    "condensation", "dirt_on_lens", "droplets",
    "fog", "snow", "zoom_blur", "contrast", "pixelate",
}


@dataclass
class FaultConfig:
    """Configuration d'une faute capteur (generee par TAF).

    `extra` : kwargs additionnels passes a la fonction OpenCV (ex: channel_swap
    recoit `order=[c0, c1, c2]`). Vide pour les fautes "standards" qui ne
    necessitent que severity.
    """
    fault_type: str
    severity: float
    from_pct: float
    to_pct: float
    extra: Optional[dict[str, Any]] = field(default_factory=dict)


def validate_faults(faults):
    """Valide les contraintes sur les fautes capteur."""
    for i, f in enumerate(faults):
        prefix = f"[Fault {i}] ({f.fault_type})"
        if f.fault_type not in KNOWN_FAULT_TYPES:
            raise ValueError(
                f"{prefix} Type inconnu '{f.fault_type}'. "
                f"Types disponibles : {sorted(KNOWN_FAULT_TYPES)}"
            )
        if not 0.0 <= f.severity <= 1.0:
            raise ValueError(f"{prefix} severity={f.severity} hors [0.0, 1.0]")
        if not 0.0 <= f.from_pct <= 100.0:
            raise ValueError(f"{prefix} from_pct={f.from_pct} hors [0, 100]")
        if not 0.0 <= f.to_pct <= 100.0:
            raise ValueError(f"{prefix} to_pct={f.to_pct} hors [0, 100]")
        if f.from_pct >= f.to_pct:
            raise ValueError(
                f"{prefix} from_pct={f.from_pct} >= to_pct={f.to_pct}"
            )


def compute_frame_faults(faults, n_frames):
    """Calcule les fautes actives par frame.

    frame 0 = 0% (debut approche, loin), frame N-1 = 100% (pres de la piste).
    """
    if not faults or n_frames <= 0:
        return [[] for _ in range(max(n_frames, 0))]
    per_frame = []
    for i in range(n_frames):
        pct = (i / max(n_frames - 1, 1)) * 100.0
        active = [(f.fault_type, f.severity, f.extra or {})
                  for f in faults if f.from_pct <= pct <= f.to_pct]
        per_frame.append(active)
    return per_frame


def save_fault_profile(faults, n_frames, output_path):
    """Sauvegarde le profil de fautes dans un fichier JSON."""
    output_path = Path(output_path)
    per_frame = compute_frame_faults(faults, n_frames)
    per_frame_summary = {}
    for i, active in enumerate(per_frame):
        if active:
            per_frame_summary[str(i)] = [
                {"type": ft, "severity": sev, **({"extra": ex} if ex else {})}
                for ft, sev, ex in active
            ]
    profile = {
        "faults": [asdict(f) for f in faults],
        "n_frames": n_frames,
        "per_frame_summary": per_frame_summary,
        "n_frames_affected": len(per_frame_summary),
    }
    with open(output_path, "w") as f:
        json.dump(profile, f, indent=2)
    print(f"  .json faults -> {output_path}")
    return str(output_path)


def load_fault_profile(profile_path):
    """Charge un profil de fautes depuis un fichier JSON."""
    with open(profile_path, "r") as f:
        profile = json.load(f)
    faults = [FaultConfig(**fc) for fc in profile["faults"]]
    return faults, profile["n_frames"]


def apply_faults_to_directory(input_dir, output_dir, faults, n_frames):
    """Applique les fautes capteur aux images d'un dossier."""
    import cv2
    import sys

    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # camera_sensor_errors/ est cote usine (sous-dossier de export/)
    cse_dir = str(Path(__file__).resolve().parent / "camera_sensor_errors")
    if cse_dir not in sys.path:
        sys.path.insert(0, cse_dir)

    from camera_sensor_errors import apply_errors

    extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff'}
    images = sorted(f for f in input_dir.iterdir() if f.suffix.lower() in extensions)

    if not images:
        print(f"  [FAULTS] Aucune image dans {input_dir}")
        return 0

    per_frame = compute_frame_faults(faults, len(images))

    count = 0
    count_affected = 0
    for i, img_path in enumerate(images):
        img = cv2.imread(str(img_path))
        if img is None:
            continue
        active = per_frame[i] if i < len(per_frame) else []
        if active:
            error_names = [ft for ft, _, _ in active]
            severities = {ft: sev for ft, sev, _ in active}
            extras = {ft: ex for ft, _, ex in active if ex}
            img = apply_errors(img, error_names, severities=severities, extras=extras)
            count_affected += 1
        cv2.imwrite(str(output_dir / img_path.name), img)
        count += 1

    print(f"  [FAULTS] {count} images traitees, {count_affected} degradees -> {output_dir}")
    return count


def apply_faults(run_dir):
    """Applique les fautes capteur a un run si fault_profile.json est present.

    Lit run_dir/fault_profile.json, applique les fautes aux images de footage/
    et ecrit dans degraded/. Skip si degraded/ existe deja avec des images.

    :return: dossier d'images a utiliser pour YOLO (degraded/ si fautes appliquees,
             footage/ sinon)
    """
    from runs import list_images

    run_dir = Path(run_dir)
    fault_json = run_dir / "fault_profile.json"
    footage_dir = run_dir / "footage"
    degraded_dir = run_dir / "degraded"

    if not fault_json.exists():
        return footage_dir

    if list_images(degraded_dir):
        print(f"  [FAULTS] degraded/ existe deja, skip application")
        return degraded_dir

    print(f"\n  [FAULTS] Application des fautes capteur ({run_dir.name})...")

    faults, n_frames = load_fault_profile(fault_json)
    if not faults:
        print(f"  [FAULTS] Aucune faute dans le profil, skip")
        return footage_dir

    fault_str = ", ".join(f"{f.fault_type}({f.severity:.2f})" for f in faults)
    print(f"  [FAULTS] Fautes : {fault_str}")

    apply_faults_to_directory(footage_dir, degraded_dir, faults, n_frames)
    return degraded_dir
