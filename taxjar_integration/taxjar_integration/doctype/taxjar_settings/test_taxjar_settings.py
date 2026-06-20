# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from taxjar_integration.taxjar_integration.taxjar_integration import (
	SUPPORTED_STATE_CODES,
	TAXJAR_ROW_DESCRIPTION,
	_format_address_suggestion,
	_get_customer_name,
	_has_taxjar_fields_changed,
	_make_safe_customer_id,
	_is_taxjar_enabled,
	_remove_taxjar_rows,
	_set_customer_sync_status,
	_set_sync_status,
	_validate_address_with_taxjar,
	check_for_nexus,
	check_sales_tax_exemption,
	delete_customer_from_taxjar,
	delete_transaction_from_taxjar,
	on_customer_validate,
	delete_transaction_manual,
	enqueue_taxjar_delete,
	enqueue_taxjar_sync,
	fetch_transaction_from_taxjar,
	get_company_config,
	get_line_item_dict,
	get_taxjar_response_html,
	on_customer_delete,
	on_customer_update,
	retry_all_failed_syncs,
	set_sales_tax,
	sync_customer_to_taxjar,
	sync_transaction_to_taxjar,
	validate_return_against,
	validate_tax_request,
)
from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	TaxJarSettings,
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
		self.return_against = None
		self.posting_date = "2025-06-01"
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

def _no_cache():
	"""Return a mock frappe.cache() that always misses."""
	mock_cache = MagicMock()
	mock_cache.get_value.return_value = None
	return mock_cache


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
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.cache", return_value=_no_cache()):
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
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.cache", return_value=_no_cache()):
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


# ── Phase 2: sync_transaction_to_taxjar row detection ────────────────────────

class TestSyncTransactionRowDetection(UnitTestCase):

	def test_taxjar_row_description_constant_value(self):
		self.assertEqual(TAXJAR_ROW_DESCRIPTION, "TaxJar Sales Tax")

	def test_sets_failed_when_no_tax_data(self):
		"""When get_tax_data returns None, sync should mark as Failed."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Template Tax", 80.0)])
		doc.docstatus = 1
		mock_client = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status") as mock_status, \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		mock_status.assert_called_with("SINV-TEST-001", "Failed", error="No TaxJar payload generated")
		mock_client.create_order.assert_not_called()

	def test_uses_taxjar_row_amount_for_transaction(self):
		"""Sales tax amount is taken from the TAXJAR_ROW_DESCRIPTION row."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, 95.0)])
		doc.docstatus = 1
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10.0}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

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


# ── Phase 3: validate_address — server-side hook ─────────────────────────────

class _MockAddress:
	"""Minimal stand-in for a Frappe Address document."""
	def __init__(self, country=None, state=None, taxjar_state_code=None, pincode=None):
		self.country = country
		self.state = state
		self.pincode = pincode
		self._taxjar_state_code = taxjar_state_code

	def get(self, key, default=None):
		if key == "taxjar_state_code":
			return self._taxjar_state_code
		return getattr(self, key, default)


class TestValidateAddress(UnitTestCase):

	def _call(self, doc, country_code):
		from taxjar_integration.taxjar_integration.taxjar_integration import validate_address
		with patch(
			"taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value",
			return_value=country_code,
		), patch(
			"taxjar_integration.taxjar_integration.taxjar_integration._is_taxjar_enabled",
			return_value=False,
		):
			validate_address(doc, None)

	# No country — early return, nothing should raise

	def test_no_country_skips_validation(self):
		from taxjar_integration.taxjar_integration.taxjar_integration import validate_address
		doc = _MockAddress(country=None)
		validate_address(doc, None)  # no mock needed — returns before DB call

	def test_empty_country_skips_validation(self):
		from taxjar_integration.taxjar_integration.taxjar_integration import validate_address
		doc = _MockAddress(country="")
		validate_address(doc, None)

	# United States — all three fields mandatory

	def test_us_missing_state_throws(self):
		doc = _MockAddress(country="United States", state=None, taxjar_state_code="CA", pincode="90210")
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "US")

	def test_us_empty_state_throws(self):
		doc = _MockAddress(country="United States", state="", taxjar_state_code="CA", pincode="90210")
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "US")

	def test_us_missing_taxjar_state_code_throws(self):
		doc = _MockAddress(country="United States", state="California", taxjar_state_code=None, pincode="90210")
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "US")

	def test_us_empty_taxjar_state_code_throws(self):
		doc = _MockAddress(country="United States", state="California", taxjar_state_code="", pincode="90210")
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "US")

	def test_us_missing_pincode_throws(self):
		doc = _MockAddress(country="United States", state="California", taxjar_state_code="CA", pincode=None)
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "US")

	def test_us_empty_pincode_throws(self):
		doc = _MockAddress(country="United States", state="California", taxjar_state_code="CA", pincode="")
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "US")

	def test_us_all_fields_present_passes(self):
		doc = _MockAddress(country="United States", state="California", taxjar_state_code="CA", pincode="90210")
		self._call(doc, "US")  # must not raise

	def test_us_country_code_case_insensitive(self):
		"""DB may return lowercase 'us' — must still apply validation."""
		doc = _MockAddress(country="United States", state=None, taxjar_state_code="CA", pincode="90210")
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "us")

	# Canada — only state is mandatory; taxjar_state_code and pincode are not

	def test_canada_missing_state_throws(self):
		doc = _MockAddress(country="Canada", state=None, taxjar_state_code=None, pincode="M5H 2N2")
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "CA")

	def test_canada_state_present_passes(self):
		doc = _MockAddress(country="Canada", state="Ontario", taxjar_state_code=None, pincode=None)
		self._call(doc, "CA")  # must not raise

	def test_canada_missing_taxjar_state_code_does_not_throw(self):
		"""taxjar_state_code is US-only — missing value is fine for Canada."""
		doc = _MockAddress(country="Canada", state="Ontario", taxjar_state_code=None, pincode="M5H 2N2")
		self._call(doc, "CA")  # must not raise

	def test_canada_missing_pincode_does_not_throw(self):
		"""Pincode is mandatory for US only."""
		doc = _MockAddress(country="Canada", state="Ontario", taxjar_state_code=None, pincode=None)
		self._call(doc, "CA")  # must not raise

	def test_canada_country_code_case_insensitive(self):
		"""DB may return lowercase 'ca' — must still enforce state."""
		doc = _MockAddress(country="Canada", state=None)
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._call(doc, "ca")

	# Other countries — no mandatory rules apply

	def test_other_country_no_fields_required(self):
		doc = _MockAddress(country="Germany", state=None, taxjar_state_code=None, pincode=None)
		self._call(doc, "DE")  # must not raise

	def test_uk_no_fields_required(self):
		doc = _MockAddress(country="United Kingdom", state=None, taxjar_state_code=None, pincode=None)
		self._call(doc, "GB")  # must not raise


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
		self.assertIn("_set_taxjar_mandatory_fields", js)
		self.assertIn('"pincode"', js)
		self.assertIn("reqd", js)

	def test_address_js_mandatory_applied_on_refresh_and_country_change(self):
		"""_set_taxjar_mandatory_fields must be called from both refresh and country handlers."""
		js = self._read_js()
		refresh_idx = js.index("refresh(frm)")
		country_idx = js.index("country(frm)")
		self.assertGreater(js.index("_set_taxjar_mandatory_fields", refresh_idx), refresh_idx)
		self.assertGreater(js.index("_set_taxjar_mandatory_fields", country_idx), country_idx)

	def test_address_js_makes_state_mandatory_for_us_and_ca(self):
		"""state must become required for both United States and Canada."""
		js = self._read_js()
		self.assertIn('"state"', js)
		self.assertIn("Canada", js)

	def test_address_js_makes_taxjar_state_code_mandatory_for_us(self):
		"""taxjar_state_code reqd must be toggled (US only, field is hidden for CA)."""
		js = self._read_js()
		self.assertIn('"taxjar_state_code"', js)
		# reqd is toggled based on is_us, not needs_state
		self.assertIn("is_us", js)

	def test_hooks_registers_address_validate(self):
		"""hooks.py must declare an Address validate doc event."""
		from taxjar_integration import hooks
		self.assertIn("Address", hooks.doc_events)
		self.assertIn("validate", hooks.doc_events["Address"])


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


# ── Phase 1: sync_nexus_list scheduled task ──────────────────────────────────

