from typing import Tuple
import torch


###########################################################
# Box conversions
###########################################################

def _box_xywh_to_xyxy(box: torch.Tensor):
    """
    Converts a box from xywh format to xyxy format.
    https://github.com/pytorch/vision/blob/main/torchvision/ops/_box_convert.py#L50
    """
    x1, y1, w, h = box.unbind(-1)
    x2 = x1 + w
    y2 = y1 + h
    return torch.stack([x1, y1, x2, y2], dim=-1)


def _box_xyxy_to_xywh(box: torch.Tensor):
    """
    Converts a box from xyxy format to xywh format.
    https://github.com/pytorch/vision/blob/main/torchvision/ops/_box_convert.py#L66
    """
    x1, y1, x2, y2  = box.unbind(-1)
    w = x2 - x1
    h = y2 - y1
    return torch.stack([x1, y1, w, h], dim=-1)


def _box_cxcywh_to_xyxy(box: torch.Tensor):
    """
    Converts a box from cxcywh format to xyxy format.
    https://github.com/pytorch/vision/blob/main/torchvision/ops/_box_convert.py#L5
    """
    cx, cy, w, h = box.unbind(-1)
    x1 = cx - 0.5 * w
    y1 = cy - 0.5 * h
    x2 = cx + 0.5 * w
    y2 = cy + 0.5 * h
    return torch.stack([x1, y1, x2, y2], dim=-1)


def _box_xyxy_to_cxcywh(box: torch.Tensor):
    """
    Converts a box from xyxy format to cxcywh format.
    https://github.com/pytorch/vision/blob/main/torchvision/ops/_box_convert.py#L28
    """
    x1, y1, x2, y2 = box.unbind(-1)
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    w = x2 - x1
    h = y2 - y1
    return torch.stack([cx, cy, w, h], dim=-1)


def bbox_convert(box: torch.Tensor, i_fmt: str, o_fmt: str):
    """
    Convert a box from one format to another.

    https://github.com/pytorch/vision/blob/main/torchvision/ops/boxes.py#L185

    Args:
        box (torch.Tensor): The input box to be converted.
        i_fmt (str): The input format of the box.
        o_fmt (str): The output format of the box.
    """
    ALLOWED_FORMATS = [
        "xyxy",    # top-left bottom-right
        "xywh",    # top-left width height (xywh or commonly xywh)
        "cxcywh",  # center x y width height (cxcywh or commonly cycywh)
    ]
    if i_fmt not in ALLOWED_FORMATS or o_fmt not in ALLOWED_FORMATS:
        raise ValueError(f"Unsupported conversion between {i_fmt} and {o_fmt} box formats.")
    if i_fmt == o_fmt:
        return box.clone()
    
    conversion = (i_fmt, o_fmt)

    if conversion == ("xywh", "xyxy"):
        return _box_xywh_to_xyxy(box)
    elif conversion == ("xyxy", "xywh"):
        return _box_xyxy_to_xywh(box)
    elif conversion == ("cxcywh", "xyxy"):
        return _box_cxcywh_to_xyxy(box)
    elif conversion == ("xyxy", "cxcywh"):
        return _box_xyxy_to_cxcywh(box)
    else:
        raise NotImplementedError(f"Conversion from {i_fmt} to {o_fmt} is not implemented yet.")
    

###########################################################
# Box calculations
###########################################################

def _box_area(box: torch.Tensor) -> torch.Tensor:
    """
    Based on https://github.com/pytorch/vision/blob/main/torchvision/ops/boxes.py#L273

    Args:
        box (torch.Tensor): [B, 4] tensor batch of boxes.

    Note:
        Both sets of boxes are expected to be in (x1, y1, x2, y2) format.
    """
    return (box[:, 2] - box[:, 0]) * (box[:, 3] - box[:, 1])


