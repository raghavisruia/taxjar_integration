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
	_get_customer_exemption_type,
	_get_customer_name,
	_get_effective_exemption,
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
	get_taxjar_breakdown_html,
	get_taxjar_response_html,
	on_customer_delete,
	on_customer_update,
	on_customer_validate,
	retry_all_failed_syncs,
	set_sales_tax,
	set_taxjar_breakdown_html,
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
	validate_taxjar_tokens,
)
from taxjar_integration.taxjar_integration.regional.united_states import (
	TAXJAR_TEMPLATE_TITLE,
	_disable_default_us_templates,
	_upsert_tax_template,
	ensure_company_ledgers_and_template,
	resolve_default_ledgers,
	sync_all_company_tax_templates,
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


class _FakeMeta:
	"""Stand-in for doc.meta - real doctype metadata, not instance state.

	Deliberately NOT driven off which attributes _FakeDoc happens to have set:
	a virtual field (is_virtual=1, no backing @property) is never set as a
	plain instance attribute on a real Document until something explicitly
	assigns it - hasattr(doc, fieldname) is unreliable for exactly the fields
	set_taxjar_breakdown_html needs to check for. This bit taxjar_breakdown_html
	in production: a hasattr guard silently no-opped for every real document.
	"""
	def __init__(self, fields=("taxjar_breakdown_html", "taxjar_breakdown_json", "taxjar_freight_taxable")):
		self._fields = set(fields)

	def has_field(self, fieldname):
		return fieldname in self._fields


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
		self.taxjar_freight_taxable = 0
		self.taxjar_breakdown_html = None
		self._onload = {}
		self.meta = _FakeMeta()

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

	def set_onload(self, key, value):
		self._onload[key] = value

	def get_onload(self, key=None):
		return self._onload[key] if key else self._onload


def _make_doc(company="Test Co", taxes=None, currency="USD"):
	return _FakeDoc(company=company, taxes=taxes, currency=currency)


# ── Phase 1: Schema & Validation ──────────────────────────────────────────────

class TestTaxJarSettings(UnitTestCase):

	def setUp(self):
		self.settings = frappe.get_single("TaxJar Settings")
		self.settings.taxjar_enabled = 0
		self.settings.api_mode = "Live"
		self.settings.set("table_hvjw", [])
		self.settings.set("company_config", [])
		self.settings.set("nexus", [])

	def _add_sandbox_credential(self):
		self.settings.append("table_hvjw", {
			"company": "_Test Company",
			"sandbox_token": "test-sandbox-token",
		})

	def _enable_feature(self, calculate=0, create=0, company="_Test Company"):
		"""Turn on the master switch and add a company config row carrying the
		per-company feature flags."""
		self.settings.taxjar_enabled = 1
		self.settings.set("company_config", [{
			"company": company,
			"tax_account_head": "Sales Tax - _TC",
			"shipping_account_head": "Freight - _TC",
			"taxjar_calculate_tax": calculate,
			"taxjar_create_transactions": create,
		}])

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
		self._enable_feature(calculate=1)
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_validate_blank_mode_with_create_transactions_throws(self):
		self.settings.api_mode = ""
		self._enable_feature(create=1)
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	# validate() — features enabled, sandbox mode

	def test_validate_sandbox_requires_sandbox_token_when_features_enabled(self):
		self.settings.api_mode = "Sandbox"
		self._enable_feature(calculate=1)
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_validate_sandbox_passes_with_sandbox_token_and_features_enabled(self):
		self.settings.api_mode = "Sandbox"
		self._enable_feature(calculate=1)
		self._add_sandbox_credential()
		self.settings.validate()  # must not raise

	def test_validate_sandbox_fails_when_only_live_token_present_and_features_enabled(self):
		"""A row with only live_token is not enough for Sandbox mode."""
		self.settings.api_mode = "Sandbox"
		self._enable_feature(calculate=1)
		self.settings.append("table_hvjw", {
			"company": "_Test Company",
			"live_token": "test-live-token",
		})
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	# validate() — features enabled, live mode

	def test_validate_live_requires_credential_when_features_enabled(self):
		self.settings.api_mode = "Live"
		self._enable_feature(calculate=1)
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_validate_live_passes_with_credential_and_features_enabled(self):
		self.settings.api_mode = "Live"
		self._enable_feature(calculate=1)
		self.settings.append("table_hvjw", {
			"company": "_Test Company",
			"live_token": "test-live-token",
		})
		self.settings.validate()  # must not raise

	def test_validate_both_features_enabled_passes(self):
		self.settings.api_mode = "Sandbox"
		self._add_sandbox_credential()
		self._enable_feature(calculate=1, create=1)
		self.settings.validate()  # must not raise

	# validate() — feature independence

	def test_create_transactions_alone_enforces_credentials(self):
		"""create_transactions alone (without calculate_tax) must still enforce credentials."""
		self.settings.api_mode = "Live"
		self._enable_feature(create=1)
		self.settings.set("table_hvjw", [])
		with self.assertRaises(frappe.exceptions.ValidationError):
			self.settings.validate()

	def test_calculate_tax_alone_enforces_credentials(self):
		self.settings.api_mode = "Sandbox"
		self._enable_feature(calculate=1)
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
		# Feature flags are per-company (moved off the TaxJar Settings single).
		self.assertIn("taxjar_calculate_tax", fieldnames)
		self.assertIn("taxjar_create_transactions", fieldnames)

	def test_taxjar_settings_has_master_switch_not_per_feature_flags(self):
		meta = frappe.get_meta("TaxJar Settings")
		self.assertIsNotNone(meta.get_field("taxjar_enabled"))
		self.assertIsNone(meta.get_field("taxjar_calculate_tax"))
		self.assertIsNone(meta.get_field("taxjar_create_transactions"))

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

	def test_customer_taxable_status_true_when_not_exempt(self):
		"""No taxjar_exemption_type set on the customer - status stays "Taxable",
		matching the pre-existing default behaviour."""
		doc = _make_doc(taxes=[])

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
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._get_customer_exemption_type", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.cache", return_value=_no_cache()):
			set_sales_tax(doc, None)

		self.assertEqual(doc.taxjar_customer_taxable, 1)
		self.assertEqual(doc.taxjar_customer_taxable_reason, "Taxable")

	def test_customer_taxable_status_reflects_exemption_type_even_when_tax_is_computed(self):
		"""Regression guard: a customer with a TaxJar exemption_type set
		(Wholesale/Government/Other) but without the blunt exempt_from_sales_tax
		checkbox still reaches the TaxJar API call - region-scoped exemption is
		TaxJar's own job via customer_id, not replicated here. Previously the
		status matrix hardcoded "Is the customer taxable? Yes" regardless of
		this, even when the customer's own master data said otherwise. The tax
		amount itself is untouched (still whatever TaxJar computed) - only the
		status label is corrected."""
		doc = _make_doc(taxes=[])

		tax_data = MagicMock()
		tax_data.amount_to_collect = 0.0
		tax_data.breakdown.line_items = []
		tax_data.jurisdictions = MagicMock(state="CA", county="", city="")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_for_nexus", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._get_customer_exemption_type", return_value="Wholesale"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.cache", return_value=_no_cache()):
			set_sales_tax(doc, None)

		self.assertEqual(doc.taxjar_customer_taxable, 0)
		self.assertIn("Wholesale", doc.taxjar_customer_taxable_reason)
		tax_rows = [t for t in doc.taxes if t.account_head == "Sales Tax - TC"]
		self.assertEqual(tax_rows[0].tax_amount, 0.0)

	def test_customer_taxable_status_shows_transaction_override_distinctly(self):
		"""taxjar_transaction_exempt (no customer-level exemption on file) must
		read differently from a master-level exemption - "Overridden (...)",
		not "Customer is exempt (...)" - so the applicability matrix makes clear
		this was a one-off decision on this transaction, not the customer's
		standing status."""
		doc = _make_doc(taxes=[])
		doc.taxjar_transaction_exempt = 1
		doc.taxjar_transaction_exemption_type = "Government"

		tax_data = MagicMock()
		tax_data.amount_to_collect = 0.0
		tax_data.breakdown.line_items = []
		tax_data.jurisdictions = MagicMock(state="CA", county="", city="")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_single_value", return_value=1), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_region", return_value="United States"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=MagicMock(tax_account_head="Sales Tax - TC", shipping_account_head="Freight - TC")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_sales_tax_exemption", return_value=(False, None)), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"dummy": True}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.check_for_nexus", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.validate_tax_request", return_value=tax_data), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._get_customer_exemption_type", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.cache", return_value=_no_cache()):
			set_sales_tax(doc, None)

		self.assertEqual(doc.taxjar_customer_taxable, 0)
		self.assertEqual(doc.taxjar_customer_taxable_reason, "Overridden (Government)")

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
		self.assertEqual(TAXJAR_ROW_DESCRIPTION, "Sales Tax")

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
		"""Sales tax amount is taken from the row matching company_config.tax_account_head -
		not by matching the row's (user-editable) description text."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, 95.0)])
		doc.docstatus = 1
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()
		mock_config = MagicMock(tax_account_head="Sales Tax - TC")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=mock_config), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10.0}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		mock_client.create_order.assert_called_once()
		self.assertEqual(mock_client.create_order.call_args[0][0]["sales_tax"], 95.0)

	def test_row_description_does_not_affect_sales_tax_match(self):
		"""A user retitling the row's description before submit must not
		change what gets reported to TaxJar - only account_head matters."""
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", "Whatever the user renamed it to", 95.0)])
		doc.docstatus = 1
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()
		mock_config = MagicMock(tax_account_head="Sales Tax - TC")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=mock_config), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10.0}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		self.assertEqual(mock_client.create_order.call_args[0][0]["sales_tax"], 95.0)

	def test_other_account_rows_excluded_even_with_matching_description(self):
		"""A non-TaxJar row that happens to share the description text must
		not be counted - account_head is the only thing that matters."""
		doc = _make_doc(taxes=[
			_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, 95.0),
			_make_tax_row("Other Account - TC", TAXJAR_ROW_DESCRIPTION, 1000.0),
		])
		doc.docstatus = 1
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()
		mock_config = MagicMock(tax_account_head="Sales Tax - TC")

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=mock_config), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10.0}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		self.assertEqual(mock_client.create_order.call_args[0][0]["sales_tax"], 95.0)

	def test_no_company_config_yields_zero_sales_tax_not_a_crash(self):
		doc = _make_doc(taxes=[_make_tax_row("Sales Tax - TC", TAXJAR_ROW_DESCRIPTION, 95.0)])
		doc.docstatus = 1
		mock_client = MagicMock()
		mock_client.create_order.return_value = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.get_doc", return_value=doc), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_tax_data", return_value={"shipping": 10.0}), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration._set_sync_status"), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.log_taxjar_call"):
			sync_transaction_to_taxjar("SINV-TEST-001")

		self.assertEqual(mock_client.create_order.call_args[0][0]["sales_tax"], 0)


# ── Phase 1: taxjar_state_code custom field on Address ───────────────────────

class TestItemProductTaxCategoryQuickEntry(UnitTestCase):
	"""product_tax_category should be pickable from Item's Quick Entry dialog
	without being made globally mandatory - Frappe's quick_entry.js includes a
	field when reqd OR allow_in_quick_entry is set (never both needed)."""

	def _get_item_field(self):
		captured = {}

		def _capture(custom_fields, update=True):
			captured.update(custom_fields)

		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.create_custom_fields",
			side_effect=_capture,
		):
			make_custom_fields()

		return next(f for f in captured["Item"] if f["fieldname"] == "product_tax_category")

	def test_allowed_in_quick_entry(self):
		field = self._get_item_field()
		self.assertEqual(field.get("allow_in_quick_entry"), 1)

	def test_not_made_globally_mandatory(self):
		"""allow_in_quick_entry, not reqd - ticking this shouldn't block saving
		an Item from the full form when left blank."""
		field = self._get_item_field()
		self.assertFalse(field.get("reqd"))


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


# ── Transaction-level exemption override (taxjar_transaction_exempt) ────────


class TestTransactionExemptionCustomFields(UnitTestCase):
	"""JSON-shape assertions on the taxjar_transaction_exempt/exemption_type
	custom fields - same intercept-create_custom_fields pattern as
	TestAddressCustomField, no DB writes."""

	def _get_custom_fields(self):
		captured = {}

		def _capture(custom_fields, update=True):
			captured.update(custom_fields)

		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.create_custom_fields",
			side_effect=_capture,
		):
			make_custom_fields()

		return captured

	def _field(self, fields, fieldname):
		return next(f for f in fields if f["fieldname"] == fieldname)

	def test_present_on_all_three_transaction_doctypes(self):
		captured = self._get_custom_fields()
		for doctype in ("Quotation", "Sales Order", "Sales Invoice"):
			fieldnames = [f["fieldname"] for f in captured[doctype]]
			self.assertIn("taxjar_transaction_exempt", fieldnames, doctype)
			self.assertIn("taxjar_transaction_exemption_type", fieldnames, doctype)

	def test_not_present_on_customer(self):
		"""Transaction-level override is per-document, not per-customer-master —
		Customer exemption stays on taxjar_exemption_type."""
		captured = self._get_custom_fields()
		fieldnames = [f["fieldname"] for f in captured["Customer"]]
		self.assertNotIn("taxjar_transaction_exempt", fieldnames)

	def test_checkbox_sits_right_after_shipping_rule(self):
		captured = self._get_custom_fields()
		for doctype in ("Quotation", "Sales Order", "Sales Invoice"):
			field = self._field(captured[doctype], "taxjar_transaction_exempt")
			self.assertEqual(field["insert_after"], "shipping_rule", doctype)

	def test_checkbox_is_a_check_field(self):
		captured = self._get_custom_fields()
		field = self._field(captured["Sales Invoice"], "taxjar_transaction_exempt")
		self.assertEqual(field["fieldtype"], "Check")
		self.assertEqual(field["label"], "Is transaction exempt from sales taxes?")

	def test_exemption_type_select_options_and_visibility(self):
		captured = self._get_custom_fields()
		field = self._field(captured["Sales Invoice"], "taxjar_transaction_exemption_type")
		self.assertEqual(field["fieldtype"], "Select")
		self.assertEqual(field["options"], "\nWholesale\nGovernment\nOther")
		self.assertNotIn("Non Exempt", field["options"])
		self.assertIn("taxjar_transaction_exempt", field["depends_on"])
		self.assertIn("taxjar_transaction_exempt", field["mandatory_depends_on"])
		self.assertEqual(field["insert_after"], "incoterm")

	def test_exemption_type_sits_right_after_incoterm(self):
		captured = self._get_custom_fields()
		for doctype in ("Quotation", "Sales Order", "Sales Invoice"):
			field = self._field(captured[doctype], "taxjar_transaction_exemption_type")
			self.assertEqual(field["insert_after"], "incoterm", doctype)


class TestHideLegacyExemptFromSalesTax(UnitTestCase):

	def test_hides_on_all_four_doctypes_when_column_exists(self):
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
			hide_legacy_exempt_from_sales_tax,
		)
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.db.has_column", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.make_property_setter") as mock_setter:
			hide_legacy_exempt_from_sales_tax()

		self.assertEqual(mock_setter.call_count, 4)
		doctypes = {c.args[0] for c in mock_setter.call_args_list}
		self.assertEqual(doctypes, {"Quotation", "Sales Order", "Sales Invoice", "Customer"})
		for call in mock_setter.call_args_list:
			self.assertEqual(call.args[1], "exempt_from_sales_tax")
			self.assertEqual(call.args[2], "hidden")
			self.assertEqual(call.args[3], "1")

	def test_skips_doctype_without_the_column(self):
		"""Non-US company / column never created by ERPNext's regional setup —
		no property setter attempted, avoids erroring on a nonexistent field."""
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
			hide_legacy_exempt_from_sales_tax,
		)
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.db.has_column", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.make_property_setter") as mock_setter:
			hide_legacy_exempt_from_sales_tax()

		mock_setter.assert_not_called()


class TestSetTaxesFieldDescription(UnitTestCase):

	def test_sets_description_on_all_three_transaction_doctypes(self):
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
			set_taxes_field_description,
			_TAXES_FIELD_DESCRIPTION,
		)
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.make_property_setter") as mock_setter:
			set_taxes_field_description()

		self.assertEqual(mock_setter.call_count, 3)
		doctypes = {c.args[0] for c in mock_setter.call_args_list}
		self.assertEqual(doctypes, {"Quotation", "Sales Order", "Sales Invoice"})
		for call in mock_setter.call_args_list:
			self.assertEqual(call.args[1], "taxes")
			self.assertEqual(call.args[2], "description")
			self.assertEqual(call.args[3], _TAXES_FIELD_DESCRIPTION)
			self.assertEqual(call.args[4], "Table")

	def test_does_not_touch_customer(self):
		"""The "taxes" field lives on transaction doctypes only - Customer has
		no such field, so it must never be targeted here."""
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
			set_taxes_field_description,
		)
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.make_property_setter") as mock_setter:
			set_taxes_field_description()

		doctypes = {c.args[0] for c in mock_setter.call_args_list}
		self.assertNotIn("Customer", doctypes)


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

	def _app_root(self):
		import os
		return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

	def _read_js(self):
		import os
		path = os.path.join(self._app_root(), "public", "js", "address.js")
		with open(path) as f:
			return f.read()

	def test_hooks_registers_address_js(self):
		"""hooks.py must declare Address in doctype_js."""
		from taxjar_integration import hooks
		self.assertIn("Address", hooks.doctype_js)
		self.assertEqual(hooks.doctype_js["Address"], "public/js/address.js")

	def test_address_js_file_exists(self):
		import os
		path = os.path.join(self._app_root(), "public", "js", "address.js")
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
		path = os.path.join(self._app_root(), "public", "js", "taxjar_utils.js")
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

	def _read_js(self):
		import os
		path = os.path.join(os.path.dirname(__file__), "taxjar_settings.js")
		with open(path) as f:
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
		self.assertIn("No nexus regions loaded", js)

	def test_settings_js_overflow_x_auto_for_responsiveness(self):
		"""Table wrapper must use overflow-x: auto for narrow-screen support."""
		js = self._read_js()
		self.assertIn("overflow-x: auto", js)


# ── Phase 1: sync_nexus_list scheduled task ──────────────────────────────────

class TestSyncNexusList(UnitTestCase):
	"""Tests for the daily scheduled task that refreshes nexus from TaxJar."""

	def _make_settings_doc(self, calculate_tax=1, create_transactions=0, has_company_config=True):
		doc = MagicMock()
		doc.taxjar_enabled = 1
		if has_company_config:
			row = MagicMock()
			row.taxjar_calculate_tax = calculate_tax
			row.taxjar_create_transactions = create_transactions
			doc.company_config = [row]
		else:
			doc.company_config = []
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


# ── Weekly Product Tax Category sync task ────────────────────────────────────

class TestSyncProductTaxCategories(UnitTestCase):
	"""Tests for the weekly scheduled task that refreshes Product Tax Category from
	TaxJar. Mirrors sync_nexus_list's guard/error-handling pattern; reuses the
	shared fetch_and_insert_categories() helper (also used by the manual "Update
	Product Tax Category List" button) rather than new insert logic."""

	def _category(self, product_tax_code, description, name):
		category = MagicMock()
		category.product_tax_code = product_tax_code
		category.description = description
		category.name = name
		return category

	def test_skips_when_taxjar_disabled(self):
		"""No client is even requested when the master switch/company gate is off."""
		from taxjar_integration.taxjar_integration.tasks import sync_product_tax_categories
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.tasks.get_client") as mock_get_client:
			sync_product_tax_categories()
		mock_get_client.assert_not_called()

	def test_skips_when_no_client(self):
		"""No usable credential (get_client returns None) -> no TaxJar call, no insert attempt."""
		from taxjar_integration.taxjar_integration.tasks import sync_product_tax_categories
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.tasks.get_client", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.tasks.fetch_and_insert_categories") as mock_fetch:
			sync_product_tax_categories()
		mock_fetch.assert_not_called()

	def test_maps_taxjar_categories_and_inserts_new_ones(self):
		"""New categories from the API get inserted; the TaxJarCategory attribute shape
		(.product_tax_code/.description/.name) is mapped to the dict shape
		create_tax_categories() expects (product_tax_code/description/name)."""
		frappe.db.delete("Product Tax Category", {"product_tax_code": "TEST_SYNC_NEW"})
		self.addCleanup(frappe.db.delete, "Product Tax Category", {"product_tax_code": "TEST_SYNC_NEW"})

		mock_client = MagicMock()
		mock_client.categories.return_value = [
			self._category("TEST_SYNC_NEW", "A brand new test category", "New Test Category"),
		]
		from taxjar_integration.taxjar_integration.tasks import sync_product_tax_categories
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.tasks.get_client", return_value=mock_client):
			sync_product_tax_categories()

		inserted = frappe.get_doc("Product Tax Category", "TEST_SYNC_NEW")
		self.assertEqual(inserted.description, "A brand new test category")
		self.assertEqual(inserted.category_name, "New Test Category")

	def test_existing_category_is_left_untouched(self):
		"""A category TaxJar still reports that already exists locally must not be
		updated or duplicated - Items are already Linked to it by product_tax_code."""
		existing = frappe.get_doc({
			"doctype": "Product Tax Category",
			"product_tax_code": "TEST_SYNC_EXISTING",
			"category_name": "Original Name",
			"description": "Original description",
		}).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, "Product Tax Category", {"product_tax_code": "TEST_SYNC_EXISTING"})

		mock_client = MagicMock()
		mock_client.categories.return_value = [
			self._category("TEST_SYNC_EXISTING", "A changed description", "Changed Name"),
		]
		from taxjar_integration.taxjar_integration.tasks import sync_product_tax_categories
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.tasks.get_client", return_value=mock_client):
			sync_product_tax_categories()

		unchanged = frappe.get_doc("Product Tax Category", existing.name)
		self.assertEqual(unchanged.category_name, "Original Name")
		self.assertEqual(unchanged.description, "Original description")

	def test_catches_exception_and_logs_error(self):
		"""Exceptions from the TaxJar call must be caught and logged, not re-raised -
		matching sync_nexus_list's error handling exactly."""
		mock_client = MagicMock()
		mock_client.categories.side_effect = Exception("TaxJar API timeout")

		from taxjar_integration.taxjar_integration.tasks import sync_product_tax_categories
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.tasks.get_client", return_value=mock_client), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_traceback", return_value="traceback"), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.log_error") as mock_log:
			sync_product_tax_categories()  # must not raise

		mock_log.assert_called_once_with("traceback", "TaxJar: Product tax category sync failed")

	def test_hooks_registers_sync_product_tax_categories_as_weekly_job(self):
		"""hooks.py must declare sync_product_tax_categories in scheduler_events['weekly']."""
		from taxjar_integration import hooks
		self.assertIn(
			"taxjar_integration.taxjar_integration.tasks.sync_product_tax_categories",
			hooks.scheduler_events.get("weekly", []),
		)


