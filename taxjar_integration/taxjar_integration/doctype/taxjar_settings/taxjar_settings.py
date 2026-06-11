# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import json
import os
from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.model.document import Document
from frappe.permissions import add_permission, update_permission_property

from taxjar_integration.taxjar_integration.taxjar_integration import get_client, log_taxjar_call


BASE_DIR = Path(__file__).resolve().parent
PRODUCT_TAX_CATEGORY_DATA_FILE = (BASE_DIR / "product_tax_category_data.json").resolve()


class TaxJarSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from taxjar_integration.taxjar_integration.doctype.taxjar_api_credential.taxjar_api_credential import TaxJarAPICredential
		from taxjar_integration.taxjar_integration.doctype.taxjar_company_config.taxjar_company_config import TaxJarCompanyConfig
		from taxjar_integration.taxjar_integration.doctype.taxjar_nexus.taxjar_nexus import TaxJarNexus

		api_mode: DF.Literal["", "Live", "Sandbox"]
		company_config: DF.Table[TaxJarCompanyConfig]
		enable_taxjar_logging: DF.Check
		nexus: DF.Table[TaxJarNexus]
		table_hvjw: DF.Table[TaxJarAPICredential]
		taxjar_calculate_tax: DF.Check
		taxjar_create_transactions: DF.Check
	# end: auto-generated types

	def on_update(self):
		features_enabled = self.taxjar_calculate_tax or self.taxjar_create_transactions

		fields_already_exist = frappe.db.exists(
			"Custom Field",
			{
				"dt": ["in", ["Item", "Sales Invoice Item"]],
				"fieldname": "product_tax_category",
			},
		)

		if features_enabled:
			if not fields_already_exist:
				add_product_tax_categories()
				make_custom_fields()
				add_permissions()
				frappe.enqueue("erpnext.regional.united_states.setup.add_product_tax_categories", now=False)
			else:
				toggle_tax_category_fields(hidden=0)
		elif fields_already_exist:
			toggle_tax_category_fields(hidden=1)

	def validate(self):
		if not (self.taxjar_calculate_tax or self.taxjar_create_transactions):
			return

		if not self.api_mode:
			frappe.throw(frappe._("Please select an API Mode before enabling features."))

		if self.api_mode == "Sandbox":
			if not any(cred.sandbox_token for cred in (self.table_hvjw or [])):
				frappe.throw(frappe._("At least one Sandbox Token is required in API Credentials for Sandbox mode."))
		else:
			if not any(cred.live_token for cred in (self.table_hvjw or [])):
				frappe.throw(frappe._("At least one Live Token is required in API Credentials for Live mode."))

	@frappe.whitelist()
	def update_nexus_list(self):
		if not self.company_config:
			frappe.throw(frappe._("Please add at least one Company Configuration before updating Nexus list"))

		self.set("nexus", [])

		for config in self.company_config:
			client = get_client(config.company)
			if not client:
				frappe.msgprint(
					frappe._("Could not connect to TaxJar for company {0}. Skipping.").format(config.company)
				)
				continue

			try:
				log_taxjar_call(action="nexus_regions", status="request", context={"company": config.company})
				nexus = client.nexus_regions()
				log_taxjar_call(action="nexus_regions", status="success", response=nexus, context={"company": config.company})
			except Exception as e:
				log_taxjar_call(action="nexus_regions", status="error", error=str(e), context={"company": config.company})
				raise

			for address in nexus:
				self.append("nexus", {
					"company": config.company,
					"region": address.region,
					"region_code": address.region_code,
					"country": address.country,
					"country_code": address.country_code,
				})

		self.save()

def toggle_tax_category_fields(hidden):
	hidden = 1 if hidden else 0
	for dt in ("Item", "Sales Invoice Item"):
		frappe.db.set_value(
			"Custom Field",
			{"dt": dt, "fieldname": "product_tax_category"},
			"hidden",
			hidden,
		)


def add_product_tax_categories():
	if PRODUCT_TAX_CATEGORY_DATA_FILE.parent != BASE_DIR or not PRODUCT_TAX_CATEGORY_DATA_FILE.is_file():
		frappe.throw(frappe._("Product tax category fixture file is missing or invalid"))

	# nosemgrep: frappe-security-file-traversal - fixed local fixture path with validation.
	tax_categories = json.loads(PRODUCT_TAX_CATEGORY_DATA_FILE.read_text(encoding="utf-8"))
	create_tax_categories(tax_categories["categories"])


def create_tax_categories(data):
	for d in data:
		if not frappe.db.exists("Product Tax Category", {"product_tax_code": d.get("product_tax_code")}):
			tax_category = frappe.new_doc("Product Tax Category")
			tax_category.description = d.get("description")
			tax_category.product_tax_code = d.get("product_tax_code")
			tax_category.category_name = d.get("name")
			tax_category.db_insert()


_US_STATE_CODE_OPTIONS = (
	"\nAL\nAK\nAZ\nAR\nCA\nCO\nCT\nDE\nDC\nFL\nGA\nHI\nID\nIL\nIN\nIA"
	"\nKS\nKY\nLA\nME\nMD\nMA\nMI\nMN\nMS\nMO\nMT\nNE\nNV\nNH\nNJ\nNM"
	"\nNY\nNC\nND\nOH\nOK\nOR\nPA\nRI\nSC\nSD\nTN\nTX\nUT\nVT\nVA\nWA\nWV\nWI\nWY"
)


def make_custom_fields(update=True):
	custom_fields = {
		"Sales Invoice Item": [
			dict(
				fieldname="product_tax_category",
				fieldtype="Link",
				insert_after="description",
				options="Product Tax Category",
				label="Product Tax Category",
				fetch_from="item_code.product_tax_category",
			),
			dict(
				fieldname="tax_collectable",
				fieldtype="Currency",
				insert_after="net_amount",
				label="Tax Collectable",
				read_only=1,
				options="currency",
			),
			dict(
				fieldname="taxable_amount",
				fieldtype="Currency",
				insert_after="tax_collectable",
				label="Taxable Amount",
				read_only=1,
				options="currency",
			),
		],
		"Item": [
			dict(
				fieldname="product_tax_category",
				fieldtype="Link",
				insert_after="item_group",
				options="Product Tax Category",
				label="Product Tax Category",
			)
		],
		"Address": [
			dict(
				fieldname="taxjar_state_code",
				fieldtype="Select",
				insert_after="state",
				label="State Code (US)",
				description="2-letter US state code for TaxJar. Auto-populated when State is entered.",
				depends_on='eval: doc.country === "United States"',
				options=_US_STATE_CODE_OPTIONS,
			)
		],
	}
	create_custom_fields(custom_fields, update=update)


def add_permissions():
	doctype = "Product Tax Category"
	for role in (
		"Accounts Manager",
		"Accounts User",
		"System Manager",
		"Item Manager",
		"Stock Manager",
	):
		if not frappe.db.exists("DocPerm", {"parent": doctype, "role": role, "permlevel": 0}):
			add_permission(doctype, role, 0)
		update_permission_property(doctype, role, 0, "write", 1)
		update_permission_property(doctype, role, 0, "create", 1)
