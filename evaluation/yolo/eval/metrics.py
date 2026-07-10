"""
Inspired from https://github.com/ultralytics/ultralytics/blob/main/ultralytics/utils/metrics.py
"""


from pathlib import Path
from typing import Union, Tuple, List, Dict, Any
import numpy as np
import torch
import matplotlib.pyplot as plt

from .metrics_utils import bbox_convert, bbox_iou
   

###########################################################
# Eval functions for Object Detection
###########################################################

def compute_metrics(
        y_pred: Union[torch.Tensor, np.ndarray], 
        y_true: Union[torch.Tensor, np.ndarray], 
        iou_thresh: float = 0.5, 
        iou_method: str = "CIOU", 
        box_format: str = "xyxy",
        t_conf = None,
        return_lst: bool = True,
        return_agg: bool = True,
) -> Any:
    """
    Function to evaluate an ML model.

    Args:
        y_pred (Union[torch.Tensor, np.ndarray]): The prediction boxes   [image_id, class_id, x1, y1, x2, y2, score]
        y_true (Union[torch.Tensor, np.ndarray]): The ground truth boxes [image_id, class_id, x1, y1, x2, y2]
        iou_thresh (Union[np.ndarray, List[float], float]): IOU threshold or list-like of IOU thresholds.
        iou_method (str): Method to use for IOU computation (IOU/GIOU/DIOU/CIOU).
        box_format (str): Format of the boxes (xyxy/xywh/cxcywh).
        t_conf (float | None): Confidence threshold at which to evaluate P, R.
                               If None, the metrics at max F1 score are returned.
        return_lst (bool): If True, cast tensor metrics to plain Python lists.
        return_agg (bool): If True, average per-class metrics into a single scalar.
    """
    # Compute binary matching of prediction and ground truths at iou_thresh
    tpfp, _ = match_predictions(y_pred, y_true, iou_thresh=iou_thresh, iou_method=iou_method, box_format=box_format)
    tp = np.where(tpfp != -1, 1, 0) if tpfp is not None else None

    # Compute metrics
    metrics = compute_ap_per_cls(tp=tp, y_pred=y_pred, y_true=y_true, t_conf=t_conf, return_curves=False)
  
    # Compute metrics averaged over classes if required
    if return_agg:
        metrics = {k: v.mean() for k, v in metrics.items()}

    if return_lst:
        metrics = {k: v.tolist() for k, v in metrics.items()}

    return metrics


