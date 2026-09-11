"""纸面回放引擎单元测试：CR/LF 配对、时序、溢出、未定义码、对比。"""
import pytest

from punchtape.app.codetable import CodeTable, ita2_table
from punchtape.app.decoding import encode_text
from punchtape.app.playback import diff_results, play
from punchtape.app.terminal import TerminalConfig

T = ita2_table()
CR, LF = 0x08, 0x02


def cfg(**kw):
    return TerminalConfig(columns=kw.pop("columns", 72),
                          baud=kw.pop("baud", 45.45), **kw)


def types(result):
    return [i["type"] for i in result.issues]


# ---------------------------------------------------------------- 配对
def test_crlf_pair_no_lone_issue():
    codes = encode_text("HELLO", T) + [CR, LF] + encode_text("WORLD", T)
    r = play(codes, T, cfg())
    assert not [t for t in types(r) if t.startswith("lone")]
    # W 落在第 2 行第 0 列
    w = next(e for e in r.events if e.char == "W")
    assert (w.row, w.col) == (1, 0)
    assert r.rows == 2 and r.line_lengths == [5, 5]


def test_lone_cr_then_print_overstrikes():
    # 回程足够快：CR 已回稳，X/Y 落在原行已有格子上 -> 重打
    codes = encode_text("ABC", T) + [CR] + encode_text("XY", T)
    r = play(codes, T, cfg(cr_ms=100, lf_ms=45))
    assert "lone_cr_overstrike" in types(r)
    assert r.overstrike_chars == 2
    assert all(c.row == 0 for c in r.cells)


def test_lf_cr_is_treated_as_valid_newline():
    codes = encode_text("AB", T) + [LF, CR] + encode_text("X", T)
    r = play(codes, T, cfg())
    assert not [t for t in types(r) if t.startswith("lone")]
    x = next(e for e in r.events if e.char == "X")
    assert (x.row, x.col) == (1, 0)


def test_lone_lf_mislines():
    codes = encode_text("ABC", T) + [LF] + encode_text("XY", T)
    r = play(codes, T, cfg())
    assert "lone_lf_misline" in types(r)
    x = next(e for e in r.events if e.char == "X")
    assert (x.row, x.col) == (1, 3)  # 字车未归位，从第 3 列继续


def test_consecutive_lf_each_flagged():
    codes = encode_text("AB", T) + [LF, LF] + encode_text("X", T)
    r = play(codes, T, cfg())
    assert sum(1 for t in types(r) if t == "lone_lf_misline") == 2


def test_auto_lf_and_auto_cr():
    codes = encode_text("AB", T) + [CR] + encode_text("X", T)
    r = play(codes, T, cfg(auto_lf=True))
    assert not [t for t in types(r) if t.startswith("lone")]
    assert next(e for e in r.events if e.char == "X").row == 1

    codes = encode_text("AB", T) + [LF] + encode_text("X", T)
    r = play(codes, T, cfg(auto_cr=True))
    assert not [t for t in types(r) if t.startswith("lone")]
    x = next(e for e in r.events if e.char == "X")
    assert (x.row, x.col) == (1, 0)


# ---------------------------------------------------------------- 时序
def test_unsettled_carriage_prints_at_interpolated_column():
    # 45.45 波特字符间隔 163ms；回程 200ms，首字到达时字车仍在途中
    codes = encode_text("ABC", T) + [CR] + encode_text("X", T)
    r = play(codes, T, cfg(cr_ms=200))
    ev = next(e for e in r.events if e.char == "X")
    assert "cr_unsettled_print" in ev.issues
    assert 0 < ev.phys_col < 3
    cell = next(c for c in r.cells if c.column_index == ev.index)
    assert cell.unsettled and cell.col == round(ev.phys_col)


def test_char_ms_from_baud():
    # 5 数据位 + 1 起始 + 1.42 停止 = 7.42 单位
    assert cfg(baud=45.45).char_ms == pytest.approx(7.42 / 45.45 * 1000)
    r = play(encode_text("AB", T), T, cfg(baud=50.0, stop_units=1.0))
    assert r.char_ms == pytest.approx(7.0 / 50 * 1000)
    assert r.events[1].time_ms == pytest.approx(140.0)


# ---------------------------------------------------------------- 溢出
def test_overflow_mark_keeps_printing_past_margin():
    r = play(encode_text("ABCDEFGH", T), T,
             cfg(columns=5, overflow="mark"))
    assert r.overflow_count == 3
    assert sum(1 for c in r.cells if c.overflow) == 3
    assert r.line_lengths == [8]


