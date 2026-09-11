"""识别流水线：扫描段 -> 孔列 -> 拼接 -> 解码 -> 入库。"""
from __future__ import annotations

import json

import cv2
import numpy as np
from sqlalchemy.orm import Session

from .codetable import CodeTable
from .decoding import decode
from .imaging import ImagingParams, process_segment
from .models import (CodeTableRow, DiagnosticRow, HoleColumnRow,
                     RecognitionJob, Revision, ScanSegment, Tape)
from .stitching import ColumnView, stitch


def load_code_table(db: Session, code_table_id: int | None) -> tuple[CodeTable, int]:
    if code_table_id is None:
        row = db.query(CodeTableRow).filter_by(name="ITA2").first()
    else:
        row = db.get(CodeTableRow, code_table_id)
    if row is None:
        raise ValueError("码表不存在")
    return CodeTable.from_dict(json.loads(row.definition)), row.id


def run_recognition(db: Session, tape: Tape, req) -> RecognitionJob:
    """执行完整识别流程并落库，返回任务记录。"""
    table, table_id = load_code_table(db, req.code_table_id)

    params = ImagingParams(
        tracks=req.tracks or tape.tracks,
        dpi=req.dpi or tape.dpi,
        tape_width_mm=req.tape_width_mm or tape.tape_width_mm,
        sprocket_after_track=req.sprocket_after_track or tape.sprocket_after_track,
        deskew=req.deskew,
        perspective_corners=req.perspective_corners,
        sprocket_y_hint=req.sprocket_y_hint,
        bit_order=req.bit_order,
    )

    if not tape.segments:
        raise ValueError("纸带还没有扫描段，请先上传分段扫描图")

    # 1) 逐段处理
    seg_views: list[tuple[int, list[ColumnView]]] = []
    seg_diag: list[dict] = []
    for seg in tape.segments:
        img = cv2.imread(seg.image_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError(f"无法读取扫描图 {seg.image_path}")
        result = process_segment(img, params)
        views = [ColumnView(tuple(c.bits), c.confidence, seg.id, c.index, c)
                 for c in result.columns]
        seg_views.append((seg.id, views))
        for d in result.diagnostics:
            seg_diag.append({"type": d["type"], "severity": "warning",
                             "column_index": -1,
                             "message": f"[段{seg.order}] {d['message']}",
                             "details": {"segment_id": seg.id}})
        for dmg in result.damaged:
            seg_diag.append({"type": "torn_region", "severity": "error",
                             "column_index": -1,
                             "message": f"[段{seg.order}] 撕裂/污损区 "
                                        f"x∈[{dmg.x0:.0f},{dmg.x1:.0f}]：{dmg.detail}",
                             "details": {"segment_id": seg.id,
                                         "x0": dmg.x0, "x1": dmg.x1,
                                         "kind": dmg.kind}})

    # 2) 拼接
    stitched = stitch(seg_views, min_overlap=req.min_overlap)
    for c in stitched.conflicts:
        seg_diag.append({
            "type": "overlap_conflict", "severity": "warning",
            "column_index": c.merged_index,
            "message": f"第 {c.merged_index} 列重叠段冲突：段{c.seg_a}="
                       f"{''.join(map(str, c.bits_a))} vs 段{c.seg_b}="
                       f"{''.join(map(str, c.bits_b))}",
            "details": {"seg_a": c.seg_a, "seg_b": c.seg_b,
                        "bits_a": list(c.bits_a), "bits_b": list(c.bits_b)}})

    # 3) 缺孔诊断：高置信应为孔却缺失（由成像置信度体现）+ 走纸孔缺失
    for m in stitched.columns:
        rep = m.representative
        if rep is not None and not rep.sprocket_present:
            seg_diag.append({"type": "missing_hole", "severity": "warning",
                             "column_index": m.index,
                             "message": f"第 {m.index} 列走纸孔缺失（疑似撕裂）",
                             "details": {}})

    # 4) 解码
    codes = []
    for m in stitched.columns:
        v = 0
        for i, b in enumerate(m.bits):
            v |= (b & 1) << i
        codes.append(v)
    confs = [m.confidence for m in stitched.columns]
    decoded = decode(codes, table, confidences=confs,
                     initial_shift=req.initial_shift)

    # 5) 入库
    job = RecognitionJob(
        tape_id=tape.id, code_table_id=table_id,
        params=json.dumps({
            "tracks": params.tracks, "dpi": params.dpi,
            "tape_width_mm": params.tape_width_mm,
            "sprocket_after_track": params.sprocket_after_track,
            "deskew": params.deskew, "bit_order": params.bit_order,
            "initial_shift": req.initial_shift,
            "min_overlap": req.min_overlap,
            "overlaps": stitched.overlaps,
        }))
    db.add(job)
    db.flush()

    for m in stitched.columns:
        rep = m.representative
        db.add(HoleColumnRow(
            job_id=job.id, index=m.index,
            bits="".join(map(str, m.bits)),
            confidence=m.confidence,
            bit_confidence=json.dumps(
                list(rep.bit_confidence) if rep else [m.confidence] * len(m.bits)),
            bit_positions=json.dumps(
                [list(p) for p in rep.bit_positions] if rep else []),
            sources=json.dumps([[s, i] for s, i in m.sources]),
            alternatives=json.dumps(["".join(map(str, a)) for a in m.alternatives]),
            sprocket_present=bool(rep.sprocket_present) if rep else True,
        ))

    for d in seg_diag:
        db.add(DiagnosticRow(job_id=job.id, type=d["type"],
                             severity=d["severity"],
                             column_index=d["column_index"],
                             message=d["message"],
                             details=json.dumps(d["details"])))
    for d in decoded.diagnostics:
        db.add(DiagnosticRow(job_id=job.id, type=d.type, severity=d.severity,
                             column_index=d.column_index, message=d.message,
                             details=json.dumps(d.details)))
    db.commit()
    db.refresh(job)
    return job


def effective_columns(db: Session, job: RecognitionJob) -> list[dict]:
    """合并原始孔列与人工修订，返回每列有效位型及锁定状态。"""
    revisions: dict[int, Revision] = {}
    for r in job.revisions:  # 已按时间排序，后者覆盖前者
        revisions[r.column_index] = r
    cols = []
    for c in job.columns:
        rev = revisions.get(c.index)
        cols.append({
            "index": c.index,
            "bits": tuple(int(b) for b in (rev.revised_bits if rev else c.bits)),
            "raw_bits": c.bits,
            "bit_confidence": json.loads(c.bit_confidence),
            "alternatives": [tuple(int(b) for b in a)
                             for a in json.loads(c.alternatives)],
            "locked": bool(rev.locked) if rev else False,
            "revised": rev is not None,
        })
    return cols
