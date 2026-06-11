# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from taxjar_integration.taxjar_integration.taxjar_integration import (
	SUPPORTED_STATE_CODES,
	TAXJAR_ROW_DESCRIPTION,
	_remove_taxjar_rows,
	check_for_nexus,
	get_company_config,
	set_sales_tax,
)
from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	_US_STATE_CODE_OPTIONS,
	make_custom_fields,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

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


class _TaxRow:
	"""Minimal stand-in for a Sales Taxes and Charges row."""
	def __init__(self, account_head, description="", tax_amount=100.0):
		self.account_head = account_head
		self.description = description
		self.tax_amount = tax_amount


def _make_tax_row(account_head, description="", tax_amount=100.0):
	return _TaxRow(account_head, description, tax_amount)


class _FakeItem:
	def __init__(self):
		self.idx = 1
		self.qty = 1
		self.rate = 100.0
		self.product_tax_category = None
		self.tax_collectable = 0.0
		self.taxable_amount = 0.0


class _FakeDoc:
	"""Minimal stand-in for a Frappe document that supports append() on taxes."""
	def __init__(self, company="Test Co", taxes=None):
		self.company = company
		self.doctype = "Sales Invoice"
		self.name = "SINV-TEST-001"
		self.docstatus = 0
		self.is_return = False
		self.net_total = 1000.0
		self.total = 1000.0
		self.exempt_from_sales_tax = 0
		self.customer = "_Test Customer"
		self.shipping_address_name = None
		self.customer_address = None
		self.items = [_FakeItem()]   # must be non-empty to pass the early-return guard
		self.taxes = list(taxes) if taxes else []

	def append(self, field, data):
		if field == "taxes":
			row = _TaxRow(
				account_head=data.get("account_head", ""),
				description=data.get("description", ""),
				tax_amount=data.get("tax_amount", 0.0),
			)
			self.taxes.append(row)

	def run_method(self, method):
		pass

	def get(self, field):
		return getattr(self, field, [])


def _make_doc(company="Test Co", taxes=None):
	return _FakeDoc(company=company, taxes=taxes)


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

	# validate() — no features enabled: always passes regardless of api_mode or credentials

	def test_validate_no_features_blank_mode_passes(self):
		"""Fresh install state: blank mode, no credentials, no features — must save cleanly."""
		self.settings.api_mode = ""
		self.settings.set("table_hvjw", [])
		self.settings.validate()  # must not raise

	def test_validate_no_features_live_mode_no_creds_passes(self):
		"""Credentials are not required when no features are enabled."""
		self.settings.api_mode = "Live"
		self.settings.set("table_hvjw", [])
		self.settings.validate()  # must not raise

	def test_validate_no_features_sandbox_mode_no_creds_passes(self):
		self.settings.api_mode = "Sandbox"
		self.settings.set("table_hvjw", [])
		self.settings.validate()  # must not raise

	# validate() — features enabled, blank mode

	def test_validate_blank_mode_with_calculate_tax_throws(self):
		"""Enabling a feature without selecting an API Mode must throw."""
		self.settings.api_mode = ""
		self.settings.taxjar_calculate_tax = 1
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_validate_blank_mode_with_create_transactions_throws(self):
		self.settings.api_mode = ""
		self.settings.taxjar_create_transactions = 1
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	# validate() — features enabled, sandbox mode

	def test_validate_sandbox_requires_sandbox_token_when_features_enabled(self):
		self.settings.api_mode = "Sandbox"
		self.settings.taxjar_calculate_tax = 1
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_validate_sandbox_passes_with_sandbox_token_and_features_enabled(self):
		self.settings.api_mode = "Sandbox"
		self.settings.taxjar_calculate_tax = 1
		self._add_sandbox_credential()
		self.settings.validate()  # must not raise

	def test_validate_sandbox_fails_when_only_live_token_present_and_features_enabled(self):
		"""A row with only live_token is not enough for Sandbox mode."""
		self.settings.api_mode = "Sandbox"
		self.settings.taxjar_calculate_tax = 1
		self.settings.append("table_hvjw", {
			"company": "_Test Company",
			"live_token": "test-live-token",
		})
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	# validate() — features enabled, live mode

	def test_validate_live_requires_credential_when_features_enabled(self):
		self.settings.api_mode = "Live"
		self.settings.taxjar_calculate_tax = 1
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_validate_live_passes_with_credential_and_features_enabled(self):
		self.settings.api_mode = "Live"
		self.settings.taxjar_calculate_tax = 1
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

	# validate() — feature independence

	def test_create_transactions_alone_enforces_credentials(self):
		"""create_transactions alone (without calculate_tax) must still enforce credentials."""
		self.settings.api_mode = "Live"
		self.settings.taxjar_calculate_tax = 0
		self.settings.taxjar_create_transactions = 1
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_calculate_tax_alone_enforces_credentials(self):
		self.settings.api_mode = "Sandbox"
		self.settings.taxjar_calculate_tax = 1
		self.settings.taxjar_create_transactions = 0
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

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


