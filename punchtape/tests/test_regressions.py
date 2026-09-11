"""缺陷回归测试：

1. 完整缺孔（高置信 0）在人工指定孔列后可枚举补孔候选；
2. 识别任务的 initial_shift 被保留：text/预览/导出在 figs 初态下
   把码字 1 解为 '3' 而非 'E'；
3. perspective_corners 与 sprocket_y_hint 持久化到任务 params；
4. 纯文本导出同时包含原始孔阵与修订孔阵。
"""
import pytest
from fastapi.testclient import TestClient

from punchtape.app.codetable import ita2_table
from punchtape.app.decoding import encode_text
from punchtape.app.main import app
from punchtape.app.repair import RepairRules, enumerate_repairs
from punchtape.tests.synth import render_tape, render_text_tape, to_png_bytes


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_tape(client, name):
    r = client.post("/tapes", json={"name": name, "tracks": 5, "dpi": 300})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(client, tape_id, img):
    r = client.post(f"/tapes/{tape_id}/segments",
                    files={"file": ("seg.png", to_png_bytes(img), "image/png")})
    assert r.status_code == 201, r.text


# ------------------------------------------------------------- 缺陷 1
def test_clean_missing_hole_repairable_when_column_specified(client):
    """完整缺孔被识为高置信 0；人工指定该列后应枚举出补孔候选。"""
    table = ita2_table()
    codes = encode_text("BE", table)          # B=11001, E=00001
    # 第 1 列（E）的第 0 道孔完整缺失 -> 识读为 00000（Blank）且高置信
    img = render_tape(codes, dpi=300, missing_data_holes={(1, 0)})
    tape_id = _make_tape(client, "完整缺孔纸带")
    _upload(client, tape_id, img)
    job_id = client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]

    cols = client.get(f"/jobs/{job_id}/columns").json()
    assert cols[1]["bits"] == "00000"
    assert min(cols[1]["bit_confidence"]) >= 0.9, "完整缺孔应表现为高置信 0"

    # 不指定列：自动选取找不到可疑列
    r = client.post(f"/jobs/{job_id}/repair", json={
        "rules": {"must_contain": ["E"], "max_flips_per_column": 1}})
    assert r.status_code == 201, r.text
    assert "BE" not in {c["text"] for c in r.json()["candidates"]}

    # 人工指定第 1 列：高置信 0 也参与翻转，补孔候选出现
    r = client.post(f"/jobs/{job_id}/repair", json={
        "columns": [1],
        "rules": {"must_contain": ["E"], "max_flips_per_column": 1}})
    assert r.status_code == 201, r.text
    texts = {c["text"] for c in r.json()["candidates"]}
    assert "BE" in texts, f"人工指定列后应枚举出补孔结果: {texts}"
    # 识别结果不被自动覆盖
    assert client.get(f"/jobs/{job_id}/text").json()["text"] == "B"


def test_enumerate_repairs_force_variants_unit():
    """单元级：人工指定列时所有位可翻转，不受 conf_threshold 限制。"""
    table = ita2_table()
    cols = [
        {"bits": (1, 0, 0, 1, 1), "bit_confidence": [0.95] * 5,  # B
         "alternatives": [], "locked": False},
        {"bits": (0, 0, 0, 0, 0), "bit_confidence": [1.0] * 5,   # 缺孔->Blank
         "alternatives": [], "locked": False},
    ]
    result = enumerate_repairs(
        cols, table, RepairRules(must_contain=["E"], max_flips_per_column=1),
        ambiguous=[1])
    assert "BE" in {c.text for c in result.candidates}


# ------------------------------------------------------------- 缺陷 2
def test_initial_shift_figs_preserved_everywhere(client):
    """initial_shift=figs 时，text/预览/导出都把码字 1 解为 '3'。"""
    img = render_tape([1, 1, 1], dpi=300)     # figs 初态下为 "333"
    tape_id = _make_tape(client, "数字移位纸带")
    _upload(client, tape_id, img)
    r = client.post(f"/tapes/{tape_id}/recognize",
                    json={"initial_shift": "figs"})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["text_preview"] == "333", "任务预览应使用 figs 初态"

    assert client.get(f"/jobs/{job['id']}/text").json()["text"] == "333"
    assert client.get(f"/jobs/{job['id']}").json()["text_preview"] == "333"

    data = client.get(f"/jobs/{job['id']}/export",
                      params={"format": "json"}).json()
    assert data["decoded_text"] == "333"
    assert data["raw_text"] == "333"
    assert data["params"]["initial_shift"] == "figs"

    # 对照：ltrs 初态下同样的孔阵解为 "EEE"
    tape2 = _make_tape(client, "字母移位纸带")
    _upload(client, tape2, img)
    job2 = client.post(f"/tapes/{tape2}/recognize", json={}).json()
    assert client.get(f"/jobs/{job2['id']}/text").json()["text"] == "EEE"


# ------------------------------------------------------------- 缺陷 3
def test_imaging_params_fully_persisted(client):
    """perspective_corners 与 sprocket_y_hint 写入任务 params。"""
    img, codes = render_text_tape("PARAMS", dpi=300)
    h, w = img.shape
    corners = [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]
    tape_id = _make_tape(client, "参数持久化纸带")
    _upload(client, tape_id, img)
    r = client.post(f"/tapes/{tape_id}/recognize", json={
        "perspective_corners": corners, "sprocket_y_hint": 105.0,
        "initial_shift": "ltrs"})
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]

    params = client.get(f"/jobs/{job_id}/export",
                        params={"format": "json"}).json()["params"]
    assert params["perspective_corners"] == corners
    assert params["sprocket_y_hint"] == 105.0
    assert params["initial_shift"] == "ltrs"
    assert params["tracks"] == 5 and params["dpi"] == 300
    # 配置完整可恢复：识别结果仍正确
    assert client.get(f"/jobs/{job_id}/text").json()["text"] == "PARAMS"


# ------------------------------------------------------------- 缺陷 4
def test_txt_export_contains_raw_and_revised_matrices(client):
    """纯文本导出同时包含原始孔阵与修订孔阵。"""
    img, codes = render_text_tape("MATRIX", dpi=300)
    tape_id = _make_tape(client, "孔阵导出纸带")
    _upload(client, tape_id, img)
    job_id = client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]

    cols = client.get(f"/jobs/{job_id}/columns").json()
    original_bits = cols[0]["bits"]
    revised_bits = "11110" if original_bits != "11110" else "00001"
    client.post(f"/jobs/{job_id}/revisions", json={
        "column_index": 0, "revised_bits": revised_bits,
        "author": "tester", "reason": "回归测试"})

    txt = client.get(f"/jobs/{job_id}/export",
                     params={"format": "txt"}).text
    assert "== 原始孔阵（1=有孔） ==" in txt
    assert "== 修订孔阵（1=有孔） ==" in txt
    raw_section = txt.split("== 原始孔阵（1=有孔） ==")[1] \
                     .split("== 修订孔阵")[0]
    revised_section = txt.split("== 修订孔阵（1=有孔） ==")[1] \
                         .split("== 诊断记录")[0]
    assert f"    0: {original_bits}" in raw_section
    assert f"    0: {revised_bits}" in revised_section
    assert "人工修订" in revised_section
