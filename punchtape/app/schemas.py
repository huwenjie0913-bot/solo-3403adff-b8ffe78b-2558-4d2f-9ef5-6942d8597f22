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
    initial_shift: str | None = Field(
        default=None, description="缺省沿用识别任务的 initial_shift")
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


# ------------------------------------------------------------------ 终端配置
class PlaybackConfigIn(BaseModel):
    """回放终端配置；全部可选，缺省取内置 Teletype Model 15 参数。"""
    columns: int | None = Field(default=None, description="每行字数")
    baud: float | None = Field(default=None, description="传输速率（波特）")
    data_bits: int | None = None
    start_bit: bool | None = None
    stop_units: float | None = Field(default=None, description="停止位单位数")
    cr_ms: float | None = Field(default=None, description="字车回程时间 ms")
    lf_ms: float | None = Field(default=None, description="走纸时间 ms")
    bell_ms: float | None = None
    nul_ms: float | None = None
    auto_lf: bool | None = Field(default=None, description="CR 自动附加 LF")
    auto_cr: bool | None = Field(default=None, description="LF 自动附加 CR")
    overflow: str | None = Field(
        default=None, description="超行宽：mark / wrap / truncate")
    initial_shift: str | None = None


class ProfileCreate(BaseModel):
    name: str
    config: PlaybackConfigIn = Field(default_factory=PlaybackConfigIn)


class ProfileOut(BaseModel):
    id: int
    name: str
    is_builtin: bool
    config: dict

    model_config = {"from_attributes": True}


class PlaybackRequest(BaseModel):
    source: str = Field(default="revised",
                        description="revised=修订孔阵（缺省），original=原始识别孔阵")
    profile_id: int | None = Field(
        default=None, description="终端配置存档 id；缺省用内置 Model 15")
    config: PlaybackConfigIn = Field(default_factory=PlaybackConfigIn)
    initial_shift: str | None = Field(
        default=None, description="缺省沿用识别任务的初始移位")
    save_as: str | None = Field(
        default=None, description="提供时把本次回放快照存入 SQLite")


class PlaybackSummary(BaseModel):
    rows: int
    line_lengths: list[int]
    events: int
    printed_chars: int
    overstrike_chars: int
    overflow_count: int
    cr_count: int
    lf_count: int
    bel_count: int
    issue_count: int


class PlaybackResultOut(BaseModel):
    source: str
    initial_shift: str
    char_ms: float
    total_ms: float
    config: dict
    summary: PlaybackSummary
    events: list[dict]
    paper: list[dict]
    issues: list[dict]
    snapshot_id: int | None = None


class SnapshotOut(BaseModel):
    id: int
    job_id: int
    profile_id: int | None
    name: str
    source: str
    config: dict
    created_at: str
    summary: dict


class PlaybackDiffRequest(BaseModel):
    """以 a/b 两组参数各跑一次回放并比较（须针对同一识别任务）。"""
    a: PlaybackRequest
    b: PlaybackRequest