# ── get_client() — per-company, per-mode token routing ───────────────────────

class TestGetClient(UnitTestCase):

	def _make_cred(self, company, live_token=None, sandbox_token=None):
		cred = MagicMock()
		cred.company = company
		cred.live_token = live_token
		cred.sandbox_token = sandbox_token
		# mimic getattr behaviour for token fields
		cred.configure_mock(**{"live_token": live_token, "sandbox_token": sandbox_token})
		return cred

	def _make_settings(self, api_mode, creds):
		settings = MagicMock()
		settings.api_mode = api_mode
		settings.table_hvjw = creds
		return settings

	def _call_get_client(self, settings, company=None, decrypted="test-key"):
		from taxjar_integration.taxjar_integration.taxjar_integration import get_client
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_decrypted_password", return_value=decrypted), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.taxjar.Client") as mock_client:
			mock_instance = MagicMock()
			mock_client.return_value = mock_instance
			result = get_client(company)
			return mock_client, result

	def test_live_mode_uses_live_token(self):
		cred = self._make_cred("Acme Inc", live_token="live_abc")
		settings = self._make_settings("Live", [cred])
		mock_client, _ = self._call_get_client(settings, company="Acme Inc")
		import taxjar as tj
		mock_client.assert_called_once()
		self.assertEqual(mock_client.call_args[1]["api_url"], tj.DEFAULT_API_URL)

	def test_sandbox_mode_uses_sandbox_token(self):
		cred = self._make_cred("Acme Inc", sandbox_token="sandbox_xyz")
		settings = self._make_settings("Sandbox", [cred])
		mock_client, _ = self._call_get_client(settings, company="Acme Inc")
		import taxjar as tj
		mock_client.assert_called_once()
		self.assertEqual(mock_client.call_args[1]["api_url"], tj.SANDBOX_API_URL)

	def test_sandbox_mode_returns_none_when_no_sandbox_token(self):
		"""Credential row with only live_token must yield no client in Sandbox mode."""
		cred = self._make_cred("Acme Inc", live_token="live_abc", sandbox_token=None)
		settings = self._make_settings("Sandbox", [cred])
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_decrypted_password", return_value=None):
			from taxjar_integration.taxjar_integration.taxjar_integration import get_client
			result = get_client("Acme Inc")
		self.assertIsNone(result)

	def test_company_filters_correct_credential_row(self):
		"""Only the row matching doc.company should be used."""
		cred_a = self._make_cred("Acme Inc", live_token="live_acme")
		cred_b = self._make_cred("Other Co", live_token="live_other")
		settings = self._make_settings("Live", [cred_a, cred_b])

		calls = []
		def _capture_decrypt(doctype, name, fieldname):
			calls.append((name, fieldname))
			return "live_acme"

		from taxjar_integration.taxjar_integration.taxjar_integration import get_client
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_decrypted_password", side_effect=_capture_decrypt), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.taxjar.Client"):
			get_client("Acme Inc")

		# Only one decrypt call, and it must be for live_token (not sandbox_token)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0][1], "live_token")


# ── Phase 2: get_company_config ───────────────────────────────────────────────

class TestGetCompanyConfig(UnitTestCase):

	def test_returns_config_for_matching_company(self):
		settings = _make_settings(company="Acme Inc")
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings):
			result = get_company_config("Acme Inc")
		self.assertIsNotNone(result)
		self.assertEqual(result.company, "Acme Inc")

	def test_returns_none_for_unknown_company(self):
		settings = _make_settings(company="Acme Inc")
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings):
			result = get_company_config("Other Co")
		self.assertIsNone(result)

	def test_returns_none_when_config_is_empty(self):
		settings = _make_settings()
		settings.company_config = []
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings):
			result = get_company_config("Any Co")
		self.assertIsNone(result)


# ── Phase 2: _remove_taxjar_rows ──────────────────────────────────────────────

