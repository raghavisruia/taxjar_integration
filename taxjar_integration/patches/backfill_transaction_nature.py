import frappe

from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	make_custom_fields,
)


def execute():
	"""Fill taxjar_transaction_nature on every Sales Invoice that predates it.

	set_transaction_nature() writes the field on save, so only invoices written
	before this release hold a blank. Those are never saved again, and a blank
	keeps them out of the Transaction Sync page's Export filter for good.

	One statement rather than a loop over documents. The field is read-only and
	derived, nothing hooks its change, and a site can hold a very large number of
	invoices - loading each one to set one column would be a long migrate for no
	added correctness.

	The destination is the shipping address where there is one and the billing
	address where there is not, which is the fallback the whole app uses - see
	_destination_address() in taxjar_integration.py. Python falls back with
	``or``, which treats an empty string as nothing, while COALESCE falls back
	only on NULL. An unset Link is stored as an empty string as often as it is
	stored as NULL, so NULLIF turns the empty string back into the NULL that
	COALESCE skips. Without it an invoice with an empty shipping address and a
	foreign billing address would be written Domestic here and read Export
	everywhere else.
	"""
	# The field is created by make_custom_fields(), which this app runs from its
	# after_migrate hook - and after_migrate runs after patches. So on the
	# migrate that first ships this field the column does not exist yet, and a
	# patch that only checked for it would skip every row and still be recorded
	# as done, leaving the backfill to never happen at all.
	#
	# make_custom_fields() is idempotent and re-runs on every migrate anyway, so
	# asking for it early costs one extra pass and settles the order.
	if not frappe.db.has_column("Sales Invoice", "taxjar_transaction_nature"):
		make_custom_fields()

	frappe.db.sql(
		"""
		UPDATE `tabSales Invoice` si
		LEFT JOIN `tabAddress` addr
			ON addr.name = COALESCE(
				NULLIF(si.shipping_address_name, ''),
				NULLIF(si.customer_address, '')
			)
		SET si.taxjar_transaction_nature = CASE
			WHEN COALESCE(addr.country, '') NOT IN ('', 'United States') THEN 'Export'
			ELSE 'Domestic'
		END
		WHERE COALESCE(si.taxjar_transaction_nature, '') = ''
		"""
	)
