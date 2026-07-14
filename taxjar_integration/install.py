import frappe

from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	add_permissions,
	add_product_tax_categories,
	make_custom_fields,
	toggle_tax_category_fields,
)
from taxjar_integration.taxjar_integration.taxjar_integration import _is_taxjar_enabled

WORKSPACE = "TaxJar Integration"

# Lucide icon names shown next to each sidebar card group. Cards without an entry
# fall back to no icon.
SIDEBAR_CARD_ICONS = {
	"Setup": "settings",
	"Manage": "layers",
	"Sync": "refresh-cw",
}


def after_install():
	setup_taxjar()


def after_migrate():
	setup_taxjar()


def setup_taxjar():
	"""Idempotent TaxJar setup that runs on install and on every migrate.

	All the one-time, heavy work lives here rather than on the first save of TaxJar
	Settings:

	* custom fields (the Customer/Sales Invoice ``taxjar_*`` columns) — created up
	  front so the desk pages never query columns that don't exist (MySQLdb 1054);
	* the Product Tax Category master (~800 rows from the bundled fixture) and the
	  permissions that let the accounting/stock roles manage it;
	* visibility of the product tax category fields, hidden until a TaxJar feature is
	  enabled (kept in sync with the current feature state on migrate).
	"""
	make_custom_fields()

	if not frappe.db.exists("Product Tax Category"):
		add_product_tax_categories()

	add_permissions()

	# Item Product Tax Category fields are not company-scoped, so they follow the
	# "any company has a feature enabled" gate.
	toggle_tax_category_fields(hidden=not _is_taxjar_enabled())

	sync_taxjar_workspace_sidebar()


def sync_taxjar_workspace_sidebar():
	"""(Re)build the desk left sidebar so it mirrors the workspace's card groups.

	The grouped sidebar lives on ``Workspace.sidebar_items`` (a child table on the
	workspace itself): each card (Card Break) becomes a collapsible Section Break
	and its links become nested child items. This is NOT the standalone
	``Workspace Sidebar`` doctype - that was merged into ``Workspace`` earlier in
	v16 (see ``frappe.patches.v16_0.migrate_workspace_sidebar_to_workspace`` and
	``frappe.boot.get_sidebar_items``, which explicitly says "the legacy Workspace
	Sidebar doctype is no longer read here"). The workspace is the single source
	of truth, so this runs on every install/migrate and stays in lockstep with
	the card layout.
	"""
	if not frappe.db.exists("Workspace", WORKSPACE):
		return

	ws = frappe.get_doc("Workspace", WORKSPACE)

	# Card display order comes from the content blocks; the links table holds the
	# Card Break -> child-link grouping.
	content = frappe.parse_json(ws.content or "[]")
	card_order = [
		block["data"]["card_name"]
		for block in content
		if block.get("type") == "card" and block.get("data", {}).get("card_name")
	]

	grouped = {}
	current = None
	for link in ws.links:
		if link.type == "Card Break":
			current = link.label
			grouped.setdefault(current, [])
		elif link.type == "Link" and current and link.link_to:
			grouped[current].append(link)

	# Honour the visual (content) order, then any card only present in links.
	ordered_cards = card_order + [card for card in grouped if card not in card_order]

	# A Home entry routes back to the workspace itself, mirroring core desk sidebars.
	items = [{
		"type": "Link",
		"label": "Home",
		"link_to": WORKSPACE,
		"link_type": "Workspace",
		"icon": "home",
	}]
	for card in ordered_cards:
		links = grouped.get(card)
		if not links:
			continue
		items.append({
			"type": "Section Break",
			"label": card,
			"icon": SIDEBAR_CARD_ICONS.get(card),
			"collapsible": 1,
			"indent": 1,
		})
		for link in links:
			items.append({
				"type": "Link",
				"label": link.label,
				"link_to": link.link_to,
				"link_type": link.link_type,
				"child": 1,
				"collapsible": 1,
			})

	ws.set("sidebar_items", [])
	for item in items:
		ws.append("sidebar_items", item)
	ws.save(ignore_permissions=True)

	# Drop the pre-merge standalone record left over from older v16 builds; it's
	# no longer read by frappe.boot.get_sidebar_items.
	if frappe.db.exists("DocType", "Workspace Sidebar"):
		frappe.delete_doc("Workspace Sidebar", WORKSPACE, ignore_missing=True, force=True)