# ── update_nexus_list(): a 401 from any one company must not crash the whole
# fetch with a raw traceback (bug report: this is exactly what happened when
# an untested/bad-token company reached the Nexus step) ─────────────────────

class TestUpdateNexusListAuthError(UnitTestCase):
	def setUp(self):
		self.settings = frappe.get_single("TaxJar Settings")
		self.settings.set("company_config", [{
			"company": "_Test Company",
			"tax_account_head": "Sales Tax - _TC",
			"shipping_account_head": "Freight - _TC",
		}])

	def _taxjar_error(self, status_code, message="error"):
		import taxjar.exceptions
		error = taxjar.exceptions.TaxJarResponseError(message)
		error.full_response = {"status_code": status_code}
		return error

	def test_401_throws_clear_message_naming_the_company(self):
		"""Previously re-raised as a bare TaxJarResponseError - a 500 to the
		browser with a raw traceback, for every company in the request, the
		moment any one of them had a bad token."""
		mock_client = MagicMock()
		mock_client.nexus_regions.side_effect = self._taxjar_error(401, "401 Unauthorized")
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=mock_client,
		), patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.log_taxjar_call"
		):
			with self.assertRaises(frappe.exceptions.ValidationError) as cm:
				self.settings.update_nexus_list()

		message = str(cm.exception)
		self.assertIn("_Test Company", message)
		self.assertIn("401", message)
		# Both remedies the user actually has, named explicitly - matching
		# the bug report's own wording ("put correct API key or remove the
		# company").
		self.assertIn("API key", message)
		self.assertIn("remove", message)

	def test_non_auth_taxjar_error_still_propagates_unchanged(self):
		"""Only a 401 gets the special message - any other TaxJar error (500,
		rate limit, ...) is not silently swallowed or reworded."""
		import taxjar.exceptions
		mock_client = MagicMock()
		mock_client.nexus_regions.side_effect = self._taxjar_error(500, "500 Internal Server Error")
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=mock_client,
		), patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.log_taxjar_call"
		):
			with self.assertRaises(taxjar.exceptions.TaxJarResponseError):
				self.settings.update_nexus_list()

	def test_successful_fetch_is_unaffected(self):
		"""The happy path (no error at all) must still work exactly as before.
		self.save() is stubbed out - _Test Company/its accounts aren't real
		records in this site, and this test isn't about link validation."""
		mock_client = MagicMock()
		mock_client.nexus_regions.return_value = []
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=mock_client,
		), patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.log_taxjar_call"
		), patch.object(self.settings, "save"):
			self.settings.update_nexus_list()  # must not raise
		mock_client.nexus_regions.assert_called_once()


# ── Phase 2: auto-enqueue nexus sync on first configuration ──────────────────

class TestAutoNexusEnqueue(UnitTestCase):
	"""
	Tests for the on_update auto-enqueue: nexus is fetched in the background
	the first time settings are saved with features + company config + empty nexus.
	"""

	def _settings(self, calculate_tax=1, create_transactions=0, has_company_config=True, has_nexus=False):
		"""Return a live TaxJar Settings single doc wired up for the test scenario."""
		doc = frappe.get_single("TaxJar Settings")
		doc.taxjar_enabled = 1 if (calculate_tax or create_transactions) else 0
		if has_company_config:
			doc.set("company_config", [{
				"company": "_Test Company",
				"tax_account_head": "Tax - TC",
				"shipping_account_head": "Freight - TC",
				"taxjar_calculate_tax": calculate_tax,
				"taxjar_create_transactions": create_transactions,
			}])
		else:
			doc.set("company_config", [])
		if has_nexus:
			doc.set("nexus", [{"company": "_Test Company", "region": "California", "region_code": "CA", "country": "United States", "country_code": "US"}])
		else:
			doc.set("nexus", [])
		# No credentials → the background token check is not enqueued, so the only
		# enqueue under test is the nexus fetch.
		doc.set("table_hvjw", [])
		return doc

	def _call_on_update(self, doc):
		"""Call on_update with frappe.flags.in_test=True and enqueue mocked."""
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.enqueue") as mock_enqueue, \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.db.exists", return_value=True):
			doc.on_update()
		return mock_enqueue

	# Trigger conditions

	def test_enqueues_when_features_enabled_config_present_nexus_empty(self):
		"""The happy path: first save after setup should trigger a background nexus fetch.

		Also enqueues the (separate, always-on-with-company_config) ledger/template
		sync job — see TestLedgerTemplateSyncEnqueue — so this asserts on the
		nexus-specific call rather than call count.
		"""
		doc = self._settings(calculate_tax=1, has_company_config=True, has_nexus=False)
		mock_enqueue = self._call_on_update(doc)
		nexus_calls = [c for c in mock_enqueue.call_args_list if "sync_nexus_list" in str(c)]
		self.assertEqual(len(nexus_calls), 1)

	def test_enqueues_when_only_create_transactions_enabled(self):
		"""create_transactions alone (without calculate_tax) should also trigger auto-fetch."""
		doc = self._settings(calculate_tax=0, create_transactions=1, has_company_config=True, has_nexus=False)
		mock_enqueue = self._call_on_update(doc)
		nexus_calls = [c for c in mock_enqueue.call_args_list if "sync_nexus_list" in str(c)]
		self.assertEqual(len(nexus_calls), 1)

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


class TestLedgerTemplateSyncEnqueue(UnitTestCase):
	"""Tests for the on_update safety-net enqueue: ledger auto-fill + tax template
	sync should run whenever company_config is non-empty, regardless of whether
	the taxjar_enabled master switch or the per-company feature flags are on -
	ledgers/template should be ready before those are ever flipped on."""

	def _settings(self, has_company_config=True, taxjar_enabled=0):
		doc = frappe.get_single("TaxJar Settings")
		doc.taxjar_enabled = taxjar_enabled
		if has_company_config:
			doc.set("company_config", [{
				"company": "_Test Company",
				"tax_account_head": "Tax - TC",
				"shipping_account_head": "Freight - TC",
				"taxjar_calculate_tax": 0,
				"taxjar_create_transactions": 0,
			}])
		else:
			doc.set("company_config", [])
		doc.set("nexus", [{"company": "_Test Company", "region": "California", "region_code": "CA",
			"country": "United States", "country_code": "US"}])
		doc.set("table_hvjw", [])
		return doc

	def _call_on_update(self, doc):
		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.enqueue") as mock_enqueue, \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.db.exists", return_value=True):
			doc.on_update()
		return mock_enqueue

	def _sync_calls(self, mock_enqueue):
		return [c for c in mock_enqueue.call_args_list if "sync_all_company_tax_templates" in str(c)]

	def test_enqueues_when_company_config_present(self):
		doc = self._settings(has_company_config=True, taxjar_enabled=1)
		sync_calls = self._sync_calls(self._call_on_update(doc))
		self.assertEqual(len(sync_calls), 1)
		self.assertIn("regional.united_states.sync_all_company_tax_templates", sync_calls[0][0][0])

	def test_enqueues_even_when_master_switch_is_off(self):
		"""Unlike the nexus fetch, this is not gated on features_enabled — ledgers
		and the template should be ready before the switch is ever flipped on."""
		doc = self._settings(has_company_config=True, taxjar_enabled=0)
		sync_calls = self._sync_calls(self._call_on_update(doc))
		self.assertEqual(len(sync_calls), 1)

	def test_does_not_enqueue_when_company_config_empty(self):
		doc = self._settings(has_company_config=False, taxjar_enabled=1)
		sync_calls = self._sync_calls(self._call_on_update(doc))
		self.assertEqual(len(sync_calls), 0)

	def test_enqueue_uses_short_queue(self):
		doc = self._settings(has_company_config=True, taxjar_enabled=1)
		sync_calls = self._sync_calls(self._call_on_update(doc))
		self.assertEqual(sync_calls[0][1]["queue"], "short")


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

	def _call_get_tax_data(self, doc, taxjar_customer_id=None, customer_exemption_type=None):
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

		# frappe.db.get_value is called for Country -> code lookups, Customer ->
		# taxjar_customer_id, and Customer -> taxjar_exemption_type - a blanket
		# return_value would answer all three identically and mask the bugs
		# this class guards against, so each is faked by fieldname.
		def fake_get_value(doctype, name=None, fieldname=None, **kwargs):
			if doctype == "Customer":
				if fieldname == "taxjar_customer_id":
					return taxjar_customer_id
				if fieldname == "taxjar_exemption_type":
					return customer_exemption_type
				return None
			return "us"

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_config", return_value=mock_company_config), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_company_address_details", return_value=mock_address), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_shipping_address_details", return_value=mock_address), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value", side_effect=fake_get_value), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_line_item_dict", return_value={}):
			return get_tax_data(doc)

	def test_customer_id_uses_synced_taxjar_customer_id_not_raw_name(self):
		"""Regression guard: TaxJar stores/matches a customer's exemption record
		under the normalized id sync_customer_to_taxjar() actually assigned (e.g.
		"Alan-Houk"), not the raw ERPNext customer name ("Alan Houk") - sending
		the raw name here silently misses the match and the customer's
		exemption never applies, so tax gets calculated as if they had none."""
		doc = _make_doc()
		doc.customer = "Alan Houk"
		result = self._call_get_tax_data(doc, taxjar_customer_id="Alan-Houk")
		self.assertEqual(result["customer_id"], "Alan-Houk")

	def test_customer_id_falls_back_to_safe_id_when_never_synced(self):
		"""Customer hasn't been synced to TaxJar yet (taxjar_customer_id blank) -
		falls back to the same normalization sync_customer_to_taxjar() would use,
		so the id stays consistent whenever it does eventually sync."""
		doc = _make_doc()
		doc.customer = "Alan Houk"
		result = self._call_get_tax_data(doc, taxjar_customer_id=None)
		self.assertEqual(result["customer_id"], "Alan-Houk")

	def test_customer_id_absent_for_lead_quotation(self):
		doc = _make_doc()
		doc.doctype = "Quotation"
		doc.quotation_to = "Lead"
		doc.party_name = "LEAD-001"
		del doc.customer
		result = self._call_get_tax_data(doc)
		self.assertNotIn("customer_id", result)

	def test_exemption_type_absent_when_no_exemption_applies(self):
		doc = _make_doc()
		doc.customer = "Alan Houk"
		result = self._call_get_tax_data(doc)
		self.assertNotIn("exemption_type", result)

	def test_exemption_type_from_transaction_override(self):
		"""taxjar_transaction_exempt with no customer-level exemption on file -
		the payload carries the mapped (lowercase) exemption_type so TaxJar can
		zero the tax without needing a synced Customer record."""
		doc = _make_doc()
		doc.customer = "Alan Houk"
		doc.taxjar_transaction_exempt = 1
		doc.taxjar_transaction_exemption_type = "Wholesale"
		result = self._call_get_tax_data(doc)
		self.assertEqual(result["exemption_type"], "wholesale")

	def test_customer_exemption_type_takes_precedence_over_transaction_override(self):
		"""Matches TaxJar's own documented precedence: a matched customer's
		exemption_type wins over whatever order-level exemption_type is sent."""
		doc = _make_doc()
		doc.customer = "Alan Houk"
		doc.taxjar_transaction_exempt = 1
		doc.taxjar_transaction_exemption_type = "Wholesale"
		result = self._call_get_tax_data(doc, customer_exemption_type="Government")
		self.assertEqual(result["exemption_type"], "government")


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


# ── TaxJar Customer API — _get_customer_exemption_type ──────────────────────


class TestGetCustomerExemptionType(UnitTestCase):
	"""_get_customer_exemption_type() feeds the "Is the customer taxable?" status
	shown on the transaction (see set_sales_tax) - distinct from
	check_sales_tax_exemption()'s hard-stop exempt_from_sales_tax check, this
	fires for customers who only have taxjar_exemption_type set (the TaxJar-
	native path, where region-scoped exemption is still resolved by TaxJar
	itself via customer_id, not here)."""

	def _patch(self, exemption_type):
		return patch(
			"taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.get_value",
			return_value=exemption_type,
		)

	def test_returns_none_when_blank(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     self._patch(None):
			self.assertIsNone(_get_customer_exemption_type(doc))

	def test_returns_none_when_non_exempt(self):
		""""Non Exempt" is an explicit selectable option meaning "normal taxable
		customer" - not a truthy exemption."""
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     self._patch("Non Exempt"):
			self.assertIsNone(_get_customer_exemption_type(doc))

	def test_returns_exemption_type_when_set(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=True), \
		     self._patch("Wholesale"):
			self.assertEqual(_get_customer_exemption_type(doc), "Wholesale")

	def test_returns_none_without_a_customer(self):
		doc = _make_doc()
		doc.doctype = "Quotation"
		doc.quotation_to = "Lead"
		doc.party_name = "LEAD-001"
		del doc.customer
		self.assertIsNone(_get_customer_exemption_type(doc))

	def test_returns_none_when_column_missing(self):
		"""Non-US company/region where the field was never installed."""
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.has_column", return_value=False):
			self.assertIsNone(_get_customer_exemption_type(doc))


class TestGetEffectiveExemption(UnitTestCase):
	"""_get_effective_exemption() combines the customer master's exemption_type
	with the transaction-level override, customer taking precedence - matches
	TaxJar's own documented precedence rule for a matched customer_id."""

	def _doc_with_transaction_override(self, exemption_type="Wholesale"):
		doc = _make_doc()
		doc.taxjar_transaction_exempt = 1
		doc.taxjar_transaction_exemption_type = exemption_type
		return doc

	def test_customer_exemption_wins_when_both_set(self):
		doc = self._doc_with_transaction_override("Wholesale")
		with patch("taxjar_integration.taxjar_integration.taxjar_integration._get_customer_exemption_type", return_value="Government"):
			result = _get_effective_exemption(doc)
		self.assertEqual(result, ("Government", "customer"))

	def test_transaction_override_used_when_customer_not_exempt(self):
		doc = self._doc_with_transaction_override("Wholesale")
		with patch("taxjar_integration.taxjar_integration.taxjar_integration._get_customer_exemption_type", return_value=None):
			result = _get_effective_exemption(doc)
		self.assertEqual(result, ("Wholesale", "transaction"))

	def test_neither_set_returns_none_none(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration._get_customer_exemption_type", return_value=None):
			result = _get_effective_exemption(doc)
		self.assertEqual(result, (None, None))

	def test_checkbox_ticked_without_a_reason_selected_is_a_noop(self):
		"""Reason is mandatory_depends_on the checkbox on the form, but a
		document saved via API could still have the box ticked with no reason -
		don't treat that as an exemption with no explanation."""
		doc = _make_doc()
		doc.taxjar_transaction_exempt = 1
		doc.taxjar_transaction_exemption_type = None
		with patch("taxjar_integration.taxjar_integration.taxjar_integration._get_customer_exemption_type", return_value=None):
			result = _get_effective_exemption(doc)
		self.assertEqual(result, (None, None))

	def test_reason_set_without_checkbox_ticked_is_ignored(self):
		"""Stale/leftover exemption_type value with the checkbox off must not
		be treated as an active override."""
		doc = _make_doc()
		doc.taxjar_transaction_exempt = 0
		doc.taxjar_transaction_exemption_type = "Wholesale"
		with patch("taxjar_integration.taxjar_integration.taxjar_integration._get_customer_exemption_type", return_value=None):
			result = _get_effective_exemption(doc)
		self.assertEqual(result, (None, None))


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


# ── Realtime notification: _set_sync_status / _set_customer_sync_status ────
# Fixes stale sync status on an open form: the async paths (on_submit/
# on_cancel hooks, the 15-min cron retry, bulk retry from the Transactions/
# Customers pages) update the DB via a bare frappe.db.set_value with no
# notification, so an already-open form kept showing "Queued" until manually
# reloaded. Same fix india_compliance already uses elsewhere in this bench
# (GSTR-3B report generation, e-Waybill PDF generation): publish_realtime
# scoped to the document's own room via doctype/docname, after_commit=True
# so the event can't race ahead of the write becoming visible.

class TestSetSyncStatusRealtime(UnitTestCase):

	def test_publishes_realtime_event_scoped_to_document(self):
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.publish_realtime") as mock_publish:
			_set_sync_status("SINV-TEST-001", "Synced")

		mock_publish.assert_called_once_with(
			"taxjar_invoice_sync_update",
			{"taxjar_sync_status": "Synced"},
			doctype="Sales Invoice",
			docname="SINV-TEST-001",
			after_commit=True,
		)

	def test_publishes_after_the_db_write(self):
		"""Must not race ahead of the db.set_value it's meant to notify
		about - a client that reload_doc()s in response to the event needs
		the write to have actually happened first."""
		calls = []
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value",
		           side_effect=lambda *a, **k: calls.append("db_write")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.publish_realtime",
		           side_effect=lambda *a, **k: calls.append("publish")):
			_set_sync_status("SINV-TEST-001", "Failed", error="timeout")

		self.assertEqual(calls, ["db_write", "publish"])

	def test_message_reflects_status_for_each_state(self):
		for status in ("Synced", "Failed", "Queued", "Not Applicable"):
			with self.subTest(status=status):
				with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.publish_realtime") as mock_publish:
					_set_sync_status("SINV-TEST-001", status)
				self.assertEqual(mock_publish.call_args[0][1], {"taxjar_sync_status": status})


class TestSetCustomerSyncStatusRealtime(UnitTestCase):

	def test_publishes_realtime_event_scoped_to_document(self):
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.publish_realtime") as mock_publish:
			_set_customer_sync_status("CUST-TEST-001", "Synced")

		mock_publish.assert_called_once_with(
			"taxjar_customer_sync_update",
			{"taxjar_customer_sync_status": "Synced"},
			doctype="Customer",
			docname="CUST-TEST-001",
			after_commit=True,
		)

	def test_publishes_after_the_db_write(self):
		calls = []
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.db.set_value",
		           side_effect=lambda *a, **k: calls.append("db_write")), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.publish_realtime",
		           side_effect=lambda *a, **k: calls.append("publish")):
			_set_customer_sync_status("CUST-TEST-001", "Failed", error="timeout")

		self.assertEqual(calls, ["db_write", "publish"])


class TestSyncStatusRealtimeJS(UnitTestCase):

	def _js_dir(self):
		import os
		return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "public", "js"))

	def _read_js(self, filename):
		import os
		with open(os.path.join(self._js_dir(), filename)) as f:
			return f.read()

	def test_sales_invoice_registers_listener_in_setup_not_refresh(self):
		"""setup(frm) runs once per form load; refresh(frm) reruns
		repeatedly (every save, tab switch back) and would stack duplicate
		frappe.realtime.on() listeners if used instead."""
		js = self._read_js("sales_invoice.js")
		setup_fn = js.split("setup(frm) {")[1].split("\n\t},")[0]
		self.assertIn('frappe.realtime.on("taxjar_invoice_sync_update"', setup_fn)
		self.assertIn("frm.reload_doc()", setup_fn)

	def test_customer_registers_listener_in_setup_not_refresh(self):
		js = self._read_js("customer.js")
		setup_fn = js.split("setup(frm) {")[1].split("\n\t},")[0]
		self.assertIn('frappe.realtime.on("taxjar_customer_sync_update"', setup_fn)
		self.assertIn("frm.reload_doc()", setup_fn)

	def test_setup_appears_before_refresh_in_sales_invoice(self):
		js = self._read_js("sales_invoice.js")
		self.assertLess(js.index("setup(frm) {"), js.index("refresh(frm) {"))

	def test_setup_appears_before_refresh_in_customer(self):
		js = self._read_js("customer.js")
		self.assertLess(js.index("setup(frm) {"), js.index("refresh(frm) {"))


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
	"""Token validation runs in the background (validate_taxjar_tokens) and reports
	problems via a realtime alert to the saving user instead of blocking the save."""

	MOD = "taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings"

	def _settings_with_cred(self, **cred):
		settings = MagicMock()
		settings.table_hvjw = [MagicMock(company="Test Co", **cred)]
		return settings

	def test_valid_token_no_alert(self):
		"""categories() succeeds → no alert published."""
		mock_client = MagicMock()
		mock_client.categories.return_value = []
		settings = self._settings_with_cred(sandbox_token="sk_test")

		with patch(f"{self.MOD}.get_client", return_value=mock_client), \
		     patch(f"{self.MOD}.frappe.get_single", return_value=settings), \
		     patch(f"{self.MOD}.frappe.publish_realtime") as mock_pub:
			validate_taxjar_tokens(user="admin@example.com")

		mock_client.categories.assert_called_once()
		mock_pub.assert_not_called()

	def test_invalid_token_alerts_red(self):
		"""401 response → red realtime alert to the saving user, no exception."""
		import taxjar.exceptions
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 401}

		mock_client = MagicMock()
		mock_client.categories.side_effect = err
		settings = self._settings_with_cred()

		with patch(f"{self.MOD}.get_client", return_value=mock_client), \
		     patch(f"{self.MOD}.frappe.get_single", return_value=settings), \
		     patch(f"{self.MOD}.frappe.publish_realtime") as mock_pub:
			validate_taxjar_tokens(user="admin@example.com")

		mock_pub.assert_called_once()
		payload = mock_pub.call_args[0][1]
		self.assertEqual(payload["indicator"], "red")
		self.assertEqual(mock_pub.call_args[1]["user"], "admin@example.com")

	def test_connection_error_alerts_orange(self):
		"""Connection error → orange alert, not an exception."""
		import taxjar.exceptions
		mock_client = MagicMock()
		mock_client.categories.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")
		settings = self._settings_with_cred()

		with patch(f"{self.MOD}.get_client", return_value=mock_client), \
		     patch(f"{self.MOD}.frappe.get_single", return_value=settings), \
		     patch(f"{self.MOD}.frappe.publish_realtime") as mock_pub:
			validate_taxjar_tokens(user="admin@example.com")

		mock_pub.assert_called_once()
		self.assertEqual(mock_pub.call_args[0][1]["indicator"], "orange")

	def test_no_alert_when_user_missing(self):
		"""Without a user to notify (e.g. a system-triggered save) nothing is published."""
		import taxjar.exceptions
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 401}
		mock_client = MagicMock()
		mock_client.categories.side_effect = err
		settings = self._settings_with_cred()

		with patch(f"{self.MOD}.get_client", return_value=mock_client), \
		     patch(f"{self.MOD}.frappe.get_single", return_value=settings), \
		     patch(f"{self.MOD}.frappe.publish_realtime") as mock_pub:
			validate_taxjar_tokens(user=None)

		mock_pub.assert_not_called()


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

	def test_sync_status_hidden_while_draft(self):
		"""Replaced by taxjar_sync_draft_message_html while a draft - showing
		the "Not Applicable" default there read as "TaxJar doesn't apply"
		rather than "not submitted yet"."""
		fields = self._get_si_field_defs()
		self.assertEqual(fields["taxjar_sync_status"]["depends_on"], "eval: doc.docstatus === 1")
		self.assertEqual(fields["taxjar_last_synced"]["depends_on"], "eval: doc.docstatus === 1")

	def test_sync_draft_message_field(self):
		fields = self._get_si_field_defs()
		f = fields["taxjar_sync_draft_message_html"]
		self.assertEqual(f["fieldtype"], "HTML")
		self.assertEqual(f["depends_on"], "eval: doc.docstatus === 0")
		self.assertIn("TaxJar: Submit to sync", f["options"])
		self.assertEqual(f["insert_after"], "taxjar_sync_section")
		self.assertEqual(fields["taxjar_sync_status"]["insert_after"], "taxjar_sync_draft_message_html")

	def test_sync_error_field(self):
		fields = self._get_si_field_defs()
		f = fields["taxjar_sync_error"]
		self.assertEqual(f["fieldtype"], "Small Text")
		self.assertTrue(f.get("read_only"))
		self.assertTrue(f.get("allow_on_submit"))

	def test_sync_error_depends_on_submitted_and_failed(self):
		fields = self._get_si_field_defs()
		depends_on = fields["taxjar_sync_error"]["depends_on"]
		self.assertIn("doc.docstatus === 1", depends_on)
		self.assertIn("doc.taxjar_sync_status == 'Failed'", depends_on)

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
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.company_creates_transactions", return_value=False):
			validate_return_against(doc, None)

	def test_throws_when_return_without_return_against(self):
		doc = _make_doc()
		doc.is_return = True
		doc.return_against = None
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.company_creates_transactions", return_value=True):
			self.assertRaises(frappe.ValidationError, validate_return_against, doc, None)

	def test_passes_when_return_with_return_against(self):
		doc = _make_doc()
		doc.is_return = True
		doc.return_against = "SINV-ORIG-001"
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.company_creates_transactions", return_value=True):
			validate_return_against(doc, None)


