import frappe
from frappe import _

from taxjar_integration.taxjar_integration.pagination import (
	PAGE_SIZE,
	paginated_response,
	parse_filters,
)


@frappe.whitelist()
def get_transactions(filters=None, page=1):
	filters = parse_filters(filters)
	page = max(1, int(page))

	conditions = _build_conditions(filters)

	total = frappe.db.count("Sales Invoice", conditions)

	invoices = frappe.get_all(
		"Sales Invoice",
		filters=conditions,
		fields=[
			"name", "posting_date", "customer_name", "grand_total",
			"is_return", "is_debit_note",
			"taxjar_sync_status", "taxjar_last_synced", "taxjar_sync_error",
		],
		order_by="posting_date desc, name desc",
		start=(page - 1) * PAGE_SIZE,
		limit_page_length=PAGE_SIZE,
	)

	for row in invoices:
		if row.is_return:
			row["transaction_type"] = "Credit Note"
		elif row.get("is_debit_note"):
			row["transaction_type"] = "Debit Note"
		else:
			row["transaction_type"] = "Invoice"

		if row.taxjar_sync_error and len(row.taxjar_sync_error) > 100:
			row["taxjar_sync_error"] = row.taxjar_sync_error[:100] + "..."

	return paginated_response("invoices", invoices, total, page)


@frappe.whitelist()
def get_summary(filters=None):
	filters = parse_filters(filters)
	conditions = _build_conditions(filters)

	# Aggregate counts in SQL instead of pulling every row into Python.
	rows = frappe.get_all(
		"Sales Invoice",
		filters=conditions,
		fields=["taxjar_sync_status", {"COUNT": "*"}],
		group_by="taxjar_sync_status",
	)
	by_status = {}
	for r in rows:
		status = r.get("taxjar_sync_status")
		by_status[status] = next((v for k, v in r.items() if k != "taxjar_sync_status"), 0)

	return {
		"total": sum(by_status.values()),
		"synced": by_status.get("Synced", 0),
		"failed": by_status.get("Failed", 0),
		"queued": by_status.get("Queued", 0),
	}


@frappe.whitelist()
def bulk_retry(invoices):
	frappe.has_permission("Sales Invoice", "write", throw=True)
	invoices = frappe.parse_json(invoices) if isinstance(invoices, str) else invoices

	queued = 0
	for name in invoices:
		status = frappe.db.get_value("Sales Invoice", name, "taxjar_sync_status")
		if status != "Failed":
			continue

		frappe.db.set_value(
			"Sales Invoice", name, "taxjar_sync_status", "Queued", update_modified=False
		)
		frappe.enqueue(
			"taxjar_integration.taxjar_integration.taxjar_integration.sync_transaction_to_taxjar",
			invoice_name=name,
			queue="short",
			job_id=f"taxjar_retry_{name}",
			deduplicate=True,
		)
		queued += 1

	return {"queued": queued}


def _build_conditions(filters):
	conditions = {"docstatus": ("in", (1, 2))}

	if filters.get("company"):
		conditions["company"] = filters["company"]

	from_date = filters.get("from_date")
	to_date = filters.get("to_date")

	if from_date and to_date:
		conditions["posting_date"] = ("between", (from_date, to_date))
	elif from_date:
		conditions["posting_date"] = (">=", from_date)
	elif to_date:
		conditions["posting_date"] = ("<=", to_date)

	if filters.get("sync_status"):
		conditions["taxjar_sync_status"] = filters["sync_status"]

	transaction_type = filters.get("transaction_type")
	if transaction_type:
		if transaction_type == "Credit Note":
			conditions["is_return"] = 1
		elif transaction_type == "Debit Note":
			conditions["is_debit_note"] = 1
		else:
			conditions["is_return"] = 0
			conditions["is_debit_note"] = 0

	return conditions
