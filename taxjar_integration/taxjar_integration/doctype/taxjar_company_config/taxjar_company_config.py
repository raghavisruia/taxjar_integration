from frappe.model.document import Document


class TaxJarCompanyConfig(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		shipping_account_head: DF.Link
		tax_account_head: DF.Link
	# end: auto-generated types

	pass