class TestRemoveTaxjarRows(UnitTestCase):

	def test_removes_rows_matching_tax_account_head(self):
		config = MagicMock()
		config.tax_account_head = "Sales Tax - TC"
		doc = _make_doc(taxes=[
			_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION),
			_make_tax_row("Freight - TC"),
			_make_tax_row("Sales Tax - TC", "Template Tax"),  # template row, same account
		])
		_remove_taxjar_rows(doc, config)
		self.assertEqual(len(doc.taxes), 1)
		self.assertEqual(doc.taxes[0].account_head, "Freight - TC")

	def test_leaves_rows_with_different_accounts_untouched(self):
		config = MagicMock()
		config.tax_account_head = "Sales Tax - TC"
		doc = _make_doc(taxes=[
			_make_tax_row("VAT - TC"),
			_make_tax_row("Freight - TC"),
		])
		_remove_taxjar_rows(doc, config)
		self.assertEqual(len(doc.taxes), 2)

	def test_handles_empty_taxes_table(self):
		config = MagicMock()
		config.tax_account_head = "Sales Tax - TC"
		doc = _make_doc(taxes=[])
		_remove_taxjar_rows(doc, config)
		self.assertEqual(len(doc.taxes), 0)


# ── Phase 2: check_for_nexus ─────────────────────────────────────────────────

class TestCheckForNexus(UnitTestCase):

	def test_returns_true_when_in_nexus(self):
		doc = _make_doc(company="Acme Inc")
		tax_dict = {"to_state": "CA"}
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value="NX-1"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock()):
			self.assertTrue(check_for_nexus(doc, tax_dict))

	def test_returns_false_and_clears_rows_when_not_in_nexus(self):
		config = MagicMock()
		config.tax_account_head = "Sales Tax - TC"
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, 50.0)])

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=config):
			result = check_for_nexus(doc, {"to_state": "TX"})

		self.assertFalse(result)
		self.assertEqual(len([t for t in doc.taxes if t.account_head == "Sales Tax - TC"]), 0)


# ── Phase 2: set_sales_tax — double-tax fix ───────────────────────────────────

