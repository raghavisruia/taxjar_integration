import frappe
from frappe import _


PAGE_SIZE = 50


@frappe.whitelist()
def get_customers(filters=None, page=1):
	filters = frappe.parse_json(filters) if filters else {}
	page = max(1, int(page))

	conditions = {}
	if filters.get("customer_name"):
		conditions["customer_name"] = ("like", f"%{filters['customer_name']}%")
	if filters.get("customer_group"):
		conditions["customer_group"] = filters["customer_group"]
	if filters.get("exemption_type"):
		if filters["exemption_type"] == "__not_set":
			conditions["taxjar_exemption_type"] = ("in", ("", None))
		else:
			conditions["taxjar_exemption_type"] = filters["exemption_type"]
	if filters.get("sync_status"):
		if filters["sync_status"] == "__not_set":
			conditions["taxjar_customer_sync_status"] = ("in", ("", None))
		else:
			conditions["taxjar_customer_sync_status"] = filters["sync_status"]

	total = frappe.db.count("Customer", conditions)

	customers = frappe.get_all(
		"Customer",
		filters=conditions,
		fields=[
			"name", "customer_name", "customer_group",
			"taxjar_exemption_type", "taxjar_customer_id",
			"taxjar_customer_sync_status",
		],
		order_by="customer_name asc",
		start=(page - 1) * PAGE_SIZE,
		limit_page_length=PAGE_SIZE,
	)

	# Fetch all region counts in one grouped query instead of one count per row.
	names = [c["name"] for c in customers]
	region_counts = {}
	if names:
		for row in frappe.get_all(
			"TaxJar Customer Exempt Region",
			filters={"parenttype": "Customer", "parent": ("in", names)},
			fields=["parent", {"COUNT": "*"}],
			group_by="parent",
		):
			region_counts[row.get("parent")] = next(
				(v for k, v in row.items() if k != "parent"), 0
			)

	for c in customers:
		c["exempt_region_count"] = region_counts.get(c["name"], 0)

	return {
		"customers": customers,
		"total": total,
		"page": page,
		"page_size": PAGE_SIZE,
		"total_pages": max(1, -(-total // PAGE_SIZE)),
	}


@frappe.whitelist()
def save_exemption_type(customer, exemption_type):
	frappe.has_permission("Customer", "write", throw=True)
	doc = frappe.get_doc("Customer", customer)
	doc.taxjar_exemption_type = exemption_type or ""
	doc.save()
	return {"ok": True}


@frappe.whitelist()
def get_exempt_regions(customer):
	regions = frappe.get_all(
		"TaxJar Customer Exempt Region",
		filters={"parent": customer, "parenttype": "Customer"},
		fields=["country", "state"],
	)
	return regions


@frappe.whitelist()
def save_exempt_regions(customer, regions):
	frappe.has_permission("Customer", "write", throw=True)
	regions = frappe.parse_json(regions) if isinstance(regions, str) else regions

	doc = frappe.get_doc("Customer", customer)
	doc.set("taxjar_exempt_regions", [])
	for r in regions:
		doc.append("taxjar_exempt_regions", {"country": r["country"], "state": r["state"]})
	doc.save()
	return {"ok": True}


@frappe.whitelist()
def bulk_set_exemption_type(customers, exemption_type):
	frappe.has_permission("Customer", "write", throw=True)
	customers = frappe.parse_json(customers) if isinstance(customers, str) else customers

	for name in customers:
		doc = frappe.get_doc("Customer", name)
		doc.taxjar_exemption_type = exemption_type or ""
		doc.save()

	return {"updated": len(customers)}


@frappe.whitelist()
def bulk_clear_exemption(customers):
	frappe.has_permission("Customer", "write", throw=True)
	customers = frappe.parse_json(customers) if isinstance(customers, str) else customers

	for name in customers:
		doc = frappe.get_doc("Customer", name)
		doc.taxjar_exemption_type = ""
		doc.set("taxjar_exempt_regions", [])
		doc.save()

	return {"updated": len(customers)}


@frappe.whitelist()
def bulk_sync_to_taxjar(customers):
	frappe.has_permission("Customer", "write", throw=True)
	customers = frappe.parse_json(customers) if isinstance(customers, str) else customers

	queued = 0
	for name in customers:
		customer_id = frappe.db.get_value("Customer", name, "taxjar_customer_id")
		exemption_type = frappe.db.get_value("Customer", name, "taxjar_exemption_type")
		if not customer_id and not exemption_type:
			continue

		frappe.db.set_value("Customer", name, "taxjar_customer_sync_status", "Queued", update_modified=False)
		taxjar_settings = frappe.get_single("TaxJar Settings")
		for config in taxjar_settings.company_config or []:
			frappe.enqueue(
				"taxjar_integration.taxjar_integration.taxjar_integration.sync_customer_to_taxjar",
				customer_name=name,
				company=config.company,
				queue="short",
				deduplicate=True,
				job_id=f"sync_customer_taxjar_{name}_{config.company}",
			)
		queued += 1

	return {"queued": queued}
