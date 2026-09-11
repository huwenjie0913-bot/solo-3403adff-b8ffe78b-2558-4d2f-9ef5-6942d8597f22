"""码表与解码单元测试。"""
import pytest

from punchtape.app.codetable import ita2_table, CodeTable
from punchtape.app.decoding import decode, encode_text


def test_ita2_roundtrip():
    table = ita2_table()
    text = "HELLO 123 WORLD"
    codes = encode_text(text, table)
    result = decode(codes, table)
    assert result.text == text


def test_ita2_shift_codes_inserted():
    table = ita2_table()
    codes = encode_text("A1", table)
    # A(字母层) -> FIGS -> 1 -> LTRS
    assert table.figs_code in codes
    assert codes[-1] == table.ltrs_code or codes[0] == 0x03


def test_illegal_codeword_detected():
    # 自定义码表：字母层只允许 A(3)，其余非法
    table = CodeTable(name="T", tracks=5, ltrs={3: "A", 4: " "}, figs={},
                      ltrs_code=31, figs_code=27)
    result = decode([3, 5, 3], table)
    assert result.chars[1].legal is False
    assert any(d.type == "illegal_codeword" for d in result.diagnostics)


def test_redundant_shift_detected():
    table = ita2_table()
    result = decode([31, 31, 3], table)  # LTRS LTRS A
    assert any(d.type == "redundant_shift" for d in result.diagnostics)


def test_long_figs_run_detected():
    table = ita2_table()
    codes = [table.figs_code] + [0x17] * 15  # FIGS + 15 个 '1'
    result = decode(codes, table, long_figs_run=12)
    assert any(d.type == "long_figs_run" for d in result.diagnostics)


def test_low_confidence_flagged():
    table = ita2_table()
    result = decode([3, 3], table, confidences=[1.0, 0.3])
    assert any(d.type == "low_confidence" and d.column_index == 1
               for d in result.diagnostics)


def test_encode_rejects_unknown_char():
    table = ita2_table()
    with pytest.raises(ValueError):
        encode_text("你好", table)
