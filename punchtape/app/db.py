"""数据库引擎与会话管理。"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_DB = "sqlite:///" + os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "punchtape.db")

DATABASE_URL = os.environ.get("PUNCHTAPE_DB", DEFAULT_DB)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from .codetable import ita2_table
    from .models import CodeTableRow
    import json

    Base.metadata.create_all(engine)
    # 预置内置 ITA2 码表
    with SessionLocal() as s:
        if not s.query(CodeTableRow).filter_by(name="ITA2").first():
            t = ita2_table()
            s.add(CodeTableRow(name="ITA2", tracks=5,
                               definition=json.dumps(t.to_dict()),
                               is_builtin=True))
            s.commit()


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