class TestSetSalesTax(UnitTestCase):

	def test_replaces_template_row_not_duplicates(self):
		"""Template row for the tax account is removed and replaced by one TaxJar row."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Sales Tax 8%", 80.0)])

		tax_data = MagicMock()
		tax_data.amount_to_collect = 85.0
		tax_data.breakdown.line_items = []

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_for_nexus", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data):
			set_sales_tax(doc, None)

		tax_rows = [t for t in doc.taxes if t.account_head == "Sales Tax - TC"]
		self.assertEqual(len(tax_rows), 1)
		self.assertEqual(tax_rows[0].description, TAXJAR_ROW_DESCRIPTION)
		self.assertEqual(tax_rows[0].tax_amount, 85.0)

	def test_recalculation_replaces_not_duplicates_taxjar_row(self):
		"""On a second save, the existing TaxJar row is replaced, not added to."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, 82.0)])

		tax_data = MagicMock()
		tax_data.amount_to_collect = 90.0
		tax_data.breakdown.line_items = []

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_for_nexus", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data):
			set_sales_tax(doc, None)

		tax_rows = [t for t in doc.taxes if t.account_head == "Sales Tax - TC"]
		self.assertEqual(len(tax_rows), 1)
		self.assertEqual(tax_rows[0].tax_amount, 90.0)

	def test_clears_tax_rows_when_outside_nexus(self):
		"""When the delivery state is not in nexus, sales tax rows must be removed."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Sales Tax 8%", 80.0)])
		company_config = MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")

		# Don't mock check_for_nexus itself — let it run so it calls _remove_taxjar_rows.
		# Mock the DB lookup inside check_for_nexus to return None (outside nexus).
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=company_config), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"to_state": "TX", "dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=None):
			set_sales_tax(doc, None)

		self.assertEqual(len([t for t in doc.taxes if t.account_head == "Sales Tax - TC"]), 0)

	def test_skips_when_calculate_tax_disabled(self):
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Some Tax", 80.0)])
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=0):
			set_sales_tax(doc, None)
		self.assertEqual(len(doc.taxes), 1)  # unchanged

	def test_skips_when_no_company_config(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=None):
			set_sales_tax(doc, None)
		self.assertEqual(len(doc.taxes), 0)


# ── Phase 2: get_line_item_dict — product_tax_code resolution ────────────────

class TestGetLineItemDict(UnitTestCase):

	def _make_item(self, item_code=None, product_tax_category=None):
		item = MagicMock()
		item.get = lambda key, default=None: {
			"idx": 1,
			"qty": 2,
			"rate": 100.0,
			"item_code": item_code,
			"product_tax_category": product_tax_category,
		}.get(key, default)
		return item

	def _call(self, item, item_master_category=None):
		from taxjar_integration.taxjar_integration.taxjar_integration import get_line_item_dict
		with patch(
			"taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value",
			return_value=item_master_category,
		):
			return get_line_item_dict(item, docstatus=0)

	# Happy path: field is populated on the line item (Sales Invoice Item via fetch_from)

	def test_uses_line_item_product_tax_category_when_set(self):
		item = self._make_item(item_code="ITEM-001", product_tax_category="20010")
		result = self._call(item)
		self.assertEqual(result["product_tax_code"], "20010")

	# Fallback: field is empty on line item (Quotation/SO Item, or fetch_from never fired)

	def test_falls_back_to_item_master_when_line_item_field_empty(self):
		item = self._make_item(item_code="ITEM-001", product_tax_category=None)
		result = self._call(item, item_master_category="31000")
		self.assertEqual(result["product_tax_code"], "31000")

	def test_falls_back_to_item_master_when_line_item_field_blank_string(self):
		item = self._make_item(item_code="ITEM-001", product_tax_category="")
		result = self._call(item, item_master_category="20010")
		self.assertEqual(result["product_tax_code"], "20010")

	def test_line_item_field_takes_priority_over_item_master(self):
		"""If line item already has the category, the Item master must not be queried."""
		item = self._make_item(item_code="ITEM-001", product_tax_category="20010")
		with patch(
			"taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value",
		) as mock_db:
			from taxjar_integration.taxjar_integration.taxjar_integration import get_line_item_dict
			get_line_item_dict(item, docstatus=0)
		mock_db.assert_not_called()

	def test_returns_none_when_no_item_code_and_no_line_item_field(self):
		"""No item_code means no fallback lookup — product_tax_code should be None."""
		item = self._make_item(item_code=None, product_tax_category=None)
		result = self._call(item)
		self.assertIsNone(result["product_tax_code"])

	def test_returns_none_when_item_master_has_no_category(self):
		item = self._make_item(item_code="ITEM-001", product_tax_category=None)
		result = self._call(item, item_master_category=None)
		self.assertIsNone(result["product_tax_code"])


# ── Phase 2: create_transaction row detection ────────────────────────────────

class TestCreateTransactionRowDetection(UnitTestCase):

	def test_taxjar_row_description_constant_value(self):
		self.assertEqual(TAXJAR_ROW_DESCRIPTION, "TaxJar Sales Tax")

	def test_skips_when_no_taxjar_row(self):
		"""Must not call TaxJar API when no row carries TAXJAR_ROW_DESCRIPTION."""
		from taxjar_integration.taxjar_integration.taxjar_integration import create_transaction

		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Template Tax", 80.0)])
		mock_client = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client):
			create_transaction(doc, None)

		mock_client.create_order.assert_not_called()
		mock_client.create_refund.assert_not_called()

	def test_uses_taxjar_row_amount_for_transaction(self):
		"""Sales tax amount is taken from the TAXJAR_ROW_DESCRIPTION row."""
		from taxjar_integration.taxjar_integration.taxjar_integration import create_transaction

		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, 95.0)])
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10.0}):
			create_transaction(doc, None)

		mock_client.create_order.assert_called_once()
		self.assertEqual(mock_client.create_order.call_args[0][0]["sales_tax"], 95.0)


# ── Phase 1: taxjar_state_code custom field on Address ───────────────────────

class TestAddressCustomField(UnitTestCase):

	def _get_address_field_def(self):
		"""Return the Address field definition dict from make_custom_fields()."""
		# Intercept create_custom_fields to capture the dict without hitting the DB
		captured = {}

		def _capture(custom_fields, update=True):
			captured.update(custom_fields)

		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.create_custom_fields",
			side_effect=_capture,
		):
			make_custom_fields()

		return captured.get("Address", [])

	def test_make_custom_fields_includes_address(self):
		fields = self._get_address_field_def()
		self.assertTrue(len(fields) > 0, "Expected at least one Address custom field")

	def test_address_custom_field_fieldname(self):
		fields = self._get_address_field_def()
		fieldnames = [f["fieldname"] for f in fields]
		self.assertIn("taxjar_state_code", fieldnames)

	def test_address_custom_field_type_is_select(self):
		fields = self._get_address_field_def()
		field = next(f for f in fields if f["fieldname"] == "taxjar_state_code")
		self.assertEqual(field["fieldtype"], "Select")

	def test_address_custom_field_inserted_after_state(self):
		fields = self._get_address_field_def()
		field = next(f for f in fields if f["fieldname"] == "taxjar_state_code")
		self.assertEqual(field["insert_after"], "state")

	def test_address_custom_field_depends_on_united_states(self):
		fields = self._get_address_field_def()
		field = next(f for f in fields if f["fieldname"] == "taxjar_state_code")
		self.assertIn("United States", field["depends_on"])

	def test_address_custom_field_options_cover_all_supported_codes(self):
		"""Every code in SUPPORTED_STATE_CODES must appear in the Select options."""
		options = set(_US_STATE_CODE_OPTIONS.split("\n"))
		for code in SUPPORTED_STATE_CODES:
			self.assertIn(code, options, f"State code {code!r} missing from taxjar_state_code options")


# ── Phase 2: get_iso_3166_2_state_code ───────────────────────────────────────

class TestGetIso3166StateCode(UnitTestCase):
	"""Tests for get_iso_3166_2_state_code() — pycountry is exercised for fallback
	paths; the DB call for country_code is mocked to "US"."""

	def _call(self, state=None, taxjar_state_code=None, country="United States"):
		from taxjar_integration.taxjar_integration.taxjar_integration import get_iso_3166_2_state_code

		address = MagicMock()
		address.get = lambda key, default=None: {
			"state": state,
			"taxjar_state_code": taxjar_state_code,
			"country": country,
		}.get(key, default)

		with patch(
			"taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value",
			return_value="US",
		):
			return get_iso_3166_2_state_code(address)

	# Fast path — taxjar_state_code is set

	def test_prefers_taxjar_state_code_when_set(self):
		"""When taxjar_state_code is a valid code, return it without pycountry."""
		result = self._call(taxjar_state_code="FL", state="Anything")
		self.assertEqual(result, "FL")

	def test_prefers_taxjar_state_code_over_state_field(self):
		"""taxjar_state_code wins even when state would also parse correctly."""
		result = self._call(taxjar_state_code="CA", state="Florida")
		self.assertEqual(result, "CA")

	def test_ignores_taxjar_state_code_if_not_in_supported_list(self):
		"""An unrecognised taxjar_state_code falls through to pycountry lookup."""
		result = self._call(taxjar_state_code="XX", state="California")
		self.assertEqual(result, "CA")

	def test_ignores_blank_taxjar_state_code(self):
		"""Empty string in taxjar_state_code falls through to pycountry."""
		result = self._call(taxjar_state_code="", state="New York")
		self.assertEqual(result, "NY")

	# Fallback — pycountry via state field

	def test_falls_back_to_state_short_code(self):
		"""state='CA' (≤3 chars, valid code) → 'CA'."""
		result = self._call(state="CA")
		self.assertEqual(result, "CA")

	def test_falls_back_to_state_full_name(self):
		"""state='New York' → 'NY' via pycountry name lookup."""
		result = self._call(state="New York")
		self.assertEqual(result, "NY")

	def test_falls_back_to_state_full_name_case_insensitive(self):
		"""state='florida' (lowercase) → 'FL'."""
		result = self._call(state="florida")
		self.assertEqual(result, "FL")

	# Error handling

	def test_none_state_throws_validation_error(self):
		"""state=None with no taxjar_state_code must throw ValidationError, not AttributeError."""
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(state=None, taxjar_state_code=None)

	def test_empty_state_throws_validation_error(self):
		"""state='' with no taxjar_state_code must throw ValidationError."""
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(state="", taxjar_state_code=None)

	def test_invalid_state_name_throws_validation_error(self):
		"""An unrecognisable state like 'Fla.' must throw ValidationError."""
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(state="Fla.", taxjar_state_code=None)

	def test_invalid_short_code_throws_validation_error(self):
		"""A 2-letter code that isn't a real state must throw ValidationError."""
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(state="ZZ", taxjar_state_code=None)


