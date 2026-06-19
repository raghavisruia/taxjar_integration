import frappe
from frappe.utils import cint


def purge_old_api_logs():
	retention_days = frappe.db.get_single_value("TaxJar Settings", "log_retention_days")
	if not retention_days:
		return
	cutoff = frappe.utils.add_days(frappe.utils.today(), -int(retention_days))
	frappe.db.delete("TaxJar API Log", {"creation": ("<", cutoff)})


def sync_nexus_list():
	"""Daily job: refresh nexus regions from TaxJar for all configured companies."""
	doc = frappe.get_doc("TaxJar Settings", "TaxJar Settings")

	if not (doc.taxjar_calculate_tax or doc.taxjar_create_transactions):
		return
	if not doc.company_config:
		return

	try:
		doc.update_nexus_list()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "TaxJar: Nexus sync failed")


def retry_failed_taxjar_syncs():
	"""Every 15 min: re-enqueue all Sales Invoices with Failed TaxJar sync status."""
	if not cint(frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions")):
		return

	failed_invoices = frappe.get_all(
		"Sales Invoice",
		filters={"taxjar_sync_status": "Failed", "docstatus": ("in", (1, 2))},
		pluck="name",
		limit=50,
	)

	for invoice_name in failed_invoices:
		frappe.enqueue(
			"taxjar_integration.taxjar_integration.taxjar_integration.sync_transaction_to_taxjar",
			invoice_name=invoice_name,
			queue="short",
			job_id=f"taxjar_retry_{invoice_name}",
			deduplicate=True,
		)
