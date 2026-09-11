"""电传终端配置：每行字数、传输速率、回程/走纸机构时延与自动换行规则。

时序模型（以毫秒为单位）：
  - 每个孔列在线路上占一个字符时间
        char_ms = (data_bits + 起始位 + 停止位单位) / baud * 1000
    第 i 列到达终端的时刻为 i * char_ms。
  - CR（字车回程）与 LF（走纸）是机械动作，分别在 cr_ms / lf_ms 后完成；
    回程途中字车物理位置按线性插值移动，若此时刻已有字符到达即重打。
  - auto_lf：终端收到 CR 时自动附加一次 LF（部分电传机的换行开关）。
  - auto_cr：终端收到 LF 时自动附加一次 CR。
"""
from __future__ import annotations

from dataclasses import dataclass, fields

OVERFLOW_MODES = ("mark", "wrap", "truncate")


@dataclass
class TerminalConfig:
    columns: int = 72            # 每行字数（右边界）
    baud: float = 45.45          # 传输速率，波特
    data_bits: int = 5           # 数据位（5 单位纸带为 5）
    start_bit: bool = True       # 是否计起始位
    stop_units: float = 1.42     # 停止位单位数（Model 15 为 1.42）
    cr_ms: float = 200.0         # 字车回程时间
    lf_ms: float = 45.0          # 走纸一行时间
    bell_ms: float = 150.0       # 振铃机构时长（仅记录）
    nul_ms: float = 0.0          # NUL/Blank 处理时延
    auto_lf: bool = False        # CR 自动附加 LF
    auto_cr: bool = False        # LF 自动附加 CR
    overflow: str = "mark"       # 超行宽处理：mark 标记 / wrap 自动换行 /
                                 #                truncate 丢弃
    initial_shift: str = "ltrs"  # 上电初始移位状态

    @property
    def char_ms(self) -> float:
        units = self.data_bits + (1 if self.start_bit else 0) + self.stop_units
        return units / self.baud * 1000.0

    def validate(self) -> None:
        if self.columns < 1:
            raise ValueError("每行字数须 >= 1")
        if self.baud <= 0:
            raise ValueError("传输速率须为正数")
        if self.data_bits < 1:
            raise ValueError("数据位数须 >= 1")
        if self.stop_units < 0 or self.cr_ms < 0 or self.lf_ms < 0 \
                or self.bell_ms < 0 or self.nul_ms < 0:
            raise ValueError("时序参数不得为负")
        if self.overflow not in OVERFLOW_MODES:
            raise ValueError(f"overflow 只支持 {OVERFLOW_MODES}")
        if self.initial_shift not in ("ltrs", "figs"):
            raise ValueError("initial_shift 只支持 ltrs / figs")

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "TerminalConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------- 内置终端机型
# 参数取常见机电式电传机的典型值，用于不同配置的对比。
BUILTIN_PROFILES: list[dict] = [
    {
        "name": "Teletype Model 15 (45.45 波特)",
        "is_builtin": True,
        "config": TerminalConfig(
            columns=72, baud=45.45, data_bits=5, stop_units=1.42,
            cr_ms=200.0, lf_ms=45.0, bell_ms=180.0),
    },
    {
        "name": "Creed 7B (50 波特)",
        "is_builtin": True,
        "config": TerminalConfig(
            columns=69, baud=50.0, data_bits=5, stop_units=1.0,
            cr_ms=150.0, lf_ms=30.0, bell_ms=150.0),
    },
    {
        "name": "Siemens T100 (50 波特, CR 自动换行)",
        "is_builtin": True,
        "config": TerminalConfig(
            columns=72, baud=50.0, data_bits=5, stop_units=1.5,
            cr_ms=180.0, lf_ms=40.0, auto_lf=True),
    },
    {
        "name": "通用 75 波特终端",
        "is_builtin": True,
        "config": TerminalConfig(
            columns=80, baud=75.0, data_bits=5, stop_units=1.0,
            cr_ms=100.0, lf_ms=20.0, bell_ms=120.0),
    },
]
