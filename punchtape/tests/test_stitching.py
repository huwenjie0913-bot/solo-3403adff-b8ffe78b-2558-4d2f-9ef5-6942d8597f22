"""拼接测试：重叠对齐、冲突检测与合并。"""
from punchtape.app.stitching import ColumnView, find_overlap, stitch


def _views(bitstrings, seg_id=None):
    return [ColumnView(tuple(int(b) for b in bs), 0.9, seg_id, i)
            for i, bs in enumerate(bitstrings)]


def test_find_overlap_exact():
    a = _views(["00011", "00101", "01110", "01001"])
    b = _views(["01110", "01001", "10000"])
    assert find_overlap(a, b, min_overlap=2) == 2


def test_stitch_two_segments():
    a = _views(["00011", "00101", "01110", "01001"], seg_id=1)
    b = _views(["01110", "01001", "10000", "00111"], seg_id=2)
    result = stitch([(1, a), (2, b)], min_overlap=2)
    bits = [m.bits for m in result.columns]
    assert bits == [(0, 0, 0, 1, 1), (0, 0, 1, 0, 1), (0, 1, 1, 1, 0),
                    (0, 1, 0, 0, 1), (1, 0, 0, 0, 0), (0, 0, 1, 1, 1)]
    assert not result.conflicts
    assert result.overlaps == [2]
    # 重叠列应有两个来源
    assert result.columns[2].sources == [(1, 2), (2, 0)]


def test_stitch_conflict_recorded():
    a = _views(["00011", "00101", "01110"], seg_id=1)
    b = [ColumnView((0, 0, 1, 0, 1), 0.9, 2, 0),   # 一致
         ColumnView((1, 1, 1, 1, 1), 0.9, 2, 1)]   # 冲突
    result = stitch([(1, a), (2, b)], min_overlap=2)
    assert len(result.conflicts) == 1
    c = result.conflicts[0]
    assert c.merged_index == 2
    assert c.bits_a == (0, 1, 1, 1, 0)
    assert c.bits_b == (1, 1, 1, 1, 1)
    # 备选读数保留在合并列上
    assert result.columns[2].alternatives == [(1, 1, 1, 1, 1)]


def test_stitch_no_overlap_appends():
    a = _views(["00011", "00101"], seg_id=1)
    b = _views(["11110", "11101"], seg_id=2)
    result = stitch([(1, a), (2, b)], min_overlap=2)
    assert len(result.columns) == 4
    assert result.overlaps == [0]
