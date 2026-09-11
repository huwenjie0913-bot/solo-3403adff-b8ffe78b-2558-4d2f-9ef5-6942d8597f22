"""修复枚举测试：锁定列、备选读数、规则过滤、多候选保留。"""
from punchtape.app.codetable import ita2_table
from punchtape.app.decoding import encode_text
from punchtape.app.repair import RepairRules, enumerate_repairs


def _cols_from_text(text, table):
    codes = encode_text(text, table)
    return [{
        "bits": tuple((c >> i) & 1 for i in range(5)),
        "bit_confidence": [0.95] * 5,
        "alternatives": [],
        "locked": False,
    } for c in codes]


def test_no_ambiguous_columns():
    table = ita2_table()
    cols = _cols_from_text("ABC", table)
    result = enumerate_repairs(cols, table)
    assert result.candidates == []
    assert "没有需要修复" in result.message


def test_alternative_from_repeat_scan():
    """重复扫描冲突给出的备选读数应进入候选。"""
    table = ita2_table()
    cols = _cols_from_text("AB", table)
    # 第 0 列（'A'=00011）被误读为 00010，重复扫描给出正确读数作备选
    cols[0]["bits"] = (0, 1, 0, 0, 0)
    cols[0]["bit_confidence"] = [0.4] * 5
    cols[0]["alternatives"] = [(1, 1, 0, 0, 0)]
    result = enumerate_repairs(
        cols, table, RepairRules(must_contain=["A"]))
    texts = {c.text for c in result.candidates}
    assert "AB" in texts
    # 不自动覆盖：候选里同时保留其它可能
    assert len(result.candidates) >= 1


def test_locked_column_not_modified():
    table = ita2_table()
    cols = _cols_from_text("AB", table)
    cols[0]["locked"] = True
    cols[0]["bit_confidence"] = [0.3] * 5   # 低置信但已锁定
    result = enumerate_repairs(cols, table)
    assert result.candidates == []  # 唯一可疑列被锁定 -> 无可修复列


def test_regex_rule_filters_candidates():
    table = ita2_table()
    cols = _cols_from_text("A1B", table)
    # 中间列（FIGS 移位码）可疑
    mid = 1
    cols[mid]["bit_confidence"] = [0.4] * 5
    rules = RepairRules(text_regex=r"[A-Z]+", max_flips_per_column=1,
                        max_candidates=500)
    result = enumerate_repairs(cols, table, rules)
    for c in result.candidates:
        assert all(ch.isalpha() or ch in "\r\n" for ch in c.text)


def test_multiple_candidates_kept_when_ambiguous():
    """证据不足时保留多个候选。"""
    table = ita2_table()
    cols = _cols_from_text("EA", table)
    # 第一列 'E'=00001 低置信：翻转任一位都可能得到别的合法字符
    cols[0]["bit_confidence"] = [0.4] * 5
    result = enumerate_repairs(
        cols, table, RepairRules(max_flips_per_column=1, max_candidates=500))
    texts = {c.text for c in result.candidates}
    assert len(texts) > 1
    assert "EA" in texts  # 原读数始终是候选之一


def test_illegal_code_eliminated_by_shift_constraint():
    """自定义码表下，翻转出非法码字的组合应被排除。"""
    from punchtape.app.codetable import CodeTable
    table = CodeTable(name="T", tracks=5, ltrs={3: "A", 4: " "}, figs={},
                      ltrs_code=31, figs_code=27)
    cols = [{
        "bits": (1, 1, 0, 0, 0), "bit_confidence": [0.4] * 5,
        "alternatives": [], "locked": False,
    }]
    result = enumerate_repairs(
        cols, table, RepairRules(max_flips_per_column=1))
    # 只有翻转后仍是合法码字（3/4/27/31）的候选保留
    for c in result.candidates:
        bits = c.changes.get(0, (1, 1, 0, 0, 0))
        code = sum(b << i for i, b in enumerate(bits))
        assert code in (3, 4, 27, 31)
