import frappe
from frappe import _

from taxjar_integration.taxjar_integration.pagination import (
	PAGE_SIZE,
	not_configured_response,
	paginated_response,
	parse_filters,
)
from taxjar_integration.taxjar_integration.taxjar_integration import _publish_transaction_update

# A representative TaxJar custom field; if this column is absent the fields were
# never created, so reads would hit MySQLdb (1054) Unknown column.
_TAXJAR_INVOICE_COLUMN = "taxjar_sync_status"

_DOC_STATUS_LABELS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


def _taxjar_invoice_fields_ready():
	return frappe.db.has_column("Sales Invoice", _TAXJAR_INVOICE_COLUMN)


@frappe.whitelist()
def get_transactions(filters=None, page=1):
	frappe.has_permission("Sales Invoice", "read", throw=True)
	if not _taxjar_invoice_fields_ready():
		return not_configured_response("invoices")

	filters = parse_filters(filters)
	page = max(1, int(page))

	conditions = _build_conditions(filters)

	total = frappe.db.count("Sales Invoice", conditions)

	invoices = frappe.get_all(
		"Sales Invoice",
		filters=conditions,
		fields=[
			"name", "posting_date", "customer_name", "grand_total", "docstatus",
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

		row["doc_status"] = _DOC_STATUS_LABELS.get(row.docstatus, "")

		if row.taxjar_sync_error and len(row.taxjar_sync_error) > 100:
			row["taxjar_sync_error"] = row.taxjar_sync_error[:100] + "..."

	return paginated_response("invoices", invoices, total, page)


@frappe.whitelist()
def get_summary(filters=None):
	frappe.has_permission("Sales Invoice", "read", throw=True)
	if not _taxjar_invoice_fields_ready():
		return not_configured_response()

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
	if not _taxjar_invoice_fields_ready():
		frappe.throw(
			_("TaxJar is not set up yet. Enable a TaxJar feature in TaxJar Settings first."),
			title=_("TaxJar Not Configured"),
		)
	invoices = frappe.parse_json(invoices) if isinstance(invoices, str) else invoices

	queued = 0
	for name in invoices:
		status = frappe.db.get_value("Sales Invoice", name, "taxjar_sync_status")
		if status != "Failed":
			continue

		# Not routed through _set_sync_status: that also nulls
		# taxjar_last_synced, which would discard the real last-sync time on a
		# doc that synced fine and only later failed its cancel-delete.
		frappe.db.set_value(
			"Sales Invoice", name, "taxjar_sync_status", "Queued", update_modified=False
		)
		_publish_transaction_update(name, "Queued")
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
