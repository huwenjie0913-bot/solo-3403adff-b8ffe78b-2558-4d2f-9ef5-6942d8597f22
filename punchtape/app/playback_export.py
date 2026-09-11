"""纸面回放导出：保留空格的纯文本、JSON 与等宽 SVG。

纯文本三段：
  1. 纸面区（竖线边界保留所有空格，叠印格子用 {A|B} 形式列出重打层）；
  2. 控制轨迹（逐孔列：时刻、行列、控制符号、问题标记）；
  3. 问题清单。
SVG：等宽字体逐格排版，问题格子着色，下方画控制轨迹。
"""
from __future__ import annotations

import html
import json

from .playback import PlayResult

KIND_SYMBOL = {
    "cr": "CR", "lf": "LF", "nul": "NUL", "bel": "BEL",
    "ltrs": "LTRS", "figs": "FIGS", "unknown": "???",
}

KIND_COLOR = {
    "cr": "#1565c0", "lf": "#2e7d32", "nul": "#757575",
    "bel": "#e65100", "ltrs": "#6a1b9a", "figs": "#ad1457",
    "unknown": "#c62828",
}


# -------------------------------------------------------------------- 纯文本
def paper_lines(result: PlayResult) -> list[str]:
    """把纸面渲染成等宽行。

    空格以真实空格保留（含尾随，行首行尾用 │ 界定）；叠印格用 {X|Y}
    列出全部重打层（多字符时该行在标记后自然变长）。
    """
    width = max([result.config.columns] + result.line_lengths + [0])
    width = max(width, 1)
    grid: dict[tuple[int, int], list[str]] = {}
    for c in result.cells:
        grid.setdefault((c.row, c.col), []).append(c.glyph)

    lines = []
    for r in range(result.rows):
        tokens = []
        for col in range(width):
            glyphs = grid.get((r, col))
            if glyphs is None:
                tokens.append(" ")
            elif len(glyphs) == 1:
                tokens.append(glyphs[0])
            else:
                tokens.append("{" + "|".join(glyphs) + "}")
        lines.append("│" + "".join(tokens) + "│")
    lines.append("└" + "─" * width + "┘")
    return lines


def control_trace(result: PlayResult) -> list[str]:
    lines = []
    for e in result.events:
        if e.kind == "print":
            shown = "SP" if e.char == " " else (e.char or "")
            sym = f"PRINT {shown!r}"
        else:
            sym = KIND_SYMBOL.get(e.kind, e.kind)
        pos = ""
        if e.kind in ("print", "lf", "cr", "nul", "bel", "unknown"):
            row = e.row if e.row is not None else "?"
            col = e.col if e.col is not None else "?"
            pos = f" 行{row} 列{col}"
            if e.phys_col is not None and e.kind == "print":
                pos += f" 物理列{e.phys_col:.1f}"
        mark = ("  [注入]" if e.injected else "")
        issues = ("  ⚠ " + ",".join(e.issues)) if e.issues else ""
        reason = f"  // {e.reason}" if e.reason else ""
        lines.append(f"[{e.index:5d}] t={e.time_ms:8.1f}ms {sym:10s}"
                     f"{pos}{mark}{issues}{reason}")
    return lines


def build_playback_text(result: PlayResult, job_id: int,
                        profile_name: str | None = None) -> str:
    cfg = result.config
    lines = [
        f"# 电传终端纸面回放  任务 #{job_id}"
        + (f"  终端机型: {profile_name}" if profile_name else ""),
        f"# 孔阵来源: {'修订孔阵' if result.source == 'revised' else '原始识别孔阵'}"
        f"  码表初始移位: {result.initial_shift}",
        f"# 配置: {cfg.columns} 字/行, {cfg.baud:g} 波特, "
        f"字符 {result.char_ms:.1f}ms, 回程 {cfg.cr_ms:g}ms, "
        f"走纸 {cfg.lf_ms:g}ms, auto_lf={cfg.auto_lf}, "
        f"auto_cr={cfg.auto_cr}, 溢出={cfg.overflow}",
        f"# 回放时长 {result.total_ms:.1f}ms, 共 {result.rows} 行, "
        f"打印 {result.printed_chars} 字, 重打 {result.overstrike_chars} 处, "
        f"溢出 {result.overflow_count} 处, BEL {result.bel_count} 次, "
        f"问题 {len(result.issues)} 条",
        "",
        "== 纸面（空格保留；{X|Y}=重打层） ==",
    ]
    lines += paper_lines(result)

    # 重打层：逐坐标列出全部叠印字形及其来源孔列
    layers: dict[tuple[int, int], list] = {}
    for c in result.cells:
        layers.setdefault((c.row, c.col), []).append(c)
    over = {k: v for k, v in layers.items() if len(v) > 1}
    lines += ["", "== 重打层（同坐标叠印） =="]
    if over:
        for (r, col), cells in sorted(over.items()):
            seq = " | ".join(f"{c.glyph}←列{c.column_index}" for c in cells)
            lines.append(f"行{r} 列{col}: {seq}")
    else:
        lines.append("（无）")

    lines += ["", "== 控制轨迹 =="]
    lines += control_trace(result)
    lines += ["", "== 问题清单 =="]
    if result.issues:
        for d in result.issues:
            lines.append(f"[{d['severity']:7s}] 列{d['column_index']:5d} "
                         f"{d['type']}: {d['message']}")
    else:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- JSON
def build_playback_json(result: PlayResult, job_id: int,
                        snapshot_id: int | None = None,
                        profile_name: str | None = None) -> str:
    payload = {"job_id": job_id, "profile_name": profile_name,
               **result.to_dict()}
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    return json.dumps(payload, ensure_ascii=False, indent=2)


# -------------------------------------------------------------------- SVG
def export_playback_svg(result: PlayResult, job_id: int,
                        profile_name: str | None = None) -> str:
    cfg = result.config
    width_units = max([cfg.columns] + result.line_lengths + [1])
    cell_w, cell_h = 11.0, 18.0
    margin_l, margin_t, margin_r = 72.0, 64.0, 24.0
    paper_h = (result.rows + 1) * cell_h + 10.0

    legend_h = 30.0
    trace_line_h = 20.0
    # 只画真实孔列（注入事件跟随触发列），按列压缩
    real_events = [e for e in result.events if e.index >= 0]
    trace_cols = sorted({e.index for e in real_events})
    trace_w = max(len(trace_cols) * cell_w + margin_l + margin_r,
                  margin_l + width_units * cell_w + margin_r)
    trace_top = margin_t + paper_h + 20.0
    trace_h = 2.2 * trace_line_h
    foot_h = 40.0
    total_h = trace_top + trace_h + foot_h
    total_w = trace_w

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_w:.0f}" height="{total_h:.0f}" '
        f'viewBox="0 0 {total_w:.0f} {total_h:.0f}" '
        f'font-family="Menlo, Consolas, \'Courier New\', monospace">')
    parts.append(f'<rect width="100%" height="100%" fill="#fafaf7"/>')

    title = (f"任务 #{job_id} 纸面回放"
             + (f" · {html.escape(profile_name)}" if profile_name else "")
             + f" · {cfg.columns}字/行 {cfg.baud:g}波特 "
             f"CR {cfg.cr_ms:g}ms LF {cfg.lf_ms:g}ms"
             f" · 孔阵来源：{'修订' if result.source == 'revised' else '原始'}")
    parts.append(f'<text x="{margin_l}" y="24" font-size="14" '
                 f'font-weight="bold" fill="#222">{html.escape(title)}</text>')
    parts.append(f'<text x="{margin_l}" y="44" font-size="11" fill="#555">'
                 f'回放 {result.total_ms:.0f}ms / {result.rows} 行 / '
                 f'打印 {result.printed_chars} 字 / 重打 '
                 f'{result.overstrike_chars} / 溢出 {result.overflow_count} '
                 f'/ 问题 {len(result.issues)}</text>')

    # 列标尺
    ruler = "".join(str(c % 10) if c % 10 == 0 or c == width_units - 1 else " "
                    for c in range(width_units))
    parts.append(f'<text x="{margin_l + 4}" y="{margin_t - 8}" '
                 f'font-size="10" fill="#888">{html.escape(ruler)}</text>')

    # 纸面色块与边框
    parts.append(f'<rect x="{margin_l}" y="{margin_t}" '
                 f'width="{width_units * cell_w}" height="{paper_h - 10}" '
                 f'fill="#ffffff" stroke="#999"/>')
    # 右边界（行宽限制）
    bx = margin_l + cfg.columns * cell_w
    parts.append(f'<line x1="{bx}" y1="{margin_t}" x2="{bx}" '
                 f'y2="{margin_t + paper_h - 10}" stroke="#d33" '
                 f'stroke-dasharray="3 3" stroke-width="1"/>')
    parts.append(f'<text x="{bx + 3}" y="{margin_t + 11}" font-size="9" '
                 f'fill="#d33">{cfg.columns} 字边界</text>')

    # 格子
    issue_cells: dict[tuple[int, int], str] = {}
    layers: dict[tuple[int, int], list[str]] = {}
    for c in result.cells:
        layers.setdefault((c.row, c.col), []).append(c.glyph)
        if c.unsettled:
            issue_cells[(c.row, c.col)] = "#fff3cd"
        elif c.overflow:
            issue_cells[(c.row, c.col)] = "#f8d7da"
        elif c.overstrike:
            issue_cells[(c.row, c.col)] = "#ffe0b2"
    for (r, cc), fill in issue_cells.items():
        parts.append(
            f'<rect x="{margin_l + cc * cell_w}" y="{margin_t + r * cell_h}" '
            f'width="{cell_w}" height="{cell_h}" fill="{fill}"/>')
    for (r, cc), glyphs in layers.items():
        shown = glyphs[-1] if len(glyphs) == 1 else glyphs[0] + "/" + glyphs[-1]
        shown = shown if shown != " " else "␠"
        color = "#333"
        if len(glyphs) > 1:
            color = "#e65100"
        parts.append(
            f'<text x="{margin_l + cc * cell_w + 2}" '
            f'y="{margin_t + (r + 1) * cell_h - 4}" font-size="12" '
            f'fill="{color}">{html.escape(shown)}</text>')

    # 控制轨迹：每个真实孔列一列
    idx_to_x = {i: k for k, i in enumerate(trace_cols)}
    y_sym = trace_top + trace_line_h
    parts.append(f'<text x="{margin_l}" y="{trace_top - 6}" font-size="11" '
                 f'font-weight="bold" fill="#222">控制轨迹（每格一个孔列，'
                 f'红=问题，虚框=自动注入）</text>')
    parts.append(f'<line x1="{margin_l}" y1="{y_sym + 4}" '
                 f'x2="{margin_l + len(trace_cols) * cell_w}" y2="{y_sym + 4}" '
                 f'stroke="#ccc"/>')
    injected_index = {abs(e.index) for e in result.events if e.injected}
    for e in real_events:
        xpos = idx_to_x[e.index]
        x = margin_l + xpos * cell_w
        y = y_sym
        if e.kind == "print":
            sym = "␠" if e.char == " " else (e.char or "?")
            color = "#333"
        else:
            sym = KIND_SYMBOL.get(e.kind, "?")
            color = KIND_COLOR.get(e.kind, "#333")
        if e.issues:
            parts.append(f'<rect x="{x}" y="{y - 12}" width="{cell_w}" '
                         f'height="16" fill="#f8d7da"/>')
        if e.index in injected_index:
            parts.append(f'<rect x="{x}" y="{y - 12}" width="{cell_w}" '
                         f'height="16" fill="none" stroke="#888" '
                         f'stroke-dasharray="2 2"/>')
        parts.append(
            f'<text x="{x + 1}" y="{y}" font-size="9" fill="{color}">'
            f'{html.escape(sym)}</text>')

    # 图例
    legend_y = total_h - 14
    legends = [("#fff3cd", "未回稳"), ("#f8d7da", "溢出/问题"),
               ("#ffe0b2", "重打"), ("#d33", "行宽边界")]
    lx = margin_l
    for fill, label in legends:
        parts.append(f'<rect x="{lx}" y="{legend_y - 10}" width="12" '
                     f'height="12" fill="{fill}" stroke="#aaa"/>')
        parts.append(f'<text x="{lx + 16}" y="{legend_y}" font-size="10" '
                     f'fill="#333">{label}</text>')
        lx += 80
    parts.append(f'<text x="{lx + 10}" y="{legend_y}" font-size="10" '
                 f'fill="#555">控制符：'
                 f'CR 字车回程 / LF 走纸 / NUL 空白 / BEL 振铃 / '
                 f'LTRS·FIGS 移位 / □ 未定义码字</text>')
    parts.append("</svg>")
    return "\n".join(parts)
