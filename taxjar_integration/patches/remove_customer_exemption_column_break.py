import frappe

# Custom Fields are named "{doctype}-{fieldname}".
_FIELDNAME = "taxjar_column_break"


def execute():
	"""Give the TaxJar Tax Exemption section back to the summary card alone.

	The section used to hold two columns: the card on the left, and Sync Status
	/ Sync Error on the right. Both of those have moved into TaxJar Sync
	Details, where the rest of the last sync already lives, so the break that
	divided them has nothing left on its far side - and a Column Break with an
	empty column after it still halves the width of the one before it. The card
	draws a bordered box when an exemption exists and frappe's empty state when
	none does, and both want the whole section.

	Removing the definition from make_custom_fields() only stops it being
	created again. after_migrate re-runs that function but never deletes what it
	no longer lists, so an already-migrated site would keep the break, and keep
	rendering the card in half a section.
	"""
	name = f"Customer-{_FIELDNAME}"
	if frappe.db.exists("Custom Field", name):
		frappe.delete_doc("Custom Field", name, ignore_missing=True)

	frappe.clear_cache(doctype="Customer")
