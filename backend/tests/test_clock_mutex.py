from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

# seed 数据：2026-11-15 有 1 笔 88.0；2026-11-16（初始钟面）有 5 笔合计 489.4，均未封。
SEED_DAY = "2026-11-16"
YESTERDAY = "2026-11-15"
YESTERDAY_TOTAL = 88.0
TODAY_TOTAL = 489.4


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """独立临时库的 TestClient：整包卸载后用 DAYSEAL_DATA_DIR 指向临时目录重新导入。"""
    monkeypatch.setenv("DAYSEAL_DATA_DIR", str(tmp_path))
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    main = importlib.import_module("app.main")
    import app.config as config
    assert config.DB_PATH == tmp_path / "dayseal.db"
    with TestClient(main.app) as c:
        yield c


def _set_day(client, day):
    return client.put("/api/settings/business-day", json={"business_day": day})


def _line(client, day, amount=100.0):
    return client.post(
        "/api/lines",
        json={"till_code": "TILL-A", "amount": amount, "pay_method": "现金", "business_day": day},
    )


def _seal(client, day, declared):
    return client.post(
        "/api/close/seal",
        json={"business_day": day, "declared_cash": declared},
    )


def _seal_seed_days(client):
    """封掉所有早于下一营业日的 seed 日，使「无未封」前置条件成立。"""
    assert _seal(client, YESTERDAY, YESTERDAY_TOTAL).status_code == 200
    assert _seal(client, SEED_DAY, TODAY_TOTAL).status_code == 200


def test_reject_advance_with_unsealed_yesterday_clock_unchanges_with_receipt(client):
    # 先造「未封昨日」场景：钟面 11-16，11-15 有流水未封 → 拨向 11-17 被拒
    r = _set_day(client, "2026-11-17")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["business_day"] == SEED_DAY
    assert body["target_business_day"] == "2026-11-17"
    days = [u["business_day"] for u in body["unsealed_days"]]
    assert YESTERDAY in days and SEED_DAY in days
    # 钟面不变
    assert client.get("/api/settings/business-day").json()["business_day"] == SEED_DAY
    for u in body["unsealed_days"]:
        assert "system_total" in u and "draft_failed" in u


def test_allow_advance_after_all_earlier_days_sealed_and_list_clears(client):
    _seal_seed_days(client)
    r = _set_day(client, "2026-11-17")
    assert r.status_code == 200
    assert r.json()["business_day"] == "2026-11-17"
    assert client.get("/api/settings/business-day").json()["business_day"] == "2026-11-17"
    assert client.get("/api/close/unsealed").json()["unsealed_days"] == []


def test_advance_with_no_unsealed_succeeds_across_multiple_days(client):
    _seal_seed_days(client)
    assert _set_day(client, "2026-11-17").status_code == 200
    r2 = _set_day(client, "2026-11-20")
    assert r2.status_code == 200
    assert r2.json()["business_day"] == "2026-11-20"
    assert r2.json()["unsealed_days"] == []


def test_dashboard_and_close_share_same_persisted_unsealed_scan(client):
    _set_day(client, "2026-11-17")  # 被拒，钟面仍是 11-16
    dash = client.get("/api/dashboard").json()
    unsealed_endpoint = client.get("/api/close/unsealed").json()["unsealed_days"]
    assert dash["unsealed_days"] == unsealed_endpoint
    y15 = next(u for u in unsealed_endpoint if u["business_day"] == YESTERDAY)
    assert y15["system_total"] == YESTERDAY_TOTAL
    assert y15["line_count"] == 1
    assert y15["draft_failed"] is False

    # 封账后三处同源清单同步消失（两日全封后清空）
    _seal_seed_days(client)
    assert client.get("/api/dashboard").json()["unsealed_days"] == []
    assert client.get("/api/close/unsealed").json()["unsealed_days"] == []


def test_sealed_day_batch_untouched_by_later_clock_advance(client):
    seal_resp = _seal(client, YESTERDAY, 90.0).json()
    batch_id = seal_resp["batch_id"]
    _seal(client, SEED_DAY, TODAY_TOTAL)
    assert _set_day(client, "2026-11-17").status_code == 200  # 放行
    batches = client.get("/api/close/batches").json()
    b = next(x for x in batches if x["id"] == batch_id)
    assert b["business_day"] == YESTERDAY
    assert b["status"] == "sealed"
    assert b["declared_cash"] == 90.0  # 已 sealed 日不被拨钟改批
    # 已封日仍禁止改写流水
    assert _line(client, YESTERDAY, 5.0).status_code == 409


def test_draft_failure_persists_in_lists_then_clears_on_seal(client):
    bad = _seal(client, YESTERDAY, "not-a-number")
    assert bad.status_code == 400
    r = _set_day(client, "2026-11-17")
    assert r.status_code == 409
    u15 = next(u for u in r.json()["detail"]["unsealed_days"] if u["business_day"] == YESTERDAY)
    assert u15["draft_failed"] is True
    assert u15["draft_failure_reason"]
    # 总览同源展示
    d15 = next(u for u in client.get("/api/dashboard").json()["unsealed_days"]
               if u["business_day"] == YESTERDAY)
    assert d15["draft_failed"] is True
    # 正常封账后标记清除，全部收口后清单清空
    _seal_seed_days(client)
    assert client.get("/api/close/unsealed").json()["unsealed_days"] == []


