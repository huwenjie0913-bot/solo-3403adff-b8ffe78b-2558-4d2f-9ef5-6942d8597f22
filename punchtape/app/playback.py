"""电传终端纸面回放：逐列驱动终端机构，模拟纸面效果并标注机械/格式问题。

每孔列按传输速率依次到达终端，根据码表与当前 LTRS/FIGS 移位状态解释为
打印字符或 NUL/BEL/CR/LF/移位控制。终端维护：
  - 逻辑字车位置 col（0 = 最左）、当前行 row；
  - CR/LF 机械动作的完成时刻，途中到达的打印字符落在插值字车位置，
    产生“字车未回稳即打印”的错列；
  - CR 与 LF 的配对窗口：CR 后紧跟 LF 为标准“回车+走纸”；CR 后未等
    到 LF 即打印 -> 同列重打；LF 前没有 CR（其后也未补 CR）即打印 ->
    字车未归位的错行；LF 之后补 CR 视为恢复，不报问题。NUL/BEL/移位
    码既不打印也不改变配对关系。

问题类型（issue）：
  line_overflow        行宽溢出（逻辑列号 >= 每行字数）
  cr_unsettled_print   字车回程未完成即继续打印
  lone_cr_overstrike   单独 CR（无配对 LF）后继续打印，覆盖原行
  lone_lf_misline      单独 LF（无配对 CR），字车未归位即错行
  cr_not_homed         纸带结束时字车不在最左位置
  lone_cr_at_end       纸带结束于一次未配对 CR
  lone_lf_at_end       纸带结束于一次未配对 CR 的 LF（其后无打印）
  unknown_control      码表在当前移位状态下未定义的码字（保留原码字）

回放是只读操作：输入取识别任务的原始或修订孔阵，绝不写回识别任务与修订。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .codetable import CodeTable
from .terminal import TerminalConfig

ISSUE_SEVERITY = {
    "line_overflow": "error",
    "cr_unsettled_print": "warning",
    "lone_cr_overstrike": "warning",
    "lone_lf_misline": "warning",
    "cr_not_homed": "warning",
    "lone_cr_at_end": "info",
    "lone_lf_at_end": "info",
    "unknown_control": "error",
}

ISSUE_MESSAGES = {
    "line_overflow": "行宽溢出：逻辑列号 {pcol} 超过每行 {columns} 字",
    "cr_unsettled_print": "字车回程未完成（{elapsed:.0f}/{cr_ms:.0f}ms）"
                          "即继续打印，字车实际位于第 {phys} 列",
    "lone_cr_overstrike": "单独 CR 未配 LF 即继续打印，第 {row} 行被重打",
    "lone_lf_misline": "单独 LF 未配 CR，字车停在第 {pcol} 列即走纸，"
                       "后续打印从该列开始（错行）",
    "cr_not_homed": "纸带结束时字车停在第 {pcol} 列，未归位",
    "lone_cr_at_end": "纸带结束于一次未配 LF 的 CR（纸面停在同一行）",
    "lone_lf_at_end": "纸带结束于一次未配 CR 的 LF（其后无打印，错行未必显现）",
    "unknown_control": "码字 {code_bits}（值 {code}）在{shift_name}"
                       "移位状态下未定义，保留原码字",
}

SHIFT_NAMES = {"ltrs": "字母", "figs": "数字"}


@dataclass
class PlayCell:
    """纸面上的一个格子。同坐标多条目表示重打（叠印）层。"""
    row: int
    col: int
    char: str | None                    # 实际字形；未定义码为 None
    glyph: str                          # 显示字形（未知码为 □）
    column_index: int
    source: str                         # original / revised
    code: int
    shift: str
    legal: bool
    overstrike: bool = False
    overflow: bool = False
    unsettled: bool = False
    layer: int = 0


@dataclass
class PlayEvent:
    """一个孔列（或自动换行注入的虚拟动作）对应的终端控制事件。"""
    index: int                          # 孔列序号；注入事件为负
    code: int
    kind: str                           # print / cr / lf / nul / bel /
                                        # ltrs / figs / unknown
    char: str | None = None             # print 字形（含空格）
    glyph: str = ""                     # 未知码的占位字形
    shift: str = "ltrs"                 # 生效的移位状态
    time_ms: float = 0.0                # 到达/执行时刻
    row: int | None = None              # 落点/所在行
    col: int | None = None              # 落点逻辑列
    phys_col: float | None = None       # 字车物理列（回程插值）
    injected: bool = False              # 自动换行注入的虚拟动作
    source: str = "revised"
    legal: bool = True
    reason: str = ""
    issues: list[str] = field(default_factory=list)


@dataclass
class PlayResult:
    config: TerminalConfig
    source: str                         # original / revised
    initial_shift: str
    char_ms: float
    total_ms: float
    rows: int
    line_lengths: list[int]
    events: list[PlayEvent]
    cells: list[PlayCell]
    issues: list[dict]
    printed_chars: int
    overstrike_chars: int
    overflow_count: int
    cr_count: int
    lf_count: int
    bel_count: int

    # ------------------------------------------------------------ 序列化
    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "initial_shift": self.initial_shift,
            "char_ms": round(self.char_ms, 3),
            "total_ms": round(self.total_ms, 3),
            "config": self.config.to_dict(),
            "summary": {
                "rows": self.rows,
                "line_lengths": self.line_lengths,
                "events": len(self.events),
                "printed_chars": self.printed_chars,
                "overstrike_chars": self.overstrike_chars,
                "overflow_count": self.overflow_count,
                "cr_count": self.cr_count,
                "lf_count": self.lf_count,
                "bel_count": self.bel_count,
                "issue_count": len(self.issues),
            },
            "events": [_event_dict(e) for e in self.events],
            "paper": [_cell_dict(c) for c in self.cells],
            "issues": self.issues,
        }


def _event_dict(e: PlayEvent) -> dict:
    d = {
        "index": e.index, "code": e.code, "kind": e.kind,
        "char": e.char, "shift": e.shift,
        "time_ms": round(e.time_ms, 3),
        "row": e.row, "col": e.col,
        "phys_col": round(e.phys_col, 3) if e.phys_col is not None else None,
        "injected": e.injected, "source": e.source, "legal": e.legal,
        "issues": list(e.issues),
    }
    if e.glyph:
        d["glyph"] = e.glyph
    if e.reason:
        d["reason"] = e.reason
    return d


def _cell_dict(c: PlayCell) -> dict:
    return {
        "row": c.row, "col": c.col, "char": c.char, "glyph": c.glyph,
        "column_index": c.column_index, "source": c.source, "code": c.code,
        "shift": c.shift, "legal": c.legal, "layer": c.layer,
        "overstrike": c.overstrike, "overflow": c.overflow,
        "unsettled": c.unsettled,
    }


# -------------------------------------------------------------------- 引擎
class _Terminal:
    def __init__(self, cfg: TerminalConfig, source: str,
                 table: CodeTable, initial_shift: str):
        self.cfg = cfg
        self.default_source = source
        self.source = source
        self.table = table
        self.shift = initial_shift

        self.col = 0          # 逻辑字车列
        self.row = 0          # 当前纸面行
        self.time = 0.0       # 当前孔列到达时刻
        self.extra_delay = 0.0  # NUL 等在线路之外增加的机械延时
        self.cells: list[PlayCell] = []
        self.events: list[PlayEvent] = []
        self.issues: list[dict] = []
        self._seq = 0         # 注入事件序号（递减为负）

        # CR 机械动作
        self.cr_started: float | None = None
        self.cr_duration = 0.0
        self.cr_from = 0.0
        # CR/LF 配对窗口
        self.pending_cr: PlayEvent | None = None
        self.pending_lf: PlayEvent | None = None

        # 统计
        self.printed = 0
        self.overstrikes = 0
        self.overflows = 0
        self.cr_count = 0
        self.lf_count = 0
        self.bel_count = 0

    # -- 基础工具 --------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq -= 1
        return self._seq

    def _phys_col(self, at: float) -> float | None:
        if self.cr_started is None:
            return None
        if self.cr_duration <= 0:
            return 0.0
        done = min(max((at - self.cr_started) / self.cr_duration, 0.0), 1.0)
        return self.cr_from * (1.0 - done)

    def _add_issue(self, type_: str, event: PlayEvent,
                   column_index: int, **fmt) -> None:
        message = ISSUE_MESSAGES[type_].format(
            tracks=self.table.tracks, columns=self.cfg.columns,
            cr_ms=self.cfg.cr_ms, shift_name=SHIFT_NAMES.get(
                self.shift, self.shift), **fmt)
        self.issues.append({
            "type": type_, "severity": ISSUE_SEVERITY[type_],
            "column_index": column_index, "event_index": event.index,
            "row": event.row, "col": event.col, "message": message,
        })
        event.issues.append(type_)

    def _start_cr_motion(self, at: float) -> None:
        self.cr_from = self._phys_col(at) if self.cr_started is not None \
            else float(self.col)
        self.cr_started = at
        self.cr_duration = max(self.cfg.cr_ms, 1e-9)

    # -- 控制动作 --------------------------------------------------------
    def do_cr(self, index: int, code: int, at: float,
              injected: bool = False, reason: str = "") -> PlayEvent:
        ev = PlayEvent(index=index if not injected else self._next_seq(),
                       code=code, kind="cr", shift=self.shift, time_ms=at,
                       row=self.row, col=self.col, phys_col=float(self.col),
                       injected=injected, source=self.source, reason=reason)
        self.cr_count += 1
        # LF 之后补 CR：错行恢复，配成一对；双重 CR：后者取代前者
        if self.pending_lf is not None:
            self.pending_lf = None
        self.pending_cr = ev
        self._start_cr_motion(at)
        self.col = 0
        self.events.append(ev)
        if self.cfg.auto_lf:
            self.do_lf(index, code, at, injected=True,
                       reason="终端 auto_lf：收到 CR 后自动走纸")
        return ev

    def do_lf(self, index: int, code: int, at: float,
              injected: bool = False, reason: str = "") -> PlayEvent:
        paired = self.pending_cr is not None or self.cfg.auto_cr
        # 上一个悬置 LF 在此刻确定未获 CR 配对（又来一个 LF 或走纸后才补 CR）
        if self.pending_lf is not None and not paired:
            old = self.pending_lf
            self._add_issue("lone_lf_misline", old, old.index, pcol=old.col)
        self.pending_cr = None
        if paired:
            self.pending_lf = None
        ev = PlayEvent(index=index if not injected else self._next_seq(),
                       code=code, kind="lf", shift=self.shift, time_ms=at,
                       row=self.row + 1, col=self.col,
                       phys_col=self._phys_col(at),
                       injected=injected, source=self.source, reason=reason)
        self.lf_count += 1
        self.row += 1
        self.events.append(ev)
        if not paired and not injected:
            self.pending_lf = ev
        if self.cfg.auto_cr:
            self.do_cr(index, code, at, injected=True,
                       reason="终端 auto_cr：收到 LF 后字车归位")
        return ev

    def do_print(self, index: int, code: int, ch: str, at: float) -> None:
        # 打印字符终结配对窗口：CR 未配 LF 而字车已回稳 -> 同坐标重打；
        # LF 未获 CR -> 字车未归位的错行。回程未完成的情况在下方按时序判定。
        if self.pending_lf is not None:
            lf_ev = self.pending_lf
            self._add_issue("lone_lf_misline", lf_ev, lf_ev.index,
                            pcol=lf_ev.col)
        self.pending_cr = None
        self.pending_lf = None
        self.printed += 1

        # 行宽溢出（空格也占列，同样可溢出）
        overflow = self.col >= self.cfg.columns
        pcol = self.col
        if overflow:
            self.overflows += 1
            if self.cfg.overflow == "wrap":
                why = f"行宽 {self.cfg.columns} 字溢出，自动 CR/LF 换行"
                self.do_cr(index, code, at, injected=True, reason=why)
                if not self.cfg.auto_lf:
                    self.do_lf(index, code, at, injected=True, reason=why)
                # 终端缓存该字符，待回程/走纸机构完成后再落纸
                at = at + max(self.cfg.cr_ms, self.cfg.lf_ms)
                pcol = 0
                overflow = False
            elif self.cfg.overflow == "truncate":
                ev = PlayEvent(index=index, code=code, kind="print", char=ch,
                               shift=self.shift, time_ms=at, row=self.row,
                               col=None, phys_col=None, source=self.source)
                self._add_issue("line_overflow", ev, index, pcol=pcol)
                ev.reason = f"超行宽被 {self.cfg.columns} 字边界截断，未落纸"
                self.events.append(ev)
                self.col += 1
                return

        phys = self._phys_col(at)
        unsettled = phys is not None and phys > 0.5
        target_col = int(round(phys)) if unsettled else pcol

        ev = PlayEvent(index=index, code=code, kind="print", char=ch,
                       shift=self.shift, time_ms=at, row=self.row,
                       col=pcol, phys_col=phys, source=self.source)
        if overflow:
            self._add_issue("line_overflow", ev, index, pcol=pcol)
        if unsettled:
            elapsed = at - (self.cr_started if self.cr_started is not None
                            else at)
            self._add_issue("cr_unsettled_print", ev, index,
                            elapsed=max(elapsed, 0.0), phys=target_col)
        self.cr_started = None  # 字锤落下时回程动作已无意义

        existing = [c for c in self.cells if c.row == self.row
                    and c.col == target_col]
        overstrike = bool(existing)
        if overstrike:
            self.overstrikes += 1
            # 字车已归位但同一格已有字：单独 CR（无 LF）造成的重打
            if not unsettled:
                self._add_issue("lone_cr_overstrike", ev, index,
                                row=self.row)

        self.cells.append(PlayCell(
            row=self.row, col=target_col, char=ch, glyph=ch,
            column_index=index, source=self.source, code=code,
            shift=self.shift, legal=True, overstrike=overstrike,
            overflow=overflow, unsettled=unsettled,
            layer=len(existing)))
        self.events.append(ev)
        self.col = pcol + 1

    def do_nul(self, index: int, code: int, at: float) -> None:
        # NUL/Blank：不走纸、不打印，也不影响 CR/LF 配对；
        # nul_ms>0 时给后续字符额外机械延时（纸带空段常被用作机构延时）
        if self.cfg.nul_ms:
            self.extra_delay += self.cfg.nul_ms
        reason = (f"空白码（附加 {self.cfg.nul_ms:g}ms 机械延时）"
                  if self.cfg.nul_ms else "")
        self.events.append(PlayEvent(
            index=index, code=code, kind="nul", shift=self.shift,
            time_ms=at, row=self.row, col=self.col,
            phys_col=self._phys_col(at), source=self.source, reason=reason))

    def do_bel(self, index: int, code: int, at: float) -> None:
        # BEL：振铃，不移动字车、不影响配对
        self.bel_count += 1
        self.events.append(PlayEvent(
            index=index, code=code, kind="bel", shift=self.shift,
            time_ms=at, row=self.row, col=self.col,
            phys_col=self._phys_col(at), source=self.source,
            reason=f"振铃（机构时长约 {self.cfg.bell_ms:.0f}ms，仅记录）"))

    def do_shift(self, index: int, code: int, at: float,
                 target: str) -> None:
        self.shift = target
        self.events.append(PlayEvent(
            index=index, code=code, kind=target, shift=target,
            time_ms=at, row=self.row, col=self.col,
            phys_col=self._phys_col(at), source=self.source))

    def do_unknown(self, index: int, code: int, at: float) -> None:
        code_bits = format(code, f"0{self.table.tracks}b")
        reason = ISSUE_MESSAGES["unknown_control"].format(
            code_bits=code_bits, code=code,
            shift_name=SHIFT_NAMES.get(self.shift, self.shift))
        ev = PlayEvent(index=index, code=code, kind="unknown", char=None,
                       glyph="□", shift=self.shift, time_ms=at,
                       row=self.row, col=self.col,
                       phys_col=self._phys_col(at), source=self.source,
                       legal=False, reason=reason)
        self._add_issue("unknown_control", ev, index, code=code,
                        code_bits=code_bits)
        self.events.append(ev)

    # -- 结束 ------------------------------------------------------------
    def finish(self) -> None:
        if self.pending_cr is not None:
            ev = self.pending_cr
            self._add_issue("lone_cr_at_end", ev, ev.index)
        elif self.pending_lf is not None:
            ev = self.pending_lf
            self._add_issue("lone_lf_at_end", ev, ev.index)
        if self.col > 0 and self.pending_cr is None:
            message = ISSUE_MESSAGES["cr_not_homed"].format(pcol=self.col - 1)
            last_print = next((e for e in reversed(self.events)
                               if e.kind == "print"), None)
            idx = last_print.index if last_print else -1
            self.issues.append({
                "type": "cr_not_homed",
                "severity": ISSUE_SEVERITY["cr_not_homed"],
                "column_index": idx, "event_index": idx,
                "row": self.row, "col": self.col - 1, "message": message})


def play(codes: list[int], table: CodeTable, cfg: TerminalConfig,
         source: str = "revised",
         initial_shift: str | None = None,
         column_sources: list[str] | None = None) -> PlayResult:
    """按终端配置逐列回放码字序列，返回纸面结果。

    source: 整批码字来源（original / revised）；column_sources 可逐列覆盖
    （修订孔阵中仅个别列被人工修改时，未改列标注 original）。
    """
    cfg.validate()
    shift0 = initial_shift or cfg.initial_shift
    term = _Terminal(cfg, source, table, shift0)
    char_ms = cfg.char_ms

    for i, code in enumerate(codes):
        at = i * char_ms + term.extra_delay
        term.time = at
        if column_sources is not None:
            term.source = column_sources[i]

        # 1) 移位控制码
        target = table.shift_of(code)
        if target is not None:
            term.do_shift(i, code, at, target)
            continue

        ch = table.lookup(code, term.shift)
        # 2) 本层未定义（None）；空串 "" 是合法的 NUL/Blank
        if ch is None:
            term.do_unknown(i, code, at)
            continue

        # 3) 控制字符（按 ITA2 字符约定）与可打印字符
        if ch == "\r":
            term.do_cr(i, code, at)
        elif ch == "\n":
            term.do_lf(i, code, at)
        elif ch == "\x00" or ch == "":
            term.do_nul(i, code, at)
        elif ch == "\a":
            term.do_bel(i, code, at)
        else:
            term.do_print(i, code, ch, at)

    term.finish()

    line_lengths = [0] * (term.row + 1)
    for c in term.cells:
        line_lengths[c.row] = max(line_lengths[c.row], c.col + 1)

    return PlayResult(
        config=cfg, source=source, initial_shift=shift0,
        char_ms=char_ms,
        total_ms=len(codes) * char_ms + term.extra_delay,
        rows=term.row + 1, line_lengths=line_lengths,
        events=term.events, cells=term.cells, issues=term.issues,
        printed_chars=term.printed, overstrike_chars=term.overstrikes,
        overflow_count=term.overflows, cr_count=term.cr_count,
        lf_count=term.lf_count, bel_count=term.bel_count)


# ---------------------------------------------------------------- 比较
def diff_results(a: PlayResult, b: PlayResult) -> dict:
    """比较两次回放（不同终端配置或修订前后）的纸面差异。"""
    def grid_map(r: PlayResult) -> dict[tuple[int, int], list[PlayCell]]:
        g: dict[tuple[int, int], list[PlayCell]] = {}
        for c in r.cells:
            g.setdefault((c.row, c.col), []).append(c)
        return g

    ga, gb = grid_map(a), grid_map(b)
    cells_diff = []
    for key in sorted(set(ga) | set(gb)):
        ca = ga.get(key)
        cb = gb.get(key)
        va = "".join(c.glyph for c in ca) if ca else None
        vb = "".join(c.glyph for c in cb) if cb else None
        if va != vb:
            cells_diff.append({"row": key[0], "col": key[1],
                               "a": va, "b": vb})

    def ev_index(r: PlayResult) -> dict[int, list[PlayEvent]]:
        out: dict[int, list[PlayEvent]] = {}
        for e in r.events:
            if e.index >= 0:
                out.setdefault(e.index, []).append(e)
        return out

    ea, eb = ev_index(a), ev_index(b)
    events_diff = []
    for idx in sorted(set(ea) | set(eb)):
        va = [e.kind for e in ea.get(idx, [])]
        vb = [e.kind for e in eb.get(idx, [])]
        ra = ea.get(idx, [None])[0]
        rb = eb.get(idx, [None])[0]
        pa = [ra.row, ra.col, round(ra.phys_col or 0.0, 2)] if ra else None
        pb = [rb.row, rb.col, round(rb.phys_col or 0.0, 2)] if rb else None
        if va != vb or pa != pb:
            events_diff.append({"index": idx, "a_kind": va, "b_kind": vb,
                                "a_pos": pa, "b_pos": pb})

    return {
        "summary_a": a.to_dict()["summary"] | {"source": a.source},
        "summary_b": b.to_dict()["summary"] | {"source": b.source},
        "paper_cell_diff_count": len(cells_diff),
        "event_diff_count": len(events_diff),
        "paper_cell_diff": cells_diff,
        "event_diff": events_diff,
    }
