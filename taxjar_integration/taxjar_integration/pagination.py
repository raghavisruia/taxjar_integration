import frappe

PAGE_SIZE = 50


def parse_filters(filters):
	"""Normalise the filters argument from a whitelisted page method."""
	return frappe.parse_json(filters) if filters else {}


def paginated_response(items_key, items, total, page, page_size=PAGE_SIZE):
	"""Build the standard page envelope shared by the TaxJar list pages."""
	return {
		items_key: items,
		"total": total,
		"page": page,
		"page_size": page_size,
		"total_pages": max(1, -(-total // page_size)),
	}
