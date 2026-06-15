import frappe


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
