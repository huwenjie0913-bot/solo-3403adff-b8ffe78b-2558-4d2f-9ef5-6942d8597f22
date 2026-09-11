"""码表路由：内置 ITA2 + 用户自定义。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..codetable import CodeTable
from ..db import get_db
from ..models import CodeTableRow
from ..schemas import CodeTableCreate, CodeTableOut

router = APIRouter(prefix="/codetables", tags=["codetables"])


@router.post("", response_model=CodeTableOut, status_code=201)
def create_code_table(body: CodeTableCreate, db: Session = Depends(get_db)):
    if db.query(CodeTableRow).filter_by(name=body.name).first():
        raise HTTPException(409, f"码表 {body.name!r} 已存在")
    table = CodeTable(
        name=body.name, tracks=body.tracks,
        ltrs={int(k): v for k, v in body.ltrs.items()},
        figs={int(k): v for k, v in body.figs.items()},
        ltrs_code=body.ltrs_code, figs_code=body.figs_code)
    # 校验：码字值不得超出道数范围
    for code in list(table.ltrs) + list(table.figs):
        if code < 0 or code > table.max_code:
            raise HTTPException(422, f"码字 {code} 超出 {body.tracks} 道范围")
    row = CodeTableRow(name=table.name, tracks=table.tracks,
                       definition=json.dumps(table.to_dict()),
                       is_builtin=False)
    db.add(row)
    db.commit()
    db.refresh(row)
    return CodeTableOut(id=row.id, name=row.name, tracks=row.tracks,
                        is_builtin=False, definition=table.to_dict())


@router.get("", response_model=list[CodeTableOut])
def list_code_tables(db: Session = Depends(get_db)):
    return [CodeTableOut(id=r.id, name=r.name, tracks=r.tracks,
                         is_builtin=r.is_builtin,
                         definition=json.loads(r.definition))
            for r in db.query(CodeTableRow).all()]


@router.get("/{table_id}", response_model=CodeTableOut)
def get_code_table(table_id: int, db: Session = Depends(get_db)):
    row = db.get(CodeTableRow, table_id)
    if not row:
        raise HTTPException(404, "码表不存在")
    return CodeTableOut(id=row.id, name=row.name, tracks=row.tracks,
                        is_builtin=row.is_builtin,
                        definition=json.loads(row.definition))
