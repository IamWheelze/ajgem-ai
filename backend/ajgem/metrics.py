"""Operator metrics in the Prometheus text format (NF08): ``GET /metrics``.

Only aggregates leave this endpoint: request counts and latency by route template, API error
codes, page processing, the review queue, job states and this month's metered usage. No tenant
names, ids, evidence or prompts. The endpoint is off unless ``AJGEM_METRICS_TOKEN`` is set, and
then needs ``Authorization: Bearer <token>``.

Alert suggestions (see docs/OPERATIONS.md): a rise in ``ajgem_api_errors_total{code="AUTH_FORBIDDEN"}``
(tenant-check failures or probing), any ``ajgem_api_errors_total{code="SCAN_UNAVAILABLE"}``, jobs stuck in
``processing``, and p95 latency above the NF09 targets.
"""

from __future__ import annotations

import threading
from collections import defaultdict

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 60)


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self.latency: dict[tuple[str, str], list[float]] = {}
        self.latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self.errors: dict[str, int] = defaultdict(int)

    def observe_request(self, method: str, route: str, status: int, seconds: float) -> None:
        key = (method, route)
        with self._lock:
            self.requests[(method, route, str(status))] += 1
            counts = self.latency.setdefault(key, [0] * (len(BUCKETS) + 1))
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    counts[i] += 1
            counts[-1] += 1  # +Inf
            self.latency_sum[key] += seconds

    def error(self, code: str) -> None:
        with self._lock:
            self.errors[code] += 1

    def render(self, gauges: dict[str, list[tuple[dict, float]]]) -> str:
        def labels(d: dict) -> str:
            if not d:
                return ""
            inner = ",".join(f'{k}="{str(v).replace(chr(92), "").replace(chr(34), "")}"' for k, v in sorted(d.items()))
            return "{" + inner + "}"

        out = ["# HELP ajgem_http_requests_total HTTP requests by route template and status.",
               "# TYPE ajgem_http_requests_total counter"]
        with self._lock:
            for (m, r, st), n in sorted(self.requests.items()):
                out.append(f"ajgem_http_requests_total{labels({'method': m, 'route': r, 'status': st})} {n}")
            out += ["# HELP ajgem_http_request_duration_seconds Request latency.",
                    "# TYPE ajgem_http_request_duration_seconds histogram"]
            for (m, r), counts in sorted(self.latency.items()):
                for i, b in enumerate(BUCKETS):
                    out.append(f"ajgem_http_request_duration_seconds_bucket{labels({'method': m, 'route': r, 'le': b})} {counts[i]}")
                out.append(f"ajgem_http_request_duration_seconds_bucket{labels({'method': m, 'route': r, 'le': '+Inf'})} {counts[-1]}")
                out.append(f"ajgem_http_request_duration_seconds_sum{labels({'method': m, 'route': r})} {self.latency_sum[(m, r)]:.6f}")
                out.append(f"ajgem_http_request_duration_seconds_count{labels({'method': m, 'route': r})} {counts[-1]}")
            out += ["# HELP ajgem_api_errors_total Structured API errors by code (AUTH_FORBIDDEN includes tenant-check refusals).",
                    "# TYPE ajgem_api_errors_total counter"]
            for code, n in sorted(self.errors.items()):
                out.append(f"ajgem_api_errors_total{labels({'code': code})} {n}")
        for name, series in gauges.items():
            out.append(f"# TYPE {name} gauge")
            for lab, value in series:
                out.append(f"{name}{labels(lab)} {value}")
        return "\n".join(out) + "\n"


def gauges(admin_sessions) -> dict[str, list[tuple[dict, float]]]:
    """Point-in-time aggregates across the deployment (owner role; counts only)."""
    from sqlalchemy import func, select

    from .models import Job, JobTask, Record, UsageEvent
    from .services.costs import month_start

    with admin_sessions() as s:
        review = s.scalar(select(func.count()).select_from(Record).where(Record.status == "needs_review")) or 0
        jobs = s.execute(select(Job.state, func.count()).group_by(Job.state)).all()
        tasks = s.execute(select(JobTask.state, func.count()).group_by(JobTask.state)).all()
        usage = s.execute(select(UsageEvent.category, func.sum(UsageEvent.quantity), func.sum(UsageEvent.input_tokens),
                                 func.sum(UsageEvent.output_tokens), func.count())
                          .where(UsageEvent.created_at >= month_start()).group_by(UsageEvent.category)).all()
    return {
        "ajgem_review_queue_records": [({}, review)],
        "ajgem_jobs": [({"state": st}, n) for st, n in jobs],
        "ajgem_job_pages": [({"state": st}, n) for st, n in tasks],
        "ajgem_usage_month_quantity": [({"category": c}, int(q or 0)) for c, q, _, _, _ in usage],
        "ajgem_usage_month_tokens": [({"category": c, "direction": d}, int(v or 0))
                                     for c, _, i, o, _ in usage for d, v in (("input", i), ("output", o)) if i or o],
        "ajgem_usage_month_events": [({"category": c}, n) for c, _, _, _, n in usage],
    }
