"""码表领域模型：ITA2（博多-默里码）与用户自定义码表。

码表把 5（或 N）位码字映射到字母移位（LTRS）与数字移位（FIGS）两个
字符集，并标记哪些码字是移位控制码。自定义码表允许任一层字符为空，
表示该码字在该移位状态下非法。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- ITA2 标准表
# 码字值按 bit0 = 第 1 道（最靠近纸带边缘的数据道）计算。
ITA2_LTRS: dict[int, str | None] = {
    0x00: "",     # Blank / Null
    0x01: "E", 0x02: "\n", 0x03: "A", 0x04: " ", 0x05: "S", 0x06: "I",
    0x07: "U", 0x08: "\r", 0x09: "D", 0x0A: "R", 0x0B: "J", 0x0C: "N",
    0x0D: "F", 0x0E: "C", 0x0F: "K", 0x10: "T", 0x11: "Z", 0x12: "L",
    0x13: "W", 0x14: "H", 0x15: "Y", 0x16: "P", 0x17: "Q", 0x18: "O",
    0x19: "B", 0x1A: "G", 0x1C: "M", 0x1D: "X", 0x1E: "V",
}
ITA2_FIGS: dict[int, str | None] = {
    0x00: "",
    0x01: "3", 0x02: "\n", 0x03: "-", 0x04: " ", 0x05: "'", 0x06: "8",
    0x07: "7", 0x08: "\r", 0x09: "$", 0x0A: "4", 0x0B: "\a", 0x0C: ",",
    0x0D: "!", 0x0E: ":", 0x0F: "(", 0x10: "5", 0x11: "+", 0x12: ")",
    0x13: "2", 0x14: "#", 0x15: "6", 0x16: "0", 0x17: "1", 0x18: "9",
    0x19: "?", 0x1A: "&", 0x1C: ".", 0x1D: "/", 0x1E: ";",
}
ITA2_LTRS_CODE = 0x1F  # 11111 切回字母层
ITA2_FIGS_CODE = 0x1B  # 11011 切到数字层


@dataclass
class CodeTable:
    """一张码表：两个移位层 + 移位控制码。"""

    name: str
    tracks: int = 5
    ltrs: dict[int, str | None] = field(default_factory=dict)
    figs: dict[int, str | None] = field(default_factory=dict)
    ltrs_code: int | None = ITA2_LTRS_CODE
    figs_code: int | None = ITA2_FIGS_CODE
    is_builtin: bool = False

    # ------------------------------------------------------------ 查询
    @property
    def max_code(self) -> int:
        return (1 << self.tracks) - 1

    def is_shift(self, code: int) -> bool:
        return code in (self.ltrs_code, self.figs_code)

    def shift_of(self, code: int) -> str | None:
        """返回该码字切换到的移位层名，非移位码返回 None。"""
        if code == self.ltrs_code:
            return "ltrs"
        if code == self.figs_code:
            return "figs"
        return None

    def lookup(self, code: int, shift: str) -> str | None:
        """查表；返回 None 表示该码字在此移位状态下非法。"""
        if code < 0 or code > self.max_code:
            return None
        layer = self.ltrs if shift == "ltrs" else self.figs
        return layer.get(code)

    def is_legal(self, code: int, shift: str) -> bool:
        if self.is_shift(code):
            return True
        return self.lookup(code, shift) is not None

    # ------------------------------------------------------------ 序列化
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tracks": self.tracks,
            "ltrs": {str(k): v for k, v in sorted(self.ltrs.items())},
            "figs": {str(k): v for k, v in sorted(self.figs.items())},
            "ltrs_code": self.ltrs_code,
            "figs_code": self.figs_code,
            "is_builtin": self.is_builtin,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CodeTable":
        return cls(
            name=data["name"],
            tracks=int(data.get("tracks", 5)),
            ltrs={int(k): v for k, v in (data.get("ltrs") or {}).items()},
            figs={int(k): v for k, v in (data.get("figs") or {}).items()},
            ltrs_code=data.get("ltrs_code"),
            figs_code=data.get("figs_code"),
            is_builtin=bool(data.get("is_builtin", False)),
        )


def ita2_table() -> CodeTable:
    """构造内置 ITA2 码表。"""
    return CodeTable(
        name="ITA2",
        tracks=5,
        ltrs=dict(ITA2_LTRS),
        figs=dict(ITA2_FIGS),
        ltrs_code=ITA2_LTRS_CODE,
        figs_code=ITA2_FIGS_CODE,
        is_builtin=True,
    )