# ── Phase 3: enqueue_taxjar_sync / enqueue_taxjar_delete ─────────────────────


class TestEnqueueTaxjarSync(UnitTestCase):

	def test_skips_when_create_transactions_disabled(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.company_creates_transactions", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			enqueue_taxjar_sync(doc, None)
		mock_enqueue.assert_not_called()

	def test_skips_when_no_client(self):
		doc = _make_doc()
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.company_creates_transactions", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.get_client", return_value=None), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			enqueue_taxjar_sync(doc, None)
		mock_enqueue.assert_not_called()

	def test_sets_queued_and_enqueues(self):
		doc = _make_doc()
		doc.db_set = MagicMock()
		mock_client = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.company_creates_transactions", return_value=True), \
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
		with patch("taxjar_integration.taxjar_integration.taxjar_integration.company_creates_transactions", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.taxjar_integration.frappe.enqueue") as mock_enqueue:
			enqueue_taxjar_delete(doc, None)
		mock_enqueue.assert_not_called()

	def test_sets_queued_and_enqueues(self):
		doc = _make_doc()
		doc.db_set = MagicMock()
		mock_client = MagicMock()

		with patch("taxjar_integration.taxjar_integration.taxjar_integration.company_creates_transactions", return_value=True), \
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

	def _app_root(self):
		import os
		return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

	def _read_js(self):
		import os
		path = os.path.join(self._app_root(), "public", "js", "sales_invoice.js")
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

	def test_skips_when_taxjar_disabled(self):
		from taxjar_integration.taxjar_integration.tasks import retry_failed_taxjar_syncs
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_syncs()
		mock_enqueue.assert_not_called()

	def test_enqueues_only_failed_invoices_for_filing_companies(self):
		from taxjar_integration.taxjar_integration.tasks import retry_failed_taxjar_syncs
		invoices = [
			frappe._dict(name="SINV-001", company="Co A"),
			frappe._dict(name="SINV-002", company="Co B"),
		]
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.tasks.company_creates_transactions", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_all", return_value=invoices), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_syncs()
		self.assertEqual(mock_enqueue.call_count, 2)

	def test_skips_invoices_for_companies_with_filing_off(self):
		from taxjar_integration.taxjar_integration.tasks import retry_failed_taxjar_syncs
		invoices = [
			frappe._dict(name="SINV-001", company="Co A"),
			frappe._dict(name="SINV-002", company="Co B"),
		]
		# Only "Co A" still has transaction filing enabled.
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.tasks.company_creates_transactions",
		           side_effect=lambda company: company == "Co A"), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_all", return_value=invoices), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_syncs()
		self.assertEqual(mock_enqueue.call_count, 1)
		self.assertEqual(mock_enqueue.call_args[1]["invoice_name"], "SINV-001")

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

	def test_get_transactions_derives_doc_status_label(self):
		from taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions import get_transactions
		mock_rows = [
			frappe._dict(
				name="SINV-001", posting_date="2026-06-01", customer_name="A",
				grand_total=100, is_return=False, is_debit_note=False, docstatus=0,
				taxjar_sync_status="Not Applicable", taxjar_last_synced=None, taxjar_sync_error="",
			),
			frappe._dict(
				name="SINV-002", posting_date="2026-06-01", customer_name="B",
				grand_total=50, is_return=False, is_debit_note=False, docstatus=1,
				taxjar_sync_status="Synced", taxjar_last_synced=None, taxjar_sync_error="",
			),
			frappe._dict(
				name="SINV-003", posting_date="2026-06-01", customer_name="C",
				grand_total=75, is_return=False, is_debit_note=False, docstatus=2,
				taxjar_sync_status="Not Applicable", taxjar_last_synced=None, taxjar_sync_error="",
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

		labels = [r["doc_status"] for r in result["invoices"]]
		self.assertEqual(labels, ["Draft", "Submitted", "Cancelled"])

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

	def _transactions_js(self):
		import os
		js_path = os.path.join(
			os.path.dirname(__file__),
			"..", "..", "page", "taxjar_transactions", "taxjar_transactions.js",
		)
		with open(os.path.normpath(js_path)) as f:
			return f.read()

	def test_column_order_and_no_last_synced_or_error_columns(self):
		"""Posting Date, Customer, Sales Invoice, Type, Grand Total, Doc Status,
		Sync Status - in that order. Last Synced and Error are gone as their
		own columns; that information now surfaces via the Sync Status cell's
		hover icons instead."""
		js = self._transactions_js()
		header_block = js.split("<thead>")[1].split("</thead>")[0]
		for col in ("Posting Date", "Customer", "Sales Invoice", "Type",
		            "Grand Total", "Doc Status", "Sync Status"):
			self.assertIn(col, header_block)
		self.assertNotIn("Last Synced", header_block)
		self.assertNotIn("__(\"Error\")", header_block)

		posting_idx = header_block.index("Posting Date")
		customer_idx = header_block.index("Customer")
		invoice_idx = header_block.index("Sales Invoice")
		type_idx = header_block.index("Type")
		total_idx = header_block.index("Grand Total")
		doc_status_idx = header_block.index("Doc Status")
		sync_status_idx = header_block.index("Sync Status")
		self.assertTrue(
			posting_idx < customer_idx < invoice_idx < type_idx
			< total_idx < doc_status_idx < sync_status_idx
		)

	def test_row_reads_doc_status_field(self):
		js = self._transactions_js()
		render_fn = js.split("render_table() {")[1].split("\n\t}\n")[0]
		self.assertIn("inv.doc_status", render_fn)
		self.assertNotIn("taxjar_last_synced", render_fn.split("render_sync_status_cell")[0])

	def _sync_status_cell_fn(self):
		js = self._transactions_js()
		return js.split("render_sync_status_cell(inv) {")[1].split("\n\t}\n")[0]

	def test_sync_status_cell_uses_one_shape_for_every_status(self):
		"""Failed reads the same as Synced/Queued/Not Applicable - a pill plus
		(when there's something to say) a separate info icon, never a special-
		cased warning icon + Retry button. Retrying goes through the checkbox +
		bulk "Retry Selected" action instead."""
		cell_fn = self._sync_status_cell_fn()
		self.assertNotIn("triangle-alert", cell_fn)
		self.assertNotIn("taxjar-retry-chip", cell_fn)
		self.assertNotIn("taxjar-retry-one", cell_fn)
		self.assertIn("inv.taxjar_sync_error", cell_fn)
		self.assertIn('} else if (status === "Failed") {', cell_fn)

	def test_sync_status_info_icon_shown_for_synced_queued_and_failed(self):
		cell_fn = self._sync_status_cell_fn()
		self.assertIn('frappe.utils.icon("info", "sm")', cell_fn)
		self.assertIn('__("Last synced: {0}"', cell_fn)
		self.assertIn('__("Queued for sync")', cell_fn)
		# Info icon must be a separate element, not nested inside the pill span.
		self.assertIn("const pill = `<span class=\"indicator-pill ${color}\">${label}</span>`;", cell_fn)
		self.assertIn('data-info="${frappe.utils.escape_html(info_text)}"', cell_fn)
		self.assertIn("return `${pill}${icon}`;", cell_fn)

	def test_no_native_title_tooltip_on_sync_icon(self):
		"""A native title attribute has a browser-enforced show delay and never
		responds to a click - the popover is hand-rolled instead (see
		_show_sync_popover), same reasoning as the guided setup's own
		.ts-info-btn/.ts-info-pop pattern."""
		cell_fn = self._sync_status_cell_fn()
		self.assertNotIn("title=", cell_fn)

	def test_sync_popover_shown_on_both_hover_and_click(self):
		js = self._transactions_js()
		make_table_fn = js.split("make_table() {")[1].split("\n\t}\n")[0]
		self.assertIn('this.tbody.on("mouseenter", ".taxjar-sync-icon"', make_table_fn)
		self.assertIn('this.tbody.on("mouseleave", ".taxjar-sync-icon"', make_table_fn)
		self.assertIn('this.tbody.on("click", ".taxjar-sync-icon"', make_table_fn)
		self.assertIn("_show_sync_popover", make_table_fn)

	def test_sync_popover_shows_and_hides_without_delay(self):
		js = self._transactions_js()
		show_fn = js.split("_show_sync_popover($trigger) {")[1].split("\n\t}\n")[0]
		self.assertIn("taxjar-sync-pop", show_fn)
		self.assertNotIn("setTimeout", show_fn)
		hide_fn = js.split("_hide_sync_popover() {")[1].split("\n\t}\n")[0]
		self.assertNotIn("setTimeout", hide_fn)

	def test_sync_icon_css_uses_pointer_cursor_not_help(self):
		"""cursor: help renders the browser's own question-mark cursor glyph
		next to the pointer - easy to mistake for a stray "?" over the icon."""
		import os
		css_path = os.path.normpath(os.path.join(
			os.path.dirname(__file__),
			"..", "..", "page", "taxjar_transactions", "taxjar_transactions.css",
		))
		with open(css_path) as f:
			css = f.read()
		self.assertIn("cursor: pointer;", css)
		self.assertNotIn("cursor: help;", css)


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
		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=False), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_customer_syncs()
		mock_enqueue.assert_not_called()

	def test_enqueues_failed_customers(self):
		from taxjar_integration.taxjar_integration.tasks import retry_failed_taxjar_customer_syncs

		config = MagicMock(company="Test Co")
		settings = MagicMock()
		settings.company_config = [config]

		with patch("taxjar_integration.taxjar_integration.tasks._is_taxjar_enabled", return_value=True), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_all", return_value=["CUST-001", "CUST-002"]), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.get_single", return_value=settings), \
		     patch("taxjar_integration.taxjar_integration.tasks.frappe.enqueue") as mock_enqueue:
			retry_failed_taxjar_customer_syncs()

		self.assertEqual(mock_enqueue.call_count, 2)


# ── Customer JS — button removed ─────────────────────────────────────────────


class TestCustomerClientScriptUpdated(UnitTestCase):

	def _app_root(self):
		import os
		return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

	def _read_js(self):
		import os
		path = os.path.join(self._app_root(), "public", "js", "customer.js")
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
		page_json = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..",
			"page", "taxjar_customers", "taxjar_customers.json",
		))
		self.assertTrue(os.path.isfile(page_json))

	def test_page_js_exists(self):
		import os
		page_js = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..",
			"page", "taxjar_customers", "taxjar_customers.js",
		))
		self.assertTrue(os.path.isfile(page_js))

	def test_page_js_has_regions_dialog(self):
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..",
			"page", "taxjar_customers", "taxjar_customers.js",
		))
		with open(path) as f:
			js = f.read()
		self.assertIn("show_regions_dialog", js)
		self.assertIn("US_STATES", js)
		self.assertIn("CA_PROVINCES", js)
		self.assertIn("select-all-country", js)

	def test_workspace_has_page_link(self):
		import json, os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..",
			"workspace", "taxjar_integration", "taxjar_integration.json",
		))
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


# ── Problem 1 / 2: install-time setup ────────────────────────────────────────


class TestInstallSetup(UnitTestCase):

	def test_after_install_hook_registered(self):
		from taxjar_integration import hooks
		self.assertEqual(hooks.after_install, "taxjar_integration.install.after_install")

	def test_after_migrate_hook_registered(self):
		from taxjar_integration import hooks
		self.assertIn("taxjar_integration.install.after_migrate", hooks.after_migrate)

	def _run_setup(self, *, categories_exist):
		from taxjar_integration import install

		# frappe.db.exists is patched on the real frappe.db object (not a local
		# copy), so a blanket return_value would also answer every other
		# frappe.db.exists() call sync_taxjar_workspace_sidebar() makes further
		# down setup_taxjar() (e.g. checking the Workspace Sidebar leftover) -
		# defeating delete_doc's ignore_missing safety net with a lie and turning
		# it into a real DoesNotExistError. Only fake the one check this is
		# actually testing; let everything else hit the real frappe.db.exists.
		real_exists = frappe.db.exists

		def fake_exists(dt, *args, **kwargs):
			if dt == "Product Tax Category" and not args and not kwargs:
				return categories_exist
			return real_exists(dt, *args, **kwargs)

		with patch("taxjar_integration.install.make_custom_fields") as mock_make, \
		     patch("taxjar_integration.install.add_product_tax_categories") as mock_cats, \
		     patch("taxjar_integration.install.add_permissions") as mock_perms, \
		     patch("taxjar_integration.install.sync_all_company_tax_templates") as mock_sync, \
		     patch("taxjar_integration.install.hide_legacy_exempt_from_sales_tax") as mock_hide, \
		     patch("taxjar_integration.install.set_taxes_field_description") as mock_desc, \
		     patch("taxjar_integration.install.add_guided_setup_alert") as mock_alert, \
		     patch("taxjar_integration.install.frappe.db.exists", side_effect=fake_exists):
			install.setup_taxjar()
		return mock_make, mock_cats, mock_perms, mock_sync, mock_hide, mock_desc, mock_alert

	def test_after_install_runs_full_setup(self):
		from taxjar_integration import install
		with patch("taxjar_integration.install.setup_taxjar") as mock_setup:
			install.after_install()
		mock_setup.assert_called_once()

		mock_make, mock_cats, mock_perms, mock_sync, mock_hide, mock_desc, mock_alert = self._run_setup(categories_exist=False)
		mock_make.assert_called_once()
		mock_cats.assert_called_once()
		mock_perms.assert_called_once()
		mock_sync.assert_called_once()
		mock_hide.assert_called_once()
		mock_desc.assert_called_once()
		mock_alert.assert_called_once()

	def test_setup_skips_category_seed_when_already_present(self):
		mock_make, mock_cats, mock_perms, mock_sync, mock_hide, mock_desc, mock_alert = self._run_setup(categories_exist=True)
		mock_cats.assert_not_called()
		mock_make.assert_called_once()
		mock_perms.assert_called_once()
		mock_sync.assert_called_once()
		mock_hide.assert_called_once()
		mock_desc.assert_called_once()
		mock_alert.assert_called_once()


# ── Workspace guided-setup alert banner ───────────────────────────────────────


class TestGuidedSetupAlert(UnitTestCase):
	"""Tests for install.add_guided_setup_alert(): the blue/outline banner at the
	top of the workspace nudging first-time users toward the guided setup wizard."""

	def setUp(self):
		from taxjar_integration.install import GUIDED_SETUP_ALERT_BLOCK
		self.block_name = GUIDED_SETUP_ALERT_BLOCK
		frappe.db.delete("Custom HTML Block", {"name": self.block_name})
		self._reset_workspace_content()
		self.addCleanup(frappe.db.delete, "Custom HTML Block", {"name": self.block_name})
		self.addCleanup(self._reset_workspace_content)

	def _reset_workspace_content(self):
		"""Strip any guided-setup-alert content block and custom_blocks child row
		this test added, leaving the rest of the workspace (Setup/Manage/Sync cards)
		exactly as it was."""
		if not frappe.db.exists("Workspace", "TaxJar Integration"):
			return
		ws = frappe.get_doc("Workspace", "TaxJar Integration")
		content = frappe.parse_json(ws.content or "[]")
		filtered_content = [
			block for block in content
			if block.get("id") not in ("taxjar_guided_setup_alert", "taxjar_guided_setup_alert_spacer")
		]
		filtered_rows = [
			row for row in (ws.custom_blocks or []) if row.custom_block_name != self.block_name
		]
		if filtered_content != content or len(filtered_rows) != len(ws.custom_blocks or []):
			ws.content = frappe.as_json(filtered_content)
			ws.set("custom_blocks", filtered_rows)
			ws.save(ignore_permissions=True)

	def test_creates_custom_html_block_with_expected_content(self):
		from taxjar_integration.install import add_guided_setup_alert
		add_guided_setup_alert()

		block = frappe.get_doc("Custom HTML Block", self.block_name)
		self.assertIn("taxjar-setup", block.html)
		self.assertIn("blue", block.html)
		self.assertIn("setup_complete", block.script)

	def test_registers_custom_blocks_child_row(self):
		"""The `custom_blocks` child table on Workspace is what the block editor
		actually looks up (by label) to resolve a "custom_block" content entry into
		a live widget - without a matching row here the content block has nothing
		to render, regardless of it being present in `content`."""
		from taxjar_integration.install import add_guided_setup_alert
		add_guided_setup_alert()

		ws = frappe.get_doc("Workspace", "TaxJar Integration")
		matches = [row for row in ws.custom_blocks if row.custom_block_name == self.block_name]
		self.assertEqual(len(matches), 1)
		self.assertEqual(matches[0].label, self.block_name)

	def test_inserts_content_block_at_full_width(self):
		"""Setup/Manage/Sync now sit in one row (4 + 4 + 4), so the banner spans
		the full row width above them rather than being paired with a spacer."""
		from taxjar_integration.install import add_guided_setup_alert
		add_guided_setup_alert()

		ws = frappe.get_doc("Workspace", "TaxJar Integration")
		content = frappe.parse_json(ws.content or "[]")
		alert_blocks = [b for b in content if b.get("data", {}).get("custom_block_name") == self.block_name]

		self.assertEqual(len(alert_blocks), 1)
		self.assertEqual(alert_blocks[0]["data"]["col"], 12)
		self.assertNotIn(
			"taxjar_guided_setup_alert_spacer", [b.get("id") for b in content]
		)

	def test_heals_stale_width_and_spacer_from_a_previous_version(self):
		"""A layout change here (col: 4 + spacer -> col: 12) must also reach sites
		that already ran an earlier version of this function, not just fresh
		installs - same self-healing principle as the html/script sync above."""
		from taxjar_integration.install import add_guided_setup_alert

		ws = frappe.get_doc("Workspace", "TaxJar Integration")
		content = frappe.parse_json(ws.content or "[]")
		stale_content = [
			{
				"id": "taxjar_guided_setup_alert",
				"type": "custom_block",
				"data": {"custom_block_name": self.block_name, "col": 4},
			},
			{"id": "taxjar_guided_setup_alert_spacer", "type": "spacer", "data": {"col": 8}},
			*content,
		]
		ws.content = frappe.as_json(stale_content)
		ws.save(ignore_permissions=True)

		add_guided_setup_alert()

		ws = frappe.get_doc("Workspace", "TaxJar Integration")
		content = frappe.parse_json(ws.content or "[]")
		alert_blocks = [b for b in content if b.get("data", {}).get("custom_block_name") == self.block_name]

		self.assertEqual(len(alert_blocks), 1)
		self.assertEqual(alert_blocks[0]["data"]["col"], 12)
		self.assertNotIn(
			"taxjar_guided_setup_alert_spacer", [b.get("id") for b in content]
		)

	def test_idempotent_on_repeated_calls(self):
		"""Running setup/migrate repeatedly must not duplicate the block, the
		custom_blocks child row, or the Custom HTML Block record."""
		from taxjar_integration.install import add_guided_setup_alert
		add_guided_setup_alert()
		add_guided_setup_alert()
		add_guided_setup_alert()

		self.assertEqual(frappe.db.count("Custom HTML Block", {"name": self.block_name}), 1)

		ws = frappe.get_doc("Workspace", "TaxJar Integration")
		content = frappe.parse_json(ws.content or "[]")
		alert_blocks = [b for b in content if b.get("data", {}).get("custom_block_name") == self.block_name]
		self.assertEqual(len(alert_blocks), 1)

		matches = [row for row in ws.custom_blocks if row.custom_block_name == self.block_name]
		self.assertEqual(len(matches), 1)

	def test_syncs_html_on_already_migrated_sites(self):
		"""A style/copy edit to the constants in install.py must reach sites that
		already ran setup once before, not just fresh installs - the app is the
		source of truth, same convention as create_custom_fields(update=True)."""
		from taxjar_integration import install

		install.add_guided_setup_alert()
		stale = frappe.get_doc("Custom HTML Block", self.block_name)
		stale.html = "<div>stale content from a previous version</div>"
		stale.script = ""
		stale.save(ignore_permissions=True)

		install.add_guided_setup_alert()

		refreshed = frappe.get_doc("Custom HTML Block", self.block_name)
		self.assertEqual(refreshed.html, install.GUIDED_SETUP_ALERT_HTML)
		self.assertEqual(refreshed.script, install.GUIDED_SETUP_ALERT_SCRIPT)


