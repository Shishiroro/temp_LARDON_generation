"""
runway_utils.py — utilitaires sur les noms de pistes (reciprocal).
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
