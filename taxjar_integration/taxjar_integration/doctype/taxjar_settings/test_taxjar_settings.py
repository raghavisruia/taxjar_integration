# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from taxjar_integration.taxjar_integration.taxjar_integration import (
	SUPPORTED_STATE_CODES,
	TAXJAR_ROW_DESCRIPTION,
	_clear_breakdown_data,
	_compute_product_taxable,
	_convert_breakdown_amounts,
	_extract_breakdown_data,
	_extract_breakdown_from_obj,
	_format_address_short,
	_format_address_suggestion,
	_get_customer_name,
	_get_transaction_date,
	_get_usd_exchange_rate,
	_has_taxjar_fields_changed,
	_is_taxjar_enabled,
	_make_safe_customer_id,
	_remove_taxjar_rows,
	_set_customer_sync_status,
	_set_sync_status,
	_set_tax_status_fields,
	_store_breakdown_data,
	_validate_address_with_taxjar,
	check_for_nexus,
	check_sales_tax_exemption,
	delete_customer_from_taxjar,
	delete_transaction_from_taxjar,
	delete_transaction_manual,
	enqueue_taxjar_delete,
	enqueue_taxjar_sync,
	fetch_transaction_from_taxjar,
	get_company_config,
	get_line_item_dict,
	get_taxjar_response_html,
	on_customer_delete,
	on_customer_update,
	on_customer_validate,
	retry_all_failed_syncs,
	set_sales_tax,
	sync_customer_to_taxjar,
	sync_transaction_to_taxjar,
	validate_return_against,
	validate_tax_request,
)
from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	TaxJarSettings,
	_ITEM_BREAKDOWN_FIELDS,
	_TRANSACTION_BREAKDOWN_FIELDS,
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
		self.taxjar_item_breakdown_json = None

	def get(self, field):
		return getattr(self, field, None)


class _FakeDoc:
	"""Minimal stand-in for a Frappe document that supports append() on taxes."""
	def __init__(self, company="Test Co", taxes=None, currency="USD"):
		self.company = company
		self.doctype = "Sales Invoice"
		self.name = "SINV-TEST-001"
		self.docstatus = 0
		self.is_return = False
		self.return_against = None
		self.posting_date = "2025-06-01"
		self.transaction_date = "2025-06-01"
		self.net_total = 1000.0
		self.total = 1000.0
		self.exempt_from_sales_tax = 0
		self.customer = "_Test Customer"
		self.shipping_address_name = "Test Address"
		self.customer_address = None
		self.currency = currency
		self.items = [_FakeItem()]   # must be non-empty to pass the early-return guard
		self.taxes = list(taxes) if taxes else []
		self.taxjar_breakdown_json = None
		self.taxjar_has_nexus = 0
		self.taxjar_nexus_reason = None
		self.taxjar_customer_taxable = 0
		self.taxjar_customer_taxable_reason = None
		self.taxjar_product_taxable = None
		self.taxjar_product_taxable_reason = None
		self.taxjar_ship_from = None
		self.taxjar_ship_to = None

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