def match_predictions(box_p: torch.Tensor, box_g: torch.Tensor, iou_thresh: float = 0.5, iou_method: str = "CIOU", box_format: str = "xyxy"):
    """
    Match predictions to ground truth boxes based on IoU.
    https://medium.com/data-science-at-microsoft/error-analysis-for-object-detection-models-338cb6534051

    Args:
        box_p (torch.Tensor, [N, 7]): The predictions 'boxes'   [image_id, class_id, x1, y1, x2, y2, score]
        box_g (torch.Tensor, [M, 6]): The ground truths 'boxes' [image_id, class_id, x1, y1, x2, y2]
        iou_thresh (float): IoU threshold for matching.
        iou_method (str): IoU variant (IOU/GIOU/DIOU/CIOU).
        box_format (str): Box format (xyxy/xywh/cxcywh).

    Note:
        The boxes are expected to be in the 'xyxy' format.

    Returns:
        (torch.Tensor, [N,]): A tensor of integers values: -1 if no match were found, {0, ..., M} as the index of the matched ground truth (in the same image)
        (torch.Tensor, [M,]): A tensor of integers values: -1 if no match were found, {0, ..., N} as the index of the matched prediction (in the same image)
    """
    if len(box_p) == 0 and len(box_g) == 0:
        return None, None
    if len(box_p) == 0:
        return torch.full((0,), -1, dtype=torch.int16), torch.full((box_g.shape[0],), -1, dtype=torch.int16)
    if len(box_g) == 0:
        return torch.full((box_p.shape[0],), -1, dtype=torch.int16), torch.full((0,), -1, dtype=torch.int16)
    
    if box_p.shape[1] != 7:
        raise ValueError(f"box_p should have 7 columns, but got {box_p.shape[1]}")
    if box_g.shape[1] != 6:
        raise ValueError(f"box_g should have 6 columns, but got {box_g.shape[1]}")
    
    p_matches = torch.full((box_p.shape[0],), -1, dtype=torch.int16)
    g_matches = torch.full((box_g.shape[0],), -1, dtype=torch.int16)

    for image_id in box_p[:, 0].unique():
        p_mask = (box_p[:, 0] == image_id)
        g_mask = (box_g[:, 0] == image_id)

        p_indx = p_mask.nonzero(as_tuple=True)[0]
        g_indx = g_mask.nonzero(as_tuple=True)[0]

        p_sset = box_p[p_mask]
        g_sset = box_g[g_mask]

        if p_sset.shape[0] == 0 or g_sset.shape[0] == 0:
            continue

        iou = bbox_iou(p_sset[:, 2:6], g_sset[:, 2:6], boxfmt=box_format, method=iou_method)
        class_match = (p_sset[:, 1].unsqueeze(1) == g_sset[:, 1])
        iou *= class_match  # In single-class OD, this does nothing

        matches_indx = (iou >= iou_thresh).nonzero(as_tuple=False)
        if matches_indx.shape[0]:
            ious = iou[matches_indx[:, 0], matches_indx[:, 1]]
            _, sort_indx = torch.sort(ious, descending=True)
            matches_indx = matches_indx[sort_indx]

            matched_p = set()
            matched_g = set()

            for p_i, g_i in matches_indx:
                abs_p_i = p_indx[p_i]
                abs_g_i = g_indx[g_i]

                if abs_p_i.item() not in matched_p and abs_g_i.item() not in matched_g:
                    p_matches[abs_p_i] = abs_g_i
                    g_matches[abs_g_i] = abs_p_i

                    matched_p.add(abs_p_i.item())
                    matched_g.add(abs_g_i.item())
    
    return p_matches, g_matches


def compute_ap(r_curve: Union[np.ndarray, List[float]], p_curve: Union[np.ndarray, List[float]]):
    """
    Compute the average precision (AP) given the recall and precision curves.
    
    Args:
        r_curve (Union[np.ndarray, List[float]]): The recall curve
        p_curve (Union[np.ndarray, List[float]]): The precision curve

    Note:
        Code taken from (https://github.com/ultralytics/ultralytics/blob/main/ultralytics/utils/metrics.py#L708)
        
    Returns:
        ap (float): The average precision
        mr (np.ndarray): The recall envelope curve
        mp (np.ndarray): The precision envelope curve
    """
    # Append sentinel values to beginning and end
    mr = np.concatenate(([0.0], r_curve, [1.0]))
    mp = np.concatenate(([1.0], p_curve, [0.0]))

    # Compute the precision envelope
    mp = np.flip(np.maximum.accumulate(np.flip(mp)))

    # Compute the area under curve via interpolation
    xx = np.linspace(0, 1, 100)  # 101 point interpolation as in COCO
    _trapz = getattr(np, 'trapezoid', np.trapz)
    ap = _trapz(np.interp(xx, mr, mp), xx)

    return ap, mr, mp


