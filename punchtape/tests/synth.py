"""合成 5 单位纸带扫描图，用于测试识别流水线。

生成暗孔亮底的灰度图：走纸孔小、数据孔大，可选倾斜、噪声、缺孔、
撕裂区，并可裁切成有重叠的扫描段。
"""
from __future__ import annotations

import cv2
import numpy as np

from punchtape.app.codetable import CodeTable, ita2_table
from punchtape.app.decoding import encode_text

PAPER = 210   # 纸色
HOLE = 30     # 孔色


def render_tape(codes: list[int], dpi: float = 300.0, tracks: int = 5,
                sprocket_after_track: int = 2, angle_deg: float = 0.0,
                noise: float = 0.0, seed: int = 0,
                missing_data_holes: set[tuple[int, int]] | None = None,
                faint_data_holes: set[tuple[int, int]] | None = None,
                missing_sprockets: set[int] | None = None,
                torn_cols: set[int] | None = None,
                extra_holes: set[tuple[int, int]] | None = None,
                ) -> np.ndarray:
    """渲染一条纸带。

    codes: 每列码字（bit0 = 第 1 道）。
    missing_data_holes: {(列, 道)} 应画而不画的孔（模拟完全缺失）。
    faint_data_holes: {(列, 道)} 画成浅灰色孔（模拟半撕裂/未透的孔）。
    missing_sprockets: {列} 不画走纸孔（模拟撕裂）。
    torn_cols: {列} 在该列涂黑色大块（模拟撕裂污损）。
    extra_holes: {(列, 道)} 多画的孔（模拟误读）。
    """
    missing_data_holes = missing_data_holes or set()
    faint_data_holes = faint_data_holes or set()
    missing_sprockets = missing_sprockets or set()
    torn_cols = torn_cols or set()
    extra_holes = extra_holes or set()

    pitch = dpi / 10.0                      # 节距 2.54mm
    spacing = pitch                         # 道距
    margin_x = int(pitch * 2)
    # 走纸孔在第 sprocket_after_track 道之后
    track_ys = [margin_x + i * spacing for i in range(tracks)]
    sprocket_y = margin_x + (sprocket_after_track - 0.5) * spacing
    height = int(margin_x * 2 + (tracks - 1) * spacing + pitch)
    width = int(margin_x * 2 + (len(codes) - 1) * pitch) if codes else 100

    img = np.full((height, width), PAPER, dtype=np.uint8)
    # 半径之和小于走纸孔到相邻道的距离（0.5*道距），避免孔位相切
    r_data = int(pitch * 0.27)
    r_spr = int(pitch * 0.14)

    for i, code in enumerate(codes):
        x = int(round(margin_x + i * pitch))
        if i not in missing_sprockets:
            cv2.circle(img, (x, int(round(sprocket_y))), r_spr, HOLE, -1)
        for t in range(tracks):
            bit = (code >> t) & 1
            if bit and (i, t) not in missing_data_holes:
                if (i, t) in faint_data_holes:
                    cv2.circle(img, (x, int(round(track_ys[t]))), r_data,
                               150, -1)  # 半撕裂：隐约可见但不过阈值
                else:
                    cv2.circle(img, (x, int(round(track_ys[t]))), r_data,
                               HOLE, -1)
        for (c, t) in extra_holes:
            if c == i:
                cv2.circle(img, (x, int(round(track_ys[t]))), r_data, HOLE, -1)
        if i in torn_cols:
            cv2.rectangle(img, (x - int(pitch * 0.45), int(track_ys[0] - pitch)),
                          (x + int(pitch * 0.45), int(track_ys[-1] + pitch)),
                          HOLE, -1)

    if noise > 0:
        rng = np.random.default_rng(seed)
        img = np.clip(img.astype(np.float32)
                      + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)

    if abs(angle_deg) > 1e-6:
        h, w = img.shape
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
        img = cv2.warpAffine(img, M, (w, h), borderValue=PAPER)
    return img


def render_text_tape(text: str, table: CodeTable | None = None,
                     **kwargs) -> tuple[np.ndarray, list[int]]:
    """把文本编码后渲染，返回 (图像, 码字序列)。"""
    table = table or ita2_table()
    codes = encode_text(text, table)
    return render_tape(codes, **kwargs), codes


def split_segments(img: np.ndarray, n_segments: int,
                   overlap_px: int) -> list[np.ndarray]:
    """把整卷图切成 n 段，相邻段重叠 overlap_px 像素。"""
    h, w = img.shape
    step = (w - overlap_px) // n_segments
    segs = []
    for i in range(n_segments):
        x0 = i * step
        x1 = w if i == n_segments - 1 else min(w, x0 + step + overlap_px)
        segs.append(img[:, x0:x1].copy())
    return segs


def to_png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()
