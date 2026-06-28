import frappe

from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	add_permissions,
	add_product_tax_categories,
	make_custom_fields,
	toggle_tax_category_fields,
)


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