# ── Nexus & Product Category tab: count/last-updated summary ─────────────────


class TestProductTaxCategorySummary(UnitTestCase):
	"""Tests for TaxJarSettings.get_product_tax_category_summary(), rendered on the
	renamed 'Nexus & Product Category' tab."""

	def setUp(self):
		self.settings = frappe.get_single("TaxJar Settings")

	def test_count_matches_table_and_last_updated_matches_max_modified(self):
		row = frappe.get_doc({
			"doctype": "Product Tax Category",
			"product_tax_code": "TEST_SUMMARY_ROW",
			"category_name": "Summary Test Category",
			"description": "For summary test",
		}).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, "Product Tax Category", {"product_tax_code": "TEST_SUMMARY_ROW"})

		summary = self.settings.get_product_tax_category_summary()

		self.assertEqual(summary["count"], frappe.db.count("Product Tax Category"))
		self.assertEqual(
			summary["last_updated"],
			frappe.db.get_value(
				"Product Tax Category", filters={}, fieldname="modified", order_by="modified desc"
			),
		)

	def test_zero_rows_returns_none_last_updated_without_raising(self):
		real_count = frappe.db.count
		real_get_value = frappe.db.get_value

		def fake_count(doctype, *args, **kwargs):
			if doctype == "Product Tax Category":
				return 0
			return real_count(doctype, *args, **kwargs)

		def fake_get_value(doctype, *args, **kwargs):
			if doctype == "Product Tax Category":
				return None
			return real_get_value(doctype, *args, **kwargs)

		with patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.db.count", side_effect=fake_count), \
		     patch("taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.frappe.db.get_value", side_effect=fake_get_value):
			summary = self.settings.get_product_tax_category_summary()  # must not raise

		self.assertEqual(summary["count"], 0)
		self.assertIsNone(summary["last_updated"])

	def test_js_formats_last_updated_via_user_timezone_not_comment_when(self):
		"""comment_when()/prettyDate() blanks the display whenever it computes a
		negative day-diff (pretty_date.js: `if (day_diff < 0) return ""`) - which
		happens for any real timestamp once System Settings' timezone drifts far
		enough from the browser's own. str_to_user() converts system tz -> user tz
		via moment-timezone and just formats it, with no comparison against the
		browser's local clock at all, so it can't hit that guard."""
		import os
		path = os.path.join(os.path.dirname(__file__), "taxjar_settings.js")
		with open(path) as f:
			js = f.read()

		self.assertIn("frappe.datetime.str_to_user(summary.last_updated)", js)
		self.assertNotIn("frappe.datetime.comment_when(summary.last_updated)", js)


class TestRefreshProductTaxCategories(UnitTestCase):
	"""Tests for TaxJarSettings.refresh_product_tax_categories(), the manual
	"Update Product Tax Category List" button - unlike the weekly scheduled job this
	is user-triggered, so a missing credential must raise, not silently no-op."""

	def setUp(self):
		self.settings = frappe.get_single("TaxJar Settings")

	def _category(self, product_tax_code, description, name):
		category = MagicMock()
		category.product_tax_code = product_tax_code
		category.description = description
		category.name = name
		return category

	def test_throws_clear_error_when_no_client(self):
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=None,
		):
			with self.assertRaises(frappe.exceptions.ValidationError) as cm:
				self.settings.refresh_product_tax_categories()
		self.assertIn("API credentials", str(cm.exception))

	def test_inserts_new_categories_and_returns_summary(self):
		frappe.db.delete("Product Tax Category", {"product_tax_code": "TEST_REFRESH_NEW"})
		self.addCleanup(frappe.db.delete, "Product Tax Category", {"product_tax_code": "TEST_REFRESH_NEW"})

		mock_client = MagicMock()
		mock_client.categories.return_value = [
			self._category("TEST_REFRESH_NEW", "A refresh-button test category", "Refresh Test Category"),
		]
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=mock_client,
		):
			summary = self.settings.refresh_product_tax_categories()

		inserted = frappe.get_doc("Product Tax Category", "TEST_REFRESH_NEW")
		self.assertEqual(inserted.category_name, "Refresh Test Category")
		self.assertEqual(summary["count"], frappe.db.count("Product Tax Category"))

	def test_does_not_depend_on_taxjar_enabled(self):
		"""Categories aren't company-scoped, so a valid client is enough - this must
		not gate on _is_taxjar_enabled() the way the weekly scheduled job does."""
		mock_client = MagicMock()
		mock_client.categories.return_value = []
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=mock_client,
		), patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings._is_taxjar_enabled",
			return_value=False,
		):
			self.settings.refresh_product_tax_categories()  # must not raise or no-op
		mock_client.categories.assert_called_once()

	def test_connection_error_raises_clear_message(self):
		"""Regression guard: this call used to have no try/except at all, so a
		connection blip surfaced as an unhandled exception instead of a clean
		message like every other TaxJar call site in this app."""
		import taxjar.exceptions
		mock_client = MagicMock()
		mock_client.categories.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=mock_client,
		):
			with self.assertRaises(frappe.exceptions.ValidationError) as cm:
				self.settings.refresh_product_tax_categories()
		self.assertIn("unreachable", str(cm.exception))

	def test_401_response_error_raises_invalid_token_message(self):
		import taxjar.exceptions
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 401}
		mock_client = MagicMock()
		mock_client.categories.side_effect = err
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=mock_client,
		):
			with self.assertRaises(frappe.exceptions.ValidationError) as cm:
				self.settings.refresh_product_tax_categories()
		self.assertIn("Invalid TaxJar API token", str(cm.exception))

	def test_other_response_error_raises_sanitized_message(self):
		import taxjar.exceptions
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 400, "detail": "to_state is invalid"}
		mock_client = MagicMock()
		mock_client.categories.side_effect = err
		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.get_client",
			return_value=mock_client,
		):
			with self.assertRaises(frappe.exceptions.ValidationError) as cm:
				self.settings.refresh_product_tax_categories()
		self.assertIn("State", str(cm.exception))  # sanitize_error_response renames "to state" -> "State"


class TestFetchAndInsertCategoriesLogging(UnitTestCase):
	"""fetch_and_insert_categories() is shared by the manual button and the
	weekly cron - it logs via log_taxjar_call() same as every other TaxJar call
	site, then re-raises so each caller keeps its own presentation behaviour."""

	MOD = "taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings"

	def test_logs_request_and_success(self):
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
			fetch_and_insert_categories,
		)
		mock_client = MagicMock()
		mock_client.categories.return_value = []
		with patch(f"{self.MOD}.log_taxjar_call") as mock_log:
			fetch_and_insert_categories(mock_client)

		actions = [c.kwargs.get("action") or c.args[0] for c in mock_log.call_args_list]
		statuses = [c.kwargs.get("status") for c in mock_log.call_args_list]
		self.assertIn("categories", actions)
		self.assertIn("request", statuses)
		self.assertIn("success", statuses)

	def test_logs_error_and_reraises(self):
		import taxjar.exceptions
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
			fetch_and_insert_categories,
		)
		mock_client = MagicMock()
		mock_client.categories.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")

		with patch(f"{self.MOD}.log_taxjar_call") as mock_log:
			with self.assertRaises(taxjar.exceptions.TaxJarConnectionError):
				fetch_and_insert_categories(mock_client)

		error_calls = [c for c in mock_log.call_args_list if c.kwargs.get("status") == "error"]
		self.assertEqual(len(error_calls), 1)


class TestTaxJarSettingsJsonNexusTab(UnitTestCase):
	"""JSON-level assertions on the doctype fixture itself - matches the pattern used
	by TestWorkspaceBranding below for the workspace fixture."""

	def _doctype_json(self):
		import json, os
		path = os.path.join(os.path.dirname(__file__), "taxjar_settings.json")
		with open(path) as f:
			return json.load(f)

	def _field(self, doctype_json, fieldname):
		for field in doctype_json["fields"]:
			if field["fieldname"] == fieldname:
				return field
		self.fail(f"Field {fieldname!r} not found in TaxJar Settings JSON")

	def test_nexus_tab_renamed(self):
		doctype_json = self._doctype_json()
		tab = self._field(doctype_json, "nexus_tab")
		self.assertEqual(tab["label"], "Nexus & Product Category")

	def test_product_tax_category_summary_fields_present(self):
		doctype_json = self._doctype_json()
		section = self._field(doctype_json, "product_tax_category_section")
		self.assertEqual(section["fieldtype"], "Section Break")

		html_field = self._field(doctype_json, "product_tax_category_html")
		self.assertEqual(html_field["fieldtype"], "HTML")

		self.assertIn("product_tax_category_section", doctype_json["field_order"])
		self.assertIn("product_tax_category_html", doctype_json["field_order"])

	def test_update_product_tax_category_button_present(self):
		doctype_json = self._doctype_json()
		btn = self._field(doctype_json, "update_product_tax_category_btn")
		self.assertEqual(btn["fieldtype"], "Button")
		self.assertEqual(btn["label"], "Update Product Tax Category List")
		self.assertIn("update_product_tax_category_btn", doctype_json["field_order"])

	def test_fresh_install_defaults(self):
		"""A never-configured TaxJar Settings singleton (fresh install, before the
		guided setup wizard has saved anything) should present Live / logging on /
		15-day retention - not the previous Sandbox / logging off / 5-day
		combination."""
		doctype_json = self._doctype_json()
		self.assertEqual(self._field(doctype_json, "api_mode")["default"], "Live")
		self.assertEqual(self._field(doctype_json, "enable_taxjar_logging")["default"], "1")
		self.assertEqual(self._field(doctype_json, "log_retention_days")["default"], "15")


# ── Problem 3: single branded workspace ──────────────────────────────────────


class TestWorkspaceBranding(UnitTestCase):

	def _workspace(self):
		import json, os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "workspace",
			"taxjar_integration", "taxjar_integration.json",
		))
		with open(path) as f:
			return json.load(f)

	def test_branded_title_and_label(self):
		ws = self._workspace()
		self.assertEqual(ws["title"], "TaxJar Integration")
		self.assertEqual(ws["label"], "TaxJar Integration")

	def test_name_drives_branded_route(self):
		"""The desk sidebar shows the workspace name and the route is its slug, so the
		name is branded and app_home points at the matching /app/taxjar-integration."""
		ws = self._workspace()
		self.assertEqual(ws["name"], "TaxJar Integration")
		self.assertEqual(frappe.get_hooks("app_home", app_name="taxjar_integration"), ["/app/taxjar-integration"])

	def test_old_workspace_removed_by_patch(self):
		import os
		patches = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "..", "patches.txt",
		))
		with open(patches) as f:
			self.assertIn("remove_old_taxjar_workspace", f.read())

	def test_taxjar_integration_casing_fix_patches_registered(self):
		"""The "Taxjar Integration" -> "TaxJar Integration" casing fix needs
		post_model_sync cleanup of the old-named Workspace/Workspace Sidebar/Module
		Def left behind once sync_all() creates the new-named records (Module Def
		can't be renamed in place - ModuleDef.before_rename blocks non-custom
		modules)."""
		import os
		patches = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "..", "patches.txt",
		))
		with open(patches) as f:
			content = f.read()
		self.assertIn("remove_old_taxjar_integration_workspace", content)
		self.assertIn("remove_old_taxjar_integration_module", content)

	def test_icon_is_valid_not_dollar_sign(self):
		ws = self._workspace()
		self.assertEqual(ws["icon"], "coins")

	def test_sidebar_mirrors_card_groups(self):
		"""The generated sidebar mirrors the workspace card groups: each card
		becomes a Section Break with its links as nested children, in the card
		display (content) order.

		The sidebar lives on ``Workspace.sidebar_items`` - a child table on the
		workspace itself - not the standalone ``Workspace Sidebar`` doctype, which
		was merged into ``Workspace`` earlier in v16 and is no longer read by
		frappe.boot.get_sidebar_items."""
		from taxjar_integration.install import sync_taxjar_workspace_sidebar

		sync_taxjar_workspace_sidebar()
		doc = frappe.get_doc("Workspace", "TaxJar Integration")

		structure = []
		current = None
		for item in doc.sidebar_items:
			if item.type == "Section Break":
				current = {"group": item.label, "children": []}
				structure.append(current)
			elif item.type == "Link" and current is not None:
				self.assertTrue(item.child)
				current["children"].append(item.link_to)

		self.assertEqual([g["group"] for g in structure], ["Setup", "Manage", "Sync"])
		groups = {g["group"]: g["children"] for g in structure}
		self.assertEqual(groups["Setup"], ["taxjar-setup", "TaxJar Settings", "TaxJar API Log"])
		self.assertEqual(groups["Manage"], ["taxjar-customers", "Product Tax Category"])
		self.assertEqual(groups["Sync"], ["taxjar-transactions"])

	def test_home_entry_uses_an_icon_that_exists_in_the_bundled_lucide_sprite(self):
		"""Regression guard: the desk sidebar resolves an icon name straight to
		#icon-<name> in frappe's bundled lucide sprite (frappe/public/icons/lucide/
		icons.svg) with no fallback - "home" was never in that sprite (only
		"house" is), so the Home entry silently rendered no icon at all."""
		from taxjar_integration.install import sync_taxjar_workspace_sidebar

		sync_taxjar_workspace_sidebar()
		doc = frappe.get_doc("Workspace", "TaxJar Integration")

		home_items = [item for item in doc.sidebar_items if item.label == "Home"]
		self.assertEqual(len(home_items), 1)
		self.assertEqual(home_items[0].icon, "house")

	def test_link_cards(self):
		ws = self._workspace()
		cards = {l["label"] for l in ws["links"] if l["type"] == "Card Break"}
		self.assertEqual(cards, {"Setup", "Manage", "Sync"})

	def test_link_targets(self):
		ws = self._workspace()
		targets = {l["link_to"] for l in ws["links"] if l["type"] == "Link"}
		self.assertEqual(
			targets,
			{"TaxJar Settings", "Product Tax Category", "taxjar-customers",
			 "taxjar-transactions", "TaxJar API Log", "taxjar-setup"},
		)

	def test_apps_screen_title_branded(self):
		from taxjar_integration import hooks
		self.assertEqual(hooks.add_to_apps_screen[0]["title"], "TaxJar Integration")


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

	def test_special_district_row_gets_a_static_label_not_a_blank_name(self):
		"""TaxJar's jurisdictions object has no per-special-district name (unlike
		state/county/city) - a blank cell there reads like missing data, so this
		row gets a static descriptive label instead."""
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		result = _extract_breakdown_data(tax_data, doc)

		special_row = next(r for r in result["transaction"] if r["jurisdiction"] == "Special")
		self.assertEqual(special_row["name"], "SPECIAL DISTRICT")

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

	def test_clears_freight_taxable(self):
		"""A stale "Shipping Taxability: Yes" pill must not survive past the
		point where the tax rows themselves get removed (exempt/no nexus)."""
		doc = _make_doc()
		doc.taxjar_freight_taxable = 1
		_clear_breakdown_data(doc)
		self.assertEqual(doc.taxjar_freight_taxable, 0)

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

	def test_freight_taxable_set_from_tax_data(self):
		"""doc.taxjar_freight_taxable mirrors TaxJar's own freight_taxable flag on
		the tax_for_order response, both ways (True and False are both real,
		meaningful values - not "unset")."""
		for freight_taxable, expected in ((True, 1), (False, 0)):
			with self.subTest(freight_taxable=freight_taxable):
				tax_data = _make_us_breakdown()
				tax_data.freight_taxable = freight_taxable
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

				self.assertEqual(doc.taxjar_freight_taxable, expected)

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
		self.assertEqual(len(_TRANSACTION_BREAKDOWN_FIELDS), 5)
		fieldnames = [f["fieldname"] for f in _TRANSACTION_BREAKDOWN_FIELDS]
		self.assertIn("taxjar_breakdown_section", fieldnames)
		self.assertIn("taxjar_breakdown_json", fieldnames)
		self.assertIn("taxjar_freight_taxable", fieldnames)
		self.assertIn("taxjar_freight_taxable_html", fieldnames)
		self.assertIn("taxjar_breakdown_html", fieldnames)

	def test_freight_taxable_field_is_hidden_read_only_check(self):
		field = next(f for f in _TRANSACTION_BREAKDOWN_FIELDS if f["fieldname"] == "taxjar_freight_taxable")
		self.assertEqual(field["fieldtype"], "Check")
		self.assertEqual(field["hidden"], 1)
		self.assertEqual(field["read_only"], 1)
		self.assertEqual(field["insert_after"], "taxjar_breakdown_json")

	def test_freight_taxable_html_is_plain_html_not_boxed(self):
		"""Deliberately plain HTML, not Text Editor - a read-only Text Editor
		field wraps its content in a boxed "like-disabled-input" background,
		which is right for the table but wrong for a standalone pill."""
		field = next(f for f in _TRANSACTION_BREAKDOWN_FIELDS if f["fieldname"] == "taxjar_freight_taxable_html")
		self.assertEqual(field["fieldtype"], "HTML")
		self.assertEqual(field["insert_after"], "taxjar_freight_taxable")

	def test_breakdown_html_is_virtual_text_editor(self):
		"""Server-rendered virtual field (set by onload/before_print via
		set_taxjar_breakdown_html), same shape as india_compliance's
		gst_breakup_table - not a plain HTML display field anymore, so it
		actually shows up in Print/PDF, not just the desk form."""
		field = next(f for f in _TRANSACTION_BREAKDOWN_FIELDS if f["fieldname"] == "taxjar_breakdown_html")
		self.assertEqual(field["fieldtype"], "Text Editor")
		self.assertEqual(field["is_virtual"], 1)
		self.assertEqual(field["read_only"], 1)
		self.assertEqual(field["allow_on_submit"], 1)
		self.assertEqual(field["insert_after"], "taxjar_freight_taxable_html")

	def test_sales_invoice_freight_taxable_allows_on_submit(self):
		"""Written alongside taxjar_breakdown_json on the post-submission
		recalculation path - needs the same allow_on_submit exemption."""
		from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import make_custom_fields

		captured = {}

		def _capture(custom_fields, update=True):
			captured.update(custom_fields)

		with patch(
			"taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings.create_custom_fields",
			side_effect=_capture,
		):
			make_custom_fields()

		field = next(f for f in captured["Sales Invoice"] if f["fieldname"] == "taxjar_freight_taxable")
		self.assertEqual(field.get("allow_on_submit"), 1)

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


# ── Tax Breakdown: server-side HTML rendering (get_taxjar_breakdown_html) ──
# Same tax-break-up/table-bordered/table-hover markup as core ERPNext's Tax
# Breakup table and india_compliance's GST Breakup Table, rendered server-side
# via Jinja (templates/includes/taxjar_breakup.html) instead of built in JS.

