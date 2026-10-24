"""``inventory`` - stock (انبارداری) and equipment of the atelier manager (URL prefix ``/inventory``).

=========================================  ==================================================================
``/inventory/``                            overview: stock value, low-stock list, recent movements, gear due
``/inventory/stock``                       items: paper, ink, frames, chassis, cellophane, other (CRUD)
``/inventory/stock/<id>``                  one item: ledger, stock-in, consumption / waste / return, stocktake
``/inventory/movements``                   the whole movement ledger (who / when / why)
``/inventory/calculator``                  print-yield calculator + printable shopping list
``/inventory/prepare[/<event id>]``        "prepare for event": expected sales -> what to pack and what to buy
``/inventory/equipment``                   equipment registry (owned / rented, serial, condition)
``/inventory/equipment/events/<id>``       per-event checklist: attach, check out / in, printable pack list
``/inventory/consumption[/<invoice id>]``  review and override of the automatic consumption of invoices
``/inventory/settings``                    preferred paper, auto consumption, history window, yield table
=========================================  ==================================================================

The module owns no money, pay or print formulas: the sheets of a print order come from
``app.services.print_yield`` (through ``app.services.consumption``), pay from ``payroll``, assigning gear to
an event from ``staffing.attach_equipment``.  ``app.services.consumption`` is the stock service (movement
ledger, low-stock alerts, automatic consumption); its handlers are subscribed to the event bus in
:func:`register`.  See ``README.md`` for the design notes and ``docs/DATA_MODEL.md`` for the contract with
the accounting module (suggested equipment-rental expense lines).
"""

from flask import Blueprint

from app.services import eventbus, nav
from app.services.constants import Role

bp = Blueprint("inventory", __name__, url_prefix="/inventory")


def _low_stock_badge(user):
    """Number of items that are low or out of stock (one COUNT per page render)."""
    from app.modules.inventory import queries
    from app.services.permissions import get_my_atelier

    atelier = get_my_atelier(required=False)
    return queries.low_stock_count(atelier.id) if atelier is not None else 0


# order: 10 home, 20-60 daily work, 70-80 marketplace and inventory, 90+ settings (docs/ARCHITECTURE.md)
nav.register(
    Role.ATELIER_MANAGER,
    "انبار",
    "inventory.index",
    icon="box",
    order=75,
    badge=_low_stock_badge,
    active=("inventory.*",),
)

# Attach the routes (keep these imports at the bottom: they import ``bp`` from this module).
from app.modules.inventory import (  # noqa: E402,F401
    calculator,
    common,
    dashboard,
    equipment,
    items,
    movements,
    prepare,
    review,
    settings,
)

bp.add_app_template_filter(common.qty_text, "inv_qty")
bp.add_app_template_filter(common.value_of, "inv_value")
bp.add_app_template_filter(common.size_text, "inv_size")
bp.add_app_template_filter(common.item_tone, "inv_tone")
bp.add_app_template_filter(common.item_state_text, "inv_state")
bp.add_app_template_filter(common.packs_of, "inv_packs")
bp.add_app_template_global(common.tab_items, "inv_tabs")
bp.add_app_template_global(common.new_token, "inv_idem")


def register(app):
    """Module hook: the event-bus subscriptions of the stock service."""
    from app.services import consumption

    eventbus.subscribe(eventbus.INVOICE_SAVED, consumption.on_invoice_saved)
    eventbus.subscribe(eventbus.EVENT_STATUS_CHANGED, consumption.on_event_status_changed)
