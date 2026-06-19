# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import json
import os
import re
from pathlib import Path

_CODE_RE = re.compile(r"^[A-Z]{2}$")

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.model.document import Document
from frappe.permissions import add_permission, update_permission_property

import taxjar

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
		log_retention_days: DF.Int
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

		# Auto-fetch nexus when first configured: features on, company config present, nexus empty.
		if features_enabled and self.company_config and not self.nexus:
			frappe.enqueue(
				"taxjar_integration.taxjar_integration.tasks.sync_nexus_list",
				queue="short",
				now=frappe.flags.in_test,
			)

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

		self._validate_tokens()

	def _validate_tokens(self):
		"""Test each credential by calling a lightweight TaxJar endpoint."""
		for cred in self.table_hvjw or []:
			company = cred.company
			try:
				test_client = get_client(company)
				if test_client:
					test_client.categories()
			except taxjar.exceptions.TaxJarResponseError as err:
				full = getattr(err, "full_response", None) or {}
				status = full.get("status_code") if isinstance(full, dict) else None
				if status == 401:
					frappe.throw(frappe._("Invalid API token for company {0}. Please check your credentials.").format(company))
			except taxjar.exceptions.TaxJarConnectionError:
				frappe.msgprint(
					frappe._("Could not reach TaxJar to verify credentials for {0}. Token not validated.").format(company),
					indicator="orange",
				)
			except Exception:
				pass

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
				region_code = (address.region_code or "").strip().upper()
				country_code = (address.country_code or "").strip().upper()
				if not _CODE_RE.match(region_code) or not _CODE_RE.match(country_code):
					continue
				self.append("nexus", {
					"company": config.company,
					"region": address.region,
					"region_code": region_code,
					"country": address.country,
					"country_code": country_code,
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
		"Sales Invoice": [
			dict(
				fieldname="taxjar_tab",
				fieldtype="Tab Break",
				insert_after="loyalty_amount",
				label="TaxJar",
			),
			dict(
				fieldname="taxjar_sync_status",
				fieldtype="Select",
				insert_after="taxjar_tab",
				label="Sync Status",
				options="Not Applicable\nQueued\nSynced\nFailed",
				default="Not Applicable",
				allow_on_submit=1,
				in_list_view=1,
				read_only=1,
			),
			dict(
				fieldname="taxjar_sync_error",
				fieldtype="Small Text",
				insert_after="taxjar_sync_status",
				label="Sync Error",
				read_only=1,
				allow_on_submit=1,
				depends_on="eval: doc.taxjar_sync_status == 'Failed'",
			),
			dict(
				fieldname="taxjar_last_synced",
				fieldtype="Datetime",
				insert_after="taxjar_sync_error",
				label="Last Synced",
				read_only=1,
				allow_on_submit=1,
			),
			dict(
				fieldname="taxjar_response_section",
				fieldtype="Section Break",
				insert_after="taxjar_last_synced",
				label="TaxJar Response",
				depends_on="eval: doc.taxjar_sync_status == 'Synced'",
			),
			dict(
				fieldname="taxjar_response_html",
				fieldtype="HTML",
				insert_after="taxjar_response_section",
				label="TaxJar Response",
			),
		],
		"Customer": [
			dict(
				fieldname="taxjar_section_break",
				fieldtype="Section Break",
				insert_after="tax_tab",
				label="TaxJar Tax Exemption",
				collapsible=0,
			),
			dict(
				fieldname="taxjar_exemption_type",
				fieldtype="Select",
				insert_after="taxjar_section_break",
				label="TaxJar Exemption Type",
				options="\nWholesale\nGovernment\nNon Exempt\nOther",
				description="Maps to TaxJar's exemption_type. Leave blank for normal taxable customers.",
			),
			dict(
				fieldname="taxjar_column_break",
				fieldtype="Column Break",
				insert_after="taxjar_exemption_type",
			),
			dict(
				fieldname="taxjar_customer_id",
				fieldtype="Data",
				insert_after="taxjar_column_break",
				label="TaxJar Customer ID",
				read_only=1,
				description="Auto-set on first sync to TaxJar. Used as customer_id in TaxJar API calls.",
			),
			dict(
				fieldname="taxjar_exempt_regions",
				fieldtype="Table",
				insert_after="taxjar_customer_id",
				label="Tax Exempt Regions",
				options="TaxJar Customer Exempt Region",
				description="States where this customer is exempt. Only applies when Exemption Type is set.",
				depends_on="eval: doc.taxjar_exemption_type && doc.taxjar_exemption_type !== 'Non Exempt'",
			),
			dict(
				fieldname="taxjar_last_synced",
				fieldtype="Datetime",
				insert_after="taxjar_exempt_regions",
				label="Last Synced to TaxJar",
				read_only=1,
			),
			dict(
				fieldname="taxjar_customer_sync_status",
				fieldtype="Select",
				insert_after="taxjar_last_synced",
				label="TaxJar Sync Status",
				options="\nQueued\nSynced\nFailed",
				read_only=1,
			),
			dict(
				fieldname="taxjar_customer_sync_error",
				fieldtype="Small Text",
				insert_after="taxjar_customer_sync_status",
				label="TaxJar Sync Error",
				read_only=1,
				depends_on="eval: doc.taxjar_customer_sync_status == 'Failed'",
			),
		],
	}
	create_custom_fields(custom_fields, update=update)

	make_property_setter(
		"Sales Invoice", "return_against", "no_copy", "0", "Check",
		for_doctype=False,
	)


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
