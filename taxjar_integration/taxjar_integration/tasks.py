import frappe

from taxjar_integration.taxjar_integration.taxjar_integration import (
	_is_taxjar_enabled,
	company_creates_transactions,
)


def purge_old_api_logs():
	retention_days = frappe.db.get_single_value("TaxJar Settings", "log_retention_days")
	if not retention_days:
		return
	cutoff = frappe.utils.add_days(frappe.utils.today(), -int(retention_days))
	frappe.db.delete("TaxJar API Log", {"creation": ("<", cutoff)})


def sync_nexus_list():
	"""Daily job: refresh nexus regions from TaxJar for all configured companies."""
	doc = frappe.get_doc("TaxJar Settings", "TaxJar Settings")

	if not _is_taxjar_enabled(doc):
		return
	if not doc.company_config:
		return

	try:
		doc.update_nexus_list()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "TaxJar: Nexus sync failed")


def retry_failed_taxjar_syncs():
	"""Every 15 min: re-enqueue Failed Sales Invoices for companies that still have
	transaction filing enabled."""
	if not _is_taxjar_enabled():
		return

	failed_invoices = frappe.get_all(
		"Sales Invoice",
		filters={"taxjar_sync_status": "Failed", "docstatus": ("in", (1, 2))},
		fields=["name", "company"],
		limit=50,
	)

	for invoice in failed_invoices:
		if not company_creates_transactions(invoice.company):
			continue
		frappe.enqueue(
			"taxjar_integration.taxjar_integration.taxjar_integration.sync_transaction_to_taxjar",
			invoice_name=invoice.name,
			queue="short",
			job_id=f"taxjar_retry_{invoice.name}",
			deduplicate=True,
		)


def retry_failed_taxjar_customer_syncs():
	"""Every 15 min: re-enqueue all Customers with Failed TaxJar sync status."""
	if not _is_taxjar_enabled():
		return

	failed_customers = frappe.get_all(
		"Customer",
		filters={"taxjar_customer_sync_status": "Failed"},
		pluck="name",
		limit=50,
	)

	taxjar_settings = frappe.get_single("TaxJar Settings")
	for customer_name in failed_customers:
		for config in taxjar_settings.company_config or []:
			if not (config.taxjar_calculate_tax or config.taxjar_create_transactions):
				continue
			frappe.enqueue(
				"taxjar_integration.taxjar_integration.taxjar_integration.sync_customer_to_taxjar",
				customer_name=customer_name,
				company=config.company,
				queue="short",
				job_id=f"taxjar_customer_retry_{customer_name}_{config.company}",
				deduplicate=True,
			)
