"""api_client / as_user fixture 的冒烟测试:确认能起 TestClient 并注入登录态。"""


def test_health_no_auth(api_client):
    assert api_client.get("/health").status_code == 200


def test_authenticated_request_boots(api_client, as_user):
    as_user(["研发部"])
    res = api_client.get("/api/wiki")
    assert res.status_code == 200
    assert res.json()["total"] == 0