def _make_doc(company="Test Co", taxes=None, currency="USD"):
	return _FakeDoc(company=company, taxes=taxes, currency=currency)


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
		tax_data.jurisdictions = MagicMock(state="CA", county="", city="")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
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
		tax_data.jurisdictions = MagicMock(state="CA", county="", city="")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
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
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
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
		"""Every code in SUPPORTED_STATE_CODES must appear in the shared state map.

		The map lives in taxjar_utils.js (loaded globally via the app bundle) and is
		referenced by address.js as taxjar_integration.US_STATE_NAMES.
		"""
		import os
		path = os.path.join(self._APP_ROOT, "public", "js", "taxjar_utils.js")
		with open(path) as f:
			js = f.read()
		# address.js must reference the shared map rather than hardcode its own copy.
		self.assertIn("taxjar_integration.US_STATE_NAMES", self._read_js())
		for code in SUPPORTED_STATE_CODES:
			self.assertIn(code, js, f"State code {code!r} missing from taxjar_utils.js")

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
		"""Document-level exempt_from_sales_tax should return (True, reason) and zero tax."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Tax", 80.0)])
		doc.exempt_from_sales_tax = 1
		config = MagicMock(tax_account_head="Sales Tax - TC")
		is_exempt, reason = check_sales_tax_exemption(doc, config)
		self.assertTrue(is_exempt)
		self.assertIn("exempt", reason.lower())
		self.assertEqual(len([t for t in doc.taxes if t.account_head == "Sales Tax - TC"]), 0)

	def test_blanket_exempt_via_customer(self):
		"""Customer-level exempt_from_sales_tax should return (True, reason) and zero tax."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Tax", 80.0)])
		doc.exempt_from_sales_tax = 0
		config = MagicMock(tax_account_head="Sales Tax - TC")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value",
		           return_value={"exempt_from_sales_tax": 1, "taxjar_exemption_type": "Wholesale"}):
			is_exempt, reason = check_sales_tax_exemption(doc, config)

		self.assertTrue(is_exempt)
		self.assertIn("exempt", reason.lower())
		self.assertEqual(len([t for t in doc.taxes if t.account_head == "Sales Tax - TC"]), 0)

	def test_state_specific_exempt_returns_false(self):
		"""Customer with exempt_regions but exempt_from_sales_tax=0 should NOT short-circuit."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Tax", 80.0)])
		doc.exempt_from_sales_tax = 0
		config = MagicMock(tax_account_head="Sales Tax - TC")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value",
		           return_value={"exempt_from_sales_tax": 0, "taxjar_exemption_type": None}):
			is_exempt, reason = check_sales_tax_exemption(doc, config)

		self.assertFalse(is_exempt)
		self.assertIsNone(reason)
		self.assertEqual(len(doc.taxes), 1)

	def test_quotation_for_lead_does_not_crash(self):
		"""Quotation for Lead has no customer — exemption check should return (False, None) safely."""
		doc = _make_doc()
		doc.doctype = "Quotation"
		doc.quotation_to = "Lead"
		doc.party_name = "LEAD-001"
		del doc.customer
		doc.exempt_from_sales_tax = 0
		config = MagicMock(tax_account_head="Sales Tax - TC")

		is_exempt, reason = check_sales_tax_exemption(doc, config)
		self.assertFalse(is_exempt)


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


# ── Phase 7: Transaction Sync page ───────────────────────────────────────────


class TestTaxJarTransactionSyncPage(UnitTestCase):

	def test_read_methods_require_permission(self):
		"""get_transactions / get_summary must reject users without Sales Invoice read."""
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import (
			get_transactions,
			get_summary,
		)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_transactions()
			with self.assertRaises(frappe.PermissionError):
				get_summary()
		finally:
			frappe.set_user("Administrator")

	def test_page_files_exist(self):
		import os
		page_dir = os.path.join(
			os.path.dirname(__file__),
			"..", "..", "page", "taxjar_transactions",
		)
		page_dir = os.path.normpath(page_dir)
		self.assertTrue(os.path.isfile(os.path.join(page_dir, "taxjar_transactions.py")))
		self.assertTrue(os.path.isfile(os.path.join(page_dir, "taxjar_transactions.js")))
		self.assertTrue(os.path.isfile(os.path.join(page_dir, "taxjar_transactions.json")))

	def test_get_transactions_returns_paginated_response(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import get_transactions
		mock_rows = [
			frappe._dict(
				name=f"SINV-{i}", posting_date="2026-06-01", customer_name="Test",
				grand_total=100, is_return=False, is_debit_note=False,
				taxjar_sync_status="Synced", taxjar_last_synced=None, taxjar_sync_error="",
			)
			for i in range(3)
		]
		with patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.get_all",
			return_value=mock_rows,
		), patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.db.count",
			return_value=3,
		):
			result = get_transactions(filters={}, page=1)

		self.assertEqual(result["total"], 3)
		self.assertEqual(result["page"], 1)
		self.assertEqual(len(result["invoices"]), 3)
		self.assertIn("total_pages", result)
		self.assertIn("page_size", result)

	def test_get_transactions_derives_transaction_type(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import get_transactions
		mock_rows = [
			frappe._dict(
				name="SINV-001", posting_date="2026-06-01", customer_name="A",
				grand_total=100, is_return=False, is_debit_note=False,
				taxjar_sync_status="Synced", taxjar_last_synced=None, taxjar_sync_error="",
			),
			frappe._dict(
				name="SINV-002", posting_date="2026-06-01", customer_name="B",
				grand_total=50, is_return=True, is_debit_note=False,
				taxjar_sync_status="Synced", taxjar_last_synced=None, taxjar_sync_error="",
			),
			frappe._dict(
				name="SINV-003", posting_date="2026-06-01", customer_name="C",
				grand_total=75, is_return=False, is_debit_note=True,
				taxjar_sync_status="Synced", taxjar_last_synced=None, taxjar_sync_error="",
			),
		]
		with patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.get_all",
			return_value=mock_rows,
		), patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.db.count",
			return_value=3,
		):
			result = get_transactions(filters={}, page=1)

		types = [r["transaction_type"] for r in result["invoices"]]
		self.assertEqual(types, ["Invoice", "Credit Note", "Debit Note"])

	def test_get_transactions_truncates_long_error(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import get_transactions
		long_error = "x" * 200
		mock_rows = [
			frappe._dict(
				name="SINV-001", posting_date="2026-06-01", customer_name="A",
				grand_total=100, is_return=False, is_debit_note=False,
				taxjar_sync_status="Failed", taxjar_last_synced=None,
				taxjar_sync_error=long_error,
			),
		]
		with patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.get_all",
			return_value=mock_rows,
		), patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.db.count",
			return_value=1,
		):
			result = get_transactions(filters={}, page=1)

		self.assertTrue(result["invoices"][0]["taxjar_sync_error"].endswith("..."))
		self.assertEqual(len(result["invoices"][0]["taxjar_sync_error"]), 103)

	def test_get_summary_counts(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import get_summary
		# get_summary now aggregates in SQL (group_by status), so the mock returns
		# one row per status with a count.
		mock_rows = [
			frappe._dict(taxjar_sync_status="Synced", cnt=2),
			frappe._dict(taxjar_sync_status="Failed", cnt=1),
			frappe._dict(taxjar_sync_status="Queued", cnt=1),
			frappe._dict(taxjar_sync_status="Not Applicable", cnt=1),
		]
		with patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.get_all",
			return_value=mock_rows,
		):
			result = get_summary(filters={})

		self.assertEqual(result["total"], 5)
		self.assertEqual(result["synced"], 2)
		self.assertEqual(result["failed"], 1)
		self.assertEqual(result["queued"], 1)

	def test_build_conditions_date_range(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import _build_conditions

		conditions = _build_conditions({"from_date": "2026-01-01", "to_date": "2026-06-30"})
		self.assertEqual(conditions["posting_date"], ("between", ("2026-01-01", "2026-06-30")))

		conditions = _build_conditions({"from_date": "2026-01-01"})
		self.assertEqual(conditions["posting_date"], (">=", "2026-01-01"))

		conditions = _build_conditions({"to_date": "2026-06-30"})
		self.assertEqual(conditions["posting_date"], ("<=", "2026-06-30"))

		conditions = _build_conditions({})
		self.assertNotIn("posting_date", conditions)

	def test_build_conditions_always_includes_docstatus(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import _build_conditions
		conditions = _build_conditions({})
		self.assertEqual(conditions["docstatus"], ("in", (1, 2)))

	def test_build_conditions_with_company_and_sync_status(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import _build_conditions
		conditions = _build_conditions({"company": "Test Co", "sync_status": "Failed"})
		self.assertEqual(conditions["company"], "Test Co")
		self.assertEqual(conditions["taxjar_sync_status"], "Failed")

	def test_get_transactions_page_clamped_to_min_1(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import get_transactions
		with patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.get_all",
			return_value=[],
		), patch(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.frappe.db.count",
			return_value=0,
		):
			result = get_transactions(filters={}, page=-5)
		self.assertEqual(result["page"], 1)

	def test_js_has_retry_button(self):
		import os
		js_path = os.path.join(
			os.path.dirname(__file__),
			"..", "..", "page", "taxjar_transactions", "taxjar_transactions.js",
		)
		with open(os.path.normpath(js_path)) as f:
			js = f.read()
		self.assertIn("Retry Selected", js)
		self.assertIn("bulk_retry", js)


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

	def test_get_customers_requires_read_permission(self):
		"""An unprivileged user must not be able to read customer data."""
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_customers()
		finally:
			frappe.set_user("Administrator")

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


# ── Problem 1: pages tolerate missing TaxJar custom fields ───────────────────


class TestNotConfiguredGuards(UnitTestCase):
	"""When the TaxJar custom fields were never created, the desk pages must return
	a not_configured envelope instead of querying non-existent columns (1054)."""

	CUSTOMERS = "taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers"
	TRANSACTIONS = "taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions"

	def test_not_configured_response_shape(self):
		from taxjar_integration.taxjar_integration.pagination import not_configured_response

		bare = not_configured_response()
		self.assertEqual(bare, {"not_configured": True})

		paged = not_configured_response("customers")
		self.assertTrue(paged["not_configured"])
		self.assertEqual(paged["customers"], [])
		self.assertEqual(paged["total"], 0)
		self.assertEqual(paged["page"], 1)
		self.assertIn("total_pages", paged)

	def test_get_customers_not_configured(self):
		from taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers import (
			get_customers,
		)
		with patch(f"{self.CUSTOMERS}.frappe.db.has_column", return_value=False):
			result = get_customers()
		self.assertTrue(result["not_configured"])
		self.assertEqual(result["customers"], [])

	def test_get_transactions_not_configured(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import (
			get_transactions,
		)
		with patch(f"{self.TRANSACTIONS}.frappe.db.has_column", return_value=False):
			result = get_transactions()
		self.assertTrue(result["not_configured"])
		self.assertEqual(result["invoices"], [])

	def test_get_summary_not_configured(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import (
			get_summary,
		)
		with patch(f"{self.TRANSACTIONS}.frappe.db.has_column", return_value=False):
			result = get_summary()
		self.assertEqual(result, {"not_configured": True})

	def test_customer_mutator_throws_when_not_configured(self):
		from taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers import (
			save_exemption_type,
		)
		with patch(f"{self.CUSTOMERS}.frappe.db.has_column", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				save_exemption_type("Any Customer", "Government")

	def test_transaction_mutator_throws_when_not_configured(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import (
			bulk_retry,
		)
		with patch(f"{self.TRANSACTIONS}.frappe.db.has_column", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				bulk_retry(["SINV-0001"])

	def test_page_js_handles_not_configured(self):
		import os
		base = os.path.join(
			os.path.dirname(__file__), "..", "..", "page",
		)
		for page in ("taxjar_customers", "taxjar_transactions"):
			path = os.path.normpath(os.path.join(base, page, f"{page}.js"))
			with open(path) as f:
				js = f.read()
			self.assertIn("not_configured", js)
			self.assertIn("show_not_configured", js)

	def test_taxjar_utils_has_not_configured_panel(self):
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "..", "public", "js", "taxjar_utils.js",
		))
		with open(path) as f:
			js = f.read()
		self.assertIn("render_not_configured_panel", js)


# ── Problem 1: after_install creates custom fields ───────────────────────────


class TestAfterInstall(UnitTestCase):

	def test_after_install_hook_registered(self):
		from taxjar_integration import hooks
		self.assertEqual(hooks.after_install, "taxjar_integration.install.after_install")

	def test_after_install_creates_fields_and_hides_tax_category(self):
		from taxjar_integration import install
		with patch("taxjar_integration.install.make_custom_fields") as mock_make, \
		     patch("taxjar_integration.install.toggle_tax_category_fields") as mock_toggle:
			install.after_install()
		mock_make.assert_called_once()
		mock_toggle.assert_called_once_with(hidden=1)


# ── Tax Breakdown: helpers ──────────────────────────────────────────────────

def _make_us_breakdown():
	"""Return a MagicMock mimicking TaxJar's US tax_for_order breakdown."""
	breakdown = MagicMock()
	breakdown.state_taxable_amount = 100.0
	breakdown.state_tax_rate = 0.0625
	breakdown.state_tax_collectable = 6.25
	breakdown.county_taxable_amount = 100.0
	breakdown.county_tax_rate = 0.01
	breakdown.county_tax_collectable = 1.0
	breakdown.city_taxable_amount = 100.0
	breakdown.city_tax_rate = 0.0
	breakdown.city_tax_collectable = 0.0
	breakdown.special_district_taxable_amount = 100.0
	breakdown.special_tax_rate = 0.025
	breakdown.special_district_tax_collectable = 2.5

	breakdown.country_taxable_amount = 0
	breakdown.country_tax_rate = 0
	breakdown.country_tax_collectable = 0
	breakdown.gst_taxable_amount = 0
	breakdown.gst_tax_rate = 0
	breakdown.gst = 0
	breakdown.pst_taxable_amount = 0
	breakdown.pst_tax_rate = 0
	breakdown.pst = 0
	breakdown.qst_taxable_amount = 0
	breakdown.qst_tax_rate = 0
	breakdown.qst = 0

	li = MagicMock()
	li.id = 1
	li.tax_collectable = 9.75
	li.taxable_amount = 100.0
	li.combined_tax_rate = 0.0975
	li.state_taxable_amount = 100.0
	li.state_sales_tax_rate = 0.0625
	li.state_amount = 6.25
	li.county_taxable_amount = 100.0
	li.county_tax_rate = 0.01
	li.county_amount = 1.0
	li.city_taxable_amount = 100.0
	li.city_tax_rate = 0.0
	li.city_amount = 0.0
	li.special_district_taxable_amount = 100.0
	li.special_tax_rate = 0.025
	li.special_district_amount = 2.5
	li.country_taxable_amount = 0
	li.country_tax_rate = 0
	li.country_tax_collectable = 0
	li.gst_taxable_amount = 0
	li.gst_tax_rate = 0
	li.gst = 0
	li.pst_taxable_amount = 0
	li.pst_tax_rate = 0
	li.pst = 0
	li.qst_taxable_amount = 0
	li.qst_tax_rate = 0
	li.qst = 0
	breakdown.line_items = [li]

	tax_data = MagicMock()
	tax_data.amount_to_collect = 9.75
	tax_data.rate = 0.0975
	tax_data.taxable_amount = 100.0
	tax_data.breakdown = breakdown

	jurisdictions = MagicMock()
	jurisdictions.state = "CA"
	jurisdictions.county = "LOS ANGELES"
	jurisdictions.city = "LOS ANGELES"
	tax_data.jurisdictions = jurisdictions

	return tax_data


