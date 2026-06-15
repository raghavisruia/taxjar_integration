import frappe


def purge_old_api_logs():
	retention_days = frappe.db.get_single_value("TaxJar Settings", "log_retention_days")
	if not retention_days:
		return
	cutoff = frappe.utils.add_days(frappe.utils.today(), -int(retention_days))
	frappe.db.delete("TaxJar API Log", {"creation": ("<", cutoff)})
