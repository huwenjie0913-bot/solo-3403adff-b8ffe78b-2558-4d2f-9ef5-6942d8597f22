"""纸面回放 API 端到端测试：回放、快照、机型配置、对比、导出、只读保证。"""
import pytest
from fastapi.testclient import TestClient

from punchtape.app.main import app
from punchtape.tests.synth import render_text_tape, to_png_bytes


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def job_id(client):
    img, _ = render_text_tape("HELLO WORLD 1945", dpi=300)
    tape_id = client.post("/tapes", json={"name": "回放纸带"}).json()["id"]
    r = client.post(f"/tapes/{tape_id}/segments",
                    files={"file": ("s.png", to_png_bytes(img), "image/png")})
    assert r.status_code == 201
    return client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]


def _play(client, job_id, **body):
    return client.post(f"/jobs/{job_id}/playback", json=body)


def test_builtin_profiles_seeded(client):
    profs = client.get("/terminal-profiles").json()
    names = [p["name"] for p in profs]
    assert any("Model 15" in n for n in names)
    assert all(p["is_builtin"] for p in profs)
    assert profs[0]["config"]["columns"] > 0


def test_playback_default_profile(client, job_id):
    data = _play(client, job_id).json()
    assert data["source"] == "revised"
    assert data["summary"]["printed_chars"] == 16
    assert data["summary"]["rows"] == 1
    kinds = {e["kind"] for e in data["events"]}
    assert {"print", "figs"} <= kinds
    # 空格保留为格子
    assert len([p for p in data["paper"] if p["char"] == " "]) == 2
    # 每列行列与时间
    prints = [e for e in data["events"] if e["kind"] == "print"]
    assert all(e["row"] == 0 and e["col"] == i for i, e in enumerate(prints))
    assert all(e["time_ms"] >= 0 for e in data["events"])


def test_playback_events_have_row_col_and_source(client, job_id):
    data = _play(client, job_id).json()
    for e in data["events"]:
        if e["kind"] == "print":
            assert isinstance(e["row"], int) and isinstance(e["col"], int)
            assert e["source"] in ("original", "revised")


def test_overflow_mark_wrap_truncate(client, job_id):
    marked = _play(client, job_id, config={"columns": 5}).json()
    assert marked["summary"]["overflow_count"] == 11
    assert marked["summary"]["rows"] == 1

    wrapped = _play(client, job_id,
                    config={"columns": 5, "overflow": "wrap"}).json()
    assert wrapped["summary"]["rows"] >= 3
    assert any(e["injected"] for e in wrapped["events"])

    truncated = _play(client, job_id,
                      config={"columns": 5, "overflow": "truncate"}).json()
    assert truncated["summary"]["line_lengths"] == [5]


def test_cr_timing_unsettled(client):
    # 高速波特 + 慢回程：CR 后字符在回程途中到达
    from punchtape.app.codetable import ita2_table
    from punchtape.app.decoding import encode_text
    from punchtape.tests.synth import render_tape
    codes = encode_text("ABC", ita2_table()) + [0x08] + encode_text("X",
                                                                     ita2_table())
    img = render_tape(codes, dpi=300)
    tape_id = client.post("/tapes", json={"name": "时序纸带"}).json()["id"]
    client.post(f"/tapes/{tape_id}/segments",
                files={"file": ("s.png", to_png_bytes(img), "image/png")})
    jid = client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]
    r = _play(client, jid, config={"baud": 300, "cr_ms": 200})
    assert any(i["type"] == "cr_unsettled_print" for i in r.json()["issues"])


def test_snapshot_save_list_get_delete(client, job_id):
    sid = _play(client, job_id, save_as="对照 T15").json()["snapshot_id"]
    assert sid
    snaps = client.get(f"/jobs/{job_id}/playback/snapshots").json()
    assert any(s["id"] == sid and s["name"] == "对照 T15" for s in snaps)
    got = client.get(f"/playback/snapshots/{sid}").json()
    assert got["snapshot_id"] == sid
    assert got["summary"]["printed_chars"] == 16
    assert client.delete(f"/playback/snapshots/{sid}").status_code == 204
    assert client.get(f"/playback/snapshots/{sid}").status_code == 404


def test_snapshot_exports_txt_json_svg(client, job_id):
    sid = _play(client, job_id, save_as="导出用",
                config={"columns": 8}).json()["snapshot_id"]
    txt = client.get(f"/playback/snapshots/{sid}/export",
                     params={"format": "txt"})
    assert txt.status_code == 200
    assert "纸面" in txt.text and "控制轨迹" in txt.text and "问题清单" in txt.text
    assert "│" in txt.text  # 边界保留空格

    js = client.get(f"/playback/snapshots/{sid}/export",
                    params={"format": "json"})
    assert js.status_code == 200 and js.json()["snapshot_id"] == sid
    assert js.json()["paper"] and js.json()["events"]

    svg = client.get(f"/playback/snapshots/{sid}/export",
                     params={"format": "svg"})
    assert svg.status_code == 200 and "image/svg+xml" in svg.headers["content-type"]
    assert svg.text.startswith("<?xml") is False
    assert svg.text.lstrip().startswith("<svg") and "</svg>" in svg.text

    assert client.get(f"/playback/snapshots/{sid}/export",
                      params={"format": "pdf"}).status_code == 422