class TestSyncNexusList(UnitTestCase):
	"""Tests for the daily scheduled task that refreshes nexus from TaxJar."""

	def _make_settings_doc(self, calculate_tax=1, create_transactions=0, has_company_config=True):
		doc = MagicMock()
		doc.taxjar_calculate_tax = calculate_tax
		doc.taxjar_create_transactions = create_transactions
		doc.company_config = [MagicMock()] if has_company_config else []
		return doc

	def _call(self, doc):
		from taxjar_integration.taxjar_integration.tasks import sync_nexus_list
		with patch("taxjar_integration.taxjar_integration.tasks.frappe.get_doc", return_value=doc):
			sync_nexus_list()

	# Guard: features disabled

	def test_skips_when_both_features_disabled(self):
		"""No API call when neither calculate_tax nor create_transactions is on."""
		doc = self._make_settings_doc(calculate_tax=0, create_transactions=0)
		self._call(doc)
		doc.update_nexus_list.assert_not_called()

	def test_runs_when_only_calculate_tax_enabled(self):
		doc = self._make_settings_doc(calculate_tax=1, create_transactions=0)
		self._call(doc)
		doc.update_nexus_list.assert_called_once()

	def test_runs_when_only_create_transactions_enabled(self):
		doc = self._make_settings_doc(calculate_tax=0, create_transactions=1)
		self._call(doc)
		doc.update_nexus_list.assert_called_once()

	def test_runs_when_both_features_enabled(self):
		doc = self._make_settings_doc(calculate_tax=1, create_transactions=1)
		self._call(doc)
		doc.update_nexus_list.assert_called_once()

	# Guard: no company config

	def test_skips_when_no_company_config(self):
		"""No API call when company_config table is empty."""
		doc = self._make_settings_doc(calculate_tax=1, has_company_config=False)
		self._call(doc)
		doc.update_nexus_list.assert_not_called()

	# Error handling

	def test_catches_exception_and_logs_error(self):
		"""Exceptions from update_nexus_list must be caught and logged, not re-raised."""
		doc = self._make_settings_doc(calculate_tax=1)
		doc.update_nexus_list.side_effect = Exception("TaxJar API timeout")

		from taxjar_integration.taxjar_integration.tasks import sync_nexus_list
		with patch("taxjar_integration.taxjar_integration.tasks.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_traceback", return_value="traceback"), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.log_error") as mock_log:
			sync_nexus_list()  # must not raise

		mock_log.assert_called_once_with("traceback", "TaxJar: Nexus sync failed")

	def test_does_not_reraise_exception(self):
		"""Scheduler must not crash if TaxJar is unreachable."""
		doc = self._make_settings_doc(calculate_tax=1)
		doc.update_nexus_list.side_effect = Exception("Network error")

		from taxjar_integration.taxjar_integration.tasks import sync_nexus_list
		with patch("taxjar_integration.taxjar_integration.tasks.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_traceback", return_value="tb"), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.log_error"):
			try:
				sync_nexus_list()
			except Exception:
				self.fail("sync_nexus_list() raised an exception — scheduler would crash")

	# Hooks registration

	def test_hooks_registers_sync_nexus_list_as_daily_job(self):
		"""hooks.py must declare sync_nexus_list in scheduler_events['daily']."""
		from taxjar_integration import hooks
		self.assertIn(
			"taxjar_integration.taxjar_integration.tasks.sync_nexus_list",
			hooks.scheduler_events.get("daily", []),
		)


# ── Phase 2: auto-enqueue nexus sync on first configuration ──────────────────

class TestAutoNexusEnqueue(UnitTestCase):
	"""
	Tests for the on_update auto-enqueue: nexus is fetched in the background
	the first time settings are saved with features + company config + empty nexus.
	"""

	def _settings(self, calculate_tax=1, create_transactions=0, has_company_config=True, has_nexus=False):
		"""Return a live TaxJar Settings single doc wired up for the test scenario."""
		doc = frappe.get_single("TaxJar Settings")
		doc.taxjar_calculate_tax = calculate_tax
		doc.taxjar_create_transactions = create_transactions
		if has_company_config:
			doc.set("company_config", [{"company": "_Test Company", "tax_account_head": "Tax - TC", "shipping_account_head": "Freight - TC"}])
		else:
			doc.set("company_config", [])
		if has_nexus:
			doc.set("nexus", [{"company": "_Test Company", "region": "California", "region_code": "CA", "country": "United States", "country_code": "US"}])
		else:
			doc.set("nexus", [])
		return doc

	def _call_on_update(self, doc):
		"""Call on_update with frappe.flags.in_test=True and enqueue mocked."""
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.enqueue") as mock_enqueue, \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.db.exists", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.toggle_tax_category_fields"):
			doc.on_update()
		return mock_enqueue

	# Trigger conditions

	def test_enqueues_when_features_enabled_config_present_nexus_empty(self):
		"""The happy path: first save after setup should trigger a background nexus fetch."""
		doc = self._settings(calculate_tax=1, has_company_config=True, has_nexus=False)
		mock_enqueue = self._call_on_update(doc)
		mock_enqueue.assert_called_once()
		call_args = mock_enqueue.call_args
		self.assertIn("sync_nexus_list", call_args[0][0])

	def test_enqueues_when_only_create_transactions_enabled(self):
		"""create_transactions alone (without calculate_tax) should also trigger auto-fetch."""
		doc = self._settings(calculate_tax=0, create_transactions=1, has_company_config=True, has_nexus=False)
		mock_enqueue = self._call_on_update(doc)
		mock_enqueue.assert_called_once()

	# Guard: nexus already populated

	def test_does_not_enqueue_when_nexus_already_populated(self):
		"""If nexus rows exist the fetch must not fire — avoids redundant API call on every save."""
		doc = self._settings(calculate_tax=1, has_company_config=True, has_nexus=True)
		mock_enqueue = self._call_on_update(doc)
		# enqueue may be called for the product_tax_categories background job — filter to nexus call only
		nexus_calls = [c for c in mock_enqueue.call_args_list if "sync_nexus_list" in str(c)]
		self.assertEqual(len(nexus_calls), 0)

	# Guard: features disabled

	def test_does_not_enqueue_when_features_disabled(self):
		"""No enqueue when both checkboxes are off."""
		doc = self._settings(calculate_tax=0, create_transactions=0, has_company_config=True, has_nexus=False)
		mock_enqueue = self._call_on_update(doc)
		nexus_calls = [c for c in mock_enqueue.call_args_list if "sync_nexus_list" in str(c)]
		self.assertEqual(len(nexus_calls), 0)

	# Guard: no company config

	def test_does_not_enqueue_when_company_config_empty(self):
		"""No company config means update_nexus_list would fail — skip the enqueue."""
		doc = self._settings(calculate_tax=1, has_company_config=False, has_nexus=False)
		mock_enqueue = self._call_on_update(doc)
		nexus_calls = [c for c in mock_enqueue.call_args_list if "sync_nexus_list" in str(c)]
		self.assertEqual(len(nexus_calls), 0)

	# Queue selection

	def test_enqueue_uses_short_queue(self):
		"""Nexus fetch should go to the short queue — it completes in seconds."""
		doc = self._settings(calculate_tax=1, has_company_config=True, has_nexus=False)
		mock_enqueue = self._call_on_update(doc)
		nexus_calls = [c for c in mock_enqueue.call_args_list if "sync_nexus_list" in str(c)]
		self.assertEqual(nexus_calls[0][1]["queue"], "short")


# ── TaxJar Customer API — _get_customer_name helper ─────────────────────────


class TestGetCustomerName(UnitTestCase):

	def test_sales_invoice(self):
		doc = MagicMock(doctype="Sales Invoice", customer="CUST-001")
		self.assertEqual(_get_customer_name(doc), "CUST-001")

	def test_sales_order(self):
		doc = MagicMock(doctype="Sales Order", customer="CUST-002")
		self.assertEqual(_get_customer_name(doc), "CUST-002")

	def test_quotation_for_customer(self):
		doc = MagicMock(doctype="Quotation", quotation_to="Customer", party_name="CUST-003")
		self.assertEqual(_get_customer_name(doc), "CUST-003")

	def test_quotation_for_lead(self):
		doc = MagicMock(doctype="Quotation", quotation_to="Lead", party_name="LEAD-001")
		self.assertIsNone(_get_customer_name(doc))

	def test_missing_customer_attr(self):
		doc = MagicMock(spec=[], doctype="Sales Invoice")
		self.assertIsNone(_get_customer_name(doc))


# ── TaxJar Customer API — customer_id in get_tax_data ────────────────────────


class TestGetTaxDataCustomerId(UnitTestCase):

	def _call_get_tax_data(self, doc):
		from taxjar_integration.taxjar_integration.taxjar_integration import get_tax_data

		mock_company_config = MagicMock(
			tax_account_head="Sales Tax - TC",
			shipping_account_head="Freight - TC",
		)
		mock_address = MagicMock(
			pincode="78701",
			city="Austin",
			address_line1="123 Main St",
			country="United States",
			state="TX",
		)
		mock_address.get.return_value = "TX"

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=mock_company_config), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_address_details", return_value=mock_address), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_shipping_address_details", return_value=mock_address), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value="us"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_line_item_dict", return_value={}):
			return get_tax_data(doc)

	def test_customer_id_included_for_sales_invoice(self):
		doc = _make_doc()
		result = self._call_get_tax_data(doc)
		self.assertEqual(result["customer_id"], "_Test Customer")

	def test_customer_id_absent_for_lead_quotation(self):
		doc = _make_doc()
		doc.doctype = "Quotation"
		doc.quotation_to = "Lead"
		doc.party_name = "LEAD-001"
		del doc.customer
		result = self._call_get_tax_data(doc)
		self.assertNotIn("customer_id", result)


# ── TaxJar Customer API — check_sales_tax_exemption ─────────────────────────


