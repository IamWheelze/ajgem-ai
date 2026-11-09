"""NF08 operator metrics: aggregates only, route templates not ids, token-protected and off by default."""

from __future__ import annotations

from .conftest import make_pdf


def test_metrics_are_off_by_default(client):
    assert client.get("/metrics").status_code == 404


def test_metrics_report_aggregates_without_tenant_data(team, app, client):
    app.state.ctx.settings.metrics_token = "s3cret-token"
    try:
        an, pid = team["analyst"], team["pid"]
        an.upload(pid, make_pdf([["Certificate No: CERT-M", "KW-111 Li 1234 ppm"]]), "m.pdf")
        team["outsider"].get(f"/projects/{pid}")  # a refused cross-tenant request
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = client.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        body = r.text
        import re

        def value(series: str) -> float:
            m = re.search(rf"^{re.escape(series)} (\S+)$", body, re.M)
            assert m, series
            return float(m.group(1))

        assert value('ajgem_http_requests_total{method="GET",route="/projects/{pid}",status="403"}') >= 1
        assert value('ajgem_api_errors_total{code="AUTH_FORBIDDEN"}') >= 1
        assert value('ajgem_http_request_duration_seconds_count{method="POST",route="/uploads/{uid}/complete"}') >= 1
        assert value("ajgem_review_queue_records") >= 1
        assert value('ajgem_usage_month_quantity{category="pages"}') >= 1
        assert value('ajgem_jobs{state="needs_review"}') >= 1
        # No ids, names or evidence.
        for leak in (pid, team["org"], "Kano", "KW-111", "1234", "CERT-M", "analyst@"):
            assert leak not in body, leak
    finally:
        app.state.ctx.settings.metrics_token = None
