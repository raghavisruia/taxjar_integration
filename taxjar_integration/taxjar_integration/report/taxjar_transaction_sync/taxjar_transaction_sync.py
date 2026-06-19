import frappe
from frappe import _


def execute(filters=None):
	columns = get_columns()
	data = get_data(filters)
	summary = get_summary(data)
	return columns, data, None, None, summary


def get_columns():
	return [
		{
			"fieldname": "name",
			"label": _("Sales Invoice"),
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"width": 180,
		},
		{
			"fieldname": "posting_date",
			"label": _("Posting Date"),
			"fieldtype": "Date",
			"width": 110,
		},
		{
			"fieldname": "customer_name",
			"label": _("Customer"),
			"fieldtype": "Data",
			"width": 200,
		},
		{
			"fieldname": "transaction_type",
			"label": _("Type"),
			"fieldtype": "Data",
			"width": 110,
		},
		{
			"fieldname": "grand_total",
			"label": _("Grand Total"),
			"fieldtype": "Currency",
			"width": 120,
		},
		{
			"fieldname": "taxjar_sync_status",
			"label": _("Sync Status"),
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"fieldname": "taxjar_last_synced",
			"label": _("Last Synced"),
			"fieldtype": "Datetime",
			"width": 160,
		},
		{
			"fieldname": "taxjar_sync_error",
			"label": _("Error"),
			"fieldtype": "Data",
			"width": 250,
		},
	]


def get_data(filters):
	conditions = {"docstatus": ("in", (1, 2))}

	if filters and filters.get("company"):
		conditions["company"] = filters["company"]
	if filters and filters.get("from_date"):
		conditions["posting_date"] = (">=", filters["from_date"])
	if filters and filters.get("to_date"):
		conditions.setdefault("posting_date", None)
		if isinstance(conditions["posting_date"], tuple):
			conditions["posting_date"] = ("between", (filters["from_date"], filters["to_date"]))
		else:
			conditions["posting_date"] = ("<=", filters["to_date"])
	if filters and filters.get("sync_status"):
		conditions["taxjar_sync_status"] = filters["sync_status"]

	invoices = frappe.get_all(
		"Sales Invoice",
		filters=conditions,
		fields=[
			"name", "posting_date", "customer_name", "grand_total",
			"is_return", "is_debit_note",
			"taxjar_sync_status", "taxjar_last_synced", "taxjar_sync_error",
		],
		order_by="posting_date desc, name desc",
		limit=500,
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

	if filters and filters.get("transaction_type"):
		invoices = [r for r in invoices if r["transaction_type"] == filters["transaction_type"]]

	return invoices


def get_summary(data):
	total = len(data)
	synced = sum(1 for r in data if r.get("taxjar_sync_status") == "Synced")
	failed = sum(1 for r in data if r.get("taxjar_sync_status") == "Failed")
	queued = sum(1 for r in data if r.get("taxjar_sync_status") == "Queued")

	return [
		{"label": _("Total Invoices"), "value": total, "datatype": "Int"},
		{"label": _("Synced"), "value": synced, "indicator": "green", "datatype": "Int"},
		{"label": _("Failed"), "value": failed, "indicator": "red", "datatype": "Int"},
		{"label": _("Queued"), "value": queued, "indicator": "blue", "datatype": "Int"},
	]