class TestCheckSalesTaxExemptionUpdated(UnitTestCase):

	def test_blanket_exempt_via_doc_flag(self):
		"""Document-level exempt_from_sales_tax should zero tax and return True."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Tax", 80.0)])
		doc.exempt_from_sales_tax = 1
		config = MagicMock(tax_account_head="Sales Tax - TC")
		result = check_sales_tax_exemption(doc, config)
		self.assertTrue(result)
		self.assertEqual(len([t for t in doc.taxes if t.account_head == "Sales Tax - TC"]), 0)

	def test_blanket_exempt_via_customer(self):
		"""Customer-level exempt_from_sales_tax should zero tax and return True."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Tax", 80.0)])
		doc.exempt_from_sales_tax = 0
		config = MagicMock(tax_account_head="Sales Tax - TC")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=1):
			result = check_sales_tax_exemption(doc, config)

		self.assertTrue(result)
		self.assertEqual(len([t for t in doc.taxes if t.account_head == "Sales Tax - TC"]), 0)

	def test_state_specific_exempt_returns_false(self):
		"""Customer with exempt_regions but exempt_from_sales_tax=0 should NOT short-circuit."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Tax", 80.0)])
		doc.exempt_from_sales_tax = 0
		config = MagicMock(tax_account_head="Sales Tax - TC")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=0):
			result = check_sales_tax_exemption(doc, config)

		self.assertFalse(result)
		self.assertEqual(len(doc.taxes), 1)

	def test_quotation_for_lead_does_not_crash(self):
		"""Quotation for Lead has no customer — exemption check should return False safely."""
		doc = _make_doc()
		doc.doctype = "Quotation"
		doc.quotation_to = "Lead"
		doc.party_name = "LEAD-001"
		del doc.customer
		doc.exempt_from_sales_tax = 0
		config = MagicMock(tax_account_head="Sales Tax - TC")

		result = check_sales_tax_exemption(doc, config)
		self.assertFalse(result)


# ── TaxJar Customer API — sync_customer_to_taxjar ───────────────────────────


class TestSyncCustomerToTaxJar(UnitTestCase):

	def _make_customer_doc(self, exemption_type="Wholesale", exempt_regions=None, customer_id=""):
		doc = MagicMock()
		doc.customer_name = "Acme Corp"
		doc.get.side_effect = lambda field, default=None: {
			"taxjar_exemption_type": exemption_type,
			"taxjar_exempt_regions": exempt_regions or [],
			"taxjar_customer_id": customer_id,
		}.get(field, default)
		return doc

	def _make_exempt_region(self, country="US", state="TX"):
		region = MagicMock()
		region.country = country
		region.state = state
		return region

	def test_new_customer_uses_create(self):
		"""When taxjar_customer_id is empty, should call create_customer directly."""
		customer_doc = self._make_customer_doc(customer_id="")
		mock_client = MagicMock()
		mock_client.create_customer.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call") as mock_log, \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value") as mock_set:
			sync_customer_to_taxjar("CUST-001", company="Test Co")

		mock_client.create_customer.assert_called_once()
		mock_client.update_customer.assert_not_called()
		payload = mock_client.create_customer.call_args[0][0]
		self.assertEqual(payload["customer_id"], "CUST-001")  # already URL-safe
		self.assertEqual(payload["exemption_type"], "wholesale")
		self.assertEqual(payload["name"], "Acme Corp")

		success_calls = [c for c in mock_log.call_args_list if c[1].get("status") == "success"]
		self.assertTrue(len(success_calls) > 0)
		self.assertTrue(mock_set.called)

	def test_new_customer_with_spaces_uses_safe_id(self):
		"""Customer names with spaces should get a URL-safe customer_id."""
		customer_doc = self._make_customer_doc(customer_id="")
		customer_doc.customer_name = "Denna Jaina"
		mock_client = MagicMock()
		mock_client.create_customer.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value") as mock_set:
			sync_customer_to_taxjar("Denna Jaina", company="Test Co")

		payload = mock_client.create_customer.call_args[0][0]
		self.assertEqual(payload["customer_id"], "Denna-Jaina")
		self.assertEqual(payload["name"], "Denna Jaina")
		# taxjar_customer_id stored as the safe ID
		id_set_calls = [c for c in mock_set.call_args_list if len(c[0]) >= 4 and c[0][2] == "taxjar_customer_id"]
		self.assertEqual(id_set_calls[0][0][3], "Denna-Jaina")

	def test_existing_customer_uses_update(self):
		"""When taxjar_customer_id is set, should call update_customer with the stored safe ID."""
		customer_doc = self._make_customer_doc(customer_id="Denna-Jaina")
		mock_client = MagicMock()
		mock_client.update_customer.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value"):
			sync_customer_to_taxjar("Denna Jaina", company="Test Co")

		mock_client.update_customer.assert_called_once()
		# The safe ID (from taxjar_customer_id) should be used, not the raw name
		self.assertEqual(mock_client.update_customer.call_args[0][0], "Denna-Jaina")
		mock_client.create_customer.assert_not_called()

	def test_update_fallback_to_create_on_404(self):
		"""When update_customer returns 404, should fall back to create without clearing taxjar_customer_id."""
		import taxjar.exceptions

		customer_doc = self._make_customer_doc(customer_id="CUST-001")
		mock_client = MagicMock()

		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 404}
		mock_client.update_customer.side_effect = err
		mock_client.create_customer.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value") as mock_set:
			sync_customer_to_taxjar("CUST-001")

		mock_client.create_customer.assert_called_once()
		# taxjar_customer_id must NOT be cleared — prevents permanent broken state if create also fails
		clear_calls = [c for c in mock_set.call_args_list if len(c[0]) >= 4 and c[0][2] == "taxjar_customer_id" and c[0][3] == ""]
		self.assertEqual(len(clear_calls), 0)

	def test_skips_when_no_client(self):
		"""Should log skip and return when client is None."""
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call") as mock_log:
			sync_customer_to_taxjar("CUST-001")

		skip_calls = [c for c in mock_log.call_args_list if c[1].get("status") == "skipped"]
		self.assertEqual(len(skip_calls), 1)

	def test_exempt_regions_serialized(self):
		"""Exempt regions from child table should appear as dicts in the payload."""
		regions = [self._make_exempt_region("US", "TX"), self._make_exempt_region("US", "CA")]
		customer_doc = self._make_customer_doc(exempt_regions=regions, customer_id="")
		mock_client = MagicMock()
		mock_client.create_customer.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value"):
			sync_customer_to_taxjar("CUST-001")

		payload = mock_client.create_customer.call_args[0][0]
		self.assertEqual(payload["exempt_regions"], [{"country": "US", "state": "TX"}, {"country": "US", "state": "CA"}])

	def test_defaults_to_non_exempt(self):
		"""When exemption_type is blank, payload should send 'non_exempt'."""
		customer_doc = self._make_customer_doc(exemption_type="", customer_id="")
		mock_client = MagicMock()
		mock_client.create_customer.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value"):
			sync_customer_to_taxjar("CUST-001")

		payload = mock_client.create_customer.call_args[0][0]
		self.assertEqual(payload["exemption_type"], "non_exempt")

	def test_connection_error_sets_failed(self):
		"""TaxJarConnectionError should set Failed status."""
		import taxjar.exceptions

		customer_doc = self._make_customer_doc(customer_id="")
		mock_client = MagicMock()
		mock_client.create_customer.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_customer_sync_status") as mock_status:
			sync_customer_to_taxjar("CUST-001")

		mock_status.assert_called_once_with("CUST-001", "Failed", error="TaxJar API is unreachable")


# ── TaxJar Customer API — on_customer_update hook ───────────────────────────


class TestOnCustomerUpdate(UnitTestCase):

	def _make_customer_doc(self, exemption_type="Wholesale", customer_id="", exempt_regions=None,
	                       has_value_changed=True, previous_regions=None):
		"""Build a mock Customer doc with TaxJar fields and change-detection support."""
		doc = MagicMock()
		doc.name = "CUST-001"
		doc.db_set = MagicMock()

		regions = exempt_regions or []
		doc.get.side_effect = lambda field, default=None: {
			"taxjar_exemption_type": exemption_type,
			"taxjar_customer_id": customer_id,
			"taxjar_exempt_regions": regions,
		}.get(field, default)

		doc.has_value_changed.return_value = has_value_changed

		if previous_regions is not None:
			previous = MagicMock()
			previous.get.return_value = previous_regions
			doc.get_doc_before_save.return_value = previous
		else:
			doc.get_doc_before_save.return_value = None

		return doc

	def test_skips_when_features_disabled(self):
		doc = self._make_customer_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=0), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			on_customer_update(doc, None)
		mock_enqueue.assert_not_called()

	def test_skips_when_no_exemption_and_never_synced(self):
		"""Clearing exemption on a never-synced customer should not sync."""
		doc = self._make_customer_doc(exemption_type="", customer_id="")
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			on_customer_update(doc, None)
		mock_enqueue.assert_not_called()

	def test_skips_when_no_taxjar_fields_changed(self):
		"""Saving a customer without changing TaxJar fields should not sync."""
		old_region = MagicMock(country="US", state="TX")
		new_region = MagicMock(country="US", state="TX")
		doc = self._make_customer_doc(
			exemption_type="Wholesale", customer_id="CUST-001",
			has_value_changed=False,
			exempt_regions=[new_region], previous_regions=[old_region],
		)
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			on_customer_update(doc, None)
		mock_enqueue.assert_not_called()

	def test_syncs_when_exemption_type_set(self):
		"""Setting exemption type should trigger sync."""
		doc = self._make_customer_doc(exemption_type="Government", customer_id="")
		config = MagicMock(company="Test Co")
		settings = MagicMock()
		settings.company_config = [config]

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			on_customer_update(doc, None)
		mock_enqueue.assert_called_once()

	def test_syncs_when_exemption_cleared_on_synced_customer(self):
		"""Clearing exemption on a previously-synced customer should sync as non_exempt."""
		doc = self._make_customer_doc(exemption_type="", customer_id="CUST-001")
		config = MagicMock(company="Test Co")
		settings = MagicMock()
		settings.company_config = [config]

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			on_customer_update(doc, None)
		mock_enqueue.assert_called_once()
		doc.db_set.assert_called_once_with("taxjar_customer_sync_status", "Queued", update_modified=False)

	def test_syncs_when_exempt_regions_changed(self):
		"""Adding exempt regions should trigger sync even if exemption_type didn't change."""
		old_region = MagicMock(country="US", state="TX")
		new_region_1 = MagicMock(country="US", state="TX")
		new_region_2 = MagicMock(country="US", state="CA")
		doc = self._make_customer_doc(
			exemption_type="Wholesale", customer_id="CUST-001",
			has_value_changed=False,
			exempt_regions=[new_region_1, new_region_2], previous_regions=[old_region],
		)
		config = MagicMock(company="Test Co")
		settings = MagicMock()
		settings.company_config = [config]

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			on_customer_update(doc, None)
		mock_enqueue.assert_called_once()

	def test_enqueues_per_company(self):
		doc = self._make_customer_doc(exemption_type="Wholesale")
		config_a = MagicMock(company="Company A")
		config_b = MagicMock(company="Company B")
		settings = MagicMock()
		settings.company_config = [config_a, config_b]

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			on_customer_update(doc, None)

		self.assertEqual(mock_enqueue.call_count, 2)
		companies_synced = [c[1]["company"] for c in mock_enqueue.call_args_list]
		self.assertIn("Company A", companies_synced)
		self.assertIn("Company B", companies_synced)

	def test_enqueue_uses_short_queue_and_deduplicate(self):
		doc = self._make_customer_doc(exemption_type="Government")
		config = MagicMock(company="Test Co")
		settings = MagicMock()
		settings.company_config = [config]

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			on_customer_update(doc, None)

		call_kwargs = mock_enqueue.call_args[1]
		self.assertEqual(call_kwargs["queue"], "short")
		self.assertTrue(call_kwargs["deduplicate"])
		self.assertIn("job_id", call_kwargs)
		self.assertIn("CUST-001", call_kwargs["job_id"])


