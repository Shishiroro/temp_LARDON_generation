"""
runway.py — utilitaires sur les noms de pistes (reciprocal, extraction depuis run_name).
"""

import re


def reciprocal_runway(rwy):
    """Retourne le reciprocal d'une piste (ex: 28L -> 10R, 09R -> 27L, 10 -> 28)."""
    m = re.match(r"^(\d{1,2})([LRC]?)$", str(rwy))
    if not m:
        return rwy
    num = int(m.group(1))
    suffix = m.group(2)
    recip_num = (num + 18) % 36 or 36
    recip_suffix = {"L": "R", "R": "L", "C": "C", "": ""}.get(suffix, suffix)
    return f"{recip_num:02d}{recip_suffix}"


def runway_from_run_name(run_name):
    """Extrait le runway du nom de run (ex: LFPG_09L_002 -> 09L, LFPO_24 -> 24).

    Retourne None si le nom n'est pas au format ICAO_RWY[_NNN].
    """
    m = re.match(r"^[A-Z]{4}_(.+?)(?:_\d{3})?$", str(run_name))
    return m.group(1) if m else None