def test_export_txt_shows_overstrike_layer(client, job_id):
    # 宽行 + CR-only 报文：另建一条带单独 CR 的任务更直接
    from punchtape.app.codetable import ita2_table
    from punchtape.app.decoding import encode_text
    from punchtape.tests.synth import render_tape
    codes = encode_text("ABC", ita2_table()) + [0x08] + encode_text("XY",
                                                                     ita2_table())
    img = render_tape(codes, dpi=300)
    tape_id = client.post("/tapes", json={"name": "重打纸带"}).json()["id"]
    client.post(f"/tapes/{tape_id}/segments",
                files={"file": ("s.png", to_png_bytes(img), "image/png")})
    jid = client.post(f"/tapes/{tape_id}/recognize", json={}).json()["id"]
    sid = _play(client, jid, save_as="重打",
                config={"cr_ms": 50}).json()["snapshot_id"]
    txt = client.get(f"/playback/snapshots/{sid}/export",
                     params={"format": "txt"}).text
    assert "重打层" in txt or "|" in txt
    assert "lone_cr_overstrike" in txt


def test_profile_crud_and_overrides(client, job_id):
    r = client.post("/terminal-profiles", json={
        "name": "资料室 50 波特",
        "config": {"columns": 40, "baud": 50, "auto_lf": True}})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.json()["is_builtin"] is False

    run = _play(client, job_id, profile_id=pid).json()
    assert run["config"]["columns"] == 40 and run["config"]["auto_lf"] is True

    # 请求内字段覆盖机型配置
    run = _play(client, job_id, profile_id=pid,
                config={"columns": 99}).json()
    assert run["config"]["columns"] == 99
    assert run["config"]["auto_lf"] is True  # 其余沿用机型

    assert client.post("/terminal-profiles",
                       json={"name": "资料室 50 波特"}).status_code == 409
    builtin = client.get("/terminal-profiles").json()[0]["id"]
    assert client.delete(f"/terminal-profiles/{builtin}").status_code == 409
    assert client.delete(f"/terminal-profiles/{pid}").status_code == 204
    assert client.get(f"/terminal-profiles/{pid}").status_code == 404


def test_validation_errors(client, job_id):
    assert _play(client, job_id, config={"columns": 0}).status_code == 422
    assert _play(client, job_id, config={"baud": -1}).status_code == 422
    assert _play(client, job_id, config={"overflow": "x"}).status_code == 422
    assert _play(client, job_id, source="raw").status_code == 422
    assert client.post("/jobs/99999/playback", json={}).status_code == 404
    assert _play(client, job_id, profile_id=99999).status_code == 404


def test_diff_configs(client, job_id):
    d = client.post(f"/jobs/{job_id}/playback/diff", json={
        "a": {"config": {"columns": 80, "overflow": "wrap"}},
        "b": {"config": {"columns": 8, "overflow": "wrap"}}}).json()
    assert d["paper_cell_diff_count"] > 0
    assert d["summary_a"]["rows"] != d["summary_b"]["rows"]
    assert "event_diff" in d and "paper_cell_diff" in d


def test_original_vs_revised_and_readonly(client, job_id):
    client.post(f"/jobs/{job_id}/revisions", json={
        "column_index": 0, "revised_bits": "00100"})  # 改为空格码
    ro = _play(client, job_id, source="original").json()
    rr = _play(client, job_id, source="revised").json()
    assert ro["paper"][0]["char"] != rr["paper"][0]["char"]
    assert rr["paper"][0]["char"] == " "
    assert rr["paper"][0]["source"] == "revised"
    assert rr["paper"][1]["source"] == "original"

    d = client.post(f"/jobs/{job_id}/playback/diff", json={
        "a": {"source": "original"}, "b": {"source": "revised"}}).json()
    assert d["paper_cell_diff_count"] >= 1

    # 回放与快照不改动识别任务和人工修订
    raw = client.get(f"/jobs/{job_id}/text", params={"revised": False}).json()
    revised = client.get(f"/jobs/{job_id}/text").json()
    assert raw["text"].startswith("HELLO")  # 原始识别文本不变
    assert revised["text"].startswith(" ELLO")  # 修订体现在修订视图
    revs = client.get(f"/jobs/{job_id}/revisions").json()
    assert len(revs) == 1
