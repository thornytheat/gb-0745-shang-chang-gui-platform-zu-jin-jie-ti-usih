import io
import json
from pathlib import Path

import pytest
from app.modules.m7_publish.service import calculate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def exit_source():
    return (ROOT / "sample_data" / "exit.json").read_bytes()


@pytest.fixture(scope="session")
def exit_run(exit_source):
    return calculate(exit_source, "2026-11")


def test_final_month_prorated_and_summary(exit_run):
    rows = {row["柜台"]: row for row in exit_run["退租清算"]}
    assert set(rows) == {"101", "102"}
    # 101 实际经营 20 天：保底 600×20/30=400 元高于抽成 168 元，全月欠缴 400 元
    assert rows["101"]["合同终止日"] == "2026-11-21"
    assert rows["101"]["保证金分"] == 100000
    assert rows["101"]["应扣合计分"] == 95000
    assert rows["101"]["实扣合计分"] == 95000
    assert rows["101"]["还欠分"] == 0
    assert rows["101"]["应退分"] == 5000
    assert rows["101"]["清算状态"] == "结清退还"


def test_waterfall_order_when_deposit_short(exit_run):
    details = [row for row in exit_run["清算明细"] if row["柜台"] == "102"]
    assert [row["项目"] for row in details] == [
        "旧账欠款",
        "末期租金及抽成",
        "迟到退单预留",
        "场地恢复预留",
        "遗留售后预留",
    ]
    assert [(row["应扣分"], row["实扣分"], row["未覆盖分"]) for row in details] == [
        (20000, 20000, 0),
        (25000, 10000, 15000),
        (4000, 0, 4000),
        (10000, 0, 10000),
        (5000, 0, 5000),
    ]
    row = next(row for row in exit_run["退租清算"] if row["柜台"] == "102")
    assert (row["还欠分"], row["应退分"], row["清算状态"]) == (34000, 0, "欠费挂账")
    totals = exit_run["清算汇总"]
    assert totals["实扣合计分"] == 125000
    assert totals["还欠合计分"] == 34000
    assert totals["应退合计分"] == 5000


def test_unclear_records_suspended_not_merged(exit_run):
    holds = [(row["柜台"], row["原因码"]) for row in exit_run["清算挂起"]]
    assert holds == [
        ("103", "EXIT_NO_DEPOSIT"),
        ("104", "EXIT_NO_DATE"),
        ("105", "EXIT_DUP"),
        ("105", "EXIT_DUP"),
    ]
    settled = {row["柜台"] for row in exit_run["退租清算"]}
    assert settled.isdisjoint(shop for shop, _ in holds)
    assert exit_run["清算汇总"]["挂起柜台"] == 4


def test_late_refund_trace_and_source_records_kept(exit_run):
    line = next(
        row for row in exit_run["清算明细"] if row["柜台"] == "101" and row["项目"] == "迟到退单预留"
    )
    assert line["应扣分"] == 20000 and "R201A" in line["溯源"] and "S101A" in line["溯源"]
    # 已出的月结与原始收银记录仍在同一批次内可追
    assert any(row["流水号"] == "S101A" for row in exit_run["收银行"])
    assert any(row["柜台"] == "101" and row["应缴分"] == 40000 for row in exit_run["柜台对账"])
    assert {row["流水号"] for row in exit_run["截止后"]} == {"R201A", "R202A"}


def test_rerun_never_double_charges(client):
    first = client.post("/api/runs", json={"sample": "exit", "period": "2026-11"})
    second = client.post("/api/runs", json={"sample": "exit", "period": "2026-11"})
    assert [first.status_code, second.status_code] == [201, 200]
    assert second.json["id"] == first.json["id"] and second.json["reused"] is True
    assert len(client.get("/api/runs").json) == 1
    settled = client.get(f"/api/exit/{first.json['id']}").json
    assert settled["清算汇总"]["实扣合计分"] == 125000
    assert settled["清算汇总"]["应退合计分"] == 5000


def test_failed_generation_restores_previous(client, exit_source):
    good = client.post("/api/runs", json={"sample": "exit"}).json
    broken = json.loads(exit_source)
    broken["exits"] = {"shop": "101"}
    response = client.post(
        "/api/runs", data={"file": (io.BytesIO(json.dumps(broken).encode()), "broken.json")}
    )
    assert response.status_code == 422 and response.json["code"] == "BAD_SECTION"
    assert client.get(f"/api/exit/{good['id']}").status_code == 200
    assert len(client.get("/api/runs").json) == 1
    rerun = client.post("/api/runs", json={"sample": "exit"})
    assert rerun.status_code == 200 and rerun.json["id"] == good["id"]


def test_exit_api_and_workbench(client):
    run = client.post("/api/runs", json={"sample": "exit"}).json
    api = client.get(f"/api/exit/{run['id']}")
    assert api.status_code == 200
    assert {"退租清算", "清算明细", "清算挂起", "清算汇总"} <= set(api.json)
    assert client.get(f"/work/exit?run={run['id']}").status_code == 200


def test_export_keeps_exit_evidence(client):
    run = client.post("/api/runs", json={"sample": "exit"}).json
    result = client.get(f"/api/export/{run['id']}")
    assert result.json["退租清算"] and result.json["收银行"] and result.json["柜台对账"]


def test_runs_without_exits_have_empty_sections(clean_run):
    assert clean_run["退租清算"] == [] and clean_run["清算挂起"] == []
    assert clean_run["清算汇总"]["撤场柜台"] == 0


def crafted(exits, events, statement):
    return json.dumps(
        {
            "contracts": [
                {
                    "shop": "201",
                    "version": "E-1",
                    "start": "2026-11-01",
                    "end": "2026-11-11",
                    "minimum": "0",
                    "tiers": [[None, 1000]],
                }
            ],
            "holidays": [],
            "events": events,
            "statement": statement,
            "exits": exits,
        },
        ensure_ascii=False,
    ).encode()


def test_invalid_late_refunds_not_reserved():
    raw = crafted(
        [{"shop": "201", "terminated": "2026-11-11", "deposit": "50"}],
        [
            {"id": "S1", "shop": "201", "day": "2026-11-02", "amount": "100", "kind": "sale"},
            {
                "id": "R1",
                "shop": "201",
                "day": "2026-11-03",
                "amount": "40",
                "kind": "refund",
                "origin": "S1",
            },
            {
                "id": "L1",
                "shop": "201",
                "day": "2026-12-01",
                "amount": "70",
                "kind": "refund",
                "origin": "S1",
            },
            {
                "id": "L2",
                "shop": "201",
                "day": "2026-12-02",
                "amount": "10",
                "kind": "refund",
                "origin": "NONE",
            },
        ],
        [{"shop": "201", "paid": "0"}],
    )
    run = calculate(raw, "2026-11")
    assert [row["项目"] for row in run["清算明细"]] == ["末期租金及抽成"]
    # 净销售 60 元抽成 6 元，L1 超过原单余量、L2 缺原单，均不计预留
    assert run["退租清算"][0]["应退分"] == 4400


def test_overpaid_counter_gets_credit_back():
    raw = crafted(
        [{"shop": "201", "terminated": "2026-11-11", "deposit": "50"}],
        [{"id": "S1", "shop": "201", "day": "2026-11-02", "amount": "100", "kind": "sale"}],
        [{"shop": "201", "paid": "20"}],
    )
    row = calculate(raw, "2026-11")["退租清算"][0]
    # 应计 10 元抽成，实缴 20 元，多缴 10 元与保证金一并退还
    assert row["多缴退回分"] == 1000
    assert row["应退分"] == 6000
    assert row["清算状态"] == "结清退还"