# ── TaxJar Customer API — Custom Field definitions ──────────────────────────


class TestCustomerCustomFields(UnitTestCase):

	def _get_customer_field_defs(self):
		"""Intercept create_custom_fields and return the Customer field list."""
		captured = {}
		def fake_create(fields, update=True):
			captured.update(fields)
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.create_custom_fields", side_effect=fake_create):
			make_custom_fields(update=True)
		return {f["fieldname"]: f for f in captured.get("Customer", [])}

	def test_exemption_type_is_select(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_exemption_type"]
		self.assertEqual(f["fieldtype"], "Select")
		for opt in ("Wholesale", "Government", "Other", "Non Exempt"):
			self.assertIn(opt, f["options"])

	def test_exempt_regions_is_table(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_exempt_regions"]
		self.assertEqual(f["fieldtype"], "Table")
		self.assertEqual(f["options"], "TaxJar Customer Exempt Region")

	def test_customer_id_is_readonly(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_customer_id"]
		self.assertEqual(f["fieldtype"], "Data")
		self.assertTrue(f.get("read_only"))

	def test_last_synced_is_readonly_datetime(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_last_synced"]
		self.assertEqual(f["fieldtype"], "Datetime")
		self.assertTrue(f.get("read_only"))

	def test_exempt_regions_depends_on_exemption_type(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_exempt_regions"]
		self.assertIn("taxjar_exemption_type", f.get("depends_on", ""))

	def test_section_inserted_in_tax_tab(self):
		"""TaxJar section must be inside the Tax tab."""
		fields = self._get_customer_field_defs()
		f = fields["taxjar_section_break"]
		self.assertEqual(f["insert_after"], "tax_tab")

	def test_sync_details_section_is_collapsible(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_sync_details_section"]
		self.assertTrue(f.get("collapsible"))

	def test_sync_details_section_depends_on_customer_id(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_sync_details_section"]
		self.assertIn("taxjar_customer_id", f.get("depends_on", ""))


# ── TaxJar Customer API — DocType schema ─────────────────────────────────────


class TestTaxJarCustomerExemptRegion(UnitTestCase):

	def test_doctype_exists(self):
		self.assertTrue(frappe.db.exists("DocType", "TaxJar Customer Exempt Region"))

	def test_is_child_table(self):
		meta = frappe.get_meta("TaxJar Customer Exempt Region")
		self.assertTrue(meta.istable)

	def test_has_country_field(self):
		meta = frappe.get_meta("TaxJar Customer Exempt Region")
		field = meta.get_field("country")
		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Select")

	def test_has_state_field(self):
		meta = frappe.get_meta("TaxJar Customer Exempt Region")
		field = meta.get_field("state")
		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Select")


# ── Transaction Compliance — async sync_transaction_to_taxjar ─────────────────


class TestSyncTransactionCompliance(UnitTestCase):

	def _make_submit_doc(self, is_return=False, return_against=None, sales_tax=85.0):
		doc = _make_doc(
			taxes=[_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, sales_tax)] if sales_tax else [],
		)
		doc.docstatus = 1
		doc.posting_date = "2025-05-31"
		doc.is_return = is_return
		doc.return_against = return_against
		return doc

	def test_transaction_date_uses_posting_date(self):
		"""transaction_date should be doc.posting_date, not today()."""
		doc = self._make_submit_doc()
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10, "dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		payload = mock_client.create_order.call_args[0][0]
		self.assertEqual(payload["transaction_date"], "2025-05-31")

	def test_refund_includes_transaction_reference_id(self):
		"""Refunds must include transaction_reference_id linking to the original order."""
		doc = self._make_submit_doc(is_return=True, return_against="SINV-ORIG-001")
		mock_client = MagicMock()
		mock_client.create_refund.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 0, "dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		payload = mock_client.create_refund.call_args[0][0]
		self.assertEqual(payload["transaction_reference_id"], "SINV-ORIG-001")

	def test_zero_tax_order_still_pushed(self):
		"""$0-tax orders must still be pushed to TaxJar for nexus tracking."""
		doc = self._make_submit_doc(sales_tax=0)
		doc.posting_date = "2025-06-01"
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 0, "dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		mock_client.create_order.assert_called_once()
		payload = mock_client.create_order.call_args[0][0]
		self.assertEqual(payload["sales_tax"], 0)

	def test_provider_field_in_payload(self):
		"""Provider should be 'ERPNext' in all transaction payloads."""
		doc = self._make_submit_doc()
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10, "dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		payload = mock_client.create_order.call_args[0][0]
		self.assertEqual(payload["provider"], "ERPNext")

	def test_sets_synced_on_success(self):
		"""On successful API call, status should be set to Synced."""
		doc = self._make_submit_doc()
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status") as mock_status, \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		mock_status.assert_called_with("SINV-TEST-001", "Synced")


class TestDeleteTransactionCompliance(UnitTestCase):

	def test_order_calls_delete_order(self):
		doc = _make_doc()
		doc.is_return = False
		mock_client = MagicMock()
		mock_client.delete_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			delete_transaction_from_taxjar(doc.name)

		mock_client.delete_order.assert_called_once_with(doc.name, params={"provider": "ERPNext"})
		mock_client.delete_refund.assert_not_called()

	def test_return_calls_delete_refund(self):
		doc = _make_doc()
		doc.is_return = True
		mock_client = MagicMock()
		mock_client.delete_refund.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			delete_transaction_from_taxjar(doc.name)

		mock_client.delete_refund.assert_called_once_with(doc.name, params={"provider": "ERPNext"})
		mock_client.delete_order.assert_not_called()


# ── Phase 2: Payload Enrichment (Items 7, 8) ────────────────────────────────


class TestGetLineItemDiscount(UnitTestCase):

	def test_discount_when_price_list_rate_higher(self):
		item = MagicMock()
		item.get.side_effect = lambda f, d=None: {
			"idx": 1, "qty": 2, "rate": 80.0, "price_list_rate": 100.0,
			"product_tax_category": None, "item_code": None, "tax_collectable": 0,
		}.get(f, d)
		result = get_line_item_dict(item, 0)
		self.assertEqual(result["unit_price"], 100.0)
		self.assertEqual(result["discount"], 20.0)

	def test_no_discount_when_no_price_list(self):
		item = MagicMock()
		item.get.side_effect = lambda f, d=None: {
			"idx": 1, "qty": 1, "rate": 50.0, "price_list_rate": 0,
			"product_tax_category": None, "item_code": None, "tax_collectable": 0,
		}.get(f, d)
		result = get_line_item_dict(item, 0)
		self.assertEqual(result["unit_price"], 50.0)
		self.assertNotIn("discount", result)

	def test_no_discount_when_same_rate(self):
		item = MagicMock()
		item.get.side_effect = lambda f, d=None: {
			"idx": 1, "qty": 1, "rate": 100.0, "price_list_rate": 100.0,
			"product_tax_category": None, "item_code": None, "tax_collectable": 0,
		}.get(f, d)
		result = get_line_item_dict(item, 0)
		self.assertEqual(result["unit_price"], 100.0)
		self.assertNotIn("discount", result)


# ── Phase 3: Token Validation (Item 5) ──────────────────────────────────────


class TestTokenValidation(UnitTestCase):

	def test_valid_token_passes(self):
		"""No error when categories() succeeds."""
		mock_client = MagicMock()
		mock_client.categories.return_value = []

		settings = MagicMock()
		settings.taxjar_calculate_tax = 1
		settings.taxjar_create_transactions = 0
		settings.api_mode = "Sandbox"
		settings.table_hvjw = [MagicMock(company="Test Co", sandbox_token="sk_test")]

		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client", return_value=mock_client):
			settings._validate_tokens = TaxJarSettings._validate_tokens.__get__(settings)
			settings._validate_tokens()

		mock_client.categories.assert_called_once()

	def test_invalid_token_throws(self):
		"""401 response should throw."""
		import taxjar.exceptions
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 401}

		mock_client = MagicMock()
		mock_client.categories.side_effect = err

		settings = MagicMock()
		settings.table_hvjw = [MagicMock(company="Test Co")]

		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client", return_value=mock_client):
			settings._validate_tokens = TaxJarSettings._validate_tokens.__get__(settings)
			self.assertRaises(frappe.ValidationError, settings._validate_tokens)

	def test_connection_error_warns_but_saves(self):
		"""Connection error should warn, not throw."""
		import taxjar.exceptions

		mock_client = MagicMock()
		mock_client.categories.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")

		settings = MagicMock()
		settings.table_hvjw = [MagicMock(company="Test Co")]

		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.msgprint") as mock_msg:
			settings._validate_tokens = TaxJarSettings._validate_tokens.__get__(settings)
			settings._validate_tokens()  # should not raise

		mock_msg.assert_called_once()


# ── Phase 4: API Resilience (Items 6, 11) ────────────────────────────────────


class TestValidateTaxRequestOutage(UnitTestCase):

	def test_connection_error_returns_none_with_warning(self):
		"""TaxJarConnectionError should return None and show msgprint, not throw."""
		import taxjar.exceptions

		mock_client = MagicMock()
		mock_client.tax_for_order.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.msgprint") as mock_msg:
			result = validate_tax_request({"dummy": True})

		self.assertIsNone(result)
		mock_msg.assert_called_once()
		self.assertIn("unreachable", mock_msg.call_args[0][0].lower())


class TestSyncTransactionOutage(UnitTestCase):

	def test_connection_error_sets_failed_status(self):
		"""TaxJarConnectionError in background should set status to Failed, not throw."""
		import taxjar.exceptions

		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, 85.0)])
		doc.docstatus = 1
		doc.posting_date = "2025-06-01"

		mock_client = MagicMock()
		mock_client.create_order.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10, "dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status") as mock_status, \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		mock_status.assert_called_with("SINV-TEST-001", "Failed", error="TaxJar API is unreachable")


class TestDeleteTransactionOutage(UnitTestCase):

	def test_connection_error_sets_failed_status(self):
		"""TaxJarConnectionError on delete should set status to Failed, not throw."""
		import taxjar.exceptions

		doc = _make_doc()
		doc.is_return = False

		mock_client = MagicMock()
		mock_client.delete_order.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status") as mock_status, \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			delete_transaction_from_taxjar(doc.name)

		mock_status.assert_called_with("SINV-TEST-001", "Failed", error="TaxJar API is unreachable")


# ── Phase 5: Address Validation (Item 14) ────────────────────────────────────


class TestAddressValidationWithTaxJar(UnitTestCase):

	def _make_address_doc(self, country_code="US"):
		doc = MagicMock()
		doc.name = "ADDR-001"
		doc.country = "United States" if country_code == "US" else "Germany"
		doc.state = "Texas"
		doc.city = "Austin"
		doc.pincode = "78701"
		doc.address_line1 = "123 Main St"
		doc.get.side_effect = lambda f, d=None: getattr(doc, f, d)
		return doc

	def test_us_address_calls_validation_api(self):
		mock_client = MagicMock()
		mock_client.validate_address.return_value = []
		doc = self._make_address_doc()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._is_taxjar_enabled", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			_validate_address_with_taxjar(doc)

		mock_client.validate_address.assert_called_once()

	def test_connection_error_does_not_block_save(self):
		import taxjar.exceptions

		mock_client = MagicMock()
		mock_client.validate_address.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")
		doc = self._make_address_doc()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			_validate_address_with_taxjar(doc)  # should not raise

	def test_validation_error_shows_warning(self):
		import taxjar.exceptions

		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"detail": "Invalid address"}

		mock_client = MagicMock()
		mock_client.validate_address.side_effect = err
		doc = self._make_address_doc()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.msgprint") as mock_msg:
			_validate_address_with_taxjar(doc)

		mock_msg.assert_called_once()

	def test_format_address_suggestion(self):
		match = MagicMock(street="123 Main St", city="Austin", state="TX", zip="78701", country="US")
		result = _format_address_suggestion(match)
		self.assertIn("Austin", result)
		self.assertIn("TX", result)

	def test_format_address_suggestion_empty(self):
		match = MagicMock(spec=[])
		result = _format_address_suggestion(match)
		self.assertIsNone(result)


