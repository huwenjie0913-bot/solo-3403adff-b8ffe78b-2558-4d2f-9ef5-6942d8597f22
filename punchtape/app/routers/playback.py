"""电传终端纸面回放路由。

只读回放识别任务的原始或修订孔阵：逐列驱动配置化的电传终端，返回每孔列
的纸面行列、控制事件、来源与机械/格式问题；快照存入 SQLite，不写回识别
任务与人工修订。支持不同终端配置/修订前后的纸面对比，以及 TXT/JSON/SVG
导出。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PlaybackSnapshot, RecognitionJob, TerminalProfile
from ..pipeline import effective_columns, job_initial_shift, load_code_table
from ..playback import PlayCell, PlayEvent, PlayResult, diff_results, play
from ..playback_export import build_playback_text, export_playback_svg
from ..schemas import (PlaybackConfigIn, PlaybackDiffRequest, PlaybackRequest,
                       PlaybackResultOut, ProfileCreate, ProfileOut,
                       SnapshotOut)
from ..terminal import TerminalConfig

router = APIRouter(tags=["playback"])


# ------------------------------------------------------------------ 工具
def _get_job(db: Session, job_id: int) -> RecognitionJob:
    job = db.get(RecognitionJob, job_id)
    if not job:
        raise HTTPException(404, "识别任务不存在")
    return job


def _get_profile(db: Session, profile_id: int | None) -> TerminalProfile | None:
    if profile_id is None:
        name = "Teletype Model 15 (45.45 波特)"
        return db.query(TerminalProfile).filter_by(name=name).first()
    prof = db.get(TerminalProfile, profile_id)
    if not prof:
        raise HTTPException(404, "终端配置不存在")
    return prof


def _build_config(profile: TerminalProfile | None,
                  overrides: PlaybackConfigIn) -> TerminalConfig:
    """以终端机型配置为底，请求中显式给出的字段覆盖。"""
    if profile is not None:
        cfg = TerminalConfig.from_dict(json.loads(profile.config))
    else:
        cfg = TerminalConfig()
    for key, value in overrides.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(cfg, key, value)
    try:
        cfg.validate()
    except ValueError as e:
        raise HTTPException(422, str(e))
    return cfg


def _job_codes(db: Session, job: RecognitionJob,
               source: str) -> tuple[list[int], list[str]]:
    cols = effective_columns(db, job)
    codes, origins = [], []
    for c in cols:
        if source == "revised":
            bits = c["bits"]
            origins.append("original" if not c["revised"] else "revised")
        elif source == "original":
            bits = tuple(int(b) for b in c["raw_bits"])
            origins.append("original")
        else:
            raise HTTPException(422, "source 只支持 revised / original")
        codes.append(sum((b & 1) << i for i, b in enumerate(bits)))
    return codes, origins


def _run(db: Session, job: RecognitionJob, req: PlaybackRequest):
    profile = _get_profile(db, req.profile_id)
    cfg = _build_config(profile, req.config)
    table, _ = load_code_table(db, job.code_table_id)
    codes, origins = _job_codes(db, job, req.source)
    initial = req.initial_shift or job_initial_shift(job)
    result = play(codes, table, cfg, source=req.source,
                  initial_shift=initial, column_sources=origins)
    return result, profile


def _snapshot_out(row: PlaybackSnapshot) -> SnapshotOut:
    result = json.loads(row.result)
    return SnapshotOut(
        id=row.id, job_id=row.job_id, profile_id=row.profile_id,
        name=row.name, source=row.source, config=json.loads(row.config),
        created_at=row.created_at.isoformat(), summary=result["summary"])


def _result_out(result, snapshot_id: int | None = None) -> PlaybackResultOut:
    data = result.to_dict()
    out = PlaybackResultOut(**data)
    out.snapshot_id = snapshot_id
    return out


def _result_from_payload(data: dict) -> PlayResult:
    """从快照 JSON 重建 PlayResult（用于 TXT/SVG 导出，保证与存档一致）。"""
    events = [PlayEvent(
        index=e["index"], code=e["code"], kind=e["kind"],
        char=e.get("char"), glyph=e.get("glyph", ""),
        shift=e["shift"], time_ms=e["time_ms"], row=e["row"],
        col=e["col"], phys_col=e["phys_col"], injected=e["injected"],
        source=e["source"], legal=e["legal"], reason=e.get("reason", ""),
        issues=list(e["issues"])) for e in data["events"]]
    cells = [PlayCell(
        row=c["row"], col=c["col"], char=c["char"], glyph=c["glyph"],
        column_index=c["column_index"], source=c["source"], code=c["code"],
        shift=c["shift"], legal=c["legal"], overstrike=c["overstrike"],
        overflow=c["overflow"], unsettled=c["unsettled"],
        layer=c["layer"]) for c in data["paper"]]
    s = data["summary"]
    return PlayResult(
        config=TerminalConfig.from_dict(data["config"]),
        source=data["source"], initial_shift=data["initial_shift"],
        char_ms=data["char_ms"], total_ms=data["total_ms"],
        rows=s["rows"], line_lengths=s["line_lengths"], events=events,
        cells=cells, issues=data["issues"],
        printed_chars=s["printed_chars"],
        overstrike_chars=s["overstrike_chars"],
        overflow_count=s["overflow_count"], cr_count=s["cr_count"],
        lf_count=s["lf_count"], bel_count=s["bel_count"])


# ------------------------------------------------------------------ 终端配置
@router.post("/terminal-profiles", response_model=ProfileOut, status_code=201)
def create_profile(body: ProfileCreate, db: Session = Depends(get_db)):
    if db.query(TerminalProfile).filter_by(name=body.name).first():
        raise HTTPException(409, f"终端配置 {body.name!r} 已存在")
    cfg = _build_config(None, body.config)
    row = TerminalProfile(name=body.name, config=json.dumps(cfg.to_dict()),
                          is_builtin=False)
    db.add(row)
    db.commit()
    db.refresh(row)
    return ProfileOut(id=row.id, name=row.name, is_builtin=False,
                      config=cfg.to_dict())


@router.get("/terminal-profiles", response_model=list[ProfileOut])
def list_profiles(db: Session = Depends(get_db)):
    return [ProfileOut(id=r.id, name=r.name, is_builtin=r.is_builtin,
                       config=json.loads(r.config))
            for r in db.query(TerminalProfile).order_by(
                TerminalProfile.is_builtin.desc(), TerminalProfile.id).all()]


@router.get("/terminal-profiles/{profile_id}", response_model=ProfileOut)
def get_profile(profile_id: int, db: Session = Depends(get_db)):
    row = db.get(TerminalProfile, profile_id)
    if not row:
        raise HTTPException(404, "终端配置不存在")
    return ProfileOut(id=row.id, name=row.name, is_builtin=row.is_builtin,
                      config=json.loads(row.config))


@router.delete("/terminal-profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    row = db.get(TerminalProfile, profile_id)
    if not row:
        raise HTTPException(404, "终端配置不存在")
    if row.is_builtin:
        raise HTTPException(409, "内置终端机型不可删除")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ 回放
@router.post("/jobs/{job_id}/playback", response_model=PlaybackResultOut,
             status_code=201)
def run_playback(job_id: int, body: PlaybackRequest,
                 db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    result, profile = _run(db, job, body)
    snapshot_id = None
    if body.save_as:
        snap = PlaybackSnapshot(
            job_id=job_id, profile_id=profile.id if profile else None,
            name=body.save_as, source=body.source,
            config=json.dumps(result.config.to_dict()),
            result=json.dumps(result.to_dict(), ensure_ascii=False))
        db.add(snap)
        db.commit()
        db.refresh(snap)
        snapshot_id = snap.id
    return _result_out(result, snapshot_id)


@router.get("/jobs/{job_id}/playback/snapshots",
            response_model=list[SnapshotOut])
def list_snapshots(job_id: int, db: Session = Depends(get_db)):
    _get_job(db, job_id)
    rows = db.query(PlaybackSnapshot).filter_by(job_id=job_id)\
        .order_by(PlaybackSnapshot.created_at).all()
    return [_snapshot_out(r) for r in rows]


@router.get("/playback/snapshots/{snapshot_id}",
            response_model=PlaybackResultOut)
def get_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    row = db.get(PlaybackSnapshot, snapshot_id)
    if not row:
        raise HTTPException(404, "回放快照不存在")
    data = json.loads(row.result)
    data["snapshot_id"] = row.id
    return PlaybackResultOut(**data)


@router.delete("/playback/snapshots/{snapshot_id}", status_code=204)
def delete_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    row = db.get(PlaybackSnapshot, snapshot_id)
    if not row:
        raise HTTPException(404, "回放快照不存在")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.get("/playback/snapshots/{snapshot_id}/export")
def export_snapshot(snapshot_id: int, format: str = "txt",
                    db: Session = Depends(get_db)):
    row = db.get(PlaybackSnapshot, snapshot_id)
    if not row:
        raise HTTPException(404, "回放快照不存在")
    if format not in ("txt", "json", "svg"):
        raise HTTPException(422, "format 只支持 txt / json / svg")
    # 从存档 JSON 还原展示内容（不再重算，保证导出与快照逐字节一致）
    payload = json.loads(row.result)
    profile_name = None
    if row.profile_id is not None:
        prof = db.get(TerminalProfile, row.profile_id)
        profile_name = prof.name if prof else None
    if format == "json":
        out = json.dumps({"job_id": row.job_id, "snapshot_id": row.id,
                          "profile_name": profile_name, **payload},
                         ensure_ascii=False, indent=2)
        return Response(out, media_type="application/json")
    result_obj = _result_from_payload(payload)
    if format == "txt":
        out = build_playback_text(result_obj, row.job_id, profile_name)
        return Response(out, media_type="text/plain; charset=utf-8")
    out = export_playback_svg(result_obj, row.job_id, profile_name)
    return Response(out, media_type="image/svg+xml")


# ------------------------------------------------------------------ 对比
@router.post("/jobs/{job_id}/playback/diff")
def diff_playback(job_id: int, body: PlaybackDiffRequest,
                  db: Session = Depends(get_db)):
    """同一任务按 a/b 两组回放参数比较纸面（可比较终端配置或修订前后）。"""
    job = _get_job(db, job_id)
    result_a, _ = _run(db, job, body.a)
    result_b, _ = _run(db, job, body.b)
    return diff_results(result_a, result_b)
