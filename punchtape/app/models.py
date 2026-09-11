"""SQLite 持久化：纸带、扫描段、码表、识别任务、孔列、解码字符、诊断、
人工修订、修复存档、电传终端配置与纸面回放快照。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tape(Base):
    __tablename__ = "tapes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    tape_width_mm: Mapped[float] = mapped_column(Float, default=17.4)
    tracks: Mapped[int] = mapped_column(Integer, default=5)
    dpi: Mapped[float] = mapped_column(Float, default=300.0)
    sprocket_after_track: Mapped[int] = mapped_column(Integer, default=2)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    segments: Mapped[list["ScanSegment"]] = relationship(
        back_populates="tape", cascade="all, delete-orphan",
        order_by="ScanSegment.order")
    jobs: Mapped[list["RecognitionJob"]] = relationship(
        back_populates="tape", cascade="all, delete-orphan")


class ScanSegment(Base):
    __tablename__ = "scan_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    tape_id: Mapped[int] = mapped_column(ForeignKey("tapes.id"))
    order: Mapped[int] = mapped_column(Integer, default=0)
    image_path: Mapped[str] = mapped_column(String(500))
    width_px: Mapped[int] = mapped_column(Integer, default=0)
    height_px: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tape: Mapped[Tape] = relationship(back_populates="segments")


class CodeTableRow(Base):
    __tablename__ = "code_tables"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    tracks: Mapped[int] = mapped_column(Integer, default=5)
    definition: Mapped[str] = mapped_column(Text)  # JSON: CodeTable.to_dict()
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RecognitionJob(Base):
    __tablename__ = "recognition_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tape_id: Mapped[int] = mapped_column(ForeignKey("tapes.id"))
    code_table_id: Mapped[int] = mapped_column(ForeignKey("code_tables.id"))
    params: Mapped[str] = mapped_column(Text, default="{}")  # JSON 识别参数
    status: Mapped[str] = mapped_column(String(20), default="done")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tape: Mapped[Tape] = relationship(back_populates="jobs")
    columns: Mapped[list["HoleColumnRow"]] = relationship(
        back_populates="job", cascade="all, delete-orphan",
        order_by="HoleColumnRow.index")
    diagnostics: Mapped[list["DiagnosticRow"]] = relationship(
        back_populates="job", cascade="all, delete-orphan")
    revisions: Mapped[list["Revision"]] = relationship(
        back_populates="job", cascade="all, delete-orphan",
        order_by="Revision.created_at")
    repair_runs: Mapped[list["RepairRun"]] = relationship(
        back_populates="job", cascade="all, delete-orphan")
    playback_snapshots: Mapped[list["PlaybackSnapshot"]] = relationship(
        back_populates="job", cascade="all, delete-orphan")


class HoleColumnRow(Base):
    """合并后的孔列；可逐列追溯到来源扫描段与原图坐标。"""

    __tablename__ = "hole_columns"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("recognition_jobs.id"))
    index: Mapped[int] = mapped_column(Integer)
    bits: Mapped[str] = mapped_column(String(16))           # 如 "01101"
    confidence: Mapped[float] = mapped_column(Float)
    bit_confidence: Mapped[str] = mapped_column(Text)       # JSON list
    bit_positions: Mapped[str] = mapped_column(Text)        # JSON [[x,y]...] 原图坐标
    sources: Mapped[str] = mapped_column(Text)              # JSON [[segment_id, 列号]...]
    alternatives: Mapped[str] = mapped_column(Text, default="[]")  # JSON 冲突备选
    sprocket_present: Mapped[bool] = mapped_column(Boolean, default=True)

    job: Mapped[RecognitionJob] = relationship(back_populates="columns")


class DiagnosticRow(Base):
    __tablename__ = "diagnostics"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("recognition_jobs.id"))
    type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    column_index: Mapped[int] = mapped_column(Integer, default=-1)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[str] = mapped_column(Text, default="{}")

    job: Mapped[RecognitionJob] = relationship(back_populates="diagnostics")


class Revision(Base):
    """人工修订：锁定/修改某列孔位。多次修订按时间叠加，最新生效。"""

    __tablename__ = "revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("recognition_jobs.id"))
    column_index: Mapped[int] = mapped_column(Integer)
    original_bits: Mapped[str] = mapped_column(String(16))
    revised_bits: Mapped[str] = mapped_column(String(16))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    author: Mapped[str] = mapped_column(String(100), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[RecognitionJob] = relationship(back_populates="revisions")


class RepairRun(Base):
    """一次修复枚举的结果存档（候选不自动应用）。"""

    __tablename__ = "repair_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("recognition_jobs.id"))
    rules: Mapped[str] = mapped_column(Text, default="{}")
    candidates: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    ambiguous_columns: Mapped[str] = mapped_column(Text, default="[]")
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[RecognitionJob] = relationship(back_populates="repair_runs")


class TerminalProfile(Base):
    """电传终端配置（内置机型或用户保存的配置）。"""

    __tablename__ = "terminal_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    config: Mapped[str] = mapped_column(Text)     # JSON: TerminalConfig.to_dict()
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlaybackSnapshot(Base):
    """一次纸面回放的只读快照；不影响识别任务与人工修订。"""

    __tablename__ = "playback_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("recognition_jobs.id"))
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("terminal_profiles.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(10), default="revised")
    config: Mapped[str] = mapped_column(Text)       # JSON 终端配置
    result: Mapped[str] = mapped_column(Text)       # JSON PlayResult.to_dict()
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[RecognitionJob] = relationship(
        back_populates="playback_snapshots")