# ── Phase 3: Address desk client script ──────────────────────────────────────

class TestAddressClientScript(UnitTestCase):
	"""Structural tests: hooks registration and JS file content."""

	_APP_ROOT = (
		"/home/raghav/frappe-work/benches/v16-bench-group"
		"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
	)

	def _read_js(self):
		import os
		path = os.path.join(self._APP_ROOT, "public", "js", "address.js")
		with open(path) as f:
			return f.read()

	def test_hooks_registers_address_js(self):
		"""hooks.py must declare Address in doctype_js."""
		from taxjar_integration import hooks
		self.assertIn("Address", hooks.doctype_js)
		self.assertEqual(hooks.doctype_js["Address"], "public/js/address.js")

	def test_address_js_file_exists(self):
		import os
		path = os.path.join(self._APP_ROOT, "public", "js", "address.js")
		self.assertTrue(os.path.isfile(path), "public/js/address.js does not exist")

	def test_address_js_has_state_handler(self):
		js = self._read_js()
		self.assertIn("state(frm)", js, "Missing 'state' event handler in address.js")

	def test_address_js_has_taxjar_state_code_handler(self):
		js = self._read_js()
		self.assertIn("taxjar_state_code(frm)", js, "Missing 'taxjar_state_code' event handler")

	def test_address_js_has_country_handler(self):
		js = self._read_js()
		self.assertIn("country(frm)", js, "Missing 'country' event handler in address.js")

	def test_address_js_guards_against_missing_field(self):
		"""All handlers must check _has_state_code_field before calling frm.set_value."""
		js = self._read_js()
		self.assertIn("_has_state_code_field", js, "Missing field-existence guard in address.js")

	def test_address_js_has_all_supported_state_codes(self):
		"""Every code in SUPPORTED_STATE_CODES must appear as a key in the JS map."""
		js = self._read_js()
		for code in SUPPORTED_STATE_CODES:
			self.assertIn(code, js, f"State code {code!r} missing from address.js")

	def test_address_js_makes_pincode_mandatory_for_us(self):
		"""pincode must be set as required when country is United States."""
		js = self._read_js()
		self.assertIn("_set_us_mandatory_fields", js)
		self.assertIn('"pincode"', js)
		self.assertIn("reqd", js)

	def test_address_js_mandatory_applied_on_refresh_and_country_change(self):
		"""_set_us_mandatory_fields must be called from both refresh and country handlers."""
		js = self._read_js()
		refresh_idx = js.index("refresh(frm)")
		country_idx = js.index("country(frm)")
		self.assertGreater(js.index("_set_us_mandatory_fields", refresh_idx), refresh_idx)
		self.assertGreater(js.index("_set_us_mandatory_fields", country_idx), country_idx)


