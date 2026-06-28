from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	make_custom_fields,
	toggle_tax_category_fields,
)


def after_install():
	"""Create the TaxJar custom fields on install.

	TaxJar features (calculate tax / create transactions) are off by default and the
	heavy field creation otherwise only runs when a feature is first enabled. Without
	this hook the Customer/Sales Invoice ``taxjar_*`` columns don't exist on a fresh
	install, so the desk pages crash with MySQLdb (1054) Unknown column the first time
	they query them. Creating the fields up front keeps the pages working immediately.

	The product tax category fields start hidden — the same state as "fields exist but
	no feature enabled" — so a fresh install with TaxJar off doesn't surface empty
	TaxJar fields on every Item. Enabling a feature later unhides them via on_update.
	"""
	make_custom_fields()
	toggle_tax_category_fields(hidden=1)
