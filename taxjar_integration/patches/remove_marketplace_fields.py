import frappe

# The Marketplace section, its two data fields, the column break, and the two
# skip flags. Custom Fields are named "{doctype}-{fieldname}".
_FIELDNAMES = (
	"taxjar_marketplace_section",
	"taxjar_is_marketplace_invoice",
	"taxjar_marketplace_platform",
	"taxjar_marketplace_cb",
	"taxjar_skip_tax_calculation",
	"taxjar_skip_transaction_sync",
)


def execute():
	"""Drop the "Marketplace" section from the Sales Invoice TaxJar tab.

	The feature it was laid out for - an invoice a marketplace already raised,
	priced and filed, which must not be re-priced or re-sent - is not in this
	release. Nothing ever read the two skip flags, so no stored value is being
	acted on and none needs migrating anywhere; the fields only offered
	settings that did nothing.

	Removing the definitions from make_custom_fields() only stops them being
	recreated; after_migrate re-runs that function but never deletes what it no
	longer lists, so already-migrated sites would keep the whole section.
	"""
	for fieldname in _FIELDNAMES:
		name = f"Sales Invoice-{fieldname}"
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, ignore_missing=True)

	frappe.clear_cache(doctype="Sales Invoice")