# ── Phase 1: Sales Invoice Custom Fields ─────────────────────────────────────


class TestSalesInvoiceCustomFields(UnitTestCase):

	def _get_si_field_defs(self):
		captured = {}
		def fake_create(fields, update=True):
			captured.update(fields)
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.create_custom_fields", side_effect=fake_create), \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.make_property_setter"):
			make_custom_fields(update=True)
		return {f["fieldname"]: f for f in captured.get("Sales Invoice", [])}

	def test_taxjar_tab_exists(self):
		fields = self._get_si_field_defs()
		self.assertIn("taxjar_tab", fields)
		self.assertEqual(fields["taxjar_tab"]["fieldtype"], "Tab Break")

	def test_sync_status_field(self):
		fields = self._get_si_field_defs()
		f = fields["taxjar_sync_status"]
		self.assertEqual(f["fieldtype"], "Select")
		for opt in ("Not Applicable", "Queued", "Synced", "Failed"):
			self.assertIn(opt, f["options"])
		self.assertTrue(f.get("allow_on_submit"))
		self.assertTrue(f.get("read_only"))

	def test_sync_error_field(self):
		fields = self._get_si_field_defs()
		f = fields["taxjar_sync_error"]
		self.assertEqual(f["fieldtype"], "Small Text")
		self.assertTrue(f.get("read_only"))
		self.assertTrue(f.get("allow_on_submit"))

	def test_last_synced_field(self):
		fields = self._get_si_field_defs()
		f = fields["taxjar_last_synced"]
		self.assertEqual(f["fieldtype"], "Datetime")
		self.assertTrue(f.get("read_only"))

	def test_response_html_field(self):
		fields = self._get_si_field_defs()
		self.assertIn("taxjar_response_html", fields)
		self.assertEqual(fields["taxjar_response_html"]["fieldtype"], "HTML")

	def test_response_section_depends_on_synced(self):
		fields = self._get_si_field_defs()
		f = fields["taxjar_response_section"]
		self.assertIn("Synced", f.get("depends_on", ""))


# ── Phase 1: Property Setter for return_against ──────────────────────────────


class TestReturnAgainstPropertySetter(UnitTestCase):

	def test_make_custom_fields_calls_property_setter(self):
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.create_custom_fields"), \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.make_property_setter") as mock_ps:
			make_custom_fields(update=True)

		mock_ps.assert_called_once_with(
			"Sales Invoice", "return_against", "no_copy", "0", "Check",
			for_doctype=False,
		)


# ── Phase 2: validate_return_against ─────────────────────────────────────────


class TestValidateReturnAgainst(UnitTestCase):

	def test_skips_non_return(self):
		doc = _make_doc()
		doc.is_return = False
		validate_return_against(doc, None)

	def test_skips_when_create_transactions_disabled(self):
		doc = _make_doc()
		doc.is_return = True
		doc.return_against = None
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=0):
			validate_return_against(doc, None)

	def test_throws_when_return_without_return_against(self):
		doc = _make_doc()
		doc.is_return = True
		doc.return_against = None
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1):
			self.assertRaises(frappe.ValidationError, validate_return_against, doc, None)

	def test_passes_when_return_with_return_against(self):
		doc = _make_doc()
		doc.is_return = True
		doc.return_against = "SINV-ORIG-001"
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1):
			validate_return_against(doc, None)


# ── Phase 3: enqueue_taxjar_sync / enqueue_taxjar_delete ─────────────────────


class TestEnqueueTaxjarSync(UnitTestCase):

	def test_skips_when_create_transactions_disabled(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=0), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			enqueue_taxjar_sync(doc, None)
		mock_enqueue.assert_not_called()

	def test_skips_when_no_client(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			enqueue_taxjar_sync(doc, None)
		mock_enqueue.assert_not_called()

	def test_sets_queued_and_enqueues(self):
		doc = _make_doc()
		doc.db_set = MagicMock()
		mock_client = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			enqueue_taxjar_sync(doc, None)

		doc.db_set.assert_called_once_with("taxjar_sync_status", "Queued", update_modified=False)
		mock_enqueue.assert_called_once()
		call_kwargs = mock_enqueue.call_args[1]
		self.assertTrue(call_kwargs["deduplicate"])
		self.assertIn("job_id", call_kwargs)


class TestEnqueueTaxjarDelete(UnitTestCase):

	def test_skips_when_create_transactions_disabled(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=0), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			enqueue_taxjar_delete(doc, None)
		mock_enqueue.assert_not_called()

	def test_sets_queued_and_enqueues(self):
		doc = _make_doc()
		doc.db_set = MagicMock()
		mock_client = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			enqueue_taxjar_delete(doc, None)

		doc.db_set.assert_called_once_with("taxjar_sync_status", "Queued", update_modified=False)
		mock_enqueue.assert_called_once()
		self.assertIn("delete", mock_enqueue.call_args[1]["job_id"])


# ── Phase 3: sync_transaction_to_taxjar — cancelled invoice routing ──────────


class TestSyncCancelledInvoice(UnitTestCase):

	def test_cancelled_invoice_routes_to_delete(self):
		"""sync_transaction_to_taxjar on a cancelled doc should call delete_transaction_from_taxjar."""
		doc = _make_doc()
		doc.docstatus = 2
		doc.is_return = False
		mock_client = MagicMock()
		mock_client.delete_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		mock_client.delete_order.assert_called_once()
		mock_client.create_order.assert_not_called()


# ── Phase 4: get_taxjar_response_html ────────────────────────────────────────


class TestGetTaxjarResponseHtml(UnitTestCase):

	def test_returns_empty_when_no_log(self):
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.exists", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=None):
			result = get_taxjar_response_html("SINV-TEST-001")
		self.assertEqual(result, "")

	def test_renders_table_from_log(self):
		import json
		response_data = json.dumps({
			"transaction_id": "SINV-001",
			"transaction_date": "2025-06-01",
			"amount": 1000.0,
			"sales_tax": 82.5,
			"shipping": 10.0,
			"from_state": "TX",
			"to_state": "CA",
			"provider": "ERPNext",
		})

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.exists", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value") as mock_get:
			mock_get.side_effect = ["LOG-001", response_data]
			result = get_taxjar_response_html("SINV-001")

		self.assertIn("SINV-001", result)
		self.assertIn("82.5", result)
		self.assertIn("ERPNext", result)
		self.assertIn("<table", result)

	def test_returns_empty_for_invalid_json(self):
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.exists", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value") as mock_get:
			mock_get.side_effect = ["LOG-001", "not-valid-json{{{"]
			result = get_taxjar_response_html("SINV-001")
		self.assertEqual(result, "")