class TestGetTaxjarBreakdownHtml(UnitTestCase):

	def test_no_json_shows_no_breakdown_message(self):
		doc = _make_doc()
		html = get_taxjar_breakdown_html(doc)
		self.assertIn("No TaxJar tax breakdown available", html)

	def test_invalid_json_falls_back_to_no_breakdown_message(self):
		doc = _make_doc()
		doc.taxjar_breakdown_json = "not json"
		html = get_taxjar_breakdown_html(doc)
		self.assertIn("No TaxJar tax breakdown available", html)

	def test_renders_table_with_core_erpnext_markup(self):
		"""Same skeleton as erpnext's itemised_tax_breakup.html /
		india_compliance's gst_breakup.html - and, unlike the old JS builder,
		no inline thead background overriding the default desk table theme."""
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		html = get_taxjar_breakdown_html(doc)
		self.assertIn('class="tax-break-up"', html)
		self.assertIn("table-bordered", html)
		self.assertIn("table-hover", html)
		self.assertIn("Jurisdiction", html)
		self.assertNotIn("background-color", html)

	def test_output_has_no_newlines_or_tabs(self):
		"""Jinja's {% if/for %} control tags leave their surrounding blank
		lines/indentation in the rendered output; a Text Editor field renders
		that whitespace as real vertical gaps in the desk form (a large empty
		block above the table). Same fix india_compliance applies to its own
		gst_breakup_table render."""
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		doc.taxjar_freight_taxable = 1
		html = get_taxjar_breakdown_html(doc)
		self.assertNotIn("\n", html)
		self.assertNotIn("\t", html)

	def test_jurisdiction_rendered_bold(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		html = get_taxjar_breakdown_html(doc)
		self.assertIn("<strong>State</strong>", html)
		self.assertIn("<strong>County</strong>", html)
		self.assertIn("<strong>Special</strong>", html)

	def test_no_usd_block_for_single_currency_doc(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		html = get_taxjar_breakdown_html(doc)
		self.assertNotIn("Tax Calculation (USD)", html)

	def test_usd_block_rendered_for_multi_currency_doc(self):
		tax_data = _make_us_breakdown()
		doc = _make_doc(currency="EUR")
		_store_breakdown_data(tax_data, doc, usd_rate=1.1)
		html = get_taxjar_breakdown_html(doc)
		self.assertIn("Tax Calculation (USD)", html)
		self.assertIn("Equivalent in Transaction Currency (EUR)", html)
		self.assertIn("multi-currency transaction", html)

	def test_pill_not_part_of_server_rendered_html(self):
		"""The shipping-taxability pill moved to its own plain-HTML field
		(taxjar_freight_taxable_html, rendered client-side by
		render_shipping_taxability in taxjar_utils.js) - a read-only Text
		Editor field boxes its whole content in a "like-disabled-input"
		background, which reads fine around the table but wrong around a
		standalone indicator pill sitting inside it."""
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		doc.taxjar_freight_taxable = 1
		html = get_taxjar_breakdown_html(doc)
		self.assertNotIn("Is shipping charges taxable?", html)
		self.assertNotIn("indicator-pill", html)

	def test_jurisdiction_and_name_are_html_escaped(self):
		"""Defensive escaping of TaxJar-sourced jurisdiction/name text, same
		posture as the old JS's frappe.utils.escape_html() calls."""
		import json as _json
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		data = _json.loads(doc.taxjar_breakdown_json)
		data["transaction"][0]["name"] = "<script>alert(1)</script>"
		doc.taxjar_breakdown_json = _json.dumps(data)
		html = get_taxjar_breakdown_html(doc)
		self.assertNotIn("<script>alert(1)</script>", html)
		self.assertIn("&lt;script&gt;", html)


# ── Tax Breakdown: onload/before_print wiring (set_taxjar_breakdown_html) ──

class TestSetTaxjarBreakdownHtml(UnitTestCase):

	def test_onload_sets_onload_key_not_field(self):
		"""Desk form: pushed via set_onload for the client shim to copy onto
		the field, since the browser already holds its own copy of the doc."""
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		set_taxjar_breakdown_html(doc, "onload")
		self.assertIsNone(doc.taxjar_breakdown_html)
		self.assertIn("Jurisdiction", doc.get_onload("_taxjar_breakdown_html"))

	def test_before_print_sets_field_directly(self):
		"""Print/PDF: assigned directly, since print rendering reads this same
		in-memory doc in the same request - no client round trip involved.
		Called as doc.run_method("before_print", print_settings) by
		frappe.www.printview - the print_settings positional arg must not
		raise a TypeError."""
		tax_data = _make_us_breakdown()
		doc = _make_doc()
		_store_breakdown_data(tax_data, doc)
		set_taxjar_breakdown_html(doc, "before_print", {"some": "print_settings"})
		self.assertIsNotNone(doc.taxjar_breakdown_html)
		self.assertIn("Jurisdiction", doc.taxjar_breakdown_html)
		self.assertEqual(doc.get_onload(), {})

	def test_noop_when_doc_has_no_breakdown_html_field(self):
		doc = _make_doc()
		doc.meta = _FakeMeta(fields=())
		set_taxjar_breakdown_html(doc, "onload")
		self.assertEqual(doc.get_onload(), {})

	def test_real_document_virtual_field_has_no_instance_attribute_when_loaded_from_db(self):
		"""Regression guard for the exact bug this class exists to prevent:
		hasattr(doc, "taxjar_breakdown_html") is False on a document loaded via
		Document.load_from_db(), because is_virtual fields with no backing
		@property are never set as instance attributes there - load_from_db
		populates BaseDocument.__init__ from a raw "SELECT *" row dict, which
		only has real DB columns, and (unlike frappe.new_doc(), which does call
		init_valid_columns() and so does NOT reproduce this) never backfills
		virtual fields to None. A guard using hasattr() instead of
		doc.meta.has_field() silently no-opped set_taxjar_breakdown_html for
		every already-saved document - exactly what onload/before_print always
		deal with.

		Reproduces that exact construction path (BaseDocument.__init__ from a
		bare dict, the same call load_from_db makes) without needing a
		persisted record, since frappe.new_doc()/frappe.get_doc({...}) both
		route through init_valid_columns() and would not reproduce the bug."""
		from frappe.model.base_document import BaseDocument
		from frappe.model.document import get_controller

		controller = get_controller("Quotation")
		doc = controller.__new__(controller)
		doc.flags = frappe._dict()
		BaseDocument.__init__(doc, {"doctype": "Quotation", "name": "QTN-TEST-0001", "company": "_Test Company"})

		self.assertTrue(doc.meta.has_field("taxjar_breakdown_html"))
		self.assertFalse(hasattr(doc, "taxjar_breakdown_html"))

	def test_onload_populates_real_document_loaded_from_db(self):
		"""End-to-end against the real onload dispatch path AND the
		load_from_db-shaped construction from the test above - together these
		cover the exact scenario that broke in production: a doc.meta.has_field()
		guard (fixed) vs. the hasattr() guard (buggy) it replaced, wired through
		the real set_onload()/__onload round trip."""
		from frappe.desk.form.load import run_onload
		from frappe.model.base_document import BaseDocument
		from frappe.model.document import get_controller

		controller = get_controller("Quotation")
		doc = controller.__new__(controller)
		doc.flags = frappe._dict()
		BaseDocument.__init__(doc, {"doctype": "Quotation", "name": "QTN-TEST-0001", "company": "_Test Company"})
		doc.items = []
		doc.append("items", {"item_code": "_Test Item", "qty": 1, "rate": 100})
		doc.taxjar_breakdown_json = frappe.as_json(_extract_breakdown_data(_make_us_breakdown(), doc))

		run_onload(doc)

		html = doc.get_onload("_taxjar_breakdown_html")
		self.assertIsNotNone(html)
		self.assertIn("Jurisdiction", html)


class TestTaxjarBreakdownHtmlHooksRegistered(UnitTestCase):
	"""onload/before_print never fire for a hook registered on the wrong key
	(e.g. a child doctype - see the child-table field investigation for why
	that matters) - so this pins the exact transaction-doctype grouping the
	hook must be registered under."""

	def _transaction_doc_events(self):
		from taxjar_integration import hooks
		for doctypes, events in hooks.doc_events.items():
			key = doctypes if isinstance(doctypes, tuple) else (doctypes,)
			if set(("Quotation", "Sales Order", "Sales Invoice")) <= set(key):
				return events
		return {}

	def test_onload_registered_on_transaction_doctypes(self):
		events = self._transaction_doc_events()
		self.assertIn("set_taxjar_breakdown_html", events.get("onload", ""))

	def test_before_print_registered_on_transaction_doctypes(self):
		events = self._transaction_doc_events()
		self.assertIn("set_taxjar_breakdown_html", events.get("before_print", ""))


# ── Tax Breakdown: JS structure tests ───────────────────────────────────────

class TestTaxBreakdownJS(UnitTestCase):

	def _js_dir(self):
		import os
		return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "public", "js"))

	def _read_js(self, filename):
		import os
		with open(os.path.join(self._js_dir(), filename)) as f:
			return f.read()

	def _read_breakup_template(self):
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "..", "templates", "includes", "taxjar_breakup.html",
		))
		with open(path) as f:
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
		path = os.path.join(self._js_dir(), "quotation.js")
		self.assertTrue(os.path.isfile(path))

	def test_sales_order_js_exists(self):
		import os
		path = os.path.join(self._js_dir(), "sales_order.js")
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
			path = os.path.join(self._js_dir(), filename)
			with open(path) as f:
				js = f.read()
			self.assertIn(child_dt, js, f"{filename} should register handler on {child_dt}")
			self.assertIn("form_render", js, f"{filename} should use form_render event")

	def test_status_cards_never_render_reason_text(self):
		"""No reason text anywhere in the matrix - only the answer pill (and,
		for a transaction-level override, the extra "Overridden" pill)."""
		js = self._read_js("taxjar_utils.js")
		render_fn = js.split("render_status_cards = function (frm) {")[1].split("\n};")[0]
		self.assertNotIn("card.reason", render_fn)
		self.assertNotIn("taxjar-status-card-reason", render_fn)
		self.assertNotIn("taxjar_product_taxable_reason", render_fn)

	def test_status_card_shows_overridden_pill_only_for_transaction_override(self):
		"""A second "Overridden" pill appears under "Is the customer taxable?"
		only when the reason came from taxjar_transaction_exempt (server sets
		it to "Overridden (<type>)") - not for a master-level customer
		exemption ("Customer is exempt (<type>)") or the plain taxable case."""
		js = self._read_js("taxjar_utils.js")
		render_fn = js.split("render_status_cards = function (frm) {")[1].split("\n};")[0]
		self.assertIn('.startsWith("Overridden")', render_fn)
		self.assertIn("taxjar-status-card-override", render_fn)
		self.assertIn('card.overridden ? `<div class="taxjar-status-card-override">', render_fn)

	def test_status_cards_use_skipped_instead_of_na(self):
		js = self._read_js("taxjar_utils.js")
		render_fn = js.split("render_status_cards = function (frm) {")[1].split("\n};")[0]
		self.assertNotIn('__("N/A")', render_fn)
		self.assertIn('__("Skipped")', render_fn)

	def test_status_card_override_css_exists(self):
		js = self._read_js("taxjar_utils.js")
		self.assertIn(".taxjar-status-card-override {", js)

	def test_js_has_no_breakdown_message(self):
		js = self._read_js("taxjar_utils.js")
		self.assertIn("No TaxJar tax breakdown available", js, "taxjar_utils.js should have no-breakdown message")

	def test_no_breakdown_msg_distinguishes_unsaved_doc(self):
		js = self._read_js("taxjar_utils.js")
		fn = js.split("_no_breakdown_msg = function (is_new) {")[1].split("\n};")[0]
		self.assertIn("Please save to see tax breakdown.", fn)
		self.assertIn("const text = is_new", fn)

	def test_render_tax_breakdown_copies_from_onload(self):
		"""The table itself is rendered server-side (see the
		get_taxjar_breakdown_html tests) - this just copies the result from
		frm.doc.__onload onto the virtual field, with a client-side fallback
		message for a truly new/unsaved doc (which never goes through the
		server onload) and for the unexpected case of a saved doc missing
		__onload entirely."""
		js = self._read_js("taxjar_utils.js")
		fn = js.split("render_tax_breakdown = function (frm) {")[1].split("\n};")[0]
		self.assertIn("_no_breakdown_msg(true)", fn)
		self.assertIn("_no_breakdown_msg(false)", fn)
		self.assertIn("frm.doc.__onload?._taxjar_breakdown_html", fn)
		self.assertIn('frm.refresh_field("taxjar_breakdown_html")', fn)

	def test_shipping_taxability_pill_defined_in_js(self):
		"""Plain-HTML field (taxjar_freight_taxable_html), rendered
		client-side straight off the already-loaded taxjar_freight_taxable
		field - no server round trip needed, and deliberately not part of
		templates/includes/taxjar_breakup.html since a read-only Text Editor
		field would box it together with the table."""
		js = self._read_js("taxjar_utils.js")
		fn = js.split("render_shipping_taxability = function (frm) {")[1].split("\n};")[0]
		self.assertIn("taxjar_freight_taxable", fn)
		self.assertIn("Is shipping charges taxable?", fn)
		self.assertIn("indicator-pill", fn)
		self.assertIn('__("Yes")', fn)
		self.assertIn('__("No")', fn)

	def test_shipping_taxability_pill_uses_bigger_font(self):
		js = self._read_js("taxjar_utils.js")
		fn = js.split("render_shipping_taxability = function (frm) {")[1].split("\n};")[0]
		self.assertIn("var(--text-md)", fn)
		self.assertNotIn("var(--text-sm)", fn)

	def test_shipping_taxability_pill_wired_into_refresh(self):
		for filename in ("sales_invoice.js", "sales_order.js", "quotation.js"):
			js = self._read_js(filename)
			self.assertIn(
				"taxjar_integration.render_shipping_taxability(frm)", js,
				f"{filename} should call render_shipping_taxability on refresh",
			)

	def test_breakup_template_has_multi_currency_support(self):
		template = self._read_breakup_template()
		self.assertIn("data.usd", template, "taxjar_breakup.html should check for USD breakdown data")
		self.assertIn("Tax Calculation (USD)", template, "taxjar_breakup.html should have USD table heading")
		self.assertIn("Equivalent in Transaction Currency", template, "taxjar_breakup.html should have converted table heading")

	def test_breakup_template_uses_erpnext_table_styling(self):
		"""Same tax-break-up/table-bordered/table-hover markup as core
		ERPNext's own Tax Breakup table and india_compliance's GST Breakup
		Table - and, unlike the old JS builder, no inline thead background
		overriding the default desk table theme."""
		template = self._read_breakup_template()
		self.assertIn("table-hover", template)
		self.assertIn('class="tax-break-up"', template)
		self.assertIn("overflow-x: auto", template)
		self.assertNotIn("table-sm", template)
		self.assertNotIn("background-color", template)

	def test_item_table_js_uses_erpnext_table_styling(self):
		"""Item-level breakdown table (build_item_table) is still JS-rendered -
		only the transaction-level table moved server-side."""
		js = self._read_js("taxjar_utils.js")
		fn = js.split("build_item_table = function (rows, currency) {")[1].split("\n};")[0]
		self.assertIn("table-hover", fn)
		self.assertIn("tax-break-up", fn)
		self.assertIn("overflow-x: auto", fn)
		self.assertNotIn("table-sm", fn)


# ── TaxJar Sync Status: sidebar pill ────────────────────────────────────────

class TestSyncStatusSidebarPill(UnitTestCase):

	def _js_dir(self):
		import os
		return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "public", "js"))

	def _read_js(self, filename):
		import os
		with open(os.path.join(self._js_dir(), filename)) as f:
			return f.read()

	def _render_fn(self):
		js = self._read_js("taxjar_utils.js")
		return js.split("render_sync_status_sidebar_pill = function (frm) {")[1].split("\n};")[0]

	def test_status_colors_match_transactions_page(self):
		"""Same mapping as STATUS_COLORS in taxjar_transactions.js, kept as
		its own copy since that page's version is bound to its own class."""
		js = self._read_js("taxjar_utils.js")
		colors = js.split("SYNC_STATUS_COLORS = {")[1].split("};")[0]
		self.assertIn('Synced: "green"', colors)
		self.assertIn('Failed: "red"', colors)
		self.assertIn('Queued: "blue"', colors)
		self.assertIn('"Not Applicable": "grey"', colors)

	def test_draft_shows_submit_to_sync_label(self):
		fn = self._render_fn()
		self.assertIn("docstatus === 0", fn)
		self.assertIn("TaxJar: Submit to sync", fn)

	def test_submitted_label_is_prefixed(self):
		fn = self._render_fn()
		self.assertIn('__("TaxJar: {0}"', fn)

	def test_synced_info_text_shows_last_synced(self):
		fn = self._render_fn()
		synced_branch = fn.split('status === "Synced"')[1].split("} else if")[0]
		self.assertIn("Last synced:", synced_branch)
		self.assertIn("taxjar_last_synced", synced_branch)

	def test_queued_info_text(self):
		fn = self._render_fn()
		self.assertIn("Queued for sync", fn)

	def test_failed_info_text_uses_sync_error(self):
		fn = self._render_fn()
		failed_branch = fn.split('status === "Failed"')[1]
		self.assertIn("taxjar_sync_error", failed_branch)

	def test_inserted_below_doc_id_above_assign(self):
		"""Sits below the doc id (after .sidebar-meta-details, the
		title/doc-id block) and above Assign/Attachments/Tags/Share, with its
		own border-bottom separating it from Assign below - matching
		.sidebar-meta-details' own border-bottom above it."""
		fn = self._render_fn()
		self.assertIn('.find(".form-sidebar .sidebar-meta-details")', fn)
		self.assertIn(".after($pill)", fn)
		self.assertIn("border-bottom", fn)

	def test_removes_stale_pill_before_rendering(self):
		"""Idempotent re-render, same pattern as india_compliance's own
		.remove()-then-readd, so repeated refresh() calls on the same
		document don't stack duplicate pills."""
		fn = self._render_fn()
		self.assertIn('.taxjar-sync-sidebar-pill-section").remove()', fn)

	def test_no_field_no_pill(self):
		"""Guards doctypes without the sync fields (Quotation, Sales Order) -
		the pill is Sales Invoice only."""
		fn = self._render_fn()
		self.assertIn("!frm.fields_dict.taxjar_sync_status", fn)

	def test_uses_indicator_pill_no_dot_class(self):
		"""Same classes india_compliance's sandbox pill uses."""
		fn = self._render_fn()
		self.assertIn("indicator-pill no-indicator-dot", fn)

	def test_hover_and_click_wired_not_native_title(self):
		"""Same interaction pattern as the Transactions page's sync icon -
		shows immediately on hover/click, not the native title attribute
		(which enforces its own delay and never responds to a click)."""
		fn = self._render_fn()
		self.assertIn('.on("mouseenter"', fn)
		self.assertIn('.on("mouseleave"', fn)
		self.assertIn('.on("click"', fn)
		self.assertNotIn("title=", fn)

	def test_wired_into_sales_invoice_refresh(self):
		js = self._read_js("sales_invoice.js")
		self.assertIn("taxjar_integration.render_sync_status_sidebar_pill(frm)", js)


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

	def test_sets_freight_taxable_true(self):
		doc = _make_doc()
		_set_tax_status_fields(doc, freight_taxable=True)
		self.assertEqual(doc.taxjar_freight_taxable, 1)

	def test_sets_freight_taxable_false_not_skipped(self):
		"""False is a real, meaningful value here (not "unset") - it must still
		be written, not treated the same as omitting the argument."""
		doc = _make_doc()
		doc.taxjar_freight_taxable = 1
		_set_tax_status_fields(doc, freight_taxable=False)
		self.assertEqual(doc.taxjar_freight_taxable, 0)

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


# ── Phase 1: Guided Setup page (get_setup_state / finish_setup) ───────────────

_SETUP_MODULE = "taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup"


class TestGuidedSetupState(UnitTestCase):
	def _settings(self):
		s = MagicMock()
		s.api_mode = "Sandbox"
		s.enable_taxjar_logging = 0
		s.log_retention_days = 90
		s.setup_complete = 0
		s.company_config = [frappe._dict(
			company="Frappe Tech", tax_account_head="Sales Tax - FT",
			shipping_account_head="Freight - FT",
			taxjar_calculate_tax=1, taxjar_create_transactions=0,
		)]
		s.table_hvjw = [frappe._dict(
			company="Frappe Tech", name="cred-1", sandbox_token="enc", live_token="",
		)]
		s.nexus = [frappe._dict(
			company="Frappe Tech", region="California", region_code="CA",
			country="United States", country_code="US",
		)]
		return s

	def test_state_shape_and_token_masking(self):
		"""companies (company_config) carries accounts + flags; credentials
		(table_hvjw) carries the masked token — two separate lists, since only
		the latter can exist before a company's accounts are known."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_setup_state
		s = self._settings()
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".get_decrypted_password", return_value="tok-abcd1234"):
			state = get_setup_state()

		self.assertEqual(state["api_mode"], "Sandbox")
		self.assertNotIn("taxjar_enabled", state)  # managed on the doctype, not by this wizard
		self.assertFalse(state["setup_complete"])

		co = state["companies"][0]
		self.assertEqual(co["company"], "Frappe Tech")
		self.assertTrue(co["calculate"])
		self.assertFalse(co["file"])
		self.assertNotIn("token_last4", co)

		cred = state["credentials"][0]
		self.assertEqual(cred["company"], "Frappe Tech")
		# Only the last 4 chars are ever exposed — never the full token.
		self.assertEqual(cred["token_last4"], "1234")
		self.assertEqual(len(state["nexus_by_company"]["Frappe Tech"]), 1)

	def test_reads_sandbox_token_in_sandbox_mode(self):
		"""In Live mode the live_token is read; sandbox mode reads sandbox_token."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_setup_state
		s = self._settings()
		s.api_mode = "Live"
		s.table_hvjw = [frappe._dict(company="Frappe Tech", name="cred-1",
			sandbox_token="", live_token="enc")]
		captured = {}
		def fake_decrypt(dt, name, field, raise_exception=False):
			captured["field"] = field
			return "live-wxyz5678"
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".get_decrypted_password", side_effect=fake_decrypt):
			state = get_setup_state()
		self.assertEqual(captured["field"], "live_token")
		self.assertEqual(state["credentials"][0]["token_last4"], "5678")

	def test_token_last4_none_when_no_token(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_setup_state
		s = self._settings()
		s.table_hvjw = [frappe._dict(company="Frappe Tech", name="cred-1", sandbox_token="", live_token="")]
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			state = get_setup_state()
		self.assertIsNone(state["credentials"][0]["token_last4"])

	def test_credentials_list_independent_of_company_config(self):
		"""A company can appear in credentials before it has a company_config row."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_setup_state
		s = self._settings()
		s.company_config = []
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".get_decrypted_password", return_value="tok-abcd1234"):
			state = get_setup_state()
		self.assertEqual(state["companies"], [])
		self.assertEqual(state["credentials"][0]["company"], "Frappe Tech")

	def test_requires_read_permission(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_setup_state
		with patch(_SETUP_MODULE + ".frappe.has_permission", side_effect=frappe.PermissionError):
			self.assertRaises(frappe.PermissionError, get_setup_state)

	def test_blank_api_mode_falls_back_to_live_not_sandbox(self):
		"""Matches the doctype's own new default (see TestTaxJarSettingsJsonNexusTab.
		test_fresh_install_defaults) - this fallback only bites if api_mode is ever
		explicitly blank rather than genuinely unset."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_setup_state
		s = self._settings()
		s.api_mode = ""
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			state = get_setup_state()
		self.assertEqual(state["api_mode"], "Live")

	def test_unconfigured_shows_fresh_defaults_over_stale_saved_values(self):
		"""Regression guard: a DocType JSON "default" only ever applies the
		very first time a Single doctype field is ever saved - it does NOT
		retroactively re-apply once a value has been stored, even an old
		default from before this one changed (e.g. log_retention_days=5,
		leftover from before the default became 15). No credentials and no
		company config is this wizard's own signal to show the current
		recommended defaults regardless of what stale value is stored."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_setup_state
		s = self._settings()
		s.api_mode = "Sandbox"
		s.enable_taxjar_logging = 0
		s.log_retention_days = 5
		s.table_hvjw = []
		s.company_config = []
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			state = get_setup_state()
		self.assertEqual(state["api_mode"], "Live")
		self.assertTrue(state["enable_taxjar_logging"])
		self.assertEqual(state["log_retention_days"], 15)

	def test_configured_site_keeps_its_stored_values(self):
		"""Once either credentials or company config exist, stored values -
		even ones that differ from the recommended defaults - are respected,
		not silently overridden."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_setup_state
		s = self._settings()
		s.api_mode = "Sandbox"
		s.enable_taxjar_logging = 0
		s.log_retention_days = 5
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			state = get_setup_state()
		self.assertEqual(state["api_mode"], "Sandbox")
		self.assertFalse(state["enable_taxjar_logging"])
		self.assertEqual(state["log_retention_days"], 5)


# ── Phase 2: test_connection ───────────────────────────────────────────────


