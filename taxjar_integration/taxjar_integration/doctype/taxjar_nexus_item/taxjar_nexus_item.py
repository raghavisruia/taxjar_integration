# Copyright (c) 2026,  Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class TaxJarNexusItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from taxjar_integration.taxjar_integration.doctype.taxjar_nexus_item.taxjar_nexus_item import TaxJarNexusItem

		company: DF.Link | None
		nexus: DF.Table[TaxJarNexusItem]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
	# end: auto-generated types

	pass
