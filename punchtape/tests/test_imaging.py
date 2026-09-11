"""图像处理流水线测试：倾斜校正、节距估算、孔列提取、损伤检测。"""
import numpy as np
import pytest

from punchtape.app.imaging import ImagingParams, process_segment
from punchtape.tests.synth import render_tape, render_text_tape


def _codes_of(result):
    return [c.code for c in result.columns]


def test_recognize_clean_tape():
    text = "HELLO 123"
    img, codes = render_text_tape(text, dpi=300)
    result = process_segment(img, ImagingParams(dpi=300))
    assert _codes_of(result) == codes
    assert abs(result.pitch_px - 30.0) < 1.5
    assert all(c.confidence >= 0.5 for c in result.columns)


def test_deskew_rotated_tape():
    text = "RYRYRY 42"
    img, codes = render_text_tape(text, dpi=300, angle_deg=2.5)
    result = process_segment(img, ImagingParams(dpi=300))
    assert abs(abs(result.angle_deg) - 2.5) < 0.4
    assert _codes_of(result) == codes


def test_noisy_tape():
    img, codes = render_text_tape("NOISE 7", dpi=300, noise=18.0, seed=3)
    result = process_segment(img, ImagingParams(dpi=300))
    assert _codes_of(result) == codes


def test_missing_sprocket_marks_column():
    img, codes = render_text_tape("ABCDEF", dpi=300,
                                  missing_sprockets={3})
    result = process_segment(img, ImagingParams(dpi=300))
    # 第 3 列附近走纸孔缺失 -> 撕裂区诊断
    assert any(d.kind == "missing_sprocket" for d in result.damaged)
    missing_cols = [c.index for c in result.columns if not c.sprocket_present]
    assert any(abs(m - 3) <= 1 for m in missing_cols)


def test_missing_data_hole_read_as_zero():
    # 'M' = 11100；把中间列第 2 道孔去掉 -> 该列读数应少该位
    from punchtape.app.codetable import ita2_table
    from punchtape.app.decoding import encode_text
    table = ita2_table()
    codes = encode_text("MMM", table)
    assert codes == [0x1C, 0x1C, 0x1C]
    img = render_tape(codes, dpi=300, missing_data_holes={(1, 2)})
    result = process_segment(img, ImagingParams(dpi=300))
    assert result.columns[1].code == 0x1C & ~(1 << 2)
    assert result.columns[0].code == 0x1C
    assert result.columns[2].code == 0x1C


def test_torn_region_detected():
    img, codes = render_text_tape("TORN TAPE", dpi=300, torn_cols={4})
    result = process_segment(img, ImagingParams(dpi=300))
    assert any(d.kind == "debris" for d in result.damaged)


def test_perspective_corners_warp():
    # 构造轻微透视变形：上边压缩
    img, codes = render_text_tape("WARP", dpi=300)
    h, w = img.shape
    src = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    dst = np.float32([[10, 6], [w - 11, 2], [w - 1, h - 1], [0, h - 1]])
    H = np.float32([[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    import cv2
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, M, (w, h), borderValue=210)
    corners = [[10, 6], [w - 11, 2], [w - 1, h - 1], [0, h - 1]]
    result = process_segment(warped, ImagingParams(
        dpi=300, perspective_corners=corners))
    assert _codes_of(result) == codes


def test_bit_positions_in_original_coords():
    img, codes = render_text_tape("XY", dpi=300, angle_deg=1.5)
    result = process_segment(img, ImagingParams(dpi=300))
    for col in result.columns:
        assert len(col.bit_positions) == 5
        for x, y in col.bit_positions:
            assert 0 <= x < img.shape[1] and 0 <= y < img.shape[0]


def test_insufficient_sprockets_raises():
    img = np.full((100, 100), 210, dtype=np.uint8)
    with pytest.raises(ValueError):
        process_segment(img, ImagingParams(dpi=300))