# ── Phase 5: Sales Invoice JS — structural tests ────────────────────────────


class TestSalesInvoiceClientScript(UnitTestCase):

	_APP_ROOT = (
		"/home/raghav/frappe-work/benches/v16-bench-group"
		"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
	)

	def _read_js(self):
		import os
		path = os.path.join(self._APP_ROOT, "public", "js", "sales_invoice.js")
		with open(path) as f:
			return f.read()

	def test_js_has_refresh_handler(self):
		js = self._read_js()
		self.assertIn("refresh(frm)", js)

	def test_js_renders_taxjar_response(self):
		js = self._read_js()
		self.assertIn("get_taxjar_response_html", js)

	def test_js_has_sync_button(self):
		js = self._read_js()
		self.assertIn("Sync to TaxJar", js)
		self.assertIn("sync_transaction_to_taxjar", js)

	def test_js_has_fetch_button(self):
		js = self._read_js()
		self.assertIn("Fetch from TaxJar", js)
		self.assertIn("fetch_transaction_from_taxjar", js)

	def test_js_has_delete_button(self):
		js = self._read_js()
		self.assertIn("Delete from TaxJar", js)
		self.assertIn("delete_transaction_manual", js)

	def test_js_buttons_grouped_under_taxjar(self):
		js = self._read_js()
		self.assertIn('__("TaxJar")', js)


# ── Phase 6: retry_failed_taxjar_syncs ───────────────────────────────────────


class TestRetryFailedTaxjarSyncs(UnitTestCase):

	def test_skips_when_create_transactions_disabled(self):
		from taxjar_integration.taxjar_integration.tasks import retry_failed_taxjar_syncs
		with patch("taxjar_integration.taxjar_integration.tasks.frappe.db.get_single_value", return_value=0), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_syncs()
		mock_enqueue.assert_not_called()

	def test_enqueues_failed_invoices(self):
		from taxjar_integration.taxjar_integration.tasks import retry_failed_taxjar_syncs
		with patch("taxjar_integration.taxjar_integration.tasks.cint", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_all", return_value=["SINV-001", "SINV-002"]), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_syncs()
		self.assertEqual(mock_enqueue.call_count, 2)

	def test_hooks_registers_cron(self):
		from taxjar_integration import hooks
		self.assertIn("cron", hooks.scheduler_events)
		cron_tasks = hooks.scheduler_events["cron"].get("*/15 * * * *", [])
		self.assertIn("taxjar_integration.taxjar_integration.tasks.retry_failed_taxjar_syncs", cron_tasks)


# ── Phase 6: retry_all_failed_syncs (whitelisted) ───────────────────────────


class TestRetryAllFailedSyncs(UnitTestCase):

	def test_returns_count_of_retried(self):
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_all", return_value=["SINV-001", "SINV-002", "SINV-003"]), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue"):
			count = retry_all_failed_syncs()
		self.assertEqual(count, 3)


# ── Phase 7: Script Report structure ─────────────────────────────────────────


class TestTaxJarTransactionSyncReport(UnitTestCase):

	def test_report_py_exists(self):
		import os
		report_dir = os.path.join(
			"/home/raghav/frappe-work/benches/v16-bench-group"
			"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
			"/taxjar_integration/report/taxjar_transaction_sync"
		)
		self.assertTrue(os.path.isfile(os.path.join(report_dir, "taxjar_transaction_sync.py")))
		self.assertTrue(os.path.isfile(os.path.join(report_dir, "taxjar_transaction_sync.js")))
		self.assertTrue(os.path.isfile(os.path.join(report_dir, "taxjar_transaction_sync.json")))

	def test_report_returns_columns_and_data(self):
		from taxjar_integration.taxjar_integration.report.taxjar_transaction_sync.taxjar_transaction_sync import execute
		with patch("taxjar_integration.taxjar_integration.report.taxjar_transaction_sync.taxjar_transaction_sync.frappe.get_all", return_value=[]):
			columns, data, _, _, summary = execute(filters={})
		self.assertTrue(len(columns) > 0)
		self.assertEqual(len(data), 0)
		self.assertTrue(len(summary) > 0)

	def test_report_summary_counts(self):
		from taxjar_integration.taxjar_integration.report.taxjar_transaction_sync.taxjar_transaction_sync import get_summary
		data = [
			{"taxjar_sync_status": "Synced"},
			{"taxjar_sync_status": "Synced"},
			{"taxjar_sync_status": "Failed"},
			{"taxjar_sync_status": "Queued"},
			{"taxjar_sync_status": "Not Applicable"},
		]
		summary = get_summary(data)
		values = {s["label"]: s["value"] for s in summary}
		self.assertEqual(values["Total Invoices"], 5)
		self.assertEqual(values["Synced"], 2)
		self.assertEqual(values["Failed"], 1)
		self.assertEqual(values["Queued"], 1)

	def test_report_transaction_type_derivation(self):
		from taxjar_integration.taxjar_integration.report.taxjar_transaction_sync.taxjar_transaction_sync import get_data
		row_invoice = frappe._dict(is_return=False, is_debit_note=False, taxjar_sync_status="Synced", taxjar_sync_error="")
		row_credit = frappe._dict(is_return=True, is_debit_note=False, taxjar_sync_status="Synced", taxjar_sync_error="")

		with patch("taxjar_integration.taxjar_integration.report.taxjar_transaction_sync.taxjar_transaction_sync.frappe.get_all", return_value=[row_invoice, row_credit]):
			data = get_data({})

		types = [r["transaction_type"] for r in data]
		self.assertIn("Invoice", types)
		self.assertIn("Credit Note", types)

	def test_report_js_has_retry_all_button(self):
		import os
		js_path = os.path.join(
			"/home/raghav/frappe-work/benches/v16-bench-group"
			"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
			"/taxjar_integration/report/taxjar_transaction_sync"
			"/taxjar_transaction_sync.js"
		)
		with open(js_path) as f:
			js = f.read()
		self.assertIn("Retry All Failed", js)
		self.assertIn("retry_all_failed_syncs", js)


# ── Hooks registration — updated hooks ───────────────────────────────────────


class TestHooksUpdated(UnitTestCase):

	def test_sales_invoice_on_submit_is_enqueue(self):
		from taxjar_integration import hooks
		si_events = hooks.doc_events.get("Sales Invoice", {})
		self.assertIn("enqueue_taxjar_sync", si_events.get("on_submit", ""))

	def test_sales_invoice_on_cancel_is_enqueue(self):
		from taxjar_integration import hooks
		si_events = hooks.doc_events.get("Sales Invoice", {})
		self.assertIn("enqueue_taxjar_delete", si_events.get("on_cancel", ""))

	def test_sales_invoice_validate_has_return_against(self):
		from taxjar_integration import hooks
		si_events = hooks.doc_events.get("Sales Invoice", {})
		self.assertIn("validate_return_against", si_events.get("validate", ""))

	def test_customer_retry_cron_registered(self):
		from taxjar_integration import hooks
		cron_tasks = hooks.scheduler_events.get("cron", {}).get("*/15 * * * *", [])
		self.assertIn("taxjar_integration.taxjar_integration.tasks.retry_failed_taxjar_customer_syncs", cron_tasks)


# ── Customer sync status tracking ────────────────────────────────────────────


class TestCustomerSyncStatusTracking(UnitTestCase):

	def _make_customer_doc(self, exemption_type="Wholesale", exempt_regions=None, customer_id=""):
		doc = MagicMock()
		doc.customer_name = "Acme Corp"
		doc.get.side_effect = lambda field, default=None: {
			"taxjar_exemption_type": exemption_type,
			"taxjar_exempt_regions": exempt_regions or [],
			"taxjar_customer_id": customer_id,
		}.get(field, default)
		return doc

	def test_sync_sets_synced_status_on_success(self):
		customer_doc = self._make_customer_doc(customer_id="CUST-001")
		mock_client = MagicMock()
		mock_client.update_customer.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value") as mock_set, \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_customer_sync_status") as mock_status:
			sync_customer_to_taxjar("CUST-001", company="Test Co")

		mock_status.assert_called_once_with("CUST-001", "Synced")

	def test_sync_sets_failed_status_on_api_error(self):
		import taxjar.exceptions

		customer_doc = self._make_customer_doc(customer_id="")
		mock_client = MagicMock()
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 500, "detail": "Server error"}
		mock_client.create_customer.side_effect = err

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_customer_sync_status") as mock_status:
			sync_customer_to_taxjar("CUST-001", company="Test Co")

		mock_status.assert_called_once()
		self.assertEqual(mock_status.call_args[0][1], "Failed")

	def test_sync_sets_failed_on_create_fallback_error(self):
		"""Update returns 404 → create also fails → status should be Failed."""
		import taxjar.exceptions

		customer_doc = self._make_customer_doc(customer_id="CUST-001")
		mock_client = MagicMock()

		update_err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		update_err.full_response = {"status_code": 404}
		mock_client.update_customer.side_effect = update_err

		create_err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		create_err.full_response = {"status_code": 500, "detail": "Create failed"}
		mock_client.create_customer.side_effect = create_err

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=customer_doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_customer_sync_status") as mock_status:
			sync_customer_to_taxjar("CUST-001", company="Test Co")

		failed_calls = [c for c in mock_status.call_args_list if c[0][1] == "Failed"]
		self.assertTrue(len(failed_calls) > 0)