def compute_ap_per_cls(
        tp: np.ndarray,
        y_pred: torch.Tensor,
        y_true: torch.Tensor,
        t_conf: Union[np.ndarray, List[float], float] = None,
        eps: float = 1e-10,
        return_curves: bool = False,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute the average precision (AP) per class for given TP array.

    Args:
        tp (np.ndarray): [N,] Binary array (1=correct, 0=incorrect), aligned with y_pred.
        y_pred (torch.Tensor): [N, 7] The predictions   (img_id, cls_id, x1, y1, x2, y2, conf)
        y_true (torch.Tensor): [M, 6] The ground truths (img_id, cls_id, x1, y1, x2, y2)
        t_conf (float | list | np.ndarray | None): Confidence threshold at which
            to compute P, R, F1. If None, the operating point at max F1 is used.
        eps (float): Numerical stabilization.
        return_curves (bool): If True, the result dict also contains p_curve / r_curve.

    Returns:
        ap_ (nc,): Average precision per class and IOU threshold
        f1_ (nc,): F1-score, per-class, maximum or at given confidence
        p_  (nc,): Precision, per-class, at max F1 or given confidence
        r_  (nc,): Recall, per-class, at max F1 or given confidence
        c_  (nc,): Confidence at max F1 or given confidence
        n_preds (nc,): Number of predictions above max F1 --or given-- confidence
        p_curve (nc, 1000): Precision curve @IOU 0.5
        r_curve (nc, 1000): Recall curve @IOU 0.5
    """
    if tp is None:
        return {
            "ap"  : np.zeros((1,), dtype=np.float32),
            "f1"  : np.zeros((1,), dtype=np.float32),
            "p"   : np.zeros((1,), dtype=np.float32),
            "r"   : np.zeros((1,), dtype=np.float32),
            "c"   : np.zeros((1,), dtype=np.float32),
            "tp"  : np.zeros((1,), dtype=np.int32),
            "fp"  : np.zeros((1,), dtype=np.int32),
            "fn"  : np.zeros((1,), dtype=np.int32),
            "n_p" : np.zeros((1,), dtype=np.int32),
            "n_t" : np.zeros((1,), dtype=np.int32),
        }
    
    # If no trues, return direct
    if y_true.shape[0] == 0:
        return {
            "ap"  : np.zeros((1,), dtype=np.float32),
            "f1"  : np.zeros((1,), dtype=np.float32),
            "p"   : np.zeros((1,), dtype=np.float32),
            "r"   : np.zeros((1,), dtype=np.float32),
            "c"   : np.zeros((1,), dtype=np.float32),
            "tp"  : np.zeros((1,), dtype=np.int32),
            "fp"  : np.array([y_pred.shape[0]], dtype=np.int32),
            "fn"  : np.zeros((1,), dtype=np.int32),
            "n_p" : np.zeros([y_pred.shape[0]], dtype=np.int32),
            "n_t" : np.zeros((1,), dtype=np.int32),
        }

    # Find number of labels and predictions
    true_cls = y_true[:, 1]
    unique_cls, nt = np.unique(true_cls, return_counts=True)
    nc = unique_cls.shape[0]  # Nb of classes

    # Init result dictionary
    result = {
        "ap"  : np.zeros((nc,), dtype=np.float32),
        "f1"  : np.zeros((nc,), dtype=np.float32),
        "p"   : np.zeros((nc,), dtype=np.float32),
        "r"   : np.zeros((nc,), dtype=np.float32),
        "c"   : np.zeros((nc,), dtype=np.float32),
        "tp"  : np.zeros((nc,), dtype=np.int32),
        "fp"  : np.zeros((nc,), dtype=np.int32),
        "fn"  : np.array([nt[ci] for ci in range(nc)]),
        "n_p" : np.zeros((nc,), dtype=np.int32),
        "n_t" : nt.copy().astype(np.int32)
    }

    # If no preds, return direct 0
    if y_pred.shape[0] == 0:
        return result

    # Sort prediction by descending confidence
    ii = np.argsort(-y_pred[:, -1])
    tp = tp[ii]
    pred_conf = y_pred[ii,-1]
    pred_cls = y_pred[ii, 1]

    # Make confidence thresholds as arrays
    if isinstance(t_conf, float):
        t_conf = np.full((nc), t_conf)
    if isinstance(t_conf, list):
        t_conf = np.array(t_conf)

    if not t_conf is None:
        if not isinstance(t_conf, np.ndarray):
            raise ValueError("Error: 't_conf' should be np.ndarray object")
        if t_conf.ndim != 1:
            raise ValueError("Error: 't_conf' should be 1D array")
        if t_conf.shape[0] != nc:
            raise ValueError("Error: 't_conf' should have num_classes size along first dimension")

    # Create curves
    xx = np.linspace(0, 1, 1000)
    p_curve = np.zeros((nc, 1000))     # Output curve for precision (per class)
    r_curve = np.zeros((nc, 1000))     # Output curve for recall (per class)

    for ci, c in enumerate(unique_cls):
        i = (pred_cls == c)
        n_t = nt[ci]   # Nb target labels
        n_p = i.sum()  # Nb predictions
        if n_p == 0 or n_t == 0:
            continue

        # Accumulate FPs and TPs
        tpc = tp[i].cumsum(axis=0)
        fpc = (1 - tp[i]).cumsum(axis=0)

        # R-curve
        rec = tpc / (n_t + eps)
        r_curve[ci] = np.interp(-xx, -pred_conf[i], rec, left=0)  # Recall curve @IOU=0.5

        # P-curve
        pre = tpc / (tpc + fpc + eps)
        p_curve[ci] = np.interp(-xx, -pred_conf[i], pre, left=1)  # Precision curve @IOU=0.5

        # AP from PR curve
        # for j in range(tp.shape[1]):  # For each level of IOU threshold
        result['ap'][ci], _, _ = compute_ap(rec, pre)
        f1 = 2 * (pre * rec) / (pre + rec + eps)

        if t_conf is not None:
            # Find the first index at which pred_conf[index] >= t_conf [sorted reverse]
            i_conf = np.argmax(pred_conf[i] < t_conf[ci])
        else:
            # Find index of max F1 score
            i_conf = np.argmax(f1)

        result['f1'][ci] = f1[i_conf]
        result['p' ][ci] = pre[i_conf]
        result['r' ][ci] = rec[i_conf]
        result['c' ][ci] = pred_conf[i_conf]
        result['tp'][ci] = tpc[i_conf]
        result['fp'][ci] = fpc[i_conf]
        result['fn'][ci] = n_t - tpc[i_conf]
        result['n_p'][ci] = (tpc + fpc)[i_conf]

    if return_curves:
        result['p_curve'] = p_curve
        result['r_curve'] = r_curve
    return result


def get_ap50(ap: np.ndarray) -> np.ndarray:
    """
    Return the average precision (AP) @ IOU = 0.5, for all classes.
    """
    return ap[:, 0] if len(ap) else []


def get_ap70(ap: np.ndarray) -> np.ndarray:
    """
    Return the average precision (AP) @ IOU = 0.7, for all classes.
    """
    return ap[:, 4] if len(ap) else []


def get_ap_range(ap: np.ndarray) -> np.ndarray:
    """
    Return the average precision (AP) over IOU range [0.50:0.95:0.05], for all classes.
    """
    return ap.mean(1) if len(ap) else []


def get_map50(ap: np.ndarray) -> float:
    """
    Return the mean average precision (mAP) @ IOU = 0.5.
    """
    return ap[:, 0].mean() if len(ap) else 0.0


def get_map70(ap: np.ndarray) -> float:
    """
    Return the mean average precision (mAP) @ IOU = 0.7.
    """
    return ap[:, 4].mean() if len(ap) else 0.0


def get_map_range(ap: np.ndarray) -> float:
    """
    Return the mean average precision (mAP) over IOU range [0.50:0.95:0.05].
    """
    return ap.mean() if len(ap) else 0.0


def plot_pr_curve(
        px: np.ndarray,
        py: np.ndarray,
        ap: np.ndarray,
        names: Dict[int, str] = {},
        savedir: Path = None,
):
    """
    Plot the precision-recall curve.

    Args:
        px (np.ndarray): X values for the PR curve
        py (np.ndarray): Y values for the PR curve
        ap (np.ndarray): AP values (for the curves)
    """
    fig, ax = plt.subplots(1, 1, figsize=(9, 6), tight_layout=True)

    py = np.stack(py, axis=1)

    if 0 < len(names) < 11:  # If less than 11 classes, display per class
        for i, y in enumerate(py.T):
            ax.plot(px, y, linewidth=1, label=f"{names[i]} {ap[i, 0]:.3f}")  # plot(recall, precision)
    else:
        ax.plot(px, py, linewidth=1, color="gray")

    ax.plot(px, py.mean(1), linewidth=3, color="blue", label=f"all classes {ap[:, 0].mean():.3f} mAP@0.5")
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(bbox_to_anchor=(1.04, 1), loc="upper left")
    ax.set_title("Precision-Recall Curve")
    if savedir:
        fig.savefig(savedir, dpi=300)
    plt.close(fig)


def preds_iou_score(box_p: torch.Tensor, box_g: torch.Tensor, box_format: str = "xyxy", iou_thresh: float = 0.5, iou_method: str = "CIOU") -> torch.Tensor:
    """
    Computes and returns the pred-level performance metrics: iou-score.

    Args:
        box_p (torch.Tensor) [N, 7]: A tensor containing predicted bounding boxes (img_id, cls_id, box, conf).
        box_g (torch.Tensor) [M, 6]: A tensor containing ground truth bounding boxes (img_id, cls_id, box).
        iou_thresh (float): The threshold value to determine if two bounding boxes overlap or not. Default is 0.5.

    Returns:
        (torch.Tensor) [N,]: A tensor containing IOU-scores for each prediction.
    """
    # Get the prediction to groundtruth matches
    p2g_match = match_predictions(box_p, box_g, box_format=box_format, iou_thresh=iou_thresh, iou_method=iou_method)[0]
    iou_score = torch.zeros(box_p.shape[0], dtype=torch.float32)

    for i, j in enumerate(p2g_match):
        # i being the index of the prediction,
        # j being the index of the groundtruth, -1 if no match was found

        # default score is 0 if no match was found
        if j == -1:
            continue 
        # compute IOU-based score otherwise [TODO: change metric here later]
        iou = bbox_iou(box_p[i, 2:6], box_g[j, 2:6], boxfmt=box_format, method=iou_method)
        iou_score[i] = iou

    return iou_score
 

###########################################################
# Eval functions for Monitors
###########################################################


def compute_safety_metrics(
        y_pred: Union[torch.Tensor, np.ndarray],
        y_true: Union[torch.Tensor, np.ndarray],
        monitor_rejects: Union[torch.Tensor, np.ndarray],
        iou_thresh: float = 0.5,
        iou_method: str = "CIOU", 
        box_format: str = "xyxy",
):
    """
    Note: monitor_flag should be a binary array (1=reject, 0=accept)
    """
    # Convert monitor_rejects to numpy array
    if isinstance(monitor_rejects, torch.Tensor):
        monitor_rejects = monitor_rejects.numpy().astype(bool)
    else:
        monitor_rejects = monitor_rejects.astype(bool)

    # Match y_preds to y_trues -> binary arrays in output
    tpfp, tpfn = match_predictions(y_pred, y_true, iou_thresh=iou_thresh, iou_method=iou_method, box_format=box_format)
    tpfp_bin = np.where(tpfp != -1, 1, 0)
    tpfn_bin = np.where(tpfn != -1, 1, 0)

    # print(tpfp_bin.shape)
    # print(tpfn_bin.shape)

    # Check for each image if ML is correct or not (no FP, no FN)
    unique_img_ids = np.unique(np.concatenate([y_pred[:, 0], y_true[:, 0]]))
    ml_FP = np.logical_not(np.array([np.logical_and.reduce(tpfp_bin[y_pred[:, 0] == img_id], axis=0) for img_id in unique_img_ids]))
    ml_FN = np.logical_not(np.array([np.logical_and.reduce(tpfn_bin[y_true[:, 0] == img_id], axis=0) for img_id in unique_img_ids]))
    ml_errors = np.logical_or(ml_FP, ml_FN).astype(bool)

    # print(ml_FP.shape)
    # print(ml_FN.shape)
    # print(ml_errors.shape)

    # Check the monitor mask is of correct length
    assert len(monitor_rejects) == len(ml_errors), "Error, monitor_reject is not of correct shape"

    # Compute SG (safety gain)
    sg_return = np.logical_and(ml_errors, monitor_rejects)
    sg = sg_return.sum(axis=0) / len(sg_return)
    # Compute AC (availability cost)
    ac_return = np.logical_and(np.logical_not(ml_errors), monitor_rejects)
    ac = ac_return.sum(axis=0) / len(ac_return)
    # Compute RH (residual hazard)
    rh_return = np.logical_and(ml_errors, np.logical_not(monitor_rejects)) 
    rh = rh_return.sum(axis=0) / len(rh_return)

    return {
        'SG': sg, 
        'RH': rh,
        'AC': ac,
    }
         
    
    

    




