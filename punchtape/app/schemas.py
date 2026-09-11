"""API 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ 纸带
class TapeCreate(BaseModel):
    name: str
    tape_width_mm: float = 17.4
    tracks: int = 5
    dpi: float = 300.0
    sprocket_after_track: int = 2
    notes: str = ""


class TapeOut(TapeCreate):
    id: int
    segment_count: int = 0

    model_config = {"from_attributes": True}


# ------------------------------------------------------------------ 码表
class CodeTableCreate(BaseModel):
    name: str
    tracks: int = 5
    ltrs: dict[str, str | None] = Field(default_factory=dict,
                                        description="码字值(字符串整数)->字母层字符")
    figs: dict[str, str | None] = Field(default_factory=dict,
                                        description="码字值(字符串整数)->数字层字符")
    ltrs_code: int | None = 31
    figs_code: int | None = 27


class CodeTableOut(BaseModel):
    id: int
    name: str
    tracks: int
    is_builtin: bool
    definition: dict


# ------------------------------------------------------------------ 识别
class RecognizeRequest(BaseModel):
    code_table_id: int | None = None      # 缺省用内置 ITA2
    tracks: int | None = None             # 缺省取纸带参数
    dpi: float | None = None
    tape_width_mm: float | None = None
    sprocket_after_track: int | None = None
    deskew: bool = True
    perspective_corners: list[list[float]] | None = None
    sprocket_y_hint: float | None = None
    bit_order: str = "lsb_first"
    initial_shift: str = "ltrs"
    min_overlap: int = 3


class ColumnOut(BaseModel):
    index: int
    bits: str
    code: int
    confidence: float
    bit_confidence: list[float]
    bit_positions: list[list[float]]
    sources: list[list[int]]
    alternatives: list[str]
    sprocket_present: bool
    revised_bits: str | None = None
    locked: bool = False


class DiagnosticOut(BaseModel):
    type: str
    severity: str
    column_index: int
    message: str
    details: dict


class JobOut(BaseModel):
    id: int
    tape_id: int
    code_table_id: int
    status: str
    column_count: int
    diagnostic_count: int
    text_preview: str


# ------------------------------------------------------------------ 修订
class RevisionCreate(BaseModel):
    column_index: int
    revised_bits: str = Field(description="新的位串，如 '01101'，长度=孔道数")
    locked: bool = False
    author: str = ""
    reason: str = ""


class RevisionOut(RevisionCreate):
    id: int
    original_bits: str
    created_at: str


# ------------------------------------------------------------------ 修复
class RepairRulesIn(BaseModel):
    text_regex: str | None = None
    allowed_chars: str | None = None
    max_shift_changes: int | None = None
    must_contain: list[str] = Field(default_factory=list)
    max_flips_per_column: int = 2
    conf_threshold: float = 0.6
    max_candidates: int = 2000


class RepairRequest(BaseModel):
    rules: RepairRulesIn = Field(default_factory=RepairRulesIn)
    initial_shift: str = "ltrs"
    columns: list[int] | None = Field(
        default=None, description="指定待修复列；缺省自动选取可疑列")


class CandidateOut(BaseModel):
    changes: dict[int, str]               # 列号 -> 新位串
    text: str
    score: float


class RepairRunOut(BaseModel):
    id: int
    candidates: list[CandidateOut]
    ambiguous_columns: list[int]
    truncated: bool
    message: str
