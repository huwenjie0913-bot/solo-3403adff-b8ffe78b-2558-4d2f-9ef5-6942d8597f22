"""纸带与扫描段路由。"""
from __future__ import annotations

import os

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ScanSegment, Tape
from ..schemas import TapeCreate, TapeOut

router = APIRouter(prefix="/tapes", tags=["tapes"])

SEGMENT_DIR = os.environ.get(
    "PUNCHTAPE_SEGMENT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "segments"))


@router.post("", response_model=TapeOut, status_code=201)
def create_tape(body: TapeCreate, db: Session = Depends(get_db)):
    tape = Tape(**body.model_dump())
    db.add(tape)
    db.commit()
    db.refresh(tape)
    return TapeOut(**body.model_dump(), id=tape.id, segment_count=0)


@router.get("", response_model=list[TapeOut])
def list_tapes(db: Session = Depends(get_db)):
    return [TapeOut(id=t.id, name=t.name, tape_width_mm=t.tape_width_mm,
                    tracks=t.tracks, dpi=t.dpi,
                    sprocket_after_track=t.sprocket_after_track,
                    notes=t.notes, segment_count=len(t.segments))
            for t in db.query(Tape).all()]


@router.get("/{tape_id}", response_model=TapeOut)
def get_tape(tape_id: int, db: Session = Depends(get_db)):
    t = db.get(Tape, tape_id)
    if not t:
        raise HTTPException(404, "纸带不存在")
    return TapeOut(id=t.id, name=t.name, tape_width_mm=t.tape_width_mm,
                   tracks=t.tracks, dpi=t.dpi,
                   sprocket_after_track=t.sprocket_after_track,
                   notes=t.notes, segment_count=len(t.segments))


@router.post("/{tape_id}/segments", status_code=201)
async def upload_segment(tape_id: int, file: UploadFile,
                         order: int | None = None,
                         db: Session = Depends(get_db)):
    """上传一段扫描图（PNG/JPEG）。order 缺省追加到末尾。"""
    tape = db.get(Tape, tape_id)
    if not tape:
        raise HTTPException(404, "纸带不存在")
    raw = await file.read()
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise HTTPException(422, "无法解码图像文件")

    os.makedirs(SEGMENT_DIR, exist_ok=True)
    if order is None:
        order = (max((s.order for s in tape.segments), default=-1) + 1)
    seg = ScanSegment(tape_id=tape_id, order=order, image_path="",
                      width_px=int(img.shape[1]), height_px=int(img.shape[0]))
    db.add(seg)
    db.flush()
    path = os.path.join(SEGMENT_DIR, f"segment_{seg.id}.png")
    cv2.imwrite(path, img)
    seg.image_path = path
    db.commit()
    return {"id": seg.id, "order": seg.order,
            "width_px": seg.width_px, "height_px": seg.height_px}


@router.get("/{tape_id}/segments")
def list_segments(tape_id: int, db: Session = Depends(get_db)):
    tape = db.get(Tape, tape_id)
    if not tape:
        raise HTTPException(404, "纸带不存在")
    return [{"id": s.id, "order": s.order, "width_px": s.width_px,
             "height_px": s.height_px} for s in tape.segments]
