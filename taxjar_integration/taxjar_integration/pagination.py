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


def not_configured_response(items_key=None):
	"""Envelope returned by page read methods when the TaxJar custom fields do not
	exist yet (features were never enabled and the columns were never created).

	Querying those columns would raise MySQLdb (1054) Unknown column, so the page
	methods detect the missing schema up front and return this instead. The page JS
	renders a "TaxJar not set up" panel when it sees ``not_configured``.
	"""
	response = {"not_configured": True}
	if items_key:
		response.update(paginated_response(items_key, [], 0, 1))
	return response