# ── Nexus HTML renderer — JS content ─────────────────────────────────────────

class TestNexusHtmlRenderer(UnitTestCase):
	"""Structural tests for the nexus grouped-HTML renderer in taxjar_settings.js."""

	_SETTINGS_JS = (
		"/home/raghav/frappe-work/benches/v16-bench-group"
		"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
		"/taxjar_integration/doctype/taxjar_settings/taxjar_settings.js"
	)

	def _read_js(self):
		with open(self._SETTINGS_JS) as f:
			return f.read()

	def test_settings_js_has_render_nexus_html_function(self):
		js = self._read_js()
		self.assertIn("_render_nexus_html", js)

	def test_settings_js_render_called_on_refresh(self):
		js = self._read_js()
		refresh_idx = js.index("refresh(frm)")
		self.assertGreater(js.index("_render_nexus_html", refresh_idx), refresh_idx)

	def test_settings_js_render_called_after_update_nexus(self):
		js = self._read_js()
		btn_idx = js.index("update_nexus_list_btn")
		self.assertGreater(js.index("_render_nexus_html", btn_idx), btn_idx)

	def test_settings_js_groups_by_company(self):
		"""Renderer must group nexus rows by company."""
		js = self._read_js()
		self.assertIn("by_company", js)

	def test_settings_js_renders_region_and_code_columns(self):
		js = self._read_js()
		self.assertIn("region_code", js)
		self.assertIn("country_code", js)

	def test_settings_js_has_empty_state_message(self):
		"""When nexus is empty, a helpful message must be shown."""
		js = self._read_js()
		self.assertIn("Update Nexus List", js)

	def test_settings_js_overflow_x_auto_for_responsiveness(self):
		"""Table wrapper must use overflow-x: auto for narrow-screen support."""
		js = self._read_js()
		self.assertIn("overflow-x: auto", js)
