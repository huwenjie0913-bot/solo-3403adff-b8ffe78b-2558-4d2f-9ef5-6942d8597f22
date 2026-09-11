"""把有重叠的扫描段按孔列模式对齐，拼成连续孔列，并报告冲突。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .imaging import HoleColumn


@dataclass
class ColumnView:
    """拼接阶段使用的轻量列视图。"""

    bits: tuple[int, ...]
    confidence: float
    segment_id: int | None = None
    segment_index: int = 0          # 在源段中的列号
    source: HoleColumn | None = None


@dataclass
class StitchConflict:
    merged_index: int
    seg_a: int | None
    seg_b: int | None
    index_a: int
    index_b: int
    bits_a: tuple[int, ...]
    bits_b: tuple[int, ...]


@dataclass
class MergedColumn:
    index: int
    bits: tuple[int, ...]
    confidence: float
    sources: list[tuple[int | None, int]] = field(default_factory=list)  # (segment_id, 列号)
    alternatives: list[tuple[int, ...]] = field(default_factory=list)    # 冲突方的备选读数
    representative: HoleColumn | None = None  # 置信度最高的原始列（坐标来源）


@dataclass
class StitchResult:
    columns: list[MergedColumn]
    conflicts: list[StitchConflict]
    overlaps: list[int]             # 每对相邻段的重叠列数


def _agreement(a: list[ColumnView], b: list[ColumnView]) -> tuple[int, int]:
    """返回 (一致列数, 参与比较的列数)，只统计双方都高置信的列。"""
    agree = total = 0
    for ca, cb in zip(a, b):
        if ca.confidence >= 0.5 and cb.confidence >= 0.5:
            total += 1
            if ca.bits == cb.bits:
                agree += 1
    return agree, total


def find_overlap(a: list[ColumnView], b: list[ColumnView],
                 min_overlap: int = 3, min_agreement: float = 0.5) -> int:
    """寻找 b 相对 a 的重叠列数（b 的头与 a 的尾对齐）。

    一致率低于 min_agreement 的对齐视为无重叠（返回 0）。
    """
    best_k, best_score = 0, -1.0
    max_k = min(len(a), len(b))
    for k in range(min_overlap, max_k + 1):
        agree, total = _agreement(a[len(a) - k:], b[:k])
        if total == 0:
            continue
        ratio = agree / total
        if ratio < min_agreement:
            continue
        score = ratio + total * 1e-3  # 一致率优先，其次倾向更长的重叠
        if score > best_score:
            best_score, best_k = score, k
    return best_k


def stitch(segments: list[tuple[int | None, list[ColumnView]]],
           min_overlap: int = 3) -> StitchResult:
    """按顺序拼接各段。segments: [(segment_id, 列视图列表), ...]"""
    merged: list[MergedColumn] = []
    conflicts: list[StitchConflict] = []
    overlaps: list[int] = []

    for seg_id, cols in segments:
        views = [ColumnView(tuple(c.bits), c.confidence, seg_id, c.segment_index, c.source)
                 for c in cols]
        if not merged:
            for v in views:
                merged.append(MergedColumn(
                    index=len(merged), bits=v.bits, confidence=v.confidence,
                    sources=[(v.segment_id, v.segment_index)],
                    representative=v.source))
            continue

        k = find_overlap(
            [ColumnView(m.bits, m.confidence) for m in merged], views,
            min_overlap=min_overlap)
        overlaps.append(k)
        if k == 0:
            # 无可靠重叠：直接顺序拼接并记录
            for v in views:
                merged.append(MergedColumn(
                    index=len(merged), bits=v.bits, confidence=v.confidence,
                    sources=[(v.segment_id, v.segment_index)],
                    representative=v.source))
            continue

        # 合并重叠区
        base = len(merged) - k
        for j in range(k):
            m = merged[base + j]
            v = views[j]
            m.sources.append((v.segment_id, v.segment_index))
            if v.bits != m.bits:
                conflicts.append(StitchConflict(
                    merged_index=m.index, seg_a=m.sources[0][0], seg_b=seg_id,
                    index_a=m.sources[0][1], index_b=v.segment_index,
                    bits_a=m.bits, bits_b=v.bits))
                if v.bits not in m.alternatives:
                    m.alternatives.append(v.bits)
                # 保留置信度更高的一方作为当前读数
                if v.confidence > m.confidence:
                    m.bits, m.confidence = v.bits, v.confidence
                    if v.source is not None:
                        m.representative = v.source
            else:
                # 重复扫描一致：提升置信度
                m.confidence = min(1.0, max(m.confidence, v.confidence) + 0.05)
                if v.source is not None and (
                        m.representative is None
                        or v.confidence >= (m.representative.confidence or 0)):
                    m.representative = v.source
        # 追加非重叠部分
        for v in views[k:]:
            merged.append(MergedColumn(
                index=len(merged), bits=v.bits, confidence=v.confidence,
                sources=[(v.segment_id, v.segment_index)],
                representative=v.source))

    return StitchResult(columns=merged, conflicts=conflicts, overlaps=overlaps)