def _make_ca_breakdown():
	"""Return a MagicMock mimicking TaxJar's Canadian GST/PST breakdown."""
	breakdown = MagicMock()
	breakdown.state_taxable_amount = 0
	breakdown.state_tax_rate = 0
	breakdown.state_tax_collectable = 0
	breakdown.county_taxable_amount = 0
	breakdown.county_tax_rate = 0
	breakdown.county_tax_collectable = 0
	breakdown.city_taxable_amount = 0
	breakdown.city_tax_rate = 0
	breakdown.city_tax_collectable = 0
	breakdown.special_district_taxable_amount = 0
	breakdown.special_tax_rate = 0
	breakdown.special_district_tax_collectable = 0
	breakdown.country_taxable_amount = 0
	breakdown.country_tax_rate = 0
	breakdown.country_tax_collectable = 0

	breakdown.gst_taxable_amount = 200.0
	breakdown.gst_tax_rate = 0.05
	breakdown.gst = 10.0
	breakdown.pst_taxable_amount = 200.0
	breakdown.pst_tax_rate = 0.07
	breakdown.pst = 14.0
	breakdown.qst_taxable_amount = 0
	breakdown.qst_tax_rate = 0
	breakdown.qst = 0

	li = MagicMock()
	li.id = 1
	li.tax_collectable = 24.0
	li.taxable_amount = 200.0
	li.combined_tax_rate = 0.12
	li.state_taxable_amount = 0
	li.state_sales_tax_rate = 0
	li.state_amount = 0
	li.county_taxable_amount = 0
	li.county_tax_rate = 0
	li.county_amount = 0
	li.city_taxable_amount = 0
	li.city_tax_rate = 0
	li.city_amount = 0
	li.special_district_taxable_amount = 0
	li.special_tax_rate = 0
	li.special_district_amount = 0
	li.country_taxable_amount = 0
	li.country_tax_rate = 0
	li.country_tax_collectable = 0
	li.gst_taxable_amount = 200.0
	li.gst_tax_rate = 0.05
	li.gst = 10.0
	li.pst_taxable_amount = 200.0
	li.pst_tax_rate = 0.07
	li.pst = 14.0
	li.qst_taxable_amount = 0
	li.qst_tax_rate = 0
	li.qst = 0
	breakdown.line_items = [li]

	tax_data = MagicMock()
	tax_data.amount_to_collect = 24.0
	tax_data.rate = 0.12
	tax_data.taxable_amount = 200.0
	tax_data.breakdown = breakdown
	tax_data.jurisdictions = MagicMock(state="", county="", city="")

	return tax_data