def _box_inter_union(box1: torch.Tensor, box2: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Based on https://github.com/pytorch/vision/blob/main/torchvision/ops/boxes.py#L294

    Args:
        box1 (torch.Tensor): First batch of boxes.
        box2 (torch.Tensor): Second batch of boxes.

    Note:
        Both sets of boxes are expected to be in (x1, y1, x2, y2) format.

    Returns:
        (Tuple[torch.Tensor, torch.Tensor]) Batch of intersection and union of boxes.
    """
    area1 = _box_area(box1)
    area2 = _box_area(box2)

    lt = torch.max(box1[:, None, :2], box2[:, :2])  # [N,M,2]
    rb = torch.min(box1[:, None, 2:], box2[:, 2:])  # [N,M,2]
    wh = (rb - lt).clamp(min=0)  # [N,M,2]

    inter = wh[:, :, 0] * wh[:, :, 1]       # [N,M]
    union = area1[:, None] + area2 - inter  # [N,M]

    return inter, union


def _box_iou(box1: torch.Tensor, box2: torch.Tensor, eps=1e-7) -> torch.Tensor:
    """
    Based on (TODO)

    Args:
        box1 (torch.Tensor): [N, 4] First batch of boxes.
        box2 (torch.Tensor): [M, 4] Second batch of boxes.
        eps  (float): Small float value to avoid dividing by zero.

    Note:
        Both sets of boxes are expected to be in (x1, y1, x2, y2) format (or xyxy).
    """
    inter, union = _box_inter_union(box1, box2)
    return inter / (union + eps)


def _box_giou(box1: torch.Tensor, box2: torch.Tensor, eps=1e-7) -> torch.Tensor:
    """
    Based on https://github.com/pytorch/vision/blob/main/torchvision/ops/boxes.py#L331

    Args:
        box1 (torch.Tensor): [N, 4] First batch of boxes.
        box2 (torch.Tensor): [M, 4] Second batch of boxes.
        eps  (float): Small float value to avoid dividing by zero.

    Note:
        Both sets of boxes are expected to be in (x1, y1, x2, y2) format (xyxy).
    """
    inter, union = _box_inter_union(box1, box2)

    iou = inter / (union + eps)
    lti = torch.min(box1[:, None, :2], box2[:, :2])  # [N,M,2]
    rbi = torch.max(box1[:, None, 2:], box2[:, 2:])  # [N,M,2]
    whi = (rbi - lti).clamp(min=0)  # [N,M,2]
    areai = whi[:, :, 0] * whi[:, :, 1]  # [N,M]

    return iou - (areai - union) / (areai + eps)


def _box_diou(box1: torch.Tensor, box2: torch.Tensor, eps=1e-7) -> torch.Tensor:
    """
    Based on https://github.com/pytorch/vision/blob/main/torchvision/ops/boxes.py#L419

    Args:
        box1 (torch.Tensor): [N, 4] First batch of boxes.
        box2 (torch.Tensor): [M, 4] Second batch of boxes.
        eps  (float): Small float value to avoid dividing by zero.

    Note:
        Both sets of boxes are expected to be in (x1, y1, x2, y2) format (xyxy).
    """
    iou = _box_iou(box1, box2, eps=eps)
    lti = torch.min(box1[:, None, :2], box2[:, :2])  # [N,M,2]
    rbi = torch.max(box1[:, None, 2:], box2[:, 2:])  # [N,M,2]
    whi = (rbi - lti).clamp(min=0)  # [N,M,2]

    diagonal_distance_sqr = (whi[:, :, 0]**2 + whi[:, :, 1]**2 + eps)  # [N,M]

    x_c1 = (box1[:, 0] + box1[:, 2]) / 2
    y_c1 = (box1[:, 1] + box1[:, 3]) / 2
    x_c2 = (box2[:, 0] + box2[:, 2]) / 2
    y_c2 = (box2[:, 1] + box2[:, 3]) / 2
    box_cntr_distance_sqr = (x_c1[:, None] - x_c2[None, :])**2 + (y_c1[:, None] - y_c2[None, :])**2  # [N,M]

    return iou - (box_cntr_distance_sqr / diagonal_distance_sqr)


def _box_ciou(box1: torch.Tensor, box2: torch.Tensor, eps=1e-7) -> torch.Tensor:
    """
    Based on https://github.com/pytorch/vision/blob/main/torchvision/ops/boxes.py#L361

    Args:
        box1 (torch.Tensor): [N, 4] First batch of boxes.
        box2 (torch.Tensor): [M, 4] Second batch of boxes.
        eps  (float): Small float value to avoid dividing by zero.

    Note:
        Both sets of boxes are expected to be in (x1, y1, x2, y2) format (xyxy).
    """
    diou = _box_diou(box1, box2, eps=eps)
    iou  = _box_iou(box1, box2, eps=eps)

    w1 = box1[:, None, 2] - box1[:, None, 0]
    h1 = box1[:, None, 3] - box1[:, None, 1]
    w2 = box2[:, 2] - box2[:, 0]
    h2 = box2[:, 3] - box2[:, 1]

    v = (4 / (torch.pi**2)) * torch.pow(torch.atan(w1 / h1) - torch.atan(w2 / h2), 2)
    with torch.no_grad():
        alpha = v / (1 - iou + v + eps)
    return diou - alpha * v


def bbox_iou(box1: torch.Tensor, box2: torch.Tensor, boxfmt: str = "xyxy", method: str = "CIOU", eps=1e-7):
    """
    Calculate the Intersection over Union (IoU) of two bounding boxes.

    Args:
        box1 (torch.Tensor): A tensor of shape [N, 4] representing one or more bounding boxes.
        box2 (torch.Tensor): A tensor of shape [M, 4] representing one or more bounding boxes.
        boxfmt (str): The format of the bounding boxes, among {"xyxy", "xywh", "cxcywh"}.
        method (str): The IOU method to use, among {"IOU", "GIOU", "DIOU", "CIOU"}.
        eps (float): A small value to avoid division by zero.

    Note:
        "xyxy"  : top-left bottom-right
        "xywh"  : top-left width height
        "cxcywh": center x y width height

    Returns:
        (torch.Tensor) A tensor of shape [N, M] representing the IoU values.
    """
    if boxfmt not in ["xyxy", "xywh", "cxcywh", "xywhr", "cxcywhr"]:
        raise ValueError(f"Unsupported box format: {boxfmt}")
    if method not in ["IOU", "GIOU", "DIOU", "CIOU"]:
        raise ValueError(f"Unsupported IOU method: {method}")
    
    if len(box1.shape) == 1:
        box1 = box1.unsqueeze(0)
    if len(box2.shape) == 1:
        box2 = box2.unsqueeze(0)
    
    if boxfmt != "xyxy":
        box1 = bbox_convert(box1, boxfmt, "xyxy")
        box2 = bbox_convert(box2, boxfmt, "xyxy")
    
    if method == "IOU":
        iou = _box_iou(box1, box2, eps=eps)
    elif method == "GIOU":
        iou = _box_giou(box1, box2, eps=eps)
    elif method == "DIOU":
        iou = _box_diou(box1, box2, eps=eps)
    elif method == "CIOU":
        iou = _box_ciou(box1, box2, eps=eps)
    else:
        raise ValueError(f"Unsupported IOU method: {method}")

    return iou