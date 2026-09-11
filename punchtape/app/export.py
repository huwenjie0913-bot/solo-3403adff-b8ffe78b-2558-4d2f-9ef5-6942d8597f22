"""导出：原始孔阵、修订孔阵、解码文本与诊断记录（JSON / 纯文本）。"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .decoding import decode
from .models import RecognitionJob
from .pipeline import effective_columns, load_code_table


def _codes(cols: list[dict], key: str = "bits") -> list[int]:
    out = []
    for c in cols:
        v = 0
        for i, b in enumerate(c[key]):
            v |= (b & 1) << i
        out.append(v)
    return out


def build_export(db: Session, job: RecognitionJob) -> dict:
    table, _ = load_code_table(db, job.code_table_id)
    cols = effective_columns(db, job)

    raw_bits = [c["raw_bits"] for c in cols]
    revised_bits = ["".join(map(str, c["bits"])) for c in cols]

    raw_codes = []
    for b in raw_bits:
        raw_codes.append(sum((int(ch) & 1) << i for i, ch in enumerate(b)))
    revised_codes = _codes(cols)

    raw_text = decode(raw_codes, table).text
    revised = decode(revised_codes, table)

    diagnostics = [{
        "type": d.type, "severity": d.severity, "column_index": d.column_index,
        "message": d.message, "details": json.loads(d.details),
    } for d in job.diagnostics]

    revisions = [{
        "column_index": r.column_index, "original_bits": r.original_bits,
        "revised_bits": r.revised_bits, "locked": r.locked,
        "author": r.author, "reason": r.reason,
        "created_at": r.created_at.isoformat(),
    } for r in job.revisions]

    # 逐字符追溯：字符 -> 孔列 -> 扫描段
    trace = []
    for c, ch in zip(job.columns, revised.chars):
        trace.append({
            "column_index": c.index,
            "bits": revised_bits[c.index],
            "code": ch.code,
            "shift": ch.shift,
            "char": ch.char,
            "legal": ch.legal,
            "confidence": c.confidence,
            "sources": json.loads(c.sources),
            "bit_positions": json.loads(c.bit_positions),
        })

    return {
        "job_id": job.id,
        "tape_id": job.tape_id,
        "code_table": table.name,
        "params": json.loads(job.params),
        "raw_matrix": raw_bits,
        "revised_matrix": revised_bits,
        "raw_text": raw_text,
        "decoded_text": revised.text,
        "diagnostics": diagnostics,
        "revisions": revisions,
        "trace": trace,
    }


def export_json(db: Session, job: RecognitionJob) -> str:
    return json.dumps(build_export(db, job), ensure_ascii=False, indent=2)


def export_text(db: Session, job: RecognitionJob) -> str:
    data = build_export(db, job)
    lines = [
        f"# 纸带识别导出  任务 #{data['job_id']}  纸带 #{data['tape_id']}",
        f"# 码表: {data['code_table']}",
        "",
        "== 解码文本（含人工修订） ==",
        data["decoded_text"],
        "",
        "== 原始识别文本 ==",
        data["raw_text"],
        "",
        "== 孔阵（修订后，1=有孔） ==",
    ]
    for i, b in enumerate(data["revised_matrix"]):
        mark = ""
        for r in data["revisions"]:
            if r["column_index"] == i:
                mark = f"  <- 人工修订(原 {r['original_bits']})"
                break
        lines.append(f"{i:5d}: {b}{mark}")
    lines += ["", "== 诊断记录 =="]
    for d in data["diagnostics"]:
        lines.append(f"[{d['severity']:7s}] 列{d['column_index']:5d} "
                     f"{d['type']}: {d['message']}")
    return "\n".join(lines) + "\n"
