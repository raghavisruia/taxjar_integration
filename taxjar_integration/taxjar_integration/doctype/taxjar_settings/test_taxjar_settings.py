# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import MagicMock

import frappe
from frappe.tests import UnitTestCase

from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	make_custom_fields,
)


def _make_settings(company="Test Co", tax_head="Sales Tax - TC", shipping_head="Freight - TC", api_mode="Sandbox", sandbox_key="sk_test"):
	config_row = MagicMock()
	config_row.company = company
	config_row.tax_account_head = tax_head
	config_row.shipping_account_head = shipping_head

	settings = MagicMock()
	settings.api_mode = api_mode
	settings.sandbox_key = sandbox_key
	settings.company_config = [config_row]
	settings.table_hvjw = []
	return settings


# ── Phase 1: Schema & Validation ──────────────────────────────────────────────

class TestTaxJarSettings(UnitTestCase):

	def setUp(self):
		self.settings = frappe.get_single("TaxJar Settings")
		self.settings.taxjar_calculate_tax = 0
		self.settings.taxjar_create_transactions = 0
		self.settings.api_mode = "Live"
		self.settings.set("table_hvjw", [])
		self.settings.set("company_config", [])
		self.settings.set("nexus", [])

	def _add_sandbox_credential(self):
		self.settings.append("table_hvjw", {
			"company": "_Test Company",
			"sandbox_token": "test-sandbox-token",
		})

	# validate() — sandbox mode

	def test_validate_sandbox_requires_sandbox_token_in_credentials(self):
		self.settings.api_mode = "Sandbox"
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_validate_sandbox_passes_with_sandbox_token_in_credentials(self):
		self.settings.api_mode = "Sandbox"
		self._add_sandbox_credential()
		self.settings.validate()  # must not raise

	def test_validate_sandbox_fails_when_only_live_token_present(self):
		"""A row with only live_token is not enough for Sandbox mode."""
		self.settings.api_mode = "Sandbox"
		self.settings.append("table_hvjw", {
			"company": "_Test Company",
			"live_token": "test-live-token",
		})
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	# validate() — live mode

	def test_validate_live_requires_credential(self):
		self.settings.api_mode = "Live"
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_validate_live_passes_with_credential(self):
		self.settings.api_mode = "Live"
		self.settings.append("table_hvjw", {
			"company": "_Test Company",
			"live_token": "test-live-token",
		})
		self.settings.validate()  # must not raise

	def test_validate_both_features_enabled_passes(self):
		self.settings.api_mode = "Sandbox"
		self._add_sandbox_credential()
		self.settings.taxjar_calculate_tax = 1
		self.settings.taxjar_create_transactions = 1
		self.settings.validate()  # must not raise

	# Phase 3: Create Transactions is independent of Tax Calculation

	def test_create_transactions_can_be_enabled_without_calculate_tax(self):
		"""After Phase 3, enabling Create Transactions alone must not throw."""
		self.settings.api_mode = "Sandbox"
		self._add_sandbox_credential()
		self.settings.taxjar_calculate_tax = 0
		self.settings.taxjar_create_transactions = 1
		self.settings.validate()  # must not raise

	def test_calculate_tax_alone_passes(self):
		"""Enable Tax Calculation without Create Transactions must pass."""
		self.settings.api_mode = "Sandbox"
		self._add_sandbox_credential()
		self.settings.taxjar_calculate_tax = 1
		self.settings.taxjar_create_transactions = 0
		self.settings.validate()  # must not raise

	# DocType schema

	def test_company_config_has_correct_fields(self):
		doc = frappe.get_meta("TaxJar Company Config")
		fieldnames = [f.fieldname for f in doc.fields]
		self.assertIn("company", fieldnames)
		self.assertIn("tax_account_head", fieldnames)
		self.assertIn("shipping_account_head", fieldnames)

	def test_taxjar_settings_has_company_config_table(self):
		meta = frappe.get_meta("TaxJar Settings")
		field = meta.get_field("company_config")
		self.assertIsNotNone(field)
		self.assertEqual(field.options, "TaxJar Company Config")

	def test_taxjar_nexus_has_company_field(self):
		meta = frappe.get_meta("TaxJar Nexus")
		field = meta.get_field("company")
		self.assertIsNotNone(field)
		self.assertEqual(field.options, "Company")

	def test_taxjar_settings_no_single_tax_account_head(self):
		meta = frappe.get_meta("TaxJar Settings")
		self.assertIsNone(meta.get_field("tax_account_head"))

	def test_taxjar_settings_no_single_shipping_account_head(self):
		meta = frappe.get_meta("TaxJar Settings")
		self.assertIsNone(meta.get_field("shipping_account_head"))

	def test_taxjar_settings_no_single_company_field(self):
		meta = frappe.get_meta("TaxJar Settings")
		company_link_fields = [
			f for f in meta.fields if f.fieldname == "company" and f.fieldtype == "Link"
		]
		self.assertEqual(len(company_link_fields), 0)

	def test_taxjar_settings_no_sandbox_key_field(self):
		"""Global sandbox_key field must be gone — sandbox token lives in the credential row."""
		meta = frappe.get_meta("TaxJar Settings")
		self.assertIsNone(meta.get_field("sandbox_key"))

	def test_taxjar_api_credential_has_sandbox_token(self):
		meta = frappe.get_meta("TaxJar API Credential")
		field = meta.get_field("sandbox_token")
		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Password")
