"""扫描段图像处理：校正倾斜与透视，沿走纸孔估算节距，提取孔列。

坐标系约定：走纸方向为 x 轴（纸带水平走纸），y 轴垂直于走纸方向。
孔道（track）按 y 排序编号 0..tracks-1，编号 0 一侧为第 1 数据道。
走纸孔（sprocket/feed hole）位于某两条数据道之间，默认在第 2、3 道之间
（5 单位纸带惯例），可由参数调整；若检测到纸带上下颠倒会自动翻转位序。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np


# ------------------------------------------------------------------ 参数
@dataclass
class ImagingParams:
    tracks: int = 5                     # 数据孔道数
    dpi: float = 300.0                  # 扫描分辨率
    tape_width_mm: float = 17.4         # 纸带标称宽度（5 单位带 11/16"）
    sprocket_after_track: int = 2       # 走纸孔位于第几条数据道之后（1 起计）
    deskew: bool = True
    perspective_corners: list[list[float]] | None = None  # 顺时针四角 [x,y]
    sprocket_y_hint: float | None = None                  # 人工指定走纸孔 y
    bit_order: str = "lsb_first"        # 第 1 道为最低位（ITA2 惯例）
    pitch_mm: float = 2.54              # 标称节距 0.1 英寸


# ------------------------------------------------------------------ 结果
@dataclass
class Blob:
    x: float
    y: float
    area: float


@dataclass
class HoleColumn:
    """一列（一个字符位置）的识读结果，坐标均为原图坐标。"""

    index: int
    x: float                            # 列中心 x（原图）
    bits: list[int]                     # 每道 0/1，道序 = track 0..n-1
    bit_confidence: list[float]
    bit_positions: list[tuple[float, float]]  # 每道期望位置（原图坐标）
    sprocket_present: bool

    @property
    def confidence(self) -> float:
        return min(self.bit_confidence) if self.bit_confidence else 0.0

    @property
    def code(self) -> int:
        v = 0
        for i, b in enumerate(self.bits):
            v |= (b & 1) << i
        return v


@dataclass
class DamagedRegion:
    kind: str                           # torn / missing_sprocket / debris
    x0: float
    x1: float
    detail: str = ""


@dataclass
class SegmentResult:
    columns: list[HoleColumn]
    angle_deg: float
    pitch_px: float
    track_ys: list[float]               # 校正后坐标系中的道中心 y
    sprocket_y: float
    flipped: bool                       # 检测到纸带上下颠倒并已翻转位序
    damaged: list[DamagedRegion] = field(default_factory=list)
    image_size: tuple[int, int] = (0, 0)  # 原图 (width, height)
    diagnostics: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------ 内部工具
def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image


def _find_blobs(gray: np.ndarray) -> tuple[list[Blob], np.ndarray]:
    """暗孔亮底：反相 Otsu 阈值后取连通域。"""
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, _, stats, cents = cv2.connectedComponentsWithStats(bw, connectivity=8)
    blobs: list[Blob] = []
    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if area < 3:  # 噪点
            continue
        cx, cy = float(cents[i][0]), float(cents[i][1])
        blobs.append(Blob(cx, cy, area))
    return blobs, bw


def _two_class_threshold(areas: list[float]) -> float | None:
    """对数面积上做 1D 二均值划分，返回小类（走纸孔）/大类（数据孔）分界。

    对少数异常大块（撕裂、粘连）不敏感。两类不可分时返回 None。
    """
    if len(areas) < 4:
        return None
    s = np.sort(np.log(np.asarray(areas, dtype=float)))
    best_i, best_score = -1, None
    for i in range(1, len(s)):
        # 类间间隙太小的划分没有意义
        score = s[:i].var() * i + s[i:].var() * (len(s) - i)
        if best_score is None or score < best_score:
            best_score, best_i = score, i
    if best_i <= 0 or best_i >= len(s):
        return None
    if s[best_i] - s[best_i - 1] < 0.1:  # 两类几乎不可分
        return None
    return float(np.exp((s[best_i - 1] + s[best_i]) / 2.0))


def _split_sprocket(blobs: list[Blob]) -> tuple[list[Blob], list[Blob], list[Blob]]:
    """按面积把连通域分成走纸孔 / 数据孔 / 异常块（撕裂、污渍）。

    走纸孔直径明显小于数据孔：用对数面积二均值聚类找分界；
    面积远超数据孔上四分位的连通域视为撕裂/污渍区。
    """
    if not blobs:
        return [], [], []
    areas = sorted(b.area for b in blobs)
    q3 = areas[int(len(areas) * 0.75)]
    threshold = _two_class_threshold(areas)
    sprocket, data, debris = [], [], []
    for b in blobs:
        if b.area > q3 * 2.5:
            debris.append(b)
        elif threshold is not None and b.area < threshold:
            sprocket.append(b)
        else:
            data.append(b)
    # sanity：走纸孔每列一个，数量不应太少；聚类失败时退回比例法
    if len(sprocket) < max(2, len(blobs) // 20):
        median = areas[len(areas) // 2]
        sprocket = [b for b in blobs if b.area < median * 0.6
                    and b.area <= q3 * 2.5]
        data = [b for b in blobs if median * 0.6 <= b.area <= q3 * 2.5]
    return sprocket, data, debris


def _estimate_angle(sprocket: list[Blob]) -> float:
    """对走纸孔中心做稳健直线拟合，返回倾斜角（度）。"""
    if len(sprocket) < 3:
        return 0.0
    xs = np.array([b.x for b in sprocket])
    ys = np.array([b.y for b in sprocket])
    # 两遍最小二乘，剔除残差大的点（抗撕裂区干扰）
    keep = np.ones(len(xs), dtype=bool)
    slope = 0.0
    for _ in range(3):
        if keep.sum() < 3:
            break
        slope, intercept = np.polyfit(xs[keep], ys[keep], 1)
        resid = np.abs(ys - (slope * xs + intercept))
        scale = np.median(resid[keep]) * 3 + 1e-6
        keep = resid < max(scale, 2.0)
    return math.degrees(math.atan(slope))


def _rotate(image: np.ndarray, angle_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """绕图心旋转，返回 (旋转后图像, 2x3 仿射矩阵)。"""
    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle_deg, 1.0)
    rotated = cv2.warpAffine(image, M, (w, h), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=210)
    return rotated, M


def _warp_perspective(image: np.ndarray, corners: list[list[float]],
                      ) -> tuple[np.ndarray, np.ndarray]:
    """按用户给出的四角做透视校正，返回 (校正图, 3x3 单应矩阵)。"""
    src = np.array(corners, dtype=np.float32)
    w = int(max(np.linalg.norm(src[1] - src[0]), np.linalg.norm(src[2] - src[3])))
    h = int(max(np.linalg.norm(src[3] - src[0]), np.linalg.norm(src[2] - src[1])))
    w, h = max(w, 8), max(h, 8)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                   dtype=np.float32)
    H = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(image, H, (w, h), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=210)
    return warped, H


def _map_points(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """把 Nx2 点集经 2x3 或 3x3 矩阵变换。"""
    pts_h = np.hstack([pts, np.ones((len(pts), 1))])
    out = pts_h @ M.T
    if M.shape[0] == 3:
        out = out[:, :2] / out[:, 2:3]
        return out
    return out[:, :2]


def _cluster_1d(values: list[float], n_clusters: int,
                gap_hint: float) -> list[float]:
    """一维聚类：排序后按间隙切开，不足 n_clusters 时返回 None。"""
    if len(values) < n_clusters:
        return []
    vals = sorted(values)
    # 找最大的 n-1 个间隙作为切分点
    gaps = sorted(((vals[i + 1] - vals[i], i) for i in range(len(vals) - 1)),
                  reverse=True)
    cuts = sorted(i for g, i in gaps[: n_clusters - 1] if g > gap_hint * 0.45)
    if len(cuts) != n_clusters - 1:
        return []
    clusters, prev = [], 0
    for c in cuts + [len(vals) - 1]:
        clusters.append(vals[prev:c + 1])
        prev = c + 1
    return [float(np.mean(c)) for c in clusters]


def _median_step(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    diffs = np.diff(sorted(xs))
    diffs = diffs[diffs > 0]
    return float(np.median(diffs)) if len(diffs) else 0.0


# ------------------------------------------------------------------ 主流程
def process_segment(image: np.ndarray, params: ImagingParams) -> SegmentResult:
    """处理一个扫描段，返回孔列与诊断信息。"""
    gray = _to_gray(image)
    orig_h, orig_w = gray.shape[:2]
    # 原图 -> 当前工作图 的复合变换；先透视后旋转
    fwd = np.eye(3)

    diagnostics: list[dict] = []

    # 1) 透视校正（用户给出四角时）
    if params.perspective_corners:
        gray, H = _warp_perspective(gray, params.perspective_corners)
        fwd = H @ fwd

    # 2) 倾斜校正：检测 -> 估角 -> 旋转 -> 重新检测
    blobs, _ = _find_blobs(gray)
    sprocket, _, _ = _split_sprocket(blobs)
    angle = _estimate_angle(sprocket) if params.deskew else 0.0
    if params.deskew and abs(angle) > 0.02:
        gray, R = _rotate(gray, angle)
        R3 = np.eye(3)
        R3[:2, :] = R
        fwd = R3 @ fwd
        blobs, bw = _find_blobs(gray)
    else:
        _, bw = _find_blobs(gray)
        angle = 0.0

    sprocket, data, debris = _split_sprocket(blobs)
    if len(sprocket) < 2:
        raise ValueError("未检测到足够的走纸孔，无法估算节距；"
                         "请检查扫描图或提供走纸孔位置参数")

    # 3) 节距：走纸孔 x 间距中位数；与标称值（dpi 推算）互相校验
    pitch = _median_step([b.x for b in sprocket])
    nominal_pitch_px = params.pitch_mm / 25.4 * params.dpi
    if pitch <= 0:
        pitch = nominal_pitch_px
        diagnostics.append({"type": "pitch_fallback",
                            "message": "走纸孔间距估算失败，使用标称节距"})
    elif nominal_pitch_px > 0 and abs(pitch - nominal_pitch_px) / nominal_pitch_px > 0.25:
        diagnostics.append({
            "type": "pitch_mismatch",
            "message": f"估算节距 {pitch:.1f}px 与标称 {nominal_pitch_px:.1f}px "
                       f"（{params.dpi}dpi）偏差超过 25%"})

    # 4) 走纸孔 y 与数据道 y
    sprocket_y = params.sprocket_y_hint
    if sprocket_y is None:
        sprocket_y = float(np.median([b.y for b in sprocket]))
    track_ys = _cluster_1d([b.y for b in data], params.tracks, pitch)
    if len(track_ys) != params.tracks:
        # 某道整段无孔导致聚类失败：以走纸孔为锚、节距为道距推算全部道位置
        # （走纸孔位于第 sprocket_after_track 道与下一道正中间）
        k = params.sprocket_after_track
        track_ys = [sprocket_y + (i - (k - 0.5)) * pitch
                    for i in range(params.tracks)]
        diagnostics.append({"type": "track_extrapolated",
                            "message": "部分孔道整段无孔，道位置由走纸孔位置推算"})

    # 5) 检测纸带是否上下颠倒：走纸孔上方应恰有 sprocket_after_track 条道
    above = sum(1 for y in track_ys if y < sprocket_y)
    flipped = above != params.sprocket_after_track
    if flipped:
        track_ys = list(reversed(track_ys))
        diagnostics.append({"type": "tape_flipped",
                            "message": "检测到纸带上下颠倒，已自动翻转道序"})

    # 6) 列网格：以走纸孔 x 为锚；仅当数据孔超出走纸孔范围半节距以上时
    #    才向端部补列（端部列的走纸孔可能缺失）
    spr_xs = sorted(b.x for b in sprocket)
    x_start, x_end = spr_xs[0], spr_xs[-1]
    if data:
        data_min = min(b.x for b in data)
        data_max = max(b.x for b in data)
        while x_start - data_min > pitch * 0.55:
            x_start -= pitch
        while data_max - x_end > pitch * 0.55:
            x_end += pitch
    n_cols = int(round((x_end - x_start) / pitch)) + 1
    grid_x = [x_start + i * pitch for i in range(n_cols)]

    # 走纸孔缺失检测（撕裂区线索）；只在实际观测到走纸孔的 x 范围内检查
    damaged: list[DamagedRegion] = []
    spr_x_arr = np.array(spr_xs)
    obs_x0, obs_x1 = spr_xs[0] - pitch * 0.5, spr_xs[-1] + pitch * 0.5
    missing_spr_cols: list[int] = []
    for i, gx in enumerate(grid_x):
        if obs_x0 <= gx <= obs_x1 and \
                np.min(np.abs(spr_x_arr - gx)) > pitch * 0.45:
            missing_spr_cols.append(i)
    if missing_spr_cols:
        # 连续缺孔合并为区域
        run_start = prev = missing_spr_cols[0]
        for c in missing_spr_cols[1:] + [None]:
            if c is None or c != prev + 1:
                damaged.append(DamagedRegion(
                    kind="missing_sprocket",
                    x0=grid_x[run_start] - pitch / 2,
                    x1=grid_x[prev] + pitch / 2,
                    detail=f"第 {run_start}..{prev} 列走纸孔缺失（疑似撕裂）"))
                run_start = c
            if c is not None:
                prev = c
    for d in debris:
        damaged.append(DamagedRegion(kind="debris", x0=d.x - math.sqrt(d.area),
                                     x1=d.x + math.sqrt(d.area),
                                     detail=f"异常墨迹/撕裂块，面积 {d.area:.0f}px²"))

    # 7) 逐列判定各道有孔/无孔
    tol_x = pitch * 0.38
    spacing_y = (np.median(np.diff(track_ys)) if len(track_ys) > 1 else pitch)
    tol_y = float(spacing_y) * 0.42
    data_arr = np.array([[b.x, b.y] for b in data]) if data else np.zeros((0, 2))

    # 原图坐标反变换矩阵
    inv = np.linalg.inv(fwd)

    columns: list[HoleColumn] = []
    for i, gx in enumerate(grid_x):
        bits, confs, positions = [], [], []
        for ty in track_ys:
            # 期望位置（校正坐标系）-> 原图坐标
            orig = _map_points(inv, np.array([[gx, ty]]))[0]
            positions.append((float(orig[0]), float(orig[1])))
            hit = False
            dist = tol_x
            if len(data_arr):
                d = np.abs(data_arr - [gx, ty])
                near = (d[:, 0] <= tol_x) & (d[:, 1] <= tol_y)
                if near.any():
                    hit = True
                    dist = float(np.hypot(d[near, 0], d[near, 1]).min())
            if hit:
                bits.append(1)
                confs.append(round(max(0.5, 1.0 - dist / max(tol_x, 1e-6)), 3))
            else:
                # 无孔：采样局部亮度佐证（亮底 -> 高置信无孔；偏暗 -> 存疑）
                x0, y0 = int(round(gx)), int(round(ty))
                r = max(2, int(pitch * 0.18))
                patch = gray[max(0, y0 - r):y0 + r + 1, max(0, x0 - r):x0 + r + 1]
                if patch.size:
                    mean = float(patch.mean())
                    # 210 附近为纸色；越暗越可疑
                    conf = min(1.0, max(0.1, (mean - 90.0) / 120.0))
                else:
                    conf = 0.5
                bits.append(0)
                confs.append(round(conf, 3))
        spr_present = i not in missing_spr_cols
        columns.append(HoleColumn(
            index=i, x=float(_map_points(inv, np.array([[gx, sprocket_y]]))[0][0]),
            bits=bits, bit_confidence=confs, bit_positions=positions,
            sprocket_present=spr_present))

    if params.bit_order == "msb_first":
        for col in columns:
            col.bits = list(reversed(col.bits))
            col.bit_confidence = list(reversed(col.bit_confidence))
            col.bit_positions = list(reversed(col.bit_positions))

    return SegmentResult(
        columns=columns,
        angle_deg=angle,
        pitch_px=pitch,
        track_ys=track_ys,
        sprocket_y=sprocket_y,
        flipped=flipped,
        damaged=damaged,
        image_size=(orig_w, orig_h),
        diagnostics=diagnostics,
    )
