import numpy as np


###################
### Box convert ###
###################

def box_extract(xs: np.ndarray, ys: np.ndarray):
    """Construit la bbox englobante (tlbr) a partir de coordonnees x/y dispersees.

    Utilise pour convertir les 4 coins piste du GT LARD en bbox 2D.

    Args:
        xs: tableau des coordonnees X (n points).
        ys: tableau des coordonnees Y (n points).

    Returns:
        np.ndarray [x_min, y_min, x_max, y_max] au format 'tlbr'.
    """
    x_min = float(xs.min())
    x_max = float(xs.max())
    y_min = float(ys.min())
    y_max = float(ys.max())
    
    return np.array([x_min, y_min, x_max, y_max])  # bbox format to 'tlbr' by default

def box_convert(box: np.ndarray, i_fmt: str, o_fmt: str):
    """
    Convert a box from one format to another.

    Args:
        box (np.ndarray): The input box to be converted.
        i_fmt (str): The input format of the box.
        o_fmt (str): The output format of the box.
    """
    ALLOWED_FORMATS = [
        "tlbr",  # top-left bottom-right
        "tlwh",  # top-left width height
        "xywh",  # center x y width height
    ]
    if i_fmt not in ALLOWED_FORMATS or o_fmt not in ALLOWED_FORMATS:
        raise ValueError(f"Unsupported conversion between {i_fmt} and {o_fmt} box formats.")
    if i_fmt == o_fmt:
        return box.copy()

    conversion = (i_fmt, o_fmt)

    if conversion == ("tlwh", "tlbr"):
        return _box_tlwh_to_tlbr(box)
    elif conversion == ("tlbr", "tlwh"):
        return _box_tlbr_to_tlwh(box)
    elif conversion == ("xywh", "tlbr"):
        return _box_xywh_to_tlbr(box)
    elif conversion == ("tlbr", "xywh"):
        return _box_tlbr_to_xywh(box)
    else:
        raise NotImplementedError(f"Conversion from {i_fmt} to {o_fmt} is not implemented yet.")
    

def _box_tlwh_to_tlbr(box: np.ndarray):
    """
    Converts a box from tlwh format to tlbr format.
    """
    x1, y1, w, h = box[..., :4].T  # Transpose to unpack the last dimension
    x2 = x1 + w
    y2 = y1 + h
    return np.stack([x1, y1, x2, y2], axis=-1)


def _box_tlbr_to_tlwh(box: np.ndarray):
    """
    Converts a box from tlbr format to tlwh format.
    """
    x1, y1, x2, y2 = box[..., :4].T  # Transpose to unpack the last dimension
    w = x2 - x1
    h = y2 - y1
    return np.stack([x1, y1, w, h], axis=-1)


def _box_xywh_to_tlbr(box: np.ndarray):
    """
    Converts a box from xywh format to tlbr format.
    """
    cx, cy, w, h = box[..., :4].T  # Transpose to unpack the last dimension
    x1 = cx - 0.5 * w
    y1 = cy - 0.5 * h
    x2 = cx + 0.5 * w
    y2 = cy + 0.5 * h
    return np.stack([x1, y1, x2, y2], axis=-1)


def _box_tlbr_to_xywh(box: np.ndarray):
    """
    Converts a box from tlbr format to xywh format.
    """
    x1, y1, x2, y2 = box[..., :4].T  # Transpose to unpack the last dimension
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    w = x2 - x1
    h = y2 - y1
    return np.stack([cx, cy, w, h], axis=-1)