def test_unsealed_day_still_accepts_supplemental_lines_until_sealed(client):
    # 拨钟被拒不影响给昨日补流水
    assert _line(client, YESTERDAY, 12.0).status_code == 200
    y15 = next(u for u in client.get("/api/close/unsealed").json()["unsealed_days"]
               if u["business_day"] == YESTERDAY)
    assert y15["system_total"] == YESTERDAY_TOTAL + 12.0
    assert y15["line_count"] == 2
    # 封账后禁写
    _seal(client, YESTERDAY, YESTERDAY_TOTAL + 12.0)
    assert _line(client, YESTERDAY, 1.0).status_code == 409


def test_backward_and_same_day_moves_are_not_mutex_blocked(client):
    _seal_seed_days(client)
    assert _set_day(client, "2026-11-17").status_code == 200
    # 回拨与同日设置不受「有未封则拒拨」影响
    assert _set_day(client, "2026-11-10").status_code == 200
    assert _set_day(client, "2026-11-10").status_code == 200
    assert client.get("/api/settings/business-day").json()["business_day"] == "2026-11-10"


def test_malformed_day_rejected_without_clock_change(client):
    r = _set_day(client, "2026/11/17")
    assert r.status_code == 400
    assert client.get("/api/settings/business-day").json()["business_day"] == SEED_DAY


def test_empty_day_rejected(client):
    r = _set_day(client, "   ")
    assert r.status_code == 400
    assert client.get("/api/settings/business-day").json()["business_day"] == SEED_DAY


def test_fixture_rejected_clock_must_not_move_and_receipt_lists_real_unsealed(client):
    """夹具「拒拨钟已变」：409 回执、钟面、未封扫描三者必须一致。"""
    before = client.get("/api/settings/business-day").json()["business_day"]
    r = _set_day(client, "2026-11-17")
    assert r.status_code == 409
    receipt = r.json()["detail"]
    # 钟面回到/留在拨前值，禁止「回执失败钟已变」
    assert client.get("/api/settings/business-day").json()["business_day"] == before
    # 回执不得藏未封：真实未封日原样列出且字段完整
    receipt_days = [u["business_day"] for u in receipt["unsealed_days"]]
    assert receipt_days == [YESTERDAY, SEED_DAY]
    # 这些日确实仍无 sealed 现行批
    for d in receipt_days:
        assert client.get(f"/api/close/preview?business_day={d}").json()["sealed"] is False
    # 回执清单与未封扫描入口、总览完全同源
    assert receipt["unsealed_days"] == client.get("/api/close/unsealed").json()["unsealed_days"]
    assert receipt["unsealed_days"] == client.get("/api/dashboard").json()["unsealed_days"]


def test_fixture_successful_advance_rebuilds_empty_lists_and_clock_moves(client):
    """夹具「成功清单空」：拨钟成功后钟面跳变，三处清单按扫描重建为空。"""
    _seal_seed_days(client)
    r = _set_day(client, "2026-11-17")
    assert r.status_code == 200
    body = r.json()
    assert body["business_day"] == "2026-11-17"
    assert body["previous_business_day"] == SEED_DAY
    assert body["unsealed_days"] == []
    # 钟面确已跳到目标日
    assert client.get("/api/settings/business-day").json()["business_day"] == "2026-11-17"
    # 总览与封账入口清单同步为空，禁止「钟已跳而清单空」之外的藏法——此处空是扫描真值
    assert client.get("/api/dashboard").json()["unsealed_days"] == []
    assert client.get("/api/close/unsealed").json()["unsealed_days"] == []


def test_fixture_lists_never_contain_sealed_days_on_any_surface(client):
    """夹具「清单含已封」：sealed 现行批存在的日不得出现在任一未封清单。"""
    # 封昨日、留当日未封：清单只能含当日
    assert _seal(client, YESTERDAY, YESTERDAY_TOTAL).status_code == 200
    r = _set_day(client, "2026-11-17")
    assert r.status_code == 409
    receipt_days = [u["business_day"] for u in r.json()["detail"]["unsealed_days"]]
    assert receipt_days == [SEED_DAY]
    assert YESTERDAY not in receipt_days

    scan_days = [u["business_day"] for u in client.get("/api/close/unsealed").json()["unsealed_days"]]
    dash_days = [u["business_day"] for u in client.get("/api/dashboard").json()["unsealed_days"]]
    assert scan_days == [SEED_DAY]
    assert dash_days == [SEED_DAY]

    # 全部封账后三表面皆空；已封日再扫也不会回流
    assert _seal(client, SEED_DAY, TODAY_TOTAL).status_code == 200
    assert _set_day(client, "2026-11-17").status_code == 200
    assert client.get("/api/close/unsealed").json()["unsealed_days"] == []
    assert client.get("/api/dashboard").json()["unsealed_days"] == []
    assert client.get("/api/settings/business-day").json()["business_day"] == "2026-11-17"

