"""端到端 API 测试：上传分段扫描图 -> 识别 -> 诊断 -> 修订 -> 修复 -> 导出。"""
import json

import pytest
from fastapi.testclient import TestClient

from punchtape.app.main import app
from punchtape.tests.synth import (render_tape, render_text_tape,
                                   split_segments, to_png_bytes)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_tape(client, name="测试纸带"):
    r = client.post("/tapes", json={
        "name": name, "tape_width_mm": 17.4, "tracks": 5, "dpi": 300,
        "sprocket_after_track": 2, "notes": "端到端测试"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(client, tape_id, img, order=None):
    params = {} if order is None else {"order": order}
    r = client.post(f"/tapes/{tape_id}/segments", params=params,
                    files={"file": ("seg.png", to_png_bytes(img),
                                    "image/png")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_builtin_ita2_seeded(client):
    r = client.get("/codetables")
    assert any(t["name"] == "ITA2" and t["is_builtin"] for t in r.json())


def test_custom_codetable(client):
    r = client.post("/codetables", json={
        "name": "CUSTOM5", "tracks": 5,
        "ltrs": {"3": "A", "4": " ", "5": "B"}, "figs": {"3": "1"},
        "ltrs_code": 31, "figs_code": 27})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r2 = client.get(f"/codetables/{tid}")
    assert r2.json()["definition"]["ltrs"]["3"] == "A"
    # 重名冲突
    assert client.post("/codetables", json={
        "name": "CUSTOM5", "tracks": 5}).status_code == 409


def test_full_pipeline_single_segment(client):
    text = "THE QUICK BROWN FOX 1945"
    img, codes = render_text_tape(text, dpi=300, angle_deg=1.2, noise=8.0)
    tape_id = _make_tape(client)
    _upload(client, tape_id, img)

    r = client.post(f"/tapes/{tape_id}/recognize", json={})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["column_count"] == len(codes)

    r = client.get(f"/jobs/{job['id']}/text")
    assert r.json()["text"] == text

    # 每列有孔位、置信度、原图坐标
    cols = client.get(f"/jobs/{job['id']}/columns").json()
    assert len(cols) == len(codes)
    for col in cols:
        assert len(col["bits"]) == 5
        assert 0.0 <= col["confidence"] <= 1.0
        assert len(col["bit_positions"]) == 5
        assert col["sources"], "每列应可追溯到扫描段"


def test_stitched_segments_pipeline(client):
    text = "SEGMENTED TAPE READING 88"
    img, codes = render_text_tape(text, dpi=300)
    segs = split_segments(img, 3, overlap_px=180)
    tape_id = _make_tape(client, "分段纸带")
    for i, s in enumerate(segs):
        _upload(client, tape_id, s, order=i)

    r = client.post(f"/tapes/{tape_id}/recognize", json={"min_overlap": 2})
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    r = client.get(f"/jobs/{job_id}/text")
    assert r.json()["text"] == text, r.json()["text"]


def test_revision_and_lock(client):
    text = "LOCK ME"
    img, codes = render_text_tape(text, dpi=300)
    tape_id = _make_tape(client, "修订纸带")
    _upload(client, tape_id, img)
    job_id = client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]

    # 人工修订第 0 列并锁定
    r = client.post(f"/jobs/{job_id}/revisions", json={
        "column_index": 0, "revised_bits": "10000", "locked": True,
        "author": "archivist", "reason": "原图第1道孔清晰可辨"})
    assert r.status_code == 201, r.text
    assert r.json()["original_bits"] != "10000"

    cols = client.get(f"/jobs/{job_id}/columns").json()
    assert cols[0]["bits"] == "10000"
    assert cols[0]["locked"] is True
    assert cols[0]["revised_bits"] == "10000"

    # 修订历史可查
    revs = client.get(f"/jobs/{job_id}/revisions").json()
    assert len(revs) == 1 and revs[0]["author"] == "archivist"

    # 非法位串被拒
    assert client.post(f"/jobs/{job_id}/revisions", json={
        "column_index": 0, "revised_bits": "123"}).status_code == 422


def test_repair_enumeration(client):
    """撕裂区造成缺孔 -> 诊断 -> 修复枚举给出候选，且不改动原识别。"""
    from punchtape.app.codetable import ita2_table
    from punchtape.app.decoding import encode_text
    table = ita2_table()
    text = "REPAIR"
    codes = encode_text(text, table)
    # 第 1 列（'E'=00001）的孔半撕裂 -> 识别为 00000（Blank）且低置信
    img = render_tape(codes, dpi=300, faint_data_holes={(1, 0)})
    tape_id = _make_tape(client, "修复纸带")
    _upload(client, tape_id, img)
    job_id = client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]

    raw_text = client.get(f"/jobs/{job_id}/text").json()["text"]
    assert raw_text != text  # 缺孔导致误读

    r = client.post(f"/jobs/{job_id}/repair", json={
        "rules": {"must_contain": ["REP"], "max_flips_per_column": 1,
                  "conf_threshold": 0.95, "max_candidates": 500}})
    assert r.status_code == 201, r.text
    body = r.json()
    texts = {c["text"] for c in body["candidates"]}
    assert text in texts, f"正确文本应在候选中: {texts}"
    assert len(body["candidates"]) >= 1

    # 不自动覆盖：识别结果保持不变
    assert client.get(f"/jobs/{job_id}/text").json()["text"] == raw_text

    # 修复运行存档可查
    runs = client.get(f"/jobs/{job_id}/repairs").json()
    assert len(runs) == 1 and runs[0]["candidates"]


def test_diagnostics_for_damage(client):
    """缺走纸孔 + 撕裂块 -> 诊断记录。"""
    from punchtape.app.codetable import ita2_table
    from punchtape.app.decoding import encode_text
    codes = encode_text("DAMAGE REPORT", ita2_table())
    img = render_tape(codes, dpi=300, missing_sprockets={5, 6},
                      torn_cols={9})
    tape_id = _make_tape(client, "损伤纸带")
    _upload(client, tape_id, img)
    job_id = client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]
    diags = client.get(f"/jobs/{job_id}/diagnostics").json()
    types = {d["type"] for d in diags}
    assert "torn_region" in types or "missing_hole" in types


def test_export_json_and_txt(client):
    text = "EXPORT ME 5"
    img, codes = render_text_tape(text, dpi=300)
    tape_id = _make_tape(client, "导出纸带")
    _upload(client, tape_id, img)
    job_id = client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]
    client.post(f"/jobs/{job_id}/revisions", json={
        "column_index": 0, "revised_bits": "00100", "locked": False,
        "author": "a", "reason": "测试导出"})

    r = client.get(f"/jobs/{job_id}/export", params={"format": "json"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["raw_matrix"]) == len(codes)
    assert len(data["revised_matrix"]) == len(codes)
    assert data["revised_matrix"][0] == "00100"
    assert "decoded_text" in data and "diagnostics" in data
    # 逐字符追溯
    assert len(data["trace"]) == len(codes)
    assert data["trace"][0]["sources"]

    r = client.get(f"/jobs/{job_id}/export", params={"format": "txt"})
    assert r.status_code == 200
    assert "解码文本" in r.text and "诊断记录" in r.text

    assert client.get(f"/jobs/{job_id}/export",
                      params={"format": "xml"}).status_code == 422


def test_recognize_without_segments_fails(client):
    tape_id = _make_tape(client, "空纸带")
    r = client.post(f"/tapes/{tape_id}/recognize", json={})
    assert r.status_code == 422


def test_traceability_character_to_segment(client):
    """逐字符追溯：每列都能指回扫描段编号与原图坐标。"""
    text = "TRACE"
    img, codes = render_text_tape(text, dpi=300)
    segs = split_segments(img, 2, overlap_px=150)
    tape_id = _make_tape(client, "追溯纸带")
    for i, s in enumerate(segs):
        _upload(client, tape_id, s, order=i)
    job_id = client.post(f"/tapes/{tape_id}/recognize",
                         json={"min_overlap": 2}).json()["id"]
    data = client.get(f"/jobs/{job_id}/export",
                      params={"format": "json"}).json()
    seg_ids = set()
    for ch in data["trace"]:
        assert ch["sources"], "每列须有来源段"
        for seg_id, col_idx in ch["sources"]:
            seg_ids.add(seg_id)
            assert isinstance(col_idx, int)
        assert len(ch["bit_positions"]) == 5
    assert len(seg_ids) >= 1
