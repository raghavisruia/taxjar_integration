import frappe

from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	add_permissions,
	add_product_tax_categories,
	make_custom_fields,
	toggle_tax_category_fields,
)

WORKSPACE = "Taxjar Integration"


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

	features_enabled = bool(
		frappe.db.get_single_value("TaxJar Settings", "taxjar_calculate_tax")
		or frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions")
	)
	toggle_tax_category_fields(hidden=not features_enabled)

	sync_taxjar_workspace_sidebar()


def sync_taxjar_workspace_sidebar():
	"""(Re)build the desk left sidebar so it mirrors the workspace's card groups.

	In v16 the in-app sidebar is a ``Workspace Sidebar`` record. Frappe only
	auto-generates a flat "Home + shortcuts" sidebar, so to get the same grouped
	layout as the workspace body we generate it ourselves: each card (Card Break)
	becomes a collapsible Section Break and its links become nested child items.
	The workspace is the single source of truth, so this runs on every install /
	migrate and stays in lockstep with the card layout.
	"""
	if not frappe.db.exists("DocType", "Workspace Sidebar"):
		return
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

	items = []
	for card in ordered_cards:
		links = grouped.get(card)
		if not links:
			continue
		items.append({"type": "Section Break", "label": card, "collapsible": 1, "indent": 1})
		for link in links:
			items.append({
				"type": "Link",
				"label": link.label,
				"link_to": link.link_to,
				"link_type": link.link_type,
				"child": 1,
				"collapsible": 1,
			})

	frappe.delete_doc("Workspace Sidebar", WORKSPACE, ignore_missing=True, force=True)
	sidebar = frappe.new_doc("Workspace Sidebar")
	sidebar.title = WORKSPACE
	sidebar.module = "Taxjar Integration"
	sidebar.app = "taxjar_integration"
	sidebar.header_icon = ws.icon or "money-coins-1"
	for item in items:
		sidebar.append("items", item)
	sidebar.insert(ignore_permissions=True)