class TestGuidedSetupTestConnection(UnitTestCase):
	def _settings(self, mode="Sandbox", creds=None):
		s = MagicMock()
		s.api_mode = mode
		s.table_hvjw = creds or []
		return s

	def test_ok_with_explicit_token_does_not_persist(self):
		"""An explicit token is validated live, transiently — never written to
		table_hvjw or saved, so a bad token typed while testing never lands in
		the database."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import test_connection
		s = self._settings()
		mock_client = MagicMock()
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".taxjar.Client", return_value=mock_client):
			res = test_connection(company="Frappe Tech", token="tok-123", mode="Sandbox")

		self.assertTrue(res["ok"])
		mock_client.categories.assert_called_once()
		s.save.assert_not_called()
		self.assertEqual(len(s.table_hvjw), 0)

	def test_falls_back_to_saved_token_when_none_given(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import test_connection
		cred = frappe._dict(company="Frappe Tech", name="cred-1", sandbox_token="enc", live_token="")
		s = self._settings(creds=[cred])
		mock_client = MagicMock()
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".get_decrypted_password", return_value="saved-token"), \
		     patch(_SETUP_MODULE + ".taxjar.Client", return_value=mock_client):
			res = test_connection(company="Frappe Tech")

		self.assertTrue(res["ok"])

	def test_no_token_available_returns_not_ok_without_calling_taxjar(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import test_connection
		s = self._settings()
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".taxjar.Client") as mock_client_cls:
			res = test_connection(company="Frappe Tech")

		self.assertFalse(res["ok"])
		mock_client_cls.assert_not_called()

	def test_blank_mode_and_blank_settings_falls_back_to_live_token_field(self):
		"""No explicit mode param, no saved api_mode - defaults to Live, so a
		saved live_token (not sandbox_token) is what gets read."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import test_connection
		cred = frappe._dict(company="Frappe Tech", name="cred-1", sandbox_token="", live_token="enc")
		s = self._settings(mode="", creds=[cred])
		mock_client = MagicMock()
		captured = {}

		def fake_decrypt(dt, name, field, raise_exception=False):
			captured["field"] = field
			return "live-token-value"

		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".get_decrypted_password", side_effect=fake_decrypt), \
		     patch(_SETUP_MODULE + ".taxjar.Client", return_value=mock_client):
			res = test_connection(company="Frappe Tech")

		self.assertTrue(res["ok"])
		self.assertEqual(res["mode"], "Live")
		self.assertEqual(captured["field"], "live_token")

	def test_401_returns_invalid_token_message(self):
		import taxjar.exceptions
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import test_connection
		s = self._settings()
		mock_client = MagicMock()
		err = taxjar.exceptions.TaxJarResponseError(MagicMock())
		err.full_response = {"status_code": 401}
		mock_client.categories.side_effect = err
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".taxjar.Client", return_value=mock_client):
			res = test_connection(company="Frappe Tech", token="bad-token")

		self.assertFalse(res["ok"])
		self.assertIn("Invalid token", res["message"])

	def test_connection_error_returns_not_ok(self):
		import taxjar.exceptions
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import test_connection
		s = self._settings()
		mock_client = MagicMock()
		mock_client.categories.side_effect = taxjar.exceptions.TaxJarConnectionError("timeout")
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s), \
		     patch(_SETUP_MODULE + ".taxjar.Client", return_value=mock_client):
			res = test_connection(company="Frappe Tech", token="tok-123")

		self.assertFalse(res["ok"])

	def test_requires_write_permission(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import test_connection
		with patch(_SETUP_MODULE + ".frappe.has_permission", side_effect=frappe.PermissionError):
			self.assertRaises(frappe.PermissionError, test_connection, company="Frappe Tech", token="x")


# ── Phase 2: save_connection ────────────────────────────────────────────────


class TestGuidedSetupSaveConnection(UnitTestCase):
	def test_sets_mode_and_creates_new_credential(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_connection
		s = MagicMock()
		s.table_hvjw = []
		appended = MagicMock(company=None)
		s.append.return_value = appended
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			res = save_connection(mode="Sandbox", credentials=[{"company": "Frappe Tech", "token": "tok-1"}])

		self.assertTrue(res["ok"])
		self.assertEqual(s.api_mode, "Sandbox")
		s.append.assert_called_once_with("table_hvjw", {"company": "Frappe Tech"})
		appended.set.assert_called_once_with("sandbox_token", "tok-1")
		s.save.assert_called_once()

	def test_updates_existing_credential_without_duplicating_row(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_connection
		cred = MagicMock(company="Frappe Tech")
		s = MagicMock()
		s.table_hvjw = [cred]
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			save_connection(mode="Live", credentials=[{"company": "Frappe Tech", "token": "new-tok"}])

		s.append.assert_not_called()
		cred.set.assert_called_once_with("live_token", "new-tok")

	def test_blank_token_keeps_existing_not_cleared(self):
		"""A blank token in the payload means the masked field wasn't retyped —
		it must not overwrite the stored token."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_connection
		cred = MagicMock(company="Frappe Tech")
		s = MagicMock()
		s.table_hvjw = [cred]
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			save_connection(mode="Sandbox", credentials=[{"company": "Frappe Tech", "token": ""}])

		cred.set.assert_not_called()

	def test_requires_write_permission(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_connection
		with patch(_SETUP_MODULE + ".frappe.has_permission", side_effect=frappe.PermissionError):
			self.assertRaises(frappe.PermissionError, save_connection, mode="Sandbox", credentials=[])


# ── Phase 2: save_company_accounts ──────────────────────────────────────────


class TestGuidedSetupSaveCompanyAccounts(UnitTestCase):
	def test_creates_new_company_config_row(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_company_accounts
		s = MagicMock()
		s.company_config = []
		appended = frappe._dict(company=None, tax_account_head=None, shipping_account_head=None)
		s.append.return_value = appended
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			res = save_company_accounts(rows=[{
				"company": "Frappe Tech",
				"tax_account_head": "Sales Tax - FT",
				"shipping_account_head": "Freight - FT",
			}])

		self.assertTrue(res["ok"])
		s.append.assert_called_once_with("company_config", {"company": "Frappe Tech"})
		self.assertEqual(appended.tax_account_head, "Sales Tax - FT")
		self.assertEqual(appended.shipping_account_head, "Freight - FT")
		s.save.assert_called_once()

	def test_updates_existing_row_without_duplicating(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_company_accounts
		cfg = frappe._dict(company="Frappe Tech", tax_account_head="Old", shipping_account_head="Old")
		s = MagicMock()
		s.company_config = [cfg]
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			save_company_accounts(rows=[{
				"company": "Frappe Tech",
				"tax_account_head": "New Tax",
				"shipping_account_head": "New Freight",
			}])

		s.append.assert_not_called()
		self.assertEqual(cfg.tax_account_head, "New Tax")
		self.assertEqual(cfg.shipping_account_head, "New Freight")

	def test_requires_write_permission(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_company_accounts
		with patch(_SETUP_MODULE + ".frappe.has_permission", side_effect=frappe.PermissionError):
			self.assertRaises(frappe.PermissionError, save_company_accounts, rows=[])


# ── Phase 2: save_features ──────────────────────────────────────────────────


class TestGuidedSetupSaveFeatures(UnitTestCase):
	def test_parameter_is_not_named_flags_or_ignore_permissions(self):
		"""Regression guard: frappe.call() - used by every real /api/method/...
		request, unlike bench execute or calling the function directly in
		Python - unconditionally strips any kwarg literally named "flags" or
		"ignore_permissions" via frappe.get_newargs() before dispatch, as a
		security measure, regardless of whether the target function declares
		that parameter. A whitelisted method using either name as its own
		parameter silently receives None over real HTTP/JS calls while
		appearing to work when tested via bench execute or direct calls in a
		unit test - exactly the trap this function fell into."""
		import inspect
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_features
		params = set(inspect.signature(save_features).parameters)
		self.assertNotIn("flags", params)
		self.assertNotIn("ignore_permissions", params)

	def test_sets_per_company_flags(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_features
		cfg = frappe._dict(company="Frappe Tech", taxjar_calculate_tax=0, taxjar_create_transactions=0)
		s = MagicMock()
		s.company_config = [cfg]
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			res = save_features(company_flags=[{"company": "Frappe Tech", "calculate": 1, "file": 0}])

		self.assertTrue(res["ok"])
		self.assertEqual(cfg.taxjar_calculate_tax, 1)
		self.assertEqual(cfg.taxjar_create_transactions, 0)
		s.save.assert_called_once()

	def test_enabling_calculate_auto_enables_master_switch(self):
		"""Per-company flags do nothing while taxjar_enabled is off (see
		_is_taxjar_enabled), which reads as "the toggle didn't save" even though the
		child row was written correctly — flip the master switch the moment any
		company ends up with a feature on."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_features
		cfg = frappe._dict(company="Frappe Tech", taxjar_calculate_tax=0, taxjar_create_transactions=0)
		s = MagicMock()
		s.company_config = [cfg]
		s.taxjar_enabled = 0
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			save_features(company_flags=[{"company": "Frappe Tech", "calculate": 1, "file": 0}])

		self.assertEqual(s.taxjar_enabled, 1)

	def test_enabling_file_alone_also_auto_enables_master_switch(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_features
		cfg = frappe._dict(company="Frappe Tech", taxjar_calculate_tax=0, taxjar_create_transactions=0)
		s = MagicMock()
		s.company_config = [cfg]
		s.taxjar_enabled = 0
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			save_features(company_flags=[{"company": "Frappe Tech", "calculate": 0, "file": 1}])

		self.assertEqual(s.taxjar_enabled, 1)

	def test_disabling_all_flags_does_not_disable_master_switch(self):
		"""Turning individual company flags off does not imply the user wants
		TaxJar off everywhere - that stays a deliberate action on the form."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_features
		cfg = frappe._dict(company="Frappe Tech", taxjar_calculate_tax=1, taxjar_create_transactions=1)
		s = MagicMock()
		s.company_config = [cfg]
		s.taxjar_enabled = 1
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			save_features(company_flags=[{"company": "Frappe Tech", "calculate": 0, "file": 0}])

		self.assertEqual(s.taxjar_enabled, 1)

	def test_skips_flags_for_company_without_existing_config_row(self):
		"""A company with credentials but no accounts yet (Accounts step not run)
		has no company_config row to flip flags on — must not throw or fabricate one."""
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_features
		s = MagicMock()
		s.company_config = []
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			save_features(company_flags=[{"company": "No Config Co", "calculate": 1, "file": 0}])

		s.append.assert_not_called()

	def test_requires_write_permission(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import save_features
		with patch(_SETUP_MODULE + ".frappe.has_permission", side_effect=frappe.PermissionError):
			self.assertRaises(frappe.PermissionError, save_features, company_flags=[])


# ── Phase 2: remove_company ─────────────────────────────────────────────────


class TestGuidedSetupRemoveCompany(UnitTestCase):
	def test_removes_credential_and_company_config_row(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import remove_company
		s = MagicMock()
		s.table_hvjw = [frappe._dict(company="Frappe Tech"), frappe._dict(company="Other Co")]
		s.company_config = [frappe._dict(company="Frappe Tech"), frappe._dict(company="Other Co")]
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			res = remove_company(company="Frappe Tech")

		self.assertTrue(res["ok"])
		set_calls = {c.args[0]: c.args[1] for c in s.set.call_args_list}
		self.assertEqual([r.company for r in set_calls["table_hvjw"]], ["Other Co"])
		self.assertEqual([r.company for r in set_calls["company_config"]], ["Other Co"])
		s.save.assert_called_once()

	def test_leaves_other_companies_untouched_when_no_match(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import remove_company
		s = MagicMock()
		s.table_hvjw = [frappe._dict(company="Other Co")]
		s.company_config = []
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			remove_company(company="Frappe Tech")

		set_calls = {c.args[0]: c.args[1] for c in s.set.call_args_list}
		self.assertEqual([r.company for r in set_calls["table_hvjw"]], ["Other Co"])

	def test_requires_write_permission(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import remove_company
		with patch(_SETUP_MODULE + ".frappe.has_permission", side_effect=frappe.PermissionError):
			self.assertRaises(frappe.PermissionError, remove_company, company="Frappe Tech")


# ── Phase 2: fetch_nexus ─────────────────────────────────────────────────────


class TestGuidedSetupFetchNexus(UnitTestCase):
	def test_calls_update_nexus_list_and_returns_grouped_nexus(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import fetch_nexus
		s = MagicMock()
		s.company_config = [frappe._dict(company="Frappe Tech")]
		s.nexus = [frappe._dict(company="Frappe Tech", region="California", region_code="CA",
			country="United States", country_code="US")]
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			res = fetch_nexus()

		self.assertTrue(res["ok"])
		s.update_nexus_list.assert_called_once()
		self.assertEqual(len(res["nexus_by_company"]["Frappe Tech"]), 1)

	def test_throws_when_no_company_config(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import fetch_nexus
		s = MagicMock()
		s.company_config = []
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=s):
			self.assertRaises(frappe.ValidationError, fetch_nexus)
		s.update_nexus_list.assert_not_called()

	def test_requires_write_permission(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import fetch_nexus
		with patch(_SETUP_MODULE + ".frappe.has_permission", side_effect=frappe.PermissionError):
			self.assertRaises(frappe.PermissionError, fetch_nexus)


class TestGuidedSetupFinish(UnitTestCase):
	def test_finish_sets_complete_and_saves(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import finish_setup
		doc = MagicMock()
		with patch(_SETUP_MODULE + ".frappe.has_permission"), \
		     patch(_SETUP_MODULE + ".frappe.get_single", return_value=doc):
			res = finish_setup()
		self.assertEqual(doc.setup_complete, 1)
		doc.save.assert_called_once()
		self.assertTrue(res["ok"])

	def test_finish_requires_write_permission(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import finish_setup
		with patch(_SETUP_MODULE + ".frappe.has_permission", side_effect=frappe.PermissionError):
			self.assertRaises(frappe.PermissionError, finish_setup)


class TestGuidedSetupSchemaAndEntry(UnitTestCase):
	def test_setup_complete_field_exists(self):
		field = frappe.get_meta("TaxJar Settings").get_field("setup_complete")
		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Check")

	def test_page_json_declares_roles(self):
		import json, os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.json"))
		data = json.load(open(path))
		self.assertEqual(data.get("standard"), "Yes")
		roles = {r["role"] for r in data.get("roles", [])}
		self.assertIn("System Manager", roles)
		self.assertIn("Accounts Manager", roles)

	def test_settings_js_has_setup_intro(self):
		import os
		js = open(os.path.join(os.path.dirname(__file__), "taxjar_settings.js")).read()
		self.assertIn("set_intro", js)
		self.assertIn("/app/taxjar-setup", js)

	def test_setup_page_js_wires_steps_and_apis(self):
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.js"))
		js = open(path).read()
		self.assertIn("get_setup_state", js)
		self.assertIn("finish_setup", js)
		for key in ("welcome", "connect", "accounts", "features", "nexus", "review"):
			self.assertIn(key, js)


# ── Phase 2: guided setup JS — native controls per step ─────────────────────


class TestGuidedSetupPhase2JS(UnitTestCase):
	"""String/structure assertions on the page JS — the same pattern used for
	other pages in this app, since there's no JS runtime in the Python test
	suite. Confirms the documented native-control plan (docs/guided-setup-
	plan.md §3/§7) is actually wired, not just described."""

	def _js(self):
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.js"))
		return open(path).read()

	def test_calls_all_five_phase2_apis(self):
		js = self._js()
		for method in ("test_connection", "save_connection", "save_company_accounts",
		               "save_features", "fetch_nexus"):
			self.assertIn(method, js)

	def test_save_features_payload_key_is_not_flags(self):
		"""Regression guard for the save_features(flags=...) trap (see
		test_parameter_is_not_named_flags_or_ignore_permissions): the payload
		key sent to the server must match the server's actual parameter name,
		company_flags - "flags" is silently stripped by frappe.call() on every
		real request, so this has to stay in lockstep on both ends."""
		js = self._js()
		save_features_call = js.split('_save_features()')[1].split("\n\t}")[0]
		self.assertIn("company_flags", save_features_call)
		self.assertIn('this._call("save_features", { company_flags })', save_features_call)

	def test_connect_step_uses_native_link_password_controls(self):
		"""API Mode moved off a native Select onto a hand-built segmented
		toggle (see test_connect_mode_is_a_segmented_toggle) - Company/token
		stay real frappe controls."""
		js = self._js()
		self.assertIn('fieldtype: "Link", fieldname: "company"', js)
		self.assertIn('fieldtype: "Password", fieldname: "token"', js)
		# Continue is gated on at least one successful test, not just field presence.
		self.assertIn("_sync_connect_gate", js)
		self.assertIn("tested", js)

	def test_connect_step_has_api_log_toggle_and_retention(self):
		"""The Connect step previously had no way to enable/disable API Logs,
		even though save_connection/get_setup_state already supported
		enable_taxjar_logging and log_retention_days end to end. "Switch" (a
		real pill toggle shipped in frappe core, controls/switch.js), not
		"Check" - a hand-rolled CSS checkbox-as-switch rendered as a broken
		grey ring in practice."""
		js = self._js()
		self.assertIn('fieldtype: "Switch", fieldname: "enable_taxjar_logging"', js)
		self.assertIn('fieldtype: "Int", fieldname: "log_retention_days"', js)
		# Retention only means anything once logging is on.
		self.assertIn("syncRetentionVisibility", js)

	def test_retention_field_visible_on_initial_load_when_logging_already_on(self):
		"""enableLogging.set_value() resolves asynchronously (frappe.run_serially),
		so calling syncRetentionVisibility() (which reads get_value()) right after
		it could still see the pre-set value - the retention field only ever
		appeared on a real toggle (a genuine click, synchronous), never on initial
		load with logging already enabled from a previous session. The initial
		visibility must be driven by the already-known state value instead."""
		js = self._js()
		render_connect = js.split("_render_connect() {")[1].split("\n\t_add_credential_card")[0]
		self.assertIn("$retentionField.toggle(!!s.enable_taxjar_logging);", render_connect)
		# The initial toggle must not be the same call used for later, real
		# toggle events - that call is fine to read get_value() from since it's
		# driven by a synchronous DOM change event.
		self.assertNotIn("syncRetentionVisibility();\n", render_connect)

	def test_save_connect_sends_logging_fields(self):
		js = self._js()
		save_connect = js.split("_save_connect() {")[1].split("\n\t}")[0]
		self.assertIn("enable_taxjar_logging", save_connect)
		self.assertIn("log_retention_days", save_connect)

	def test_review_shows_api_log_status(self):
		"""Two separate pills - "Enabled" and "N day(s) retention" - not one
		combined "On · retain Nd" string."""
		js = self._js()
		self.assertIn('__("API Logs")', js)
		self.assertIn('__("Enabled")', js)
		self.assertIn(
			'__("{0} {1} retention", [retentionDays, retentionDays === 1 ? __("day") : __("days")])', js
		)

	def test_gated_continue_stays_clickable_and_explains_itself(self):
		"""A native `disabled` button eats clicks silently — Connect's gate must
		use a CSS-only look-disabled state so a click can still explain what's
		missing, instead of appearing to do nothing."""
		js = self._js()
		self.assertIn("_set_next_gated", js)
		self.assertIn("this._nextGated", js)
		self.assertIn(
			'__("Test the connection for {0} (or remove it) before continuing.", [untested.company])', js
		)
		# _on_next() must check the gate before anything else, so a click while
		# gated always shows the message rather than silently trying to save.
		on_next = js.split("_on_next() {")[1].split("\n\t}\n")[0]
		self.assertIn("this._nextGated", on_next)
		self.assertIn("frappe.show_alert", on_next)

	def test_connect_gate_requires_every_company_tested_not_just_one(self):
		"""Regression guard for the actual bug report: gating on "at least one
		company tested" let an untested or failed credential ride along past
		Connect - invisible until the Nexus step later fetched nexus for every
		company in one request and hard-crashed with a raw 401 traceback the
		moment any one of them turned out to have a bad token. Every company
		with a name must test successfully now, and the gate message names the
		specific offending company with both ways out: fix its token and
		re-test, or remove it."""
		js = self._js()
		gate = js.split("_sync_connect_gate() {")[1].split("\n\t}\n")[0]
		self.assertIn("const withCompany = this._connectCards.filter((c) => c.company);", gate)
		self.assertIn("const untested = withCompany.find((c) => !c.tested);", gate)
		self.assertNotIn("c.controls.company.get_value()", gate)

	def test_connect_gate_reads_tracked_company_not_control_mid_flight(self):
		"""Regression guard: _sync_connect_gate() runs synchronously right after
		every card is added, including a restored card's companyControl.set_value()
		- which, like the mode label and retention-visibility bugs, resolves
		asynchronously. Reading company.get_value() (a read-only control at that
		point) right here could still see the pre-set value and wrongly gate
		Continue on an already-saved, already-tested credential. c.company (kept
		in sync directly on the entry, not re-derived from the control) must be
		used instead."""
		js = self._js()
		gate = js.split("_sync_connect_gate() {")[1].split("\n\t}\n")[0]
		self.assertIn("c.company", gate)
		self.assertNotIn("c.controls.company.get_value()", gate)

	def test_test_connection_button_is_not_plain_default(self):
		"""Visually elevated above a plain .btn-default so it reads as the
		action that actually matters before Continue unlocks."""
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		self.assertIn(".ts-test {", css)

	def test_connect_excludes_already_added_companies_via_get_query(self):
		js = self._js()
		self.assertIn("otherCompanies", js)
		self.assertIn("not in", js)

	# ── Connect step redesign v4: API Mode alone, Enable API logs moved below
	# API Credentials as a toggle-switch row, credential rows top-aligned
	# (per latest user feedback, superseding v3's shared top-row/divider) ──

	def test_connect_mode_is_a_segmented_toggle_not_a_dropdown(self):
		"""Sandbox/Live is a binary choice, shown as a two-button pill toggle
		instead of hiding one option behind a closed <select> dropdown -
		_render_mode_toggle is a hand-built stand-in exposing only the
		get_value/set_value subset the rest of the file calls on
		this.controls.mode, not a real frappe control (so no native
		show_description_on_click/InfoCard for it - the label+description
		are hand-authored instead, same as Enable API logs)."""
		js = self._js()
		self.assertNotIn('fieldtype: "Select", fieldname: "api_mode"', js)
		self.assertIn(
			'this.controls.mode = this._render_mode_toggle(this.$body.find(".ts-field-mode"), s.api_mode || "Live");',
			js,
		)
		toggle_fn = js.split("_render_mode_toggle($parent, initial) {")[1].split("\n\t}\n")[0]
		self.assertIn('data-value="Sandbox"', toggle_fn)
		self.assertIn('data-value="Live"', toggle_fn)
		self.assertIn("get_value: () => value,", toggle_fn)
		self.assertIn("set_value: (v) => { value = v; setActive(); },", toggle_fn)
		# A real click drives _on_mode_change() directly - no $input "change"
		# event to bind, since this isn't a real frappe control.
		self.assertIn("this._on_mode_change();", toggle_fn)

		render_connect = js.split("_render_connect() {")[1].split("\n\t\tthis.controls.mode")[0]
		mode_card = render_connect.split('<div class="ts-field-mode">')[0]
		self.assertIn('<label class="control-label" style="margin:0">', mode_card)
		self.assertIn('<span class="ts-reqd">*</span>', mode_card)
		self.assertIn('<p class="ts-fieldnote" style="margin:2px 0 0">', mode_card)
		self.assertIn("Live requests affect real filings.", mode_card)

	def test_connect_logs_card_sits_below_api_credentials(self):
		"""Enable API logs' position has moved a few times across rounds
		(shared row with API Mode -> below Credentials -> above Credentials)
		- settled below/after API Credentials, each card getting the same
		20px top margin for consistent spacing between all three cards."""
		js = self._js()
		render_connect = js.split("_render_connect() {")[1].split("\n\t\tthis.controls.mode")[0]
		self.assertLess(
			render_connect.index('class="ts-card-h ts-cred-heading"'),
			render_connect.index('class="ts-card ts-logtoggle"'),
		)
		self.assertIn('<div class="ts-card ts-logtoggle" style="margin-top:20px">', render_connect)

	def test_connect_logs_card_has_toggle_row_then_retention_row(self):
		"""One card, two rows divided by a border: the Switch control (its own
		native label+description, .ts-field-logging is its only content) on
		top, Retention (a plain label+description on the left, the day-count
		input on the right - the same label-left/control-right shape as
		.ts-mode-row) below it."""
		js = self._js()
		render_connect = js.split("_render_connect() {")[1].split("\n\t\tthis.controls.mode")[0]
		logtoggle = render_connect.split('<div class="ts-card ts-logtoggle"')[1].split("`);")[0]

		retention_row = logtoggle.split('<div class="ts-card-b ts-retention-row">')[1]
		self.assertIn('<label class="control-label" style="margin:0">${__("Retention")}</label>', retention_row)
		self.assertIn("Older logs are deleted automatically.", retention_row)
		self.assertIn('class="ts-retention-wrap"', retention_row)

		self.assertLess(
			logtoggle.index('class="ts-field-logging"'),
			logtoggle.index('class="ts-card-b ts-retention-row"'),
		)

		logging_control = js.split("this.controls.enableLogging = frappe.ui.form.make_control({")[1].split("});")[0]
		self.assertIn('fieldtype: "Switch", fieldname: "enable_taxjar_logging"', logging_control)
		self.assertIn('label: __("Enable API logs")', logging_control)
		self.assertIn(
			'description: __("Records API requests, responses, and errors in TaxJar API Log.")', logging_control
		)

	def test_connect_logs_toggle_uses_frappe_core_switch_control(self):
		"""fieldtype "Switch" (frappe.ui.form.ControlSwitch, controls/switch.js)
		is a real pill toggle already shipped and styled in frappe core
		(common/controls.scss's .switch-control/.switch-visual/.switch-thumb,
		already part of the desk CSS bundle - no extra CSS/import needed
		here) - not a hand-rolled CSS checkbox, which rendered as a broken
		grey ring in practice, and not frappe-ui's Vue Switch component
		either (confirmed not feasible from this plain, unbundled page
		script - frappe-ui isn't even installed in this bench, and Vue isn't
		exposed as a global here)."""
		js = self._js()
		self.assertNotIn('input[type="checkbox"]', js)
		self.assertNotIn("appearance: none", js)

		import os
		switch_path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "..", "..", "..", "frappe", "frappe",
			"public", "js", "frappe", "form", "controls", "switch.js"))
		self.assertTrue(os.path.isfile(switch_path), "frappe core's Switch control must exist for fieldtype: \"Switch\" to work")

		css = open(os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))).read()
		self.assertNotIn('input[type="checkbox"]', css)

	def test_connect_retention_row_has_border_divider_and_hides_as_one_unit(self):
		""".ts-retention-row gets a border-top (the divider between the two
		rows), and syncRetentionVisibility toggles the whole row - hiding it
		wholesale rather than leaving "Retention / Older logs are deleted
		automatically" visible with no functioning input when logging is off."""
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		retention_row_rule = css.split(".taxjar-setup .ts-retention-row {")[1].split("}")[0]
		self.assertIn("border-top: 1px solid var(--border-color)", retention_row_rule)

		js = self._js()
		self.assertIn('const $retentionField = this.$body.find(".ts-retention-row");', js)

	def test_info_popover_is_click_to_show_not_hover(self):
		"""The info popover is built and shown from a real click handler
		(delegated once in _build_shell, not rebound per render), not a CSS
		:hover rule - works the same on touch and desktop."""
		js = self._js()
		self.assertIn('this.$root.on("click", ".ts-info-btn"', js)
		self.assertIn("_toggle_info_popover", js)
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		self.assertNotIn(":hover .ts-info-pop", css)
		self.assertNotIn(".ts-info-pop:hover", css)

	def test_info_popover_toggles_closed_on_second_click_of_same_button(self):
		js = self._js()
		fn = js.split("_toggle_info_popover($trigger) {")[1].split("\n\t}\n")[0]
		self.assertIn('const reopening = $trigger.hasClass("ts-info-btn-active");', fn)
		self.assertIn("if (reopening) return;", fn)

	def test_connect_retention_default_is_fifteen_and_shows_pluralised_unit(self):
		"""Default is 15 days, and the "day"/"days" unit word is a separate
		visible element next to the input (not baked into the input itself),
		pluralised off the live value - just the bare unit word, not "day/days
		retention", since the row now has its own "Retention" label doing that
		job (saying it twice on one row read redundant)."""
		js = self._js()
		self.assertIn(
			"this.controls.logRetention.set_value(s.log_retention_days != null ? s.log_retention_days : 15);", js
		)
		self.assertIn("ts-retention-unit", js)
		self.assertIn(
			'$retentionUnit.text(cint(this.controls.logRetention.get_value()) === 1 ? __("day") : __("days"));',
			js,
		)

		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "doctype", "taxjar_settings", "taxjar_settings.json"))
		import json
		with open(path) as f:
			meta = json.load(f)
		field = next(f for f in meta["fields"] if f["fieldname"] == "log_retention_days")
		self.assertEqual(field["default"], "15")

	def test_connect_credentials_section_is_one_collapsible_heading(self):
		"""Replaces the old per-company collapsible header with a single,
		generic "API Credentials" heading for the whole section - clicking it
		toggles every row at once, not one company at a time."""
		js = self._js()
		render_connect = js.split("_render_connect() {")[1].split("\n\t\tthis.controls.mode")[0]
		self.assertIn('class="ts-card-h ts-cred-heading"', render_connect)
		self.assertIn('__("API Credentials")', render_connect)
		self.assertIn("_set_creds_expanded", js)
		self.assertIn(
			'this.$body.find(".ts-cred-heading").on("click", () => this._set_creds_expanded(!this._credsExpanded));',
			js,
		)

	def test_connect_add_company_button_lives_inside_the_heading(self):
		"""'+ Add another company' sits inside .ts-cred-heading itself, not
		below the row list - and stopPropagation keeps clicking it from also
		toggling the heading's own collapse."""
		js = self._js()
		render_connect = js.split("_render_connect() {")[1].split("\n\t\tthis.controls.mode")[0]
		heading = render_connect.split('<div class="ts-card-h ts-cred-heading">')[1].split("</div>")[0]
		self.assertIn("ts-add-cred", heading)
		add_click = js.split('this.$body.find(".ts-add-cred").on("click", (e) => {')[1].split("\n\t\t});")[0]
		self.assertIn("e.stopPropagation();", add_click)
		self.assertIn("this._add_credential_card({ company: null, token_last4: null });", add_click)
		self.assertIn("this._set_creds_expanded(true);", add_click)

	def test_connect_credentials_section_starts_expanded(self):
		"""Required step - starting collapsed would just cost an extra click
		on every single visit."""
		js = self._js()
		self.assertIn("this._credsExpanded = true;", js)

	def test_connect_credential_rows_are_flat_with_dividers_not_accordion_cards(self):
		"""Each company is one plain .ts-cred-row (Company / Live token /
		action slot / remove), always fully visible once the section is
		expanded - not its own collapsible card."""
		js = self._js()
		add_card = js.split("_add_credential_card(cred) {")[1].split("\n\t_render_cred_action")[0]
		self.assertIn('<div class="ts-cred-row">', add_card)
		self.assertNotIn("ts-acc-row", add_card)
		self.assertNotIn("ts-acc-head", add_card)

		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		row_rule = css.split(".taxjar-setup .ts-cred-row {")[1].split("}")[0]
		self.assertIn("align-items: flex-start", row_rule)
		self.assertIn(".ts-cred-row + .ts-cred-row { padding-top: 12px; border-top:", css)

	def test_connect_credential_row_labels_top_align_regardless_of_field_height(self):
		"""Regression guard: bottom-aligning the row (the previous approach)
		visibly staggered the Company/Live token labels whenever one field
		grew taller than the other (e.g. a per-field description) - reported
		as "alignment breaks after save". Top-aligning is what stays correct
		regardless of which field ends up taller. The action slot/remove
		button opt out via align-self: flex-end so they line up with the
		bottom of the input boxes themselves (Company and Live token are now
		equal height) rather than align-self: center, which centered against
		the full label+input span and floated up near the label line -
		reported as the pill/remove not lining up with the inputs."""
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		self.assertIn(
			".taxjar-setup .ts-cred-row .ts-cred-action { flex: none; display: flex; align-items: center; gap: 6px; align-self: flex-end; }",
			css,
		)
		self.assertIn(".taxjar-setup .ts-cred-row .ts-card-remove { align-self: flex-end; }", css)

	def test_connect_token_field_has_no_per_field_description(self):
		""""Leave blank to keep the saved token." was the extra description
		line that made an already-saved credential's Live token field taller
		than Company - removed rather than compensated for, since the
		placeholder ("...ending in 2429") already conveys there's a stored
		token."""
		js = self._js()
		add_card = js.split("_add_credential_card(cred) {")[1].split("\n\t_render_cred_action")[0]
		token_control = add_card.split("const tokenControl = frappe.ui.form.make_control({")[1].split("});")[0]
		self.assertNotIn("description", token_control)
		self.assertNotIn("Leave blank to keep the saved token.", js)
		on_mode_change = js.split("_on_mode_change() {")[1].split("\n\t}\n")[0]
		self.assertNotIn("tokenCtrl.df.description", on_mode_change)

	def test_connect_test_connection_pill_replaces_button_on_result(self):
		"""_render_cred_action cycles the action slot through an idle "Connect"
		button, a green Success pill, and a yellow Retry pill - the pill
		replaces the button rather than sitting beside it."""
		js = self._js()
		fn = js.split("_render_cred_action(entry) {")[1].split("\n\t}\n")[0]
		self.assertIn('$action.html(`<span class="ts-chip ok ts-cred-pill">', fn)
		self.assertIn('$action.html(`<button type="button" class="btn btn-default ts-test">${__("Connect")}</button>`);', fn)
		self.assertIn('<span class="ts-chip retry ts-cred-pill">${__("Retry")}</span>', fn)

	def test_connect_failed_state_shows_warning_icon_outside_the_pill(self):
		"""The warning icon is a sibling of the Retry pill, not nested inside
		it - so clicking the icon (to see the failure reason) is never also
		routed through the pill's own click-to-retry handler."""
		js = self._js()
		fn = js.split("_render_cred_action(entry) {")[1].split("\n\t}\n")[0]
		failed_branch = fn.split("else if (entry.lastError) {")[1].split("} else {")[0]
		# The info button call comes before the pill span, and is not nested
		# inside its <span> tag.
		self.assertLess(
			failed_branch.index("this._info_btn_html(entry.lastError)"),
			failed_branch.index('<span class="ts-chip retry ts-cred-pill">'),
		)
		self.assertNotIn(
			'<span class="ts-chip retry ts-cred-pill">${this._info_btn_html', failed_branch
		)
		self.assertIn("this._info_btn_html(entry.lastError)", fn)
		# Triangle-alert (a warning icon), not the plain info icon used
		# elsewhere - the failure state reads as a warning, not neutral info.
		# "md" (20px), not "xs" (12px) - too small to read as a warning at a
		# glance next to normal body text.
		info_btn_fn = js.split("_info_btn_html(text) {")[1].split("\n\t}\n")[0]
		self.assertIn('frappe.utils.icon("triangle-alert", "md")', info_btn_fn)
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		info_btn_rule = css.split(".taxjar-setup .ts-info-btn {")[1].split("}")[0]
		self.assertIn("width: 26px", info_btn_rule)
		self.assertIn("height: 26px", info_btn_rule)

	def test_retry_pill_is_visually_distinct_yellow_not_the_neutral_warn_chip(self):
		""".warn stays the neutral "in progress" chip (the Connect spinner and
		the unrelated Nexus fetch-status chip both still use it) - the new
		Retry state needed to be actually yellow, so it gets its own .retry
		class rather than repurposing .warn and silently recolouring those
		other two spinners too."""
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		retry_rule = css.split(".taxjar-setup .ts-chip.retry {")[1].split("}")[0]
		self.assertIn("yellow", retry_rule)
		warn_rule = css.split(".taxjar-setup .ts-chip.warn {")[1].split("}")[0]
		self.assertNotIn("yellow", warn_rule)

	def test_connect_button_renamed_to_connect(self):
		"""The idle action-slot button reads "Connect", not "Test connection"."""
		js = self._js()
		fn = js.split("_render_cred_action(entry) {")[1].split("\n\t}\n")[0]
		self.assertIn('${__("Connect")}</button>', fn)
		self.assertNotIn("Test connection", fn)

	def test_connect_pill_click_retests(self):
		"""Clicking either result pill re-runs the test - the same handler as
		the idle button, just bound to whichever element ends up in the slot."""
		js = self._js()
		fn = js.split("_render_cred_action(entry) {")[1].split("\n\t}\n")[0]
		self.assertIn(
			'$action.find(".ts-test, .ts-cred-pill").on("click", () => this._test_connection(entry));', fn
		)

	def test_connect_edits_fall_back_through_reset_cred_status_to_idle_button(self):
		"""Editing company or token after a test must clear lastError too, not
		just tested - otherwise _render_cred_action would still show the old
		Failed pill instead of falling back to the idle button."""
		js = self._js()
		fn = js.split("_reset_cred_status(entry) {")[1].split("\n\t}\n")[0]
		self.assertIn("entry.lastError = null;", fn)
		self.assertIn("this._render_cred_action(entry);", fn)

	def test_connect_css_supports_credentials_section_and_rows(self):
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		self.assertIn(".ts-cred-heading { cursor: pointer; }", css)
		self.assertIn(".ts-acc-chevron-open { transform: rotate(90deg); }", css)
		self.assertIn(".ts-cred-pill { cursor: pointer; }", css)

	def test_accounts_step_uses_company_scoped_account_links(self):
		js = self._js()
		self.assertIn('fieldtype: "Link", fieldname: "tax_account_head", options: "Account"', js)
		self.assertIn('fieldtype: "Link", fieldname: "shipping_account_head", options: "Account"', js)
		self.assertIn("company: cred.company", js)

	def test_features_step_has_no_master_switch(self):
		"""taxjar_enabled is a TaxJar Settings form field, deliberately left alone
		by this wizard — no control for it, no lock/grey behaviour tied to it."""
		js = self._js()
		self.assertNotIn('fieldname: "taxjar_enabled"', js)
		self.assertNotIn("_sync_master_lock", js)
		self.assertIn('fieldtype: "Check", fieldname: "calculate"', js)
		self.assertIn('fieldtype: "Check", fieldname: "file"', js)

	def test_nexus_step_has_fetch_action_and_grouped_render(self):
		js = self._js()
		self.assertIn("_fetch_nexus", js)
		self.assertIn("_render_nexus_groups", js)

	def test_nexus_note_uses_framework_alert_warning_not_bespoke_banner(self):
		"""A locally-invented .ts-banner box (custom border/background) reused
		the framework's own .alert.alert-warning instead - the same themed,
		light/dark-aware component used elsewhere in the desk (e.g. the
		doctype permissions "customized" banner), rather than reinventing a
		yellow box with hand-picked colors."""
		js = self._js()
		self.assertIn('class="alert alert-warning ts-nexusnote" role="alert"', js)
		self.assertNotIn("ts-banner", js)

	def test_token_label_reads_tracked_mode_not_control_mid_flight(self):
		"""_modeIsLive is a plain instance flag set directly from state, read
		by the token label instead of this.controls.mode.get_value() - a
		holdover from when mode was a real frappe control whose set_value()
		resolved asynchronously (frappe.run_serially), so a synchronous read
		right after could still see the pre-set value. The segmented toggle
		that replaced it has no such async step, but _modeIsLive is still
		needed for a different reason: it must exist before any credential
		card is built (see _add_credential_card), not just after a real
		change event."""
		js = self._js()
		self.assertIn("this._modeIsLive = (s.api_mode || \"Live\") === \"Live\";", js)
		self.assertIn('label: this._modeIsLive ? __("Live token") : __("Sandbox token")', js)
		self.assertNotIn('label: this.controls.mode.get_value() === "Live"', js)
		# _on_mode_change is a real change event, so it's safe to read the control
		# there - but it must also keep _modeIsLive in sync for any card added later.
		on_mode_change = js.split("_on_mode_change() {")[1].split("\n\t}\n")[0]
		self.assertIn("this._modeIsLive = live;", on_mode_change)

	def test_credential_card_starts_saved_shows_success_pill_when_already_connected(self):
		"""Re-running the guided setup for a company with a stored token must not
		visually demand a re-test of a connection nothing has changed about -
		_render_cred_action reads entry.tested straight into the same "Success"
		pill a fresh test produces, not a separate "Saved" wording that reads
		as less certain and invites re-testing anyway."""
		js = self._js()
		self.assertIn("const alreadySaved = !!cred.token_last4;", js)
		self.assertIn("tested: alreadySaved", js)
		fn = js.split("_render_cred_action(entry) {")[1].split("\n\t}\n")[0]
		self.assertIn('$action.html(`<span class="ts-chip ok ts-cred-pill"><span class="ts-chip-dot"></span> ${__("Success")}</span>`);', fn)

	def test_restoring_existing_company_does_not_fire_onchange_reset(self):
		"""Regression guard: frappe's set_value() invokes df.onchange itself as
		part of setting the value, not only on real user input. Populating an
		already-saved card's Company field with its existing value (so the
		field isn't blank) therefore fired the "company changed" reset and
		immediately wiped the "Success" pill _add_credential_card had just set,
		straight back to the idle button - the exact bug reported. A guard must
		skip that reset exactly once, for the initial programmatic restore."""
		js = self._js()
		add_card = js.split("_add_credential_card(cred) {")[1].split("\n\t_render_cred_action")[0]
		self.assertIn("let restoringInitialCompany = !!cred.company;", add_card)
		onchange = add_card.split("companyControl.df.onchange = () => {")[1].split("};")[0]
		self.assertIn("if (restoringInitialCompany)", onchange)
		self.assertIn("restoringInitialCompany = false;", onchange)
		self.assertIn("return;", onchange)

	def test_editing_token_forces_a_fresh_test(self):
		"""The converse of the above: once the user actually types into the token
		field, the previously-saved-and-trusted state no longer applies."""
		js = self._js()
		self.assertIn('tokenControl.$input.on("input"', js)
		on_input = js.split('tokenControl.$input.on("input", () => {')[1].split("});")[0]
		self.assertIn("entry.tested = false;", on_input)
		self.assertIn("this._reset_cred_status(entry);", on_input)
		self.assertIn("this._sync_connect_gate();", on_input)

	def test_save_methods_reload_state_before_advancing(self):
		"""Every save-then-advance step re-fetches state so the wizard stays
		resumable/consistent instead of trusting a locally-guessed delta — once
		on initial load, once each from the three save steps, and once after
		removing an already-saved company."""
		js = self._js()
		self.assertIn("_save_connect", js)
		self.assertIn("_save_accounts", js)
		self.assertIn("_save_features", js)
		self.assertEqual(js.count("this._reload_state()"), 5)

	def test_review_summarises_connection_accounts_features_nexus(self):
		js = self._js()
		self.assertIn("_render_review", js)
		for label in ('__("Connection")', '__("Accounts")', '__("Features")', '__("Nexus")'):
			self.assertIn(label, js)

	def test_connect_card_has_remove_action_wired_to_server_api(self):
		js = self._js()
		self.assertIn("_remove_credential_card", js)
		self.assertIn("remove_company", js)
		self.assertIn("frappe.confirm", js)

	def test_token_control_disables_password_strength_meter(self):
		"""A TaxJar token isn't a password being created — the strength-meter
		request it fires per keystroke doesn't apply and errored in practice."""
		js = self._js()
		self.assertIn("disable_password_checks", js)

	def test_token_control_shows_masked_placeholder_when_stored(self):
		js = self._js()
		self.assertIn("placeholder: cred.token_last4", js)

	def test_nexus_step_auto_fetches_on_open(self):
		"""Opening the Nexus step pulls fresh data immediately — no need to
		remember to click Fetch just to see current nexus."""
		js = self._js()
		render_nexus = js.split("_render_nexus(")[1].split("\n\t_render_nexus_groups(")[0]
		self.assertIn("this._fetch_nexus()", render_nexus)

	def test_fetch_nexus_status_pluralises_region_and_company(self):
		"""1 region/1 company must not read "1 regions across 1 companies"."""
		js = self._js()
		self.assertIn('const regionWord = total === 1 ? __("region") : __("regions");', js)
		self.assertIn('const companyWord = companiesN === 1 ? __("company") : __("companies");', js)
		self.assertIn(
			'__("Fetched {0} {1} across {2} {3}", [total, regionWord, companiesN, companyWord])', js
		)

	def test_review_nexus_row_pluralises_company(self):
		js = self._js()
		self.assertIn(
			'__("{0} across {1} {2}", [totalNexus, nexusCompaniesN, nexusCompaniesN === 1 ? __("company") : __("companies")])',
			js,
		)

	def test_review_has_no_taxjar_enabled_row_and_uses_green_badges(self):
		"""The master switch isn't managed by this wizard, so Review must not
		claim to report its state; Live mode and the nightly refresh cadence
		get Frappe's native green indicator-pill instead of plain text."""
		js = self._js()
		self.assertNotIn('__("TaxJar")', js)
		self.assertIn("indicator-pill green", js)
		self.assertIn('__("Auto-Refresh")', js)
		self.assertIn('__("Daily at midnight")', js)

	def test_daily_at_midnight_pill_matches_other_pills_styling(self):
		"""Regression guard: every other Review pill (Live, Enabled, N days
		retention) sits nested one level inside the row's value span, so
		.ts-kv > span:last-child's bold override lands on that wrapper, not the
		pill - .indicator-pill's own "regular" weight wins. "Daily at midnight"
		was the one pill placed as a *direct* child of .ts-kv, so it alone took
		the bold override directly and rendered inconsistently bolder than
		every other pill on the same screen."""
		js = self._js()
		self.assertIn(
			'<span>${__("Auto-Refresh")}</span><span><span class="indicator-pill green no-indicator-dot">${__("Daily at midnight")}</span></span>',
			js,
		)

	def test_review_accounts_stack_company_and_detail_on_separate_lines(self):
		js = self._js()
		self.assertIn("ts-acc-company", js)
		self.assertIn("ts-acc-detail", js)

	def test_review_accounts_label_tax_and_shipping_ledgers_on_separate_lines(self):
		"""Two bare account names side by side ("X · Y") gave no indication of
		which was the tax ledger and which was the shipping ledger; each now
		gets its own labelled line rather than sharing one."""
		js = self._js()
		account_rows = js.split("const accountRows = companies.map((c) => `")[1].split("`).join")[0]
		self.assertEqual(account_rows.count('<div class="ts-acc-detail">'), 2)
		self.assertIn('__("Tax Ledger")}: ${frappe.utils.escape_html(c.tax_account_head', account_rows)
		self.assertIn('__("Shipping Ledger")}: ${frappe.utils.escape_html(c.shipping_account_head', account_rows)

	def test_welcome_step_button_says_continue_not_save(self):
		"""Nothing is saved on the Welcome step (no form fields) — its button
		must not claim to "Save"."""
		js = self._js()
		welcome_step = js.split('key: "welcome"')[1].split("},")[0]
		self.assertIn('nextLabel: __("Continue")', welcome_step)

	def test_check_sub_items_are_not_card_scoped_to_top_level_li(self):
		"""Regression guard: .ts-check li (no `>`) is a descendant selector, so
		it would also match .ts-check-sub's own <li>s two levels down and wrongly
		card-ify "Sales Tax Ledger" / "Shipping Charges Ledger" as bordered boxes instead of
		plain indented bullets."""
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		self.assertIn(".ts-check > li {", css)
		self.assertNotIn(".ts-check li {", css)

	def test_kv_bold_rule_is_child_scoped_not_descendant(self):
		"""Same class of bug as .ts-check li above: .ts-kv span:last-child (no
		`>`) is a descendant selector, so for a row whose value holds two
		stacked indicator-pills (API Logs: "Enabled" + "N days retention") it
		would also match the second pill (itself a last-child of its own
		wrapper) and bold only that one, inconsistent with the first."""
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		self.assertIn(".ts-kv > span:first-child {", css)
		self.assertIn(".ts-kv > span:last-child {", css)
		self.assertNotIn(".ts-kv span:first-child {", css)
		self.assertNotIn(".ts-kv span:last-child {", css)

	def test_card_body_zeroes_form_group_margin_to_avoid_double_gap(self):
		"""Bootstrap's default .form-group margin-bottom (15px) stacks on top of
		.ts-card-b's own flex gap (12px), doubling the visual gap between
		stacked fields (e.g. Company -> Live token) to ~27px for no reason."""
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		self.assertIn(".ts-card-b .form-group { margin-bottom: 0; }", css)

	def test_css_does_not_clip_link_dropdowns_and_caps_card_width(self):
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.css"))
		css = open(path).read()
		# .ts-card holds Link controls (Company/Account) whose search dropdown can
		# extend past the card's own box — it must not clip them. (.ts-check li,
		# which has no dropdown content, is free to clip its own hover background.)
		card_rule = css.split(".taxjar-setup .ts-card {")[1].split("}")[0]
		self.assertNotIn("overflow: hidden", card_rule)
		self.assertIn("minmax(280px, 420px)", css)


# ── Regional: United States — ledger auto-select & tax template sync ────────


class _FakeTemplateRow:
	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)

	def get(self, field):
		return getattr(self, field, None)

	def set(self, field, value):
		setattr(self, field, value)


class _FakeTemplateDoc:
	"""Minimal stand-in for a Sales Taxes and Charges Template doc."""
	def __init__(self, name, taxes=None, is_default=0):
		self.name = name
		self.taxes = taxes or []
		self.is_default = is_default
		self.saved = False

	def append(self, field, data):
		if field == "taxes":
			self.taxes.append(_FakeTemplateRow(**data))

	def save(self, ignore_permissions=False):
		self.saved = True

	def insert(self, ignore_permissions=False):
		self.saved = True


def _fake_company_lookup(abbr="TC", cost_center="Main - TC"):
	"""Side_effect for frappe.db.get_value("Company", company, field)."""
	def _get(doctype, company, field):
		return {"abbr": abbr, "cost_center": cost_center}.get(field)
	return _get


REGIONAL = "taxjar_integration.taxjar_integration.regional.united_states"


class TestResolveDefaultLedgers(UnitTestCase):

	def test_matches_by_account_number_first(self):
		def fake_get_value(doctype, filters):
			if filters.get("account_number") == "21400":
				return "Sales Tax Payable - TC"
			if filters.get("account_number") == "41200":
				return "Shipping and Freight Income - TC"
			return None

		with patch(f"{REGIONAL}.frappe.db.get_value", side_effect=fake_get_value):
			result = resolve_default_ledgers("Test Co")

		self.assertEqual(result["tax_account_head"], "Sales Tax Payable - TC")
		self.assertEqual(result["shipping_account_head"], "Shipping and Freight Income - TC")

	def test_falls_back_to_account_name_when_number_not_found(self):
		def fake_get_value(doctype, filters):
			if "account_number" in filters:
				return None
			if filters.get("account_name") == "Sales Tax Payable":
				return "Custom Sales Tax - TC"
			if filters.get("account_name") == "Shipping and Freight Income":
				return "Custom Freight - TC"
			return None

		with patch(f"{REGIONAL}.frappe.db.get_value", side_effect=fake_get_value):
			result = resolve_default_ledgers("Test Co")

		self.assertEqual(result["tax_account_head"], "Custom Sales Tax - TC")
		self.assertEqual(result["shipping_account_head"], "Custom Freight - TC")

	def test_returns_none_when_neither_found(self):
		"""Non-standard chart of accounts: no account, no exception."""
		with patch(f"{REGIONAL}.frappe.db.get_value", return_value=None):
			result = resolve_default_ledgers("Test Co")

		self.assertIsNone(result["tax_account_head"])
		self.assertIsNone(result["shipping_account_head"])

	def test_lookup_is_company_scoped(self):
		def fake_get_value(doctype, filters):
			if filters.get("company") == "Test Co" and filters.get("account_number") == "21400":
				return "Sales Tax Payable - TC"
			return None

		with patch(f"{REGIONAL}.frappe.db.get_value", side_effect=fake_get_value):
			result = resolve_default_ledgers("Other Co")

		self.assertIsNone(result["tax_account_head"])


class TestEnsureCompanyLedgersAndTemplate(UnitTestCase):

	def _row(self, company="Test Co", tax_account_head=None, shipping_account_head=None, calculate_tax=0):
		row = MagicMock()
		row.name = "row-1"
		row.company = company
		row.tax_account_head = tax_account_head
		row.shipping_account_head = shipping_account_head
		row.taxjar_calculate_tax = calculate_tax
		return row

	def test_backfills_both_blank_fields(self):
		row = self._row()
		resolved = {"tax_account_head": "Sales Tax Payable - TC", "shipping_account_head": "Shipping and Freight Income - TC"}

		with patch(f"{REGIONAL}.resolve_default_ledgers", return_value=resolved), \
		     patch(f"{REGIONAL}.frappe.db.set_value") as mock_set, \
		     patch(f"{REGIONAL}._upsert_tax_template") as mock_upsert, \
		     patch(f"{REGIONAL}._disable_default_us_templates") as mock_disable:
			ensure_company_ledgers_and_template(row)

		mock_set.assert_called_once_with("TaxJar Company Config", "row-1", resolved)
		self.assertEqual(row.tax_account_head, resolved["tax_account_head"])
		self.assertEqual(row.shipping_account_head, resolved["shipping_account_head"])
		mock_upsert.assert_called_once_with("Test Co", resolved["tax_account_head"], is_default=False)
		mock_disable.assert_not_called()

	def test_does_not_overwrite_existing_ledger_value(self):
		"""An admin's own choice is never overwritten, even if it differs from what
		the standard-CoA lookup would resolve."""
		row = self._row(tax_account_head="Manual Tax - TC", shipping_account_head="Manual Freight - TC")
		resolved = {"tax_account_head": "Sales Tax Payable - TC", "shipping_account_head": "Shipping and Freight Income - TC"}

		with patch(f"{REGIONAL}.resolve_default_ledgers", return_value=resolved), \
		     patch(f"{REGIONAL}.frappe.db.set_value") as mock_set, \
		     patch(f"{REGIONAL}._upsert_tax_template") as mock_upsert:
			ensure_company_ledgers_and_template(row)

		mock_set.assert_not_called()
		mock_upsert.assert_called_once_with("Test Co", "Manual Tax - TC", is_default=False)

	def test_backfills_only_the_blank_field(self):
		row = self._row(tax_account_head="Manual Tax - TC", shipping_account_head=None)
		resolved = {"tax_account_head": "Sales Tax Payable - TC", "shipping_account_head": "Shipping and Freight Income - TC"}

		with patch(f"{REGIONAL}.resolve_default_ledgers", return_value=resolved), \
		     patch(f"{REGIONAL}.frappe.db.set_value") as mock_set, \
		     patch(f"{REGIONAL}._upsert_tax_template"):
			ensure_company_ledgers_and_template(row)

		mock_set.assert_called_once_with(
			"TaxJar Company Config", "row-1", {"shipping_account_head": resolved["shipping_account_head"]}
		)
		self.assertEqual(row.tax_account_head, "Manual Tax - TC")
		self.assertEqual(row.shipping_account_head, resolved["shipping_account_head"])

	def test_leaves_blank_and_skips_template_when_neither_resolves(self):
		row = self._row()
		resolved = {"tax_account_head": None, "shipping_account_head": None}

		with patch(f"{REGIONAL}.resolve_default_ledgers", return_value=resolved), \
		     patch(f"{REGIONAL}.frappe.db.set_value") as mock_set, \
		     patch(f"{REGIONAL}._upsert_tax_template") as mock_upsert, \
		     patch(f"{REGIONAL}._disable_default_us_templates") as mock_disable:
			ensure_company_ledgers_and_template(row)

		mock_set.assert_not_called()
		mock_upsert.assert_not_called()
		mock_disable.assert_not_called()

	def test_gates_is_default_on_taxjar_calculate_tax(self):
		row = self._row(tax_account_head="Tax - TC", calculate_tax=1)
		resolved = {"tax_account_head": None, "shipping_account_head": None}

		with patch(f"{REGIONAL}.resolve_default_ledgers", return_value=resolved), \
		     patch(f"{REGIONAL}._upsert_tax_template") as mock_upsert, \
		     patch(f"{REGIONAL}._disable_default_us_templates") as mock_disable:
			ensure_company_ledgers_and_template(row)

		mock_upsert.assert_called_once_with("Test Co", "Tax - TC", is_default=True)
		mock_disable.assert_called_once_with("Test Co")

	def test_does_not_disable_defaults_when_calculate_tax_off(self):
		"""Ledgers/template stay in sync even with tax calc off, but ERPNext's own
		defaults are only ever disabled once TaxJar's template is actually active."""
		row = self._row(tax_account_head="Tax - TC", calculate_tax=0)
		resolved = {"tax_account_head": None, "shipping_account_head": None}

		with patch(f"{REGIONAL}.resolve_default_ledgers", return_value=resolved), \
		     patch(f"{REGIONAL}._upsert_tax_template") as mock_upsert, \
		     patch(f"{REGIONAL}._disable_default_us_templates") as mock_disable:
			ensure_company_ledgers_and_template(row)

		mock_upsert.assert_called_once_with("Test Co", "Tax - TC", is_default=False)
		mock_disable.assert_not_called()


class TestUpsertTaxTemplate(UnitTestCase):

	def _patch_company_lookups(self, abbr="TC", cost_center="Main - TC"):
		return patch(f"{REGIONAL}.frappe.db.get_value", side_effect=_fake_company_lookup(abbr, cost_center))

	def test_creates_new_template_with_single_actual_row(self):
		created = {}

		def fake_get_doc(arg):
			created["dict"] = arg
			doc = _FakeTemplateDoc(name=f"{arg['title']} - TC", taxes=list(arg["taxes"]), is_default=arg["is_default"])
			created["doc"] = doc
			return doc

		with self._patch_company_lookups(), \
		     patch(f"{REGIONAL}.frappe.db.exists", return_value=False), \
		     patch(f"{REGIONAL}.frappe.get_doc", side_effect=fake_get_doc):
			name = _upsert_tax_template("Test Co", "Sales Tax Payable - TC", is_default=True)

		self.assertEqual(created["dict"]["doctype"], "Sales Taxes and Charges Template")
		self.assertEqual(created["dict"]["title"], TAXJAR_TEMPLATE_TITLE)
		self.assertEqual(created["dict"]["is_default"], 1)
		self.assertEqual(len(created["dict"]["taxes"]), 1)

		row = created["dict"]["taxes"][0]
		self.assertEqual(row["charge_type"], "Actual")
		self.assertEqual(row["account_head"], "Sales Tax Payable - TC")
		self.assertEqual(row["description"], TAXJAR_ROW_DESCRIPTION)
		self.assertEqual(row["cost_center"], "Main - TC")
		self.assertEqual(name, created["doc"].name)

	def test_no_shipping_row_is_ever_added(self):
		created = {}

		def fake_get_doc(arg):
			created["dict"] = arg
			return _FakeTemplateDoc(name="x", taxes=list(arg["taxes"]), is_default=arg["is_default"])

		with self._patch_company_lookups(), \
		     patch(f"{REGIONAL}.frappe.db.exists", return_value=False), \
		     patch(f"{REGIONAL}.frappe.get_doc", side_effect=fake_get_doc):
			_upsert_tax_template("Test Co", "Sales Tax Payable - TC", is_default=True)

		self.assertEqual(len(created["dict"]["taxes"]), 1)

	def test_updates_existing_template_account_head_when_changed(self):
		existing_row = _FakeTemplateRow(charge_type="Actual", account_head="Old Tax - TC",
			description=TAXJAR_ROW_DESCRIPTION, cost_center="Main - TC")
		doc = _FakeTemplateDoc(name="TaxJar Sales Tax - TC", taxes=[existing_row], is_default=1)

		with self._patch_company_lookups(), \
		     patch(f"{REGIONAL}.frappe.db.exists", return_value=True), \
		     patch(f"{REGIONAL}.frappe.get_doc", return_value=doc):
			_upsert_tax_template("Test Co", "New Tax - TC", is_default=True)

		self.assertEqual(doc.taxes[0].account_head, "New Tax - TC")
		self.assertTrue(doc.saved)

	def test_no_save_when_already_in_sync(self):
		"""Idempotency: a second sync with identical inputs makes zero writes."""
		existing_row = _FakeTemplateRow(charge_type="Actual", account_head="Tax - TC",
			description=TAXJAR_ROW_DESCRIPTION, cost_center="Main - TC")
		doc = _FakeTemplateDoc(name="TaxJar Sales Tax - TC", taxes=[existing_row], is_default=1)

		with self._patch_company_lookups(), \
		     patch(f"{REGIONAL}.frappe.db.exists", return_value=True), \
		     patch(f"{REGIONAL}.frappe.get_doc", return_value=doc):
			_upsert_tax_template("Test Co", "Tax - TC", is_default=True)

		self.assertFalse(doc.saved)

	def test_is_default_flip_triggers_save(self):
		existing_row = _FakeTemplateRow(charge_type="Actual", account_head="Tax - TC",
			description=TAXJAR_ROW_DESCRIPTION, cost_center="Main - TC")
		doc = _FakeTemplateDoc(name="TaxJar Sales Tax - TC", taxes=[existing_row], is_default=1)

		with self._patch_company_lookups(), \
		     patch(f"{REGIONAL}.frappe.db.exists", return_value=True), \
		     patch(f"{REGIONAL}.frappe.get_doc", return_value=doc):
			_upsert_tax_template("Test Co", "Tax - TC", is_default=False)

		self.assertEqual(doc.is_default, 0)
		self.assertTrue(doc.saved)


class TestDisableDefaultUsTemplates(UnitTestCase):

	def test_disables_matching_titles_only(self):
		existing = {"US ST 6%": "US ST 6% - TC", "US ST 4%": "US ST 4% - TC"}

		def fake_get_value(doctype, filters):
			return existing.get(filters.get("title"))

		with patch(f"{REGIONAL}.frappe.db.get_value", side_effect=fake_get_value), \
		     patch(f"{REGIONAL}.frappe.db.set_value") as mock_set:
			_disable_default_us_templates("Test Co")

		self.assertEqual(mock_set.call_count, 2)
		disabled_names = {c.args[1] for c in mock_set.call_args_list}
		self.assertEqual(disabled_names, {"US ST 6% - TC", "US ST 4% - TC"})
		for call in mock_set.call_args_list:
			self.assertEqual(call.args[2], {"is_default": 0, "disabled": 1})

	def test_noop_when_none_present(self):
		with patch(f"{REGIONAL}.frappe.db.get_value", return_value=None), \
		     patch(f"{REGIONAL}.frappe.db.set_value") as mock_set:
			_disable_default_us_templates("Test Co")

		mock_set.assert_not_called()

	def test_does_not_touch_similarly_named_custom_template(self):
		"""Only the three exact literal titles are ever matched - a user's own
		template named e.g. "US ST 6% (custom)" is untouched."""
		with patch(f"{REGIONAL}.frappe.db.get_value", return_value=None) as mock_get:
			_disable_default_us_templates("Test Co")

		queried_titles = {c.args[1]["title"] for c in mock_get.call_args_list}
		self.assertEqual(queried_titles, {"US ST 6%", "US ST 4%", "US ST 6.25%"})


class TestSyncAllCompanyTaxTemplates(UnitTestCase):

	def test_iterates_explicit_rows_without_touching_settings_singleton(self):
		rows = [MagicMock(), MagicMock()]

		with patch(f"{REGIONAL}.ensure_company_ledgers_and_template") as mock_ensure, \
		     patch(f"{REGIONAL}.frappe.get_single") as mock_get_single:
			sync_all_company_tax_templates(rows)

		mock_get_single.assert_not_called()
		self.assertEqual(mock_ensure.call_count, 2)

	def test_reads_from_settings_singleton_when_rows_omitted(self):
		settings = MagicMock()
		row = MagicMock()
		settings.company_config = [row]

		with patch(f"{REGIONAL}.ensure_company_ledgers_and_template") as mock_ensure, \
		     patch(f"{REGIONAL}.frappe.get_single", return_value=settings):
			sync_all_company_tax_templates()

		mock_ensure.assert_called_once_with(row)

	def test_true_noop_on_empty_company_config(self):
		"""Fresh install: nothing configured yet, nothing to reconcile."""
		settings = MagicMock()
		settings.company_config = []

		with patch(f"{REGIONAL}.ensure_company_ledgers_and_template") as mock_ensure, \
		     patch(f"{REGIONAL}.frappe.get_single", return_value=settings):
			sync_all_company_tax_templates()

		mock_ensure.assert_not_called()


class TestGetDefaultLedgersAPI(UnitTestCase):
	"""Whitelisted wrapper in taxjar_setup.py - the guided setup JS's _call()
	helper hardcodes that module path, so the real lookup in regional/united_states.py
	needs a thin pass-through here to be reachable from the client."""

	def test_delegates_to_resolve_default_ledgers_with_read_permission_check(self):
		from taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup import get_default_ledgers

		resolved = {"tax_account_head": "Tax - TC", "shipping_account_head": "Freight - TC"}
		with patch("taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup.frappe.has_permission") as mock_perm, \
		     patch(f"{REGIONAL}.resolve_default_ledgers", return_value=resolved) as mock_resolve:
			result = get_default_ledgers("Test Co")

		mock_perm.assert_called_once_with("TaxJar Settings", "read", throw=True)
		mock_resolve.assert_called_once_with("Test Co")
		self.assertEqual(result, resolved)


class TestAccountsStepLedgerAutoFill(UnitTestCase):
	"""String/structure assertions on _render_accounts() - same pattern as
	TestGuidedSetupPhase2JS since there's no JS runtime in this test suite."""

	def _js(self):
		import os
		path = os.path.normpath(os.path.join(
			os.path.dirname(__file__), "..", "..", "page", "taxjar_setup", "taxjar_setup.js"))
		return open(path).read()

	def _render_accounts_body(self):
		js = self._js()
		return js.split("_render_accounts()")[1].split("\n\t_save_accounts()")[0]

	def test_render_accounts_calls_get_default_ledgers(self):
		self.assertIn('this._call("get_default_ledgers"', self._render_accounts_body())

	def test_autofill_is_guarded_on_blank_fields(self):
		"""Must not fire for a company whose ledgers are already fully configured -
		otherwise revisiting the wizard makes a wasted round trip on every load."""
		body = self._render_accounts_body()
		call_site = body.split('this._call("get_default_ledgers"')[0]
		self.assertIn("if (!cfg.tax_account_head || !cfg.shipping_account_head)", call_site[-400:])

	def test_autofill_never_overwrites_an_already_set_field(self):
		body = self._render_accounts_body()
		autofill_block = body.split('this._call("get_default_ledgers"')[1]
		self.assertIn("if (!cfg.tax_account_head && defaults.tax_account_head)", autofill_block)
		self.assertIn("if (!cfg.shipping_account_head && defaults.shipping_account_head)", autofill_block)
