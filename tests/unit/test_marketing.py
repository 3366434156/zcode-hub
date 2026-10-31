"""运营位（marketing/touch）套餐发现 —— 形状抓取与失败语义。

基线样本取自 2026-10-30 线上真实下发（billing/preview 同时恒空的当天）：
deliveries[].banner.buttons[].action = claim_zcode_plan，套餐元数据在
banner.background.args.zcode_plan。
"""

from __future__ import annotations

import pytest

from app import marketing, settings
from tests.conftest import seed_account

PLAN_ID = "zcode-v3-start-plan-1030-wk"


def _real_payload() -> dict:
    """线上 banner 的裁剪版（保留 claim 动作、元数据层级与一个噪音授权项）。"""
    zcode_plan = {
        "plan_id": PLAN_ID,
        "name": "ZCODE WEEKEND BUILD",
        "priority": 101,
        "starts_at": 1793358000,
        "ends_at": 1793581200,
        "entitlements": [
            {"entitlement_id": "ent-1", "show_name": "GLM-5.3-Flash", "meter": "model_usage",
             "unit_type": "token", "grant_units": 500000000, "period": "one_time",
             "effective_at": 1793372400, "priority": 101},
            {"entitlement_id": "noise", "show_name": "噪音项", "meter": "storage",
             "unit_type": "gb", "grant_units": 1, "period": "daily"},
        ],
    }
    return {
        "code": 0, "msg": "",
        "data": {
            "server_time": 1793371377,
            "language": "zh-CN",
            "deliveries": [{
                "campaign_id": "2101141259813175296",
                "resource_position": "banner",
                "priority": 50,
                "banner": {
                    "buttons": [
                        {"text": {"format": "", "content": ""},
                         "action": {"type": "close", "args": None}},
                        {"text": {"format": "", "content": ""},
                         "action": {"type": "claim_zcode_plan",
                                    "args": {"plan_id": PLAN_ID}}},
                    ],
                    "background": {"type": "bundle", "args": {"zcode_plan": zcode_plan}},
                },
            }],
        },
    }


def test_extract_claim_plan_from_real_banner():
    plans = marketing.extract_claim_plans(_real_payload())
    assert [p["plan_id"] for p in plans] == [PLAN_ID]
    plan = plans[0]
    assert plan["name"] == "ZCODE WEEKEND BUILD"
    assert plan["priority"] == 101
    # 只留 model_usage+token；storage/gb 噪音项被过滤
    assert plan["grants"] == [{"name": "GLM-5.3-Flash", "units": 500_000_000.0,
                               "period": "one_time"}]


def test_extract_ignores_display_only_delivery():
    """纯展示位（只有 close / navigate 按钮）不得被当成可领套餐。"""
    payload = {"code": 0, "data": {"deliveries": [{
        "campaign_id": "c1", "resource_position": "banner",
        "banner": {"buttons": [{"action": {"type": "close", "args": None}}],
                   "background": {"args": {"zcode_plan": {"plan_id": "display-only",
                                                          "name": "展示商品"}}}},
    }]}}
    assert marketing.extract_claim_plans(payload) == []


def test_extract_dedupes_repeated_claim_buttons():
    """同一 plan_id 在多个按钮/多个位里重复投放时只算一个套餐。"""
    payload = _real_payload()
    banner = payload["data"]["deliveries"][0]["banner"]
    banner["buttons"].append({"action": {"type": "claim_zcode_plan",
                                        "args": {"plan_id": PLAN_ID}}})
    payload["data"]["deliveries"].append({"campaign_id": "c2", "resource_position": "hero",
                                          "banner": banner})
    plans = marketing.extract_claim_plans(payload)
    assert [p["plan_id"] for p in plans] == [PLAN_ID]


def test_extract_tolerates_missing_metadata():
    """只有 plan_id（元数据位缺失）时仍要返回套餐，名称留空由上层兜底。"""
    payload = {"code": 0, "data": {"deliveries": [{
        "campaign_id": "c1",
        "banner": {"buttons": [{"action": {"type": "claim_zcode_plan",
                                          "args": {"plan_id": "p-only"}}}]},
    }]}}
    plans = marketing.extract_claim_plans(payload)
    assert plans == [{"plan_id": "p-only", "name": "", "description": "",
                      "priority": 0, "grants": []}]


def test_extract_handles_garbage():
    assert marketing.extract_claim_plans({}) == []
    assert marketing.extract_claim_plans({"code": 0}) == []
    assert marketing.extract_claim_plans({"code": 0, "data": []}) == []
    assert marketing.extract_claim_plans({"code": 0, "data": {"deliveries": "x"}}) == []


async def test_fetch_claim_plans_reads_marketing_touch(mock_server, fresh_app, monkeypatch):
    mock_app, port = mock_server
    monkeypatch.setattr(settings, "ZCODE_MARKETING_URL",
                        f"http://127.0.0.1:{port}/api/v1/marketing/touch")
    acc = seed_account(fresh_app, "h1.eyJzdWIiOiJhIn0.sig", name="mk")
    mock_app.state.marketing_plans = [
        {"plan_id": "p-low", "name": "低优先", "priority": 10, "entitlements": []},
        {"plan_id": PLAN_ID, "name": "ZCODE WEEKEND BUILD", "priority": 101,
         "entitlements": [{"show_name": "GLM-5.3-Flash", "meter": "model_usage",
                           "unit_type": "token", "grant_units": 500_000_000,
                           "period": "one_time"}]},
    ]
    try:
        plans = await marketing.fetch_claim_plans(acc)
    finally:
        mock_app.state.marketing_plans = None
    # 按优先级降序返回
    assert [p["plan_id"] for p in plans] == [PLAN_ID, "p-low"]
    assert plans[0]["grants"][0]["units"] == 500_000_000.0


async def test_fetch_claim_plans_reports_http_failure(mock_server, fresh_app, monkeypatch):
    mock_app, port = mock_server
    monkeypatch.setattr(settings, "ZCODE_MARKETING_URL",
                        f"http://127.0.0.1:{port}/api/v1/marketing/touch")
    acc = seed_account(fresh_app, "h1.eyJzdWIiOiJhIn0.sig", name="mk2")
    mock_app.state.marketing_scenario = "marketing_http500"
    try:
        with pytest.raises(marketing.MarketingError):
            await marketing.fetch_claim_plans(acc)
    finally:
        mock_app.state.marketing_scenario = None


async def test_fetch_claim_plans_reports_business_code(mock_server, fresh_app, monkeypatch):
    mock_app, port = mock_server
    monkeypatch.setattr(settings, "ZCODE_MARKETING_URL",
                        f"http://127.0.0.1:{port}/api/v1/marketing/touch")
    acc = seed_account(fresh_app, "h1.eyJzdWIiOiJhIn0.sig", name="mk3")
    mock_app.state.marketing_scenario = "marketing_badcode"
    try:
        with pytest.raises(marketing.MarketingError):
            await marketing.fetch_claim_plans(acc)
    finally:
        mock_app.state.marketing_scenario = None