# ── Tax Breakdown: _extract_breakdown_data tests ────────────────────────────

class TestExtractBreakdownData(UnitTestCase):

	def test_us_breakdown_transaction_rows(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)

		self.assertIsNotNone(result)
		self.assertEqual(len(result["transaction"]), 4)
		jurisdictions = [r["jurisdiction"] for r in result["transaction"]]
		self.assertEqual(jurisdictions, ["State", "County", "City", "Special"])

	def test_us_breakdown_transaction_values(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)

		state_row = result["transaction"][0]
		self.assertEqual(state_row["jurisdiction"], "State")
		self.assertEqual(state_row["name"], "CA")
		self.assertAlmostEqual(state_row["rate"], 0.0625)
		self.assertAlmostEqual(state_row["tax_amount"], 6.25)

	def test_us_breakdown_totals(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)

		self.assertAlmostEqual(result["totals"]["rate"], 0.0975)
		self.assertAlmostEqual(result["totals"]["amount_to_collect"], 9.75)
		self.assertAlmostEqual(result["totals"]["taxable_amount"], 100.0)

	def test_us_breakdown_line_items(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)

		self.assertEqual(len(result["line_items"]), 1)
		li = result["line_items"][0]
		self.assertEqual(li["id"], 1)
		self.assertAlmostEqual(li["tax_collectable"], 9.75)
		self.assertAlmostEqual(li["taxable_amount"], 100.0)
		self.assertEqual(len(li["breakdown"]), 4)

	def test_us_item_exempt_or_non_taxable(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)

		li = result["line_items"][0]
		for row in li["breakdown"]:
			self.assertAlmostEqual(row["exempt_or_non_taxable"], 0.0)
			self.assertAlmostEqual(row["taxable_amount"], 100.0)

	def test_us_item_partial_exemption(self):
		"""When item_amount > taxable_amount, exempt_or_non_taxable should be the difference."""
		tax_data = _make_us_breakdown()
		tax_data.breakdown.line_items[0].state_taxable_amount = 60.0
		tax_data.breakdown.line_items[0].county_taxable_amount = 60.0
		tax_data.breakdown.line_items[0].city_taxable_amount = 60.0
		tax_data.breakdown.line_items[0].special_district_taxable_amount = 60.0
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)

		li = result["line_items"][0]
		for row in li["breakdown"]:
			self.assertAlmostEqual(row["taxable_amount"], 60.0)
			self.assertAlmostEqual(row["exempt_or_non_taxable"], 40.0)

	def test_canadian_gst_pst_breakdown(self):
		tax_data = _make_ca_breakdown()
		doc = _make_doc()
		doc.items[0].qty = 2
		doc.items[0].rate = 100.0
		result = _extract_breakdown_data(tax_data, doc)

		self.assertIsNotNone(result)
		jurisdictions = [r["jurisdiction"] for r in result["transaction"]]
		self.assertIn("GST", jurisdictions)
		self.assertIn("PST", jurisdictions)
		self.assertNotIn("State", jurisdictions)

	def test_canadian_breakdown_values(self):
		tax_data = _make_ca_breakdown()
		doc = _make_doc()
		doc.items[0].qty = 2
		doc.items[0].rate = 100.0
		result = _extract_breakdown_data(tax_data, doc)

		gst = next(r for r in result["transaction"] if r["jurisdiction"] == "GST")
		self.assertAlmostEqual(gst["rate"], 0.05)
		self.assertAlmostEqual(gst["tax_amount"], 10.0)

		pst = next(r for r in result["transaction"] if r["jurisdiction"] == "PST")
		self.assertAlmostEqual(pst["rate"], 0.07)
		self.assertAlmostEqual(pst["tax_amount"], 14.0)

	def test_no_breakdown_returns_none(self):
		tax_data = MagicMock()
		tax_data.breakdown = None
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)
		self.assertIsNone(result)

	def test_jurisdiction_names_from_tax_data(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)

		state_row = next(r for r in result["transaction"] if r["jurisdiction"] == "State")
		self.assertEqual(state_row["name"], "CA")
		county_row = next(r for r in result["transaction"] if r["jurisdiction"] == "County")
		self.assertEqual(county_row["name"], "LOS ANGELES")


