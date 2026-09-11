"""按字母/数字移位状态解码孔列，并产生诊断记录。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .codetable import CodeTable


@dataclass
class Diagnostic:
    type: str                 # illegal_codeword / redundant_shift / long_figs_run /
                              # overlap_conflict / missing_hole / torn_region /
                              # low_confidence / shift_at_end ...
    severity: str             # info / warning / error
    column_index: int
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class DecodedChar:
    index: int                # 孔列序号
    code: int
    shift: str                # 解码时生效的移位状态
    char: str                 # 解码字符；移位码为空串；非法为 None
    legal: bool


@dataclass
class DecodeResult:
    chars: list[DecodedChar]
    diagnostics: list[Diagnostic]

    @property
    def text(self) -> str:
        return "".join(c.char for c in self.chars if c.char)


def decode(codes: list[int], table: CodeTable,
           confidences: list[float] | None = None,
           initial_shift: str = "ltrs",
           low_conf_threshold: float = 0.5,
           long_figs_run: int = 12) -> DecodeResult:
    """解码一列码字序列，跟踪 LTRS/FIGS 移位状态并生成诊断。"""
    chars: list[DecodedChar] = []
    diags: list[Diagnostic] = []
    shift = initial_shift
    figs_run = 0

    for i, code in enumerate(codes):
        conf = confidences[i] if confidences else 1.0
        if conf < low_conf_threshold:
            diags.append(Diagnostic(
                type="low_confidence", severity="warning", column_index=i,
                message=f"第 {i} 列识读置信度低（{conf:.2f}）",
                details={"confidence": conf, "code": code}))

        target = table.shift_of(code)
        if target is not None:
            if target == shift:
                diags.append(Diagnostic(
                    type="redundant_shift", severity="info", column_index=i,
                    message=f"第 {i} 列出现冗余移位码（已处于 "
                            f"{'字母' if shift == 'ltrs' else '数字'}状态）",
                    details={"code": code, "shift": shift}))
            shift = target
            figs_run = 0
            chars.append(DecodedChar(i, code, shift, "", True))
            continue

        ch = table.lookup(code, shift)
        if ch is None:
            diags.append(Diagnostic(
                type="illegal_codeword", severity="error", column_index=i,
                message=f"第 {i} 列码字 {code:0{table.tracks}b} 在"
                        f"{'字母' if shift == 'ltrs' else '数字'}状态下非法",
                details={"code": code, "shift": shift}))
            chars.append(DecodedChar(i, code, shift, "", False))
            continue

        if shift == "figs":
            figs_run += 1
            if figs_run == long_figs_run:
                diags.append(Diagnostic(
                    type="long_figs_run", severity="warning", column_index=i,
                    message=f"第 {i} 列起数字移位已连续 {figs_run} 字符，"
                            "可能缺失 LTRS 移位（移位状态中断）",
                    details={"run_length": figs_run}))
        else:
            figs_run = 0
        chars.append(DecodedChar(i, code, shift, ch, True))

    if shift != initial_shift and codes:
        diags.append(Diagnostic(
            type="shift_at_end", severity="info", column_index=len(codes) - 1,
            message="纸带结束于数字移位状态",
            details={"final_shift": shift}))

    return DecodeResult(chars=chars, diagnostics=diags)


def encode_text(text: str, table: CodeTable,
                initial_shift: str = "ltrs") -> list[int]:
    """把文本编码为码字序列（自动插入移位码）。测试与校验用。"""
    rev_l = {v: k for k, v in table.ltrs.items() if v}
    rev_f = {v: k for k, v in table.figs.items() if v}
    codes: list[int] = []
    shift = initial_shift
    for ch in text:
        if ch in (rev_l if shift == "ltrs" else rev_f):
            codes.append((rev_l if shift == "ltrs" else rev_f)[ch])
        elif ch in rev_l:
            if shift != "ltrs":
                codes.append(table.ltrs_code)
                shift = "ltrs"
            codes.append(rev_l[ch])
        elif ch in rev_f:
            if shift != "figs":
                codes.append(table.figs_code)
                shift = "figs"
            codes.append(rev_f[ch])
        else:
            raise ValueError(f"字符 {ch!r} 不在码表 {table.name} 中")
    return codes
