"""识别任务路由：执行识别、查询孔列/文本/诊断、人工修订、修复枚举、导出。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..decoding import decode
from ..export import export_json, export_text
from ..models import RecognitionJob, RepairRun, Revision, Tape
from ..pipeline import (effective_columns, job_initial_shift, load_code_table,
                        run_recognition)
from ..repair import RepairRules, enumerate_repairs
from ..schemas import (CandidateOut, ColumnOut, DiagnosticOut, JobOut,
                       RecognizeRequest, RepairRequest, RepairRunOut,
                       RevisionCreate, RevisionOut)

router = APIRouter(tags=["jobs"])


# ------------------------------------------------------------------ 工具
def _get_job(db: Session, job_id: int) -> RecognitionJob:
    job = db.get(RecognitionJob, job_id)
    if not job:
        raise HTTPException(404, "识别任务不存在")
    return job


def _job_out(db: Session, job: RecognitionJob) -> JobOut:
    table, _ = load_code_table(db, job.code_table_id)
    cols = effective_columns(db, job)
    codes = [sum((b & 1) << i for i, b in enumerate(c["bits"])) for c in cols]
    text = decode(codes, table, initial_shift=job_initial_shift(job)).text
    preview = text[:80] + ("…" if len(text) > 80 else "")
    return JobOut(id=job.id, tape_id=job.tape_id,
                  code_table_id=job.code_table_id, status=job.status,
                  column_count=len(job.columns),
                  diagnostic_count=len(job.diagnostics),
                  text_preview=preview)


# ------------------------------------------------------------------ 识别
@router.post("/tapes/{tape_id}/recognize", response_model=JobOut, status_code=201)
def recognize(tape_id: int, body: RecognizeRequest,
              db: Session = Depends(get_db)):
    tape = db.get(Tape, tape_id)
    if not tape:
        raise HTTPException(404, "纸带不存在")
    try:
        job = run_recognition(db, tape, body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _job_out(db, job)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    return _job_out(db, _get_job(db, job_id))


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db)):
    return [_job_out(db, j) for j in db.query(RecognitionJob).all()]


# ------------------------------------------------------------------ 孔列 / 文本 / 诊断
@router.get("/jobs/{job_id}/columns", response_model=list[ColumnOut])
def get_columns(job_id: int, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    eff = {c["index"]: c for c in effective_columns(db, job)}
    out = []
    for c in job.columns:
        e = eff[c.index]
        bits = "".join(map(str, e["bits"]))
        out.append(ColumnOut(
            index=c.index, bits=bits,
            code=sum((b & 1) << i for i, b in enumerate(e["bits"])),
            confidence=c.confidence,
            bit_confidence=json.loads(c.bit_confidence),
            bit_positions=json.loads(c.bit_positions),
            sources=json.loads(c.sources),
            alternatives=json.loads(c.alternatives),
            sprocket_present=c.sprocket_present,
            revised_bits=bits if e["revised"] else None,
            locked=e["locked"],
        ))
    return out


@router.get("/jobs/{job_id}/text")
def get_text(job_id: int, revised: bool = True, db: Session = Depends(get_db)):
    """解码文本。revised=true 用修订后孔阵，否则用原始识别孔阵。"""
    job = _get_job(db, job_id)
    table, _ = load_code_table(db, job.code_table_id)
    cols = effective_columns(db, job)
    if revised:
        codes = [sum((b & 1) << i for i, b in enumerate(c["bits"]))
                 for c in cols]
    else:
        codes = [sum((int(ch) & 1) << i for i, ch in enumerate(c["raw_bits"]))
                 for c in cols]
    result = decode(codes, table, initial_shift=job_initial_shift(job))
    return {"job_id": job_id, "revised": revised, "text": result.text,
            "chars": [{"index": c.index, "code": c.code, "shift": c.shift,
                       "char": c.char, "legal": c.legal}
                      for c in result.chars]}


@router.get("/jobs/{job_id}/diagnostics", response_model=list[DiagnosticOut])
def get_diagnostics(job_id: int, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    return [DiagnosticOut(type=d.type, severity=d.severity,
                          column_index=d.column_index, message=d.message,
                          details=json.loads(d.details))
            for d in job.diagnostics]


# ------------------------------------------------------------------ 人工修订
@router.post("/jobs/{job_id}/revisions", response_model=RevisionOut,
             status_code=201)
def create_revision(job_id: int, body: RevisionCreate,
                    db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    col = next((c for c in job.columns if c.index == body.column_index), None)
    if col is None:
        raise HTTPException(404, f"第 {body.column_index} 列不存在")
    if len(body.revised_bits) != len(col.bits) or \
            any(b not in "01" for b in body.revised_bits):
        raise HTTPException(422, f"位串须为 {len(col.bits)} 位 0/1 串")
    rev = Revision(job_id=job_id, column_index=body.column_index,
                   original_bits=col.bits, revised_bits=body.revised_bits,
                   locked=body.locked, author=body.author, reason=body.reason)
    db.add(rev)
    db.commit()
    db.refresh(rev)
    return RevisionOut(id=rev.id, column_index=rev.column_index,
                       revised_bits=rev.revised_bits, locked=rev.locked,
                       author=rev.author, reason=rev.reason,
                       original_bits=rev.original_bits,
                       created_at=rev.created_at.isoformat())


@router.get("/jobs/{job_id}/revisions", response_model=list[RevisionOut])
def list_revisions(job_id: int, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    return [RevisionOut(id=r.id, column_index=r.column_index,
                        revised_bits=r.revised_bits, locked=r.locked,
                        author=r.author, reason=r.reason,
                        original_bits=r.original_bits,
                        created_at=r.created_at.isoformat())
            for r in job.revisions]


# ------------------------------------------------------------------ 修复枚举
@router.post("/jobs/{job_id}/repair", response_model=RepairRunOut,
             status_code=201)
def repair(job_id: int, body: RepairRequest, db: Session = Depends(get_db)):
    """枚举补孔/去孔候选。只产出候选并存档，不改动识别结果。"""
    job = _get_job(db, job_id)
    table, _ = load_code_table(db, job.code_table_id)
    cols = effective_columns(db, job)
    rules = RepairRules(**body.rules.model_dump())
    initial = body.initial_shift or job_initial_shift(job)
    try:
        result = enumerate_repairs(cols, table, rules,
                                   initial_shift=initial,
                                   ambiguous=body.columns)
    except ValueError as e:
        raise HTTPException(422, str(e))

    run = RepairRun(
        job_id=job_id, rules=json.dumps(body.rules.model_dump()),
        candidates=json.dumps([{
            "changes": {str(k): "".join(map(str, v))
                        for k, v in c.changes.items()},
            "text": c.text, "score": c.score} for c in result.candidates],
            ensure_ascii=False),
        ambiguous_columns=json.dumps(result.ambiguous_columns),
        truncated=result.truncated, message=result.message)
    db.add(run)
    db.commit()
    db.refresh(run)
    return RepairRunOut(
        id=run.id,
        candidates=[CandidateOut(
            changes={k: "".join(map(str, v)) for k, v in c.changes.items()},
            text=c.text, score=c.score) for c in result.candidates],
        ambiguous_columns=result.ambiguous_columns,
        truncated=result.truncated, message=result.message)


@router.get("/jobs/{job_id}/repairs", response_model=list[RepairRunOut])
def list_repairs(job_id: int, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    out = []
    for run in job.repair_runs:
        cands = json.loads(run.candidates)
        out.append(RepairRunOut(
            id=run.id,
            candidates=[CandidateOut(changes={int(k): v for k, v in
                                              c["changes"].items()},
                                     text=c["text"], score=c["score"])
                        for c in cands],
            ambiguous_columns=json.loads(run.ambiguous_columns),
            truncated=run.truncated, message=run.message))
    return out


# ------------------------------------------------------------------ 导出
@router.get("/jobs/{job_id}/export")
def export(job_id: int, format: str = "json", db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    if format == "json":
        return Response(export_json(db, job), media_type="application/json")
    if format == "txt":
        return Response(export_text(db, job), media_type="text/plain; "
                                                      "charset=utf-8")
    raise HTTPException(422, "format 只支持 json 或 txt")