# ── Tax Breakdown: _clear_breakdown_data tests ──────────────────────────────

class TestClearBreakdownData(UnitTestCase):

	def test_clears_doc_breakdown_json(self):
		doc = _make_doc()
		doc.taxjar_breakdown_json = '{"transaction": []}'
		_clear_breakdown_data(doc)
		self.assertIsNone(doc.taxjar_breakdown_json)

	def test_clears_item_breakdown_json(self):
		doc = _make_doc()
		doc.items[0].taxjar_item_breakdown_json = '{"breakdown": []}'
		_clear_breakdown_data(doc)
		self.assertIsNone(doc.items[0].taxjar_item_breakdown_json)

	def test_handles_doc_without_breakdown_field(self):
		doc = MagicMock()
		doc.get.return_value = []
		del doc.taxjar_breakdown_json
		_clear_breakdown_data(doc)


# ── Tax Breakdown: _store_breakdown_data tests ──────────────────────────────

class TestStoreBreakdownData(UnitTestCase):

	def test_stores_json_on_doc(self):
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)

		self.assertIsNotNone(doc.taxjar_breakdown_json)
		data = json.loads(doc.taxjar_breakdown_json)
		self.assertIn("transaction", data)
		self.assertIn("totals", data)
		self.assertIn("line_items", data)

	def test_stores_json_on_items(self):
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)

		self.assertIsNotNone(doc.items[0].taxjar_item_breakdown_json)
		data = json.loads(doc.items[0].taxjar_item_breakdown_json)
		self.assertIn("breakdown", data)
		self.assertEqual(data["id"], 1)

	def test_no_breakdown_leaves_none(self):
		tax_data = MagicMock()
		tax_data.breakdown = None
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		self.assertIsNone(doc.taxjar_breakdown_json)


# ── Tax Breakdown: set_sales_tax integration ────────────────────────────────

