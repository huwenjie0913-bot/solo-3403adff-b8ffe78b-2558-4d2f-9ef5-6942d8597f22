"""数字修复：在锁定孔位与校验规则约束下枚举补孔/去孔候选。

原则：证据不足时保留多个候选结果，绝不自动覆盖识别结果。
候选来源：
  1. 重复扫描冲突给出的备选读数（alternatives）；
  2. 低置信位的翻转（补孔 0->1 或去孔 1->0），每列最多翻转 max_flips 位；
约束：
  - 锁定列（人工确认）不得改动；
  - 解码全程不得出现非法码字；
  - 用户校验规则：解码文本正则、允许字符集、最大移位次数、必含子串。
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from .codetable import CodeTable
from .decoding import decode


@dataclass
class RepairRules:
    text_regex: str | None = None        # 解码文本（忽略移位码）须整体匹配
    allowed_chars: str | None = None     # 解码字符必须落在该集合内
    max_shift_changes: int | None = None # 移位码总数上限
    must_contain: list[str] = field(default_factory=list)  # 必含子串
    max_flips_per_column: int = 2        # 每列最多翻转位数
    conf_threshold: float = 0.6          # 低于该置信度的位才可翻转
    max_candidates: int = 2000           # 候选数量上限


@dataclass
class CandidateColumn:
    index: int
    bits_options: list[tuple[int, ...]]   # 该列可选位型（含原读数）


@dataclass
class RepairCandidate:
    changes: dict[int, tuple[int, ...]]   # 列号 -> 新位型（仅含被修改的列）
    text: str
    score: float                          # 越高越可信


@dataclass
class RepairResult:
    candidates: list[RepairCandidate]
    ambiguous_columns: list[int]
    truncated: bool                       # 枚举是否因上限被截断
    message: str = ""


def _code_of(bits: tuple[int, ...]) -> int:
    v = 0
    for i, b in enumerate(bits):
        v |= (b & 1) << i
    return v


def _variants(bits: tuple[int, ...], bit_conf: list[float],
              rules: RepairRules) -> list[tuple[int, ...]]:
    """对一列生成备选位型：原样 + 低置信位的 1..max_flips 翻转组合。"""
    options = [tuple(bits)]
    flippable = [i for i, c in enumerate(bit_conf) if c < rules.conf_threshold]
    for k in range(1, rules.max_flips_per_column + 1):
        for combo in itertools.combinations(flippable, k):
            alt = list(bits)
            for i in combo:
                alt[i] ^= 1
            t = tuple(alt)
            if t not in options:
                options.append(t)
    return options


def _check_rules(codes: list[int], table: CodeTable,
                 rules: RepairRules, initial_shift: str) -> tuple[bool, str, float]:
    """解码并校验规则；返回 (通过?, 文本, 得分)。"""
    result = decode(codes, table, initial_shift=initial_shift)
    if any(not c.legal for c in result.chars):
        return False, "", 0.0
    text = result.text
    if rules.allowed_chars is not None:
        allowed = set(rules.allowed_chars)
        if any(ch not in allowed for ch in text if ch not in "\r\n"):
            return False, "", 0.0
    if rules.text_regex is not None:
        try:
            if not re.fullmatch(rules.text_regex, text, flags=re.DOTALL):
                return False, "", 0.0
        except re.error:
            return False, "", 0.0
    for sub in rules.must_contain:
        if sub not in text:
            return False, "", 0.0
    if rules.max_shift_changes is not None:
        shifts = sum(1 for c in result.chars
                     if table.shift_of(c.code) is not None and c.code != -1)
        # 只统计真正改变状态的移位
        real = 0
        shift = initial_shift
        for c in result.chars:
            tgt = table.shift_of(c.code)
            if tgt and tgt != shift:
                real += 1
                shift = tgt
        if real > rules.max_shift_changes:
            return False, "", 0.0
    # 得分：非法诊断越少、文本越完整越高
    errors = sum(1 for d in result.diagnostics if d.severity == "error")
    return True, text, 1.0 - 0.1 * errors


def enumerate_repairs(
    columns: list[dict],           # [{bits, bit_confidence, alternatives, locked}]
    table: CodeTable,
    rules: RepairRules | None = None,
    initial_shift: str = "ltrs",
    ambiguous: list[int] | None = None,
) -> RepairResult:
    """枚举修复候选。columns 为合并后的孔列（含置信度与备选读数）。"""
    rules = rules or RepairRules()
    n = len(columns)

    if ambiguous is None:
        ambiguous = [i for i, c in enumerate(columns)
                     if not c.get("locked")
                     and (min(c["bit_confidence"]) < rules.conf_threshold
                          or c.get("alternatives"))]

    option_lists: list[tuple[int, list[tuple[int, ...]]]] = []
    for i in ambiguous:
        c = columns[i]
        if c.get("locked"):
            continue
        opts = [tuple(a) for a in c.get("alternatives", [])]
        opts += _variants(tuple(c["bits"]), c["bit_confidence"], rules)
        # 去重且保留原读数在首位
        seen, uniq = set(), []
        for o in [tuple(c["bits"])] + opts:
            if o not in seen:
                seen.add(o)
                uniq.append(o)
        if len(uniq) > 1:
            option_lists.append((i, uniq))

    if not option_lists:
        return RepairResult(candidates=[], ambiguous_columns=[],
                            truncated=False, message="没有需要修复的可疑列")

    base_codes = [_code_of(tuple(c["bits"])) for c in columns]
    candidates: list[RepairCandidate] = []
    truncated = False

    idxs = [i for i, _ in option_lists]
    pools = [opts for _, opts in option_lists]
    total = 1
    for p in pools:
        total *= len(p)
    if total > rules.max_candidates * 4:
        truncated = True

    count = 0
    for combo in itertools.product(*pools):
        count += 1
        if count > rules.max_candidates * 4:
            truncated = True
            break
        codes = list(base_codes)
        changes: dict[int, tuple[int, ...]] = {}
        for i, bits in zip(idxs, combo):
            if bits != tuple(columns[i]["bits"]):
                changes[i] = bits
            codes[i] = _code_of(bits)
        ok, text, score = _check_rules(codes, table, rules, initial_shift)
        if ok:
            candidates.append(RepairCandidate(changes=changes, text=text,
                                              score=round(score, 3)))
        if len(candidates) >= rules.max_candidates:
            truncated = True
            break

    # 原读数本身若合法应始终在候选中（changes 为空）
    candidates.sort(key=lambda c: (len(c.changes), -c.score))
    return RepairResult(
        candidates=candidates,
        ambiguous_columns=idxs,
        truncated=truncated,
        message="" if candidates else "没有满足约束的修复候选",
    )
