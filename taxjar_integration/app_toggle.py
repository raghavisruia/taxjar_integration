"""Hide this app's customizations while the site disables the app, and show them again.

A disabled app keeps its schema and its data. The site only stops the app from taking
effect. Frappe drops the app from ``get_active_apps()``, so the workspace, the sidebar
and the scheduled jobs go quiet on their own.

The customizations do not. setup_taxjar() writes Custom Fields and Property Setters onto
doctypes this app does not own - Sales Invoice, Quotation, Sales Order, their item
tables, Customer, Item and Address. Those rows belong to the core doctypes, so nothing
removes them when the app goes quiet. A site that disables the app would keep the
``taxjar_*`` fields on every sales form. It would also keep ERPNext's own
``exempt_from_sales_tax`` checkbox hidden, behind a Property Setter this app wrote.

``frappe.custom.hide_customizations()`` sets ``is_app_disabled`` on those rows. The rows
and their data stay in the database. ``unhide_customizations()`` clears the flag again.
Both throw if the site has no ``is_app_disabled`` column yet, which means the site needs
``bench migrate`` first.

Frappe defines four hooks around the switch: ``before_disable``, ``after_disable``,
``before_enable`` and ``after_enable``. This app declares two of them, the pair frappe's
own hooks.md names for this job:

* ``before_disable`` hides the customizations, while the app is still active.
* ``after_enable`` shows them again, once the app is active again.

The other two have no work here, so this app leaves them undeclared.

``bench migrate`` runs ``before_disable`` again for every disabled app. It has to:
migrate calls ``after_migrate`` for every *installed* app, disabled ones included, so
setup_taxjar() runs and ``make_property_setter()`` writes a fresh row with the flag
clear. frappe.installer.reapply_disabled_app_state() then runs this module again, at the
end of post_schema_updates. So ``before_disable`` must give the same result on every
run. It does: it derives the row list from the two lists install writes from, and it
writes one flag value.
"""

from frappe.custom import hide_customizations, unhide_customizations

from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	get_custom_fields,
)

# The same list uninstall.py deletes. One copy, so a Property Setter added to install
# later cannot reach one of the two paths that undo it and miss the other.
from taxjar_integration.uninstall import _PROPERTY_SETTERS


def before_disable():
	hide_customizations(get_customizations())


def after_enable():
	unhide_customizations(get_customizations())


def get_customizations():
	"""Return every customization this app writes onto a doctype it does not own.

	The shape is the one frappe.custom expects: each customization doctype maps to a
	list of row filters. Every filter names one doctype, because frappe rejects a
	filter without one - it would reach another app's rows on the same table.

	Each filter also names the field. A filter of ``{"dt": "Sales Invoice"}`` alone
	would hide every Custom Field on Sales Invoice, including fields other apps and the
	site's own users wrote.

	Custom DocPerm rows are not here. add_permissions() only changes permissions on
	Product Tax Category, which this app owns. Frappe conceals a disabled app's own
	doctypes, so those rows are already out of reach.
	"""
	return {
		"Custom Field": [
			{"dt": doctype, "fieldname": field["fieldname"]}
			for doctype, fields in get_custom_fields().items()
			for field in fields
		],
		"Property Setter": [
			{"doc_type": doctype, "field_name": fieldname, "property": prop}
			for doctype, fieldname, prop in _PROPERTY_SETTERS
		],
	}