class TestSetSalesTaxBreakdown(UnitTestCase):

	def test_breakdown_json_populated_on_tax_calc(self):
		"""set_sales_tax should populate taxjar_breakdown_json when tax is calculated."""
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_for_nexus", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.cache", return_value=_no_cache()):
			set_sales_tax(doc, None)

		self.assertIsNotNone(doc.taxjar_breakdown_json)
		data = json.loads(doc.taxjar_breakdown_json)
		self.assertEqual(len(data["transaction"]), 4)
		self.assertAlmostEqual(data["totals"]["amount_to_collect"], 9.75)

	def test_breakdown_json_populated_on_items(self):
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_for_nexus", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.cache", return_value=_no_cache()):
			set_sales_tax(doc, None)

		self.assertIsNotNone(doc.items[0].taxjar_item_breakdown_json)
		data = json.loads(doc.items[0].taxjar_item_breakdown_json)
		self.assertEqual(len(data["breakdown"]), 4)

	def test_zero_tax_keeps_row_and_stores_breakdown(self):
		"""When TaxJar returns zero tax, a $0 row should be added and breakdown stored."""
		import json
		tax_data = _make_us_breakdown()
		tax_data.amount_to_collect = 0.0
		tax_data.rate = 0.0
		doc = _make_doc()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_for_nexus", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.cache", return_value=_no_cache()):
			set_sales_tax(doc, None)

		tax_rows = [t for t in doc.taxes if t.account_head == "Sales Tax - TC"]
		self.assertEqual(len(tax_rows), 1)
		self.assertEqual(tax_rows[0].tax_amount, 0.0)
		self.assertIsNotNone(doc.taxjar_breakdown_json)
		data = json.loads(doc.taxjar_breakdown_json)
		self.assertIn("transaction", data)

	def test_breakdown_cleared_when_outside_nexus(self):
		"""When delivery is outside nexus, breakdown should be cleared."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Tax", 80.0)])
		doc.taxjar_breakdown_json = '{"old": "data"}'
		doc.items[0].taxjar_item_breakdown_json = '{"old": "item_data"}'
		company_config = MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=company_config), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"to_state": "TX", "dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=None):
			set_sales_tax(doc, None)

		self.assertIsNone(doc.taxjar_breakdown_json)
		self.assertIsNone(doc.items[0].taxjar_item_breakdown_json)


# ── Tax Breakdown: Custom field schema tests ────────────────────────────────

class TestTaxBreakdownCustomFields(UnitTestCase):

	def test_transaction_breakdown_fields_defined(self):
		self.assertEqual(len(_TRANSACTION_BREAKDOWN_FIELDS), 3)
		fieldnames = [f["fieldname"] for f in _TRANSACTION_BREAKDOWN_FIELDS]
		self.assertIn("taxjar_breakdown_section", fieldnames)
		self.assertIn("taxjar_breakdown_json", fieldnames)
		self.assertIn("taxjar_breakdown_html", fieldnames)

	def test_item_breakdown_fields_defined(self):
		self.assertEqual(len(_ITEM_BREAKDOWN_FIELDS), 3)
		fieldnames = [f["fieldname"] for f in _ITEM_BREAKDOWN_FIELDS]
		self.assertIn("taxjar_item_tax_section", fieldnames)
		self.assertIn("taxjar_item_breakdown_json", fieldnames)
		self.assertIn("taxjar_item_breakdown_html", fieldnames)

	def test_breakdown_fields_on_all_transaction_doctypes(self):
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import make_custom_fields
		import inspect
		source = inspect.getsource(make_custom_fields)
		for dt in ("Quotation", "Sales Order", "Sales Invoice"):
			self.assertIn(dt, source, f"make_custom_fields should reference {dt}")

	def test_item_breakdown_fields_on_all_item_tables(self):
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import make_custom_fields
		import inspect
		source = inspect.getsource(make_custom_fields)
		for dt in ("Quotation Item", "Sales Order Item", "Sales Invoice Item"):
			self.assertIn(dt, source, f"make_custom_fields should reference {dt}")

	def test_sales_invoice_breakdown_json_allows_on_submit(self):
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import make_custom_fields
		import inspect
		source = inspect.getsource(make_custom_fields)
		self.assertIn("allow_on_submit", source)

	def test_transaction_fields_insert_after_other_charges(self):
		for f in _TRANSACTION_BREAKDOWN_FIELDS:
			if f["fieldname"] == "taxjar_breakdown_section":
				self.assertEqual(f["insert_after"], "other_charges_calculation")

	def test_item_fields_insert_after_taxable_amount(self):
		for f in _ITEM_BREAKDOWN_FIELDS:
			if f["fieldname"] == "taxjar_item_tax_section":
				self.assertEqual(f["insert_after"], "taxable_amount")


# ── Tax Breakdown: JS structure tests ───────────────────────────────────────

class TestTaxBreakdownJS(UnitTestCase):

	JS_DIR = (
		"/home/raghav/frappe-work/benches/v16-bench-group"
		"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
		"/public/js"
	)

	def _read_js(self, filename):
		import os
		with open(os.path.join(self.JS_DIR, filename)) as f:
			return f.read()

	def test_shared_utils_defines_render_functions(self):
		# Breakdown rendering is shared in the globally-bundled taxjar_utils.js;
		# the per-doctype scripts call the namespaced helpers (see tests below).
		js = self._read_js("taxjar_utils.js")
		self.assertIn("taxjar_integration.render_tax_breakdown", js)
		self.assertIn("taxjar_integration.render_single_item_breakdown", js)
		self.assertIn("taxjar_breakdown_json", js)
		self.assertIn("taxjar_item_breakdown_json", js)

	def test_quotation_js_exists(self):
		import os
		path = os.path.join(
			"/home/raghav/frappe-work/benches/v16-bench-group"
			"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
			"/public/js/quotation.js"
		)
		self.assertTrue(os.path.isfile(path))

	def test_sales_order_js_exists(self):
		import os
		path = os.path.join(
			"/home/raghav/frappe-work/benches/v16-bench-group"
			"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
			"/public/js/sales_order.js"
		)
		self.assertTrue(os.path.isfile(path))

	def test_quotation_js_has_render_functions(self):
		js = self._read_js("quotation.js")
		self.assertIn("taxjar_integration.render_tax_breakdown", js)
		self.assertIn('taxjar_integration.render_single_item_breakdown(frm, cdn, "Quotation Item")', js)

	def test_sales_order_js_has_render_functions(self):
		js = self._read_js("sales_order.js")
		self.assertIn("taxjar_integration.render_tax_breakdown", js)
		self.assertIn('taxjar_integration.render_single_item_breakdown(frm, cdn, "Sales Order Item")', js)

	def test_sales_invoice_js_has_render_functions(self):
		js = self._read_js("sales_invoice.js")
		self.assertIn("taxjar_integration.render_tax_breakdown", js)
		self.assertIn('taxjar_integration.render_single_item_breakdown(frm, cdn, "Sales Invoice Item")', js)

	def test_hooks_register_quotation_js(self):
		from taxjar_integration.hooks import doctype_js
		self.assertIn("Quotation", doctype_js)
		self.assertIn("quotation.js", doctype_js["Quotation"])

	def test_hooks_register_sales_order_js(self):
		from taxjar_integration.hooks import doctype_js
		self.assertIn("Sales Order", doctype_js)
		self.assertIn("sales_order.js", doctype_js["Sales Order"])

	def test_js_renders_jurisdiction_columns(self):
		js = self._read_js("taxjar_utils.js")
		self.assertIn("Jurisdiction", js, "taxjar_utils.js should render Jurisdiction column")
		self.assertIn("Rate", js, "taxjar_utils.js should render Rate column")
		self.assertIn("Tax Amount", js, "taxjar_utils.js should render Tax Amount column")

	def test_item_js_renders_exempt_columns(self):
		js = self._read_js("taxjar_utils.js")
		self.assertIn("Exempt/Non-Taxable", js, "taxjar_utils.js should render Exempt column")
		self.assertIn("Taxable", js, "taxjar_utils.js should render Taxable column")

	def test_item_breakdown_uses_form_render_event(self):
		import os
		child_doctypes = {
			"quotation.js": "Quotation Item",
			"sales_order.js": "Sales Order Item",
			"sales_invoice.js": "Sales Invoice Item",
		}
		for filename, child_dt in child_doctypes.items():
			path = os.path.join(
				"/home/raghav/frappe-work/benches/v16-bench-group"
				"/v16-taxjar-bench/apps/taxjar_integration/taxjar_integration"
				"/public/js",
				filename,
			)
			with open(path) as f:
				js = f.read()
			self.assertIn(child_dt, js, f"{filename} should register handler on {child_dt}")
			self.assertIn("form_render", js, f"{filename} should use form_render event")

	def test_js_has_no_breakdown_message(self):
		js = self._read_js("taxjar_utils.js")
		self.assertIn("No TaxJar tax breakdown available", js, "taxjar_utils.js should have no-breakdown message")

	def test_js_has_multi_currency_support(self):
		js = self._read_js("taxjar_utils.js")
		self.assertIn("data.usd", js, "taxjar_utils.js should check for USD breakdown data")
		self.assertIn("Tax Calculation (USD)", js, "taxjar_utils.js should have USD table heading")
		self.assertIn("Equivalent in Transaction Currency", js, "taxjar_utils.js should have converted table heading")

	def test_js_uses_erpnext_table_styling(self):
		js = self._read_js("taxjar_utils.js")
		self.assertIn("table-hover", js, "taxjar_utils.js should use table-hover class")
		self.assertIn("tax-break-up", js, "taxjar_utils.js should use tax-break-up wrapper")
		self.assertIn("overflow-x: auto", js, "taxjar_utils.js should have overflow-x auto")
		self.assertNotIn("table-sm", js, "taxjar_utils.js should not use table-sm class")


# ── Tax Breakdown: Multi-currency tests ─────────────────────────────────────

class TestGetTransactionDate(UnitTestCase):

	def test_uses_posting_date_for_sales_invoice(self):
		doc = _make_doc()
		doc.posting_date = "2026-01-15"
		doc.transaction_date = "2026-01-10"
		self.assertEqual(_get_transaction_date(doc), "2026-01-15")

	def test_falls_back_to_transaction_date(self):
		doc = _make_doc()
		doc.posting_date = None
		doc.transaction_date = "2026-01-10"
		self.assertEqual(_get_transaction_date(doc), "2026-01-10")


class TestGetUsdExchangeRate(UnitTestCase):

	def test_returns_none_for_usd(self):
		doc = _make_doc(currency="USD")
		self.assertIsNone(_get_usd_exchange_rate(doc))

	def test_returns_rate_for_non_usd(self):
		doc = _make_doc(currency="EUR")
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_exchange_rate", return_value=1.0856):
			rate = _get_usd_exchange_rate(doc)
		self.assertAlmostEqual(rate, 1.0856)

	def test_throws_when_rate_not_found(self):
		doc = _make_doc(currency="EUR")
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_exchange_rate", return_value=0), \
		     self.assertRaises(Exception):
			_get_usd_exchange_rate(doc)


class TestConvertBreakdownAmounts(UnitTestCase):

	def test_converts_transaction_rows(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		usd_data = _extract_breakdown_data(tax_data, doc)
		converted = _convert_breakdown_amounts(usd_data, 1.1)

		state = converted["transaction"][0]
		self.assertAlmostEqual(state["tax_amount"], 6.25 / 1.1, places=2)

	def test_converts_totals(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		usd_data = _extract_breakdown_data(tax_data, doc)
		converted = _convert_breakdown_amounts(usd_data, 1.1)

		self.assertAlmostEqual(converted["totals"]["amount_to_collect"], 9.75 / 1.1, places=2)
		self.assertEqual(converted["totals"]["rate"], usd_data["totals"]["rate"])

	def test_converts_line_item_amounts(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		usd_data = _extract_breakdown_data(tax_data, doc)
		converted = _convert_breakdown_amounts(usd_data, 1.1)

		li = converted["line_items"][0]
		self.assertAlmostEqual(li["tax_collectable"], 9.75 / 1.1, places=2)
		self.assertAlmostEqual(li["item_amount"], 100.0 / 1.1, places=2)

	def test_converts_item_breakdown_rows(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		usd_data = _extract_breakdown_data(tax_data, doc)
		converted = _convert_breakdown_amounts(usd_data, 2.0)

		li_row = converted["line_items"][0]["breakdown"][0]
		self.assertAlmostEqual(li_row["taxable_amount"], 50.0)
		self.assertAlmostEqual(li_row["exempt_or_non_taxable"], 0.0)


class TestStoreBreakdownMultiCurrency(UnitTestCase):

	def test_usd_doc_stores_currency_field(self):
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc(currency="USD")
		_store_breakdown_data(tax_data, doc)

		data = json.loads(doc.taxjar_breakdown_json)
		self.assertEqual(data["currency"], "USD")
		self.assertNotIn("usd", data)

	def test_non_usd_doc_stores_both(self):
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc(currency="EUR")
		_store_breakdown_data(tax_data, doc, usd_rate=1.1)

		data = json.loads(doc.taxjar_breakdown_json)
		self.assertEqual(data["currency"], "EUR")
		self.assertEqual(data["base_currency"], "USD")
		self.assertAlmostEqual(data["exchange_rate"], 1.1)
		self.assertIn("usd", data)
		self.assertIn("transaction", data["usd"])
		self.assertIn("totals", data["usd"])

	def test_non_usd_doc_converted_amounts_differ(self):
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc(currency="EUR")
		_store_breakdown_data(tax_data, doc, usd_rate=1.1)

		data = json.loads(doc.taxjar_breakdown_json)
		usd_total = data["usd"]["totals"]["amount_to_collect"]
		converted_total = data["totals"]["amount_to_collect"]
		self.assertAlmostEqual(usd_total, 9.75)
		self.assertAlmostEqual(converted_total, 9.75 / 1.1, places=2)

	def test_non_usd_item_json_has_usd_key(self):
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc(currency="EUR")
		_store_breakdown_data(tax_data, doc, usd_rate=1.1)

		item_data = json.loads(doc.items[0].taxjar_item_breakdown_json)
		self.assertIn("usd", item_data)
		self.assertEqual(item_data["currency"], "EUR")
		self.assertAlmostEqual(item_data["exchange_rate"], 1.1)

	def test_non_usd_stores_exchange_date(self):
		import json
		tax_data = _make_us_breakdown()
		doc = _make_doc(currency="EUR")
		_store_breakdown_data(tax_data, doc, usd_rate=1.1)

		data = json.loads(doc.taxjar_breakdown_json)
		self.assertEqual(data["exchange_date"], "2025-06-01")


# ── TaxJar Transparency Tab — New helpers ──────────────────────────────────


class TestSetTaxStatusFields(UnitTestCase):

	def test_sets_nexus_fields(self):
		doc = _make_doc()
		_set_tax_status_fields(doc, has_nexus=True, nexus_reason="Nexus in CA")
		self.assertEqual(doc.taxjar_has_nexus, 1)
		self.assertEqual(doc.taxjar_nexus_reason, "Nexus in CA")

	def test_sets_customer_fields(self):
		doc = _make_doc()
		_set_tax_status_fields(doc, customer_taxable=False, customer_reason="Customer is exempt (Wholesale)")
		self.assertEqual(doc.taxjar_customer_taxable, 0)
		self.assertEqual(doc.taxjar_customer_taxable_reason, "Customer is exempt (Wholesale)")

	def test_sets_product_fields(self):
		doc = _make_doc()
		_set_tax_status_fields(doc, product_taxable="Partially", product_reason="2 of 3 items taxable")
		self.assertEqual(doc.taxjar_product_taxable, "Partially")
		self.assertEqual(doc.taxjar_product_taxable_reason, "2 of 3 items taxable")

	def test_sets_address_fields(self):
		doc = _make_doc()
		_set_tax_status_fields(doc, ship_from="Austin, TX 78701", ship_to="New York, NY 10001")
		self.assertEqual(doc.taxjar_ship_from, "Austin, TX 78701")
		self.assertEqual(doc.taxjar_ship_to, "New York, NY 10001")

	def test_skips_none_values(self):
		doc = _make_doc()
		doc.taxjar_nexus_reason = "Old reason"
		_set_tax_status_fields(doc, has_nexus=True)
		self.assertEqual(doc.taxjar_nexus_reason, "Old reason")

	def test_skips_missing_fields(self):
		doc = MagicMock(spec=[])
		_set_tax_status_fields(doc, has_nexus=True, nexus_reason="test")


class TestFormatAddressShort(UnitTestCase):

	def test_full_address(self):
		tax_dict = {"from_city": "Austin", "from_state": "TX", "from_zip": "78701"}
		self.assertEqual(_format_address_short(tax_dict, "from"), "Austin, TX 78701")

	def test_missing_zip(self):
		tax_dict = {"to_city": "Austin", "to_state": "TX", "to_zip": ""}
		self.assertEqual(_format_address_short(tax_dict, "to"), "Austin, TX")

	def test_missing_city(self):
		tax_dict = {"from_city": "", "from_state": "CA", "from_zip": "94105"}
		self.assertEqual(_format_address_short(tax_dict, "from"), "CA 94105")

	def test_empty_dict(self):
		self.assertEqual(_format_address_short({}, "from"), "")


class TestComputeProductTaxable(UnitTestCase):

	def _make_item_with_ptc(self, ptc=None):
		item = _FakeItem()
		item.product_tax_category = ptc
		return item

	def test_all_taxable(self):
		doc = _make_doc()
		doc.items = [self._make_item_with_ptc("20010"), self._make_item_with_ptc("31000")]
		status, reason = _compute_product_taxable(doc)
		self.assertEqual(status, "Yes")
		self.assertIn("2 of 2", reason)

	def test_all_exempt(self):
		doc = _make_doc()
		doc.items = [self._make_item_with_ptc("99999"), self._make_item_with_ptc("99999")]
		status, reason = _compute_product_taxable(doc)
		self.assertEqual(status, "No")
		self.assertIn("0 of 2", reason)

	def test_partially_taxable(self):
		doc = _make_doc()
		doc.items = [self._make_item_with_ptc("20010"), self._make_item_with_ptc("99999")]
		status, reason = _compute_product_taxable(doc)
		self.assertEqual(status, "Partially")
		self.assertIn("1 of 2", reason)

	def test_no_items(self):
		doc = _make_doc()
		doc.items = []
		status, reason = _compute_product_taxable(doc)
		self.assertEqual(status, "")

	def test_no_ptc_counts_as_taxable(self):
		doc = _make_doc()
		doc.items = [self._make_item_with_ptc(None)]
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=None):
			status, reason = _compute_product_taxable(doc)
		self.assertEqual(status, "Yes")


class TestCheckForNexusStatusFields(UnitTestCase):

	def test_no_nexus_sets_status_fields(self):
		config = MagicMock(tax_account_head="Sales Tax - TC")
		doc = _make_doc()
		tax_dict = {"to_state": "DC", "from_city": "Austin", "from_state": "TX", "from_zip": "78701",
		            "to_city": "Washington", "to_zip": "20001"}
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=config):
			result = check_for_nexus(doc, tax_dict)
		self.assertFalse(result)
		self.assertEqual(doc.taxjar_has_nexus, 0)
		self.assertIn("DC", doc.taxjar_nexus_reason)
		self.assertEqual(doc.taxjar_ship_from, "Austin, TX 78701")
		self.assertIn("Washington", doc.taxjar_ship_to)

	def test_no_nexus_does_not_write_breakdown_json(self):
		config = MagicMock(tax_account_head="Sales Tax - TC")
		doc = _make_doc()
		tax_dict = {"to_state": "DC"}
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=config):
			check_for_nexus(doc, tax_dict)
		self.assertIsNone(doc.taxjar_breakdown_json)

	def test_in_nexus_returns_true(self):
		doc = _make_doc()
		tax_dict = {"to_state": "CA"}
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", return_value="NX-1"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock()):
			self.assertTrue(check_for_nexus(doc, tax_dict))


class TestExemptionReasonInTuple(UnitTestCase):

	def test_customer_exempt_with_type(self):
		doc = _make_doc()
		doc.exempt_from_sales_tax = 0
		config = MagicMock(tax_account_head="Sales Tax - TC")
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value",
		           return_value={"exempt_from_sales_tax": 1, "taxjar_exemption_type": "Wholesale"}):
			is_exempt, reason = check_sales_tax_exemption(doc, config)
		self.assertTrue(is_exempt)
		self.assertIn("Wholesale", reason)

	def test_doc_exempt_reason(self):
		doc = _make_doc()
		doc.exempt_from_sales_tax = 1
		config = MagicMock(tax_account_head="Sales Tax - TC")
		is_exempt, reason = check_sales_tax_exemption(doc, config)
		self.assertTrue(is_exempt)
		self.assertIn("Document", reason)