# ── Customer sync custom fields ──────────────────────────────────────────────


class TestCustomerSyncStatusFields(UnitTestCase):

	def _get_customer_field_defs(self):
		captured = {}
		def fake_create(fields, update=True):
			captured.update(fields)
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.create_custom_fields", side_effect=fake_create), \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.make_property_setter"):
			make_custom_fields(update=True)
		return {f["fieldname"]: f for f in captured.get("Customer", [])}

	def test_sync_status_field_exists(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_customer_sync_status"]
		self.assertEqual(f["fieldtype"], "Select")
		for opt in ("Queued", "Synced", "Failed"):
			self.assertIn(opt, f["options"])
		self.assertTrue(f.get("read_only"))

	def test_sync_error_field_exists(self):
		fields = self._get_customer_field_defs()
		f = fields["taxjar_customer_sync_error"]
		self.assertEqual(f["fieldtype"], "Small Text")
		self.assertTrue(f.get("read_only"))
		self.assertIn("Failed", f.get("depends_on", ""))


# ── Customer retry scheduled task ────────────────────────────────────────────


class TestRetryFailedCustomerSyncs(UnitTestCase):

	def test_skips_when_features_disabled(self):
		from taxjar_integration.taxjar_integration.tasks import retry_failed_taxjar_customer_syncs
		with patch("taxjar_integration.taxjar_integration.tasks.cint", return_value=0), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.db.get_single_value", return_value=0), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_customer_syncs()
		mock_enqueue.assert_not_called()

	def test_enqueues_failed_customers(self):
		from taxjar_integration.taxjar_integration.tasks import retry_failed_taxjar_customer_syncs

		config = MagicMock(company="Test Co")
		settings = MagicMock()
		settings.company_config = [config]

		with patch("taxjar_integration.taxjar_integration.tasks.cint", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_all", return_value=["CUST-001", "CUST-002"]), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_customer_syncs()

		self.assertEqual(mock_enqueue.call_count, 2)


# ── Customer JS — button removed ─────────────────────────────────────────────


class TestCustomerClientScriptUpdated(UnitTestCase):

	_APP_ROOT = (
		"/home/raghav/frappe-work/benches/v16-bench-group"
		"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
	)

	def _read_js(self):
		import os
		path = os.path.join(self._APP_ROOT, "public", "js", "customer.js")
		with open(path) as f:
			return f.read()

	def test_has_sync_button(self):
		"""Manual Sync to TaxJar button should exist for force-syncing."""
		js = self._read_js()
		self.assertIn("Sync to TaxJar", js)

	def test_no_auto_fill_customer_id(self):
		"""taxjar_customer_id should NOT be auto-filled in JS — only set server-side on sync."""
		js = self._read_js()
		self.assertNotIn("frm.set_value(\"taxjar_customer_id\"", js)
		self.assertNotIn("frm.set_value('taxjar_customer_id'", js)

	def test_clears_regions_on_exemption_change(self):
		js = self._read_js()
		self.assertIn("taxjar_exemption_type(frm)", js)
		self.assertIn("clear_table", js)

	def test_sync_button_grouped_under_taxjar(self):
		js = self._read_js()
		self.assertIn('__("TaxJar")', js)


# ── TaxJar Customer API — delete_customer_from_taxjar ─────────────────────


class TestDeleteCustomerFromTaxJar(UnitTestCase):

	def test_happy_path_delete(self):
		"""Successful delete should log success."""
		mock_client = MagicMock()
		mock_client.delete_customer.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call") as mock_log:
			delete_customer_from_taxjar("CUST-001", company="Test Co")

		mock_client.delete_customer.assert_called_once_with("CUST-001")
		success_calls = [c for c in mock_log.call_args_list if c[1].get("status") == "success"]
		self.assertTrue(len(success_calls) > 0)

	def test_404_treated_as_success(self):
		"""Deleting a customer that doesn't exist in TaxJar should not raise."""
		import taxjar.exceptions

		mock_client = MagicMock()
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 404}
		mock_client.delete_customer.side_effect = err

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			delete_customer_from_taxjar("CUST-001")  # should not raise

	def test_500_error_raises(self):
		"""Non-404 API errors should be re-raised."""
		import taxjar.exceptions

		mock_client = MagicMock()
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 500}
		mock_client.delete_customer.side_effect = err

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			with self.assertRaises(taxjar.exceptions.TaxJarResponseError):
				delete_customer_from_taxjar("CUST-001")

	def test_skips_when_no_client(self):
		"""Should return early when client is None."""
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=None):
			delete_customer_from_taxjar("CUST-001")  # should not raise

	def test_connection_error_raises(self):
		"""TaxJarConnectionError should be re-raised."""
		import taxjar.exceptions

		mock_client = MagicMock()
		mock_client.delete_customer.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			with self.assertRaises(taxjar.exceptions.TaxJarConnectionError):
				delete_customer_from_taxjar("CUST-001")


# ── TaxJar Customer API — on_customer_delete hook ──────────────────────────


class TestOnCustomerDelete(UnitTestCase):

	def _make_customer_doc(self, customer_id="CUST-001"):
		doc = MagicMock()
		doc.name = "CUST-001"
		doc.get.side_effect = lambda field, default=None: {
			"taxjar_customer_id": customer_id,
		}.get(field, default)
		return doc

	def test_calls_delete_when_customer_id_set(self):
		doc = self._make_customer_doc(customer_id="CUST-001")
		config = MagicMock(company="Test Co")
		settings = MagicMock()
		settings.company_config = [config]

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.delete_customer_from_taxjar") as mock_delete:
			on_customer_delete(doc, None)

		mock_delete.assert_called_once_with("CUST-001", "Test Co")

	def test_skips_when_no_customer_id(self):
		doc = self._make_customer_doc(customer_id="")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.delete_customer_from_taxjar") as mock_delete:
			on_customer_delete(doc, None)

		mock_delete.assert_not_called()

	def test_skips_when_features_disabled(self):
		doc = self._make_customer_doc(customer_id="CUST-001")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=0), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.delete_customer_from_taxjar") as mock_delete:
			on_customer_delete(doc, None)

		mock_delete.assert_not_called()

	def test_does_not_block_delete_on_api_error(self):
		"""API errors during delete should be caught, not prevent Customer deletion."""
		doc = self._make_customer_doc(customer_id="CUST-001")
		config = MagicMock(company="Test Co")
		settings = MagicMock()
		settings.company_config = [config]

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.delete_customer_from_taxjar", side_effect=Exception("API down")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._get_taxjar_logger"):
			on_customer_delete(doc, None)  # should not raise

	def test_hooks_registers_on_trash(self):
		from taxjar_integration import hooks
		customer_events = hooks.doc_events.get("Customer", {})
		self.assertIn("on_trash", customer_events)
		self.assertIn("on_customer_delete", customer_events["on_trash"])


# ── TaxJar Customer API — _make_safe_customer_id ──────────────────────────


class TestMakeSafeCustomerId(UnitTestCase):

	def test_simple_name_unchanged(self):
		self.assertEqual(_make_safe_customer_id("Acme"), "Acme")

	def test_alphanumeric_with_digits(self):
		self.assertEqual(_make_safe_customer_id("Customer123"), "Customer123")

	def test_spaces_replaced_with_hyphens(self):
		self.assertEqual(_make_safe_customer_id("Denna Jaina"), "Denna-Jaina")

	def test_multiple_spaces_collapsed(self):
		self.assertEqual(_make_safe_customer_id("Don  Bosco"), "Don-Bosco")

	def test_apostrophe_replaced(self):
		self.assertEqual(_make_safe_customer_id("O'Brien"), "O-Brien")

	def test_ampersand_replaced(self):
		self.assertEqual(_make_safe_customer_id("AT&T Corp"), "AT-T-Corp")

	def test_mixed_special_chars(self):
		self.assertEqual(_make_safe_customer_id("Smith & O'Neal (LLC)"), "Smith-O-Neal-LLC")

	def test_leading_trailing_specials_stripped(self):
		self.assertEqual(_make_safe_customer_id(" -Test- "), "Test")

	def test_already_safe_id_unchanged(self):
		self.assertEqual(_make_safe_customer_id("CUST-001"), "CUST-001")

	def test_hyphens_preserved(self):
		self.assertEqual(_make_safe_customer_id("some-id"), "some-id")

	def test_unicode_replaced(self):
		result = _make_safe_customer_id("Müller GmbH")
		self.assertNotIn("ü", result)
		self.assertIn("ller", result)


# ── TaxJar Customer API — _has_taxjar_fields_changed with customer_name ────


