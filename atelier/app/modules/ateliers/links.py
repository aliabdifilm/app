"""Links from the atelier panel to pages owned by other modules.

The panel never duplicates invoices, job posts or stock screens; it links to them.  The
other modules are built independently and may be disabled (``DISABLED_MODULES``), so every
link is resolved defensively: a link is ``None`` when the target does not exist, and the
templates then show a disabled hint instead of a broken button.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional
from urllib.parse import urlencode

from flask import current_app, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.routing import BuildError, RequestRedirect

# Endpoint names tried in order (the first one that exists wins), then a literal path.
MARKETPLACE_NEW_JOB_ENDPOINTS = (
    "marketplace.job_new",
    "marketplace.new_job",
    "marketplace.job_create",
    "marketplace.post_job",
    "marketplace.jobs_new",
)
MARKETPLACE_NEW_JOB_PATH = "/jobs/new"
ACCOUNTING_INVOICE_ENDPOINTS = (
    "accounting.event_invoice",
    "accounting.invoice_edit",
    "accounting.invoice_form",
    "accounting.save_invoice",
)
INVENTORY_ENDPOINTS = ("inventory.index", "inventory.dashboard", "inventory.stock")


def route_exists(path: str, method: str = "GET") -> bool:
    """True when the application can route *path* for *method* (404/405 -> False)."""
    try:
        current_app.url_map.bind("localhost").match(path, method=method)
    except RequestRedirect:
        return True
    except HTTPException:
        return False
    return True


def _first_endpoint(endpoints: Iterable[str], **values: Any) -> Optional[str]:
    for endpoint in endpoints:
        try:
            return url_for(endpoint, **values)
        except BuildError:
            continue
    return None


def tiers_query(tiers: Iterable[dict]) -> str:
    """``[{"threshold": 70, "amount": 150000}]`` -> ``'70:150000'`` (the marketplace prefill format)."""
    return ",".join(f"{tier['threshold']}:{tier['amount']}" for tier in tiers)


def marketplace_job_url(event: Any, role: Any, terms: dict, *, slots: int = 1) -> Optional[str]:
    """Create-job form of the marketplace, prefilled from one afish slot.

    Query parameters (the contract with the marketplace module): ``event`` (id), ``role``,
    ``date`` (ISO), ``city``, ``title``, ``slots``, ``base``, ``commission`` and ``tiers``
    (``threshold:amount`` pairs separated by commas).
    """
    role_value = getattr(role, "value", role)
    label = getattr(role, "label", role_value)
    hall = event.hall
    params = {
        "event": event.id,
        "role": role_value,
        "date": event.date.isoformat(),
        "city": (hall.city if hall is not None else "") or "",
        "title": f"{label} برای مراسم {hall.name if hall is not None else ''}".strip(),
        "slots": max(1, int(slots)),
        "base": int(terms.get("base") or 0),
        "commission": int(terms.get("commission_per_unit") or 0),
    }
    tiers = tiers_query(terms.get("bonus_tiers") or [])
    if tiers:
        params["tiers"] = tiers
    url = _first_endpoint(MARKETPLACE_NEW_JOB_ENDPOINTS, **params)
    if url:
        return url
    if route_exists(MARKETPLACE_NEW_JOB_PATH):
        return f"{MARKETPLACE_NEW_JOB_PATH}?{urlencode(params)}"
    return None


def accounting_invoice_url(event: Any) -> Optional[str]:
    """The page where the invoice of *event* is entered / shown (accounting module)."""
    url = _first_endpoint(ACCOUNTING_INVOICE_ENDPOINTS, event_id=event.id)
    if url:
        return url
    path = f"/accounting/events/{event.id}/invoice"
    return path if route_exists(path) else None


def inventory_url() -> Optional[str]:
    url = _first_endpoint(INVENTORY_ENDPOINTS)
    if url:
        return url
    return "/inventory" if route_exists("/inventory") else None


def job_posts_url() -> Optional[str]:
    """The atelier's own job posts list in the marketplace (best effort)."""
    url = _first_endpoint(("marketplace.my_posts", "marketplace.my_jobs", "marketplace.manage_jobs"))
    if url:
        return url
    return "/jobs/mine" if route_exists("/jobs/mine") else None
