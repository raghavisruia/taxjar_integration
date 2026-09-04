import frappe

# Custom Fields are named "{doctype}-{fieldname}".
_FIELDNAME = "taxjar_sync_draft_message_html"


def execute():
	"""Drop the "TaxJar: Submit to sync" placeholder from the Sales Invoice.

	The Transaction Sync section now hides itself entirely while the invoice is
	a draft, so the placeholder that used to stand in for the hidden Sync
	Status / Last Synced fields has nothing left to explain - the sidebar pill
	already says the same thing.

	Removing the definition from make_custom_fields() only stops it being
	recreated; after_migrate re-runs that function but never deletes what it no
	longer lists, so already-migrated sites would keep rendering the
	placeholder - and, since a draft's section break would then still hold one
	visible field, the section along with it.
	"""
	name = f"Sales Invoice-{_FIELDNAME}"
	if frappe.db.exists("Custom Field", name):
		frappe.delete_doc("Custom Field", name, ignore_missing=True)

	frappe.clear_cache(doctype="Sales Invoice")