class TestHasTaxjarFieldsChangedCustomerName(UnitTestCase):

	def test_customer_name_change_triggers_sync(self):
		"""Changing customer_name should trigger sync even if exemption fields unchanged."""
		doc = MagicMock()
		doc.has_value_changed.side_effect = lambda f: f == "customer_name"
		doc.get.return_value = []
		self.assertTrue(_has_taxjar_fields_changed(doc))

	def test_no_change_returns_false(self):
		"""No changes to any tracked field should return False."""
		doc = MagicMock()
		doc.has_value_changed.return_value = False
		old_region = MagicMock(country="US", state="TX")
		previous = MagicMock()
		previous.get.return_value = [old_region]
		doc.get_doc_before_save.return_value = previous
		new_region = MagicMock(country="US", state="TX")
		doc.get.return_value = [new_region]
		self.assertFalse(_has_taxjar_fields_changed(doc))


# ── on_customer_validate — preserve read-only TaxJar fields ───────────────


class TestOnCustomerValidate(UnitTestCase):

	def _make_doc(self, customer_id="", sync_status="", last_synced=""):
		doc = MagicMock()
		doc.name = "CUST-001"
		doc.is_new.return_value = False
		_values = {
			"taxjar_customer_id": customer_id,
			"taxjar_customer_sync_status": sync_status,
			"taxjar_last_synced": last_synced,
		}
		doc.get.side_effect = lambda f, d=None: _values.get(f, d)
		return doc

	def test_preserves_customer_id_from_stale_overwrite(self):
		"""Form save with stale empty taxjar_customer_id must restore the DB value."""
		doc = self._make_doc(customer_id="")
		db_values = frappe._dict(taxjar_customer_id="CUST-001", taxjar_customer_sync_status="Synced", taxjar_last_synced="2026-06-20 10:00:00")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=db_values):
			on_customer_validate(doc, None)

		doc.set.assert_any_call("taxjar_customer_id", "CUST-001")

	def test_preserves_sync_status_from_stale_overwrite(self):
		"""Sync status should also be preserved from stale form data."""
		doc = self._make_doc(sync_status="")
		db_values = frappe._dict(taxjar_customer_id="CUST-001", taxjar_customer_sync_status="Synced", taxjar_last_synced="")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=db_values):
			on_customer_validate(doc, None)

		doc.set.assert_any_call("taxjar_customer_sync_status", "Synced")

	def test_does_not_overwrite_when_form_has_value(self):
		"""If the form already has the field value, don't touch it."""
		doc = self._make_doc(customer_id="CUST-001", sync_status="Synced")
		db_values = frappe._dict(taxjar_customer_id="CUST-001", taxjar_customer_sync_status="Synced", taxjar_last_synced="")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=db_values):
			on_customer_validate(doc, None)

		doc.set.assert_not_called()

	def test_skips_for_new_customer(self):
		"""New customers have no DB values to preserve."""
		doc = MagicMock()
		doc.is_new.return_value = True

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value") as mock_db:
			on_customer_validate(doc, None)

		mock_db.assert_not_called()

	def test_skips_when_db_has_no_values(self):
		"""If DB fields are also empty, nothing to restore."""
		doc = self._make_doc(customer_id="")
		db_values = frappe._dict(taxjar_customer_id="", taxjar_customer_sync_status="", taxjar_last_synced="")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=db_values):
			on_customer_validate(doc, None)

		doc.set.assert_not_called()

	def test_hooks_registers_validate(self):
		from taxjar_integration import hooks
		customer_events = hooks.doc_events.get("Customer", {})
		self.assertIn("validate", customer_events)
		self.assertIn("on_customer_validate", customer_events["validate"])


# ── TaxJar Customer Config Page — Python API ──────────────────────────────


from taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers import (
	get_customers,
	save_exemption_type,
	get_exempt_regions,
	save_exempt_regions,
	bulk_set_exemption_type,
	bulk_clear_exemption,
	bulk_sync_to_taxjar,
)


class TestCustomerConfigPageAPI(UnitTestCase):

	def test_get_customers_returns_structure(self):
		result = get_customers()
		self.assertIn("customers", result)
		self.assertIn("total", result)
		self.assertIn("page", result)
		self.assertIn("page_size", result)
		self.assertIn("total_pages", result)
		self.assertEqual(result["page"], 1)
		self.assertEqual(result["page_size"], 50)

	def test_get_customers_returns_expected_fields(self):
		result = get_customers()
		if result["customers"]:
			c = result["customers"][0]
			for key in ("name", "customer_name", "customer_group", "taxjar_exemption_type",
			            "taxjar_customer_id", "taxjar_customer_sync_status", "exempt_region_count"):
				self.assertIn(key, c)

	def test_get_customers_filter_by_name(self):
		result = get_customers(filters='{"customer_name": "NONEXISTENT_XYZ"}')
		self.assertEqual(result["total"], 0)
		self.assertEqual(len(result["customers"]), 0)

	def test_get_customers_filter_by_exemption_not_set(self):
		result = get_customers(filters='{"exemption_type": "__not_set"}')
		for c in result["customers"]:
			self.assertIn(c["taxjar_exemption_type"], ("", None))

	def test_get_customers_filter_by_sync_not_set(self):
		result = get_customers(filters='{"sync_status": "__not_set"}')
		for c in result["customers"]:
			self.assertIn(c["taxjar_customer_sync_status"], ("", None))

	def test_get_customers_pagination(self):
		result = get_customers(page=1)
		self.assertEqual(result["page"], 1)
		self.assertGreaterEqual(result["total_pages"], 1)

	def test_get_customers_page_out_of_range(self):
		result = get_customers(page=9999)
		self.assertEqual(len(result["customers"]), 0)

	def test_save_exemption_type(self):
		customers = get_customers()["customers"]
		if not customers:
			return

		name = customers[0]["name"]
		original = customers[0]["taxjar_exemption_type"]

		with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue"):
			save_exemption_type(name, "Government")

		val = frappe.db.get_value("Customer", name, "taxjar_exemption_type")
		self.assertEqual(val, "Government")

		with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue"):
			save_exemption_type(name, original or "")

	def test_save_and_get_exempt_regions(self):
		customers = get_customers()["customers"]
		if not customers:
			return

		name = customers[0]["name"]

		with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue"):
			save_exempt_regions(name, [{"country": "US", "state": "TX"}, {"country": "CA", "state": "ON"}])

		regions = get_exempt_regions(name)
		states = {r["state"] for r in regions}
		self.assertIn("TX", states)
		self.assertIn("ON", states)
		self.assertEqual(len(regions), 2)

		with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue"):
			save_exempt_regions(name, [])

	def test_bulk_set_exemption_type(self):
		customers = get_customers()["customers"]
		if len(customers) < 1:
			return

		names = [customers[0]["name"]]
		originals = {c["name"]: c["taxjar_exemption_type"] for c in customers[:1]}

		with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue"):
			result = bulk_set_exemption_type(names, "Other")

		self.assertEqual(result["updated"], 1)
		val = frappe.db.get_value("Customer", names[0], "taxjar_exemption_type")
		self.assertEqual(val, "Other")

		for name, orig in originals.items():
			with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue"):
				save_exemption_type(name, orig or "")

	def test_bulk_clear_exemption(self):
		customers = get_customers()["customers"]
		if len(customers) < 1:
			return

		name = customers[0]["name"]
		original = frappe.db.get_value("Customer", name, "taxjar_exemption_type")

		with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue"):
			save_exemption_type(name, "Wholesale")
			result = bulk_clear_exemption([name])

		self.assertEqual(result["updated"], 1)
		val = frappe.db.get_value("Customer", name, "taxjar_exemption_type")
		self.assertIn(val, ("", None))

		with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue"):
			save_exemption_type(name, original or "")

	def test_bulk_sync_to_taxjar(self):
		customers = get_customers()["customers"]
		if not customers:
			return

		with patch("taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.frappe.enqueue") as mock_enqueue:
			result = bulk_sync_to_taxjar([c["name"] for c in customers])

		self.assertIn("queued", result)

	def test_page_json_exists(self):
		import os
		page_json = os.path.join(
			"/home/raghav/frappe-work/benches/v16-bench-group"
			"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
			"/taxjar_integration/page/taxjar_customers/taxjar_customers.json"
		)
		self.assertTrue(os.path.isfile(page_json))

	def test_page_js_exists(self):
		import os
		page_js = os.path.join(
			"/home/raghav/frappe-work/benches/v16-bench-group"
			"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
			"/taxjar_integration/page/taxjar_customers/taxjar_customers.js"
		)
		self.assertTrue(os.path.isfile(page_js))

	def test_page_js_has_regions_dialog(self):
		import os
		path = os.path.join(
			"/home/raghav/frappe-work/benches/v16-bench-group"
			"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
			"/taxjar_integration/page/taxjar_customers/taxjar_customers.js"
		)
		with open(path) as f:
			js = f.read()
		self.assertIn("show_regions_dialog", js)
		self.assertIn("US_STATES", js)
		self.assertIn("CA_PROVINCES", js)
		self.assertIn("select-all-country", js)

	def test_workspace_has_page_link(self):
		import json, os
		path = os.path.join(
			"/home/raghav/frappe-work/benches/v16-bench-group"
			"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
			"/taxjar_integration/workspace/taxjar_integration/taxjar_integration.json"
		)
		with open(path) as f:
			ws = json.load(f)
		page_links = [l for l in ws["links"] if l.get("link_to") == "taxjar-customers"]
		self.assertTrue(len(page_links) > 0)