def test_overflow_wrap_injects_cr_lf():
    r = play(encode_text("ABCDEFGH", T), T,
             cfg(columns=5, overflow="wrap"))
    assert r.rows == 2 and r.line_lengths == [5, 3]
    injected = [e for e in r.events if e.injected]
    assert {(e.kind) for e in injected} == {"cr", "lf"}
    assert all("溢出" in e.reason for e in injected)
    # 注入字符延迟到回程完成后落纸，不标未回稳
    f = next(e for e in r.events if e.char == "F")
    assert not f.issues and (f.row, f.col) == (1, 0)


def test_overflow_truncate_drops_cells():
    r = play(encode_text("ABCDEFGH", T), T,
             cfg(columns=5, overflow="truncate"))
    assert r.line_lengths == [5]
    assert len(r.cells) == 5
    dropped = [e for e in r.events if e.kind == "print" and e.col is None]
    assert len(dropped) == 3


# ---------------------------------------------------------------- 控制码
def test_nul_bel_shift_events():
    # FIGS, BEL(0x0B), NUL(0x00), LTRS
    r = play([0x1B, 0x0B, 0x00, 0x1F], T, cfg())
    kinds = [e.kind for e in r.events]
    assert kinds == ["figs", "bel", "nul", "ltrs"]
    assert r.bel_count == 1 and r.printed_chars == 0
    assert "振铃" in r.events[1].reason
    # 控制码不移动字车、不落纸
    assert r.cells == []


def test_nul_does_not_break_cr_lf_pair():
    codes = encode_text("AB", T) + [CR, 0x00, LF] + encode_text("X", T)
    r = play(codes, T, cfg())
    assert not [t for t in types(r) if t.startswith("lone")]


def test_unknown_codeword_preserved_with_reason():
    ct = CodeTable(name="MIN", tracks=5, ltrs={3: "A", 4: " "},
                   figs={}, ltrs_code=31, figs_code=27)
    r = play([3, 5, 31], ct, cfg())  # 码 5（00101）两层均未定义
    unk = [i for i in r.issues if i["type"] == "unknown_control"]
    assert len(unk) == 1
    assert "00101" in unk[0]["message"] and "值 5" in unk[0]["message"]
    ev = next(e for e in r.events if e.kind == "unknown")
    assert ev.legal is False and ev.glyph == "□" and ev.code == 5
    # 未定义码不落纸、不推进字车；后续 A 仍在第 0 列
    assert len(r.cells) == 1


def test_nul_ms_adds_delay_that_can_settle_carriage():
    # CR 后插一个 100ms NUL：回程 200ms，字符间隔 163ms
    # 无 NUL 时 X 未回稳；有 NUL 时已回稳落第 0 列
    base = encode_text("ABC", T) + [CR]
    fast = play(base + encode_text("X", T), T, cfg(cr_ms=200))
    assert "cr_unsettled_print" in types(fast)
    delayed = play(base + [0x00] + encode_text("X", T), T,
                   cfg(cr_ms=200, nul_ms=100))
    x = next(e for e in delayed.events if e.char == "X")
    assert "cr_unsettled_print" not in x.issues
    assert x.phys_col == 0.0 and x.col == 0
    # 字车已回稳但重打在原行格子上：单独 CR 重打仍成立
    assert "lone_cr_overstrike" in x.issues
    assert delayed.total_ms > fast.total_ms


def test_config_validation():
    with pytest.raises(ValueError):
        TerminalConfig(columns=0).validate()
    with pytest.raises(ValueError):
        TerminalConfig(baud=0).validate()
    with pytest.raises(ValueError):
        TerminalConfig(overflow="nope").validate()


# ---------------------------------------------------------------- 结束
def test_carriage_not_homed_at_end():
    r = play(encode_text("HELLO", T), T, cfg())
    assert "cr_not_homed" in types(r)


def test_lone_cr_and_lone_lf_at_end_are_info():
    r = play(encode_text("HELLO", T) + [CR], T, cfg())
    assert "lone_cr_at_end" in types(r)
    assert "cr_not_homed" not in types(r)

    r = play(encode_text("HELLO", T) + [LF], T, cfg())
    assert "lone_lf_at_end" in types(r)


# ---------------------------------------------------------------- 来源/比较
def test_column_sources_propagate_to_cells_and_events():
    codes = encode_text("A B", T)
    r = play(codes, T, cfg(), source="revised",
             column_sources=["original", "original", "revised"])
    assert [c.source for c in r.cells] == ["original", "original", "revised"]
    assert [c.char for c in r.cells] == ["A", " ", "B"]


def test_diff_different_widths_and_sources():
    codes = encode_text("ABCDEFGH", T)
    a = play(codes, T, cfg(columns=80, overflow="wrap"))
    b = play(codes, T, cfg(columns=5, overflow="wrap"))
    d = diff_results(a, b)
    assert d["paper_cell_diff_count"] > 0
    assert d["summary_a"]["rows"] != d["summary_b"]["rows"]
