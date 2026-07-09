"""Server APIs for the TaxJar guided setup desk page (/app/taxjar-setup).

Every endpoint is permission-guarded against TaxJar Settings and saves go through
doc.save(), so the doctype's own validate()/on_update (mode/credential checks,
field-visibility toggles, nexus auto-fetch) still fire — nothing here bypasses
the doctype's own rules, and nothing writes data the doctype itself can't.

Two child tables, two different lifecycles:

* TaxJar API Credential (table_hvjw) has no mandatory fields — a company can be
  added here (Connect step) before its accounts are known.
* TaxJar Company Config (company_config) requires both account heads, so a row
  can only be created once the Accounts step actually has them. Its two flags
  (taxjar_calculate_tax / taxjar_create_transactions) are edited by the
  Features step, on rows Accounts already created.

get_setup_state() therefore exposes ``credentials`` (from table_hvjw, drives the
Connect step) and ``companies`` (from company_config, drives Accounts/Features/
Review) as two separate lists rather than one merged shape.
"""

import taxjar
import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password

SETTINGS = "TaxJar Settings"


def _token_last4(cred, field):
	"""Return the last 4 chars of an encrypted credential token, or None.

	Full tokens are never sent to the client — only enough to recognise which token
	is stored for a company.
	"""
	if not cred or not cred.get(field):
		return None
	value = get_decrypted_password(
		"TaxJar API Credential", cred.name, field, raise_exception=False
	)
	return value[-4:] if value else None


def _nexus_by_company(settings):
	nexus_by_company = {}
	for row in settings.nexus or []:
		nexus_by_company.setdefault(row.company or "", []).append({
			"region": row.region,
			"region_code": row.region_code,
			"country": row.country,
			"country_code": row.country_code,
		})
	return nexus_by_company


@frappe.whitelist()
def get_setup_state():
	"""Return the current TaxJar Settings slice the guided setup renders from."""
	frappe.has_permission(SETTINGS, "read", throw=True)

	settings = frappe.get_single(SETTINGS)
	mode = settings.api_mode or "Sandbox"
	token_field = "sandbox_token" if mode == "Sandbox" else "live_token"

	credentials = [
		{
			"company": cred.company,
			"token_last4": _token_last4(cred, token_field),
		}
		for cred in (settings.table_hvjw or [])
		if cred.company
	]

	companies = [
		{
			"company": cfg.company,
			"tax_account_head": cfg.tax_account_head,
			"shipping_account_head": cfg.shipping_account_head,
			"calculate": bool(cfg.taxjar_calculate_tax),
			"file": bool(cfg.taxjar_create_transactions),
		}
		for cfg in (settings.company_config or [])
	]

	return {
		"api_mode": mode,
		"taxjar_enabled": bool(settings.taxjar_enabled),
		"enable_taxjar_logging": bool(settings.enable_taxjar_logging),
		"log_retention_days": settings.log_retention_days,
		"setup_complete": bool(settings.setup_complete),
		"credentials": credentials,
		"companies": companies,
		"nexus_by_company": _nexus_by_company(settings),
	}


@frappe.whitelist()
def test_connection(company, token=None, mode=None):
	"""Verify a TaxJar token against a lightweight endpoint, without persisting.

	If ``token`` is omitted, falls back to the already-saved credential for
	``company`` (e.g. re-testing a previously connected company). ``mode``
	defaults to the settings' current API Mode so a not-yet-saved mode change
	on Step 2 can still be tested before Continue is pressed.
	"""
	frappe.has_permission(SETTINGS, "write", throw=True)

	settings = frappe.get_single(SETTINGS)
	mode = mode or settings.api_mode or "Sandbox"
	is_sandbox = mode == "Sandbox"
	token_field = "sandbox_token" if is_sandbox else "live_token"

	api_key = token
	if not api_key:
		for cred in settings.table_hvjw or []:
			if cred.company == company and getattr(cred, token_field, None):
				api_key = get_decrypted_password(
					"TaxJar API Credential", cred.name, token_field, raise_exception=False
				)
				break

	if not api_key:
		return {"ok": False, "message": _("Enter a token to test.")}

	api_url = taxjar.SANDBOX_API_URL if is_sandbox else taxjar.DEFAULT_API_URL
	client = taxjar.Client(api_key=api_key, api_url=api_url)
	client.set_api_config("headers", {"x-api-version": "2022-01-24"})

	try:
		client.categories()
	except taxjar.exceptions.TaxJarResponseError as err:
		full = getattr(err, "full_response", None) or {}
		status = full.get("status_code") if isinstance(full, dict) else None
		if status == 401:
			return {"ok": False, "message": _("Invalid token (401). Check you copied the {0} token.").format(mode)}
		return {"ok": False, "message": _("TaxJar rejected the request.")}
	except taxjar.exceptions.TaxJarConnectionError:
		return {"ok": False, "message": _("Could not reach TaxJar. Check your connection and try again.")}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "TaxJar: test_connection failed")
		return {"ok": False, "message": _("Something went wrong testing this connection.")}

	return {"ok": True, "company": company, "mode": mode}


@frappe.whitelist()
def save_connection(mode, credentials=None, enable_taxjar_logging=None, log_retention_days=None):
	"""Persist API mode + per-company tokens. A blank token in the payload means
	"keep the existing one" (the masked field wasn't retyped), not "clear it"."""
	frappe.has_permission(SETTINGS, "write", throw=True)

	credentials = frappe.parse_json(credentials) if isinstance(credentials, str) else (credentials or [])

	settings = frappe.get_single(SETTINGS)
	settings.api_mode = mode
	if enable_taxjar_logging is not None:
		settings.enable_taxjar_logging = cint(enable_taxjar_logging)
	if log_retention_days is not None:
		settings.log_retention_days = cint(log_retention_days)

	token_field = "sandbox_token" if mode == "Sandbox" else "live_token"
	existing = {cred.company: cred for cred in (settings.table_hvjw or [])}

	for row in credentials:
		company = row.get("company")
		token = row.get("token")
		if not company:
			continue
		cred = existing.get(company)
		if not cred:
			cred = settings.append("table_hvjw", {"company": company})
		if token:
			cred.set(token_field, token)

	settings.save()
	return {"ok": True}


@frappe.whitelist()
def save_company_accounts(rows):
	"""Upsert company_config account heads. Both heads are mandatory on the
	child doctype, so an incomplete row surfaces that as a normal save error."""
	frappe.has_permission(SETTINGS, "write", throw=True)

	rows = frappe.parse_json(rows) if isinstance(rows, str) else (rows or [])

	settings = frappe.get_single(SETTINGS)
	existing = {cfg.company: cfg for cfg in (settings.company_config or [])}

	for row in rows:
		company = row.get("company")
		if not company:
			continue
		cfg = existing.get(company)
		if not cfg:
			cfg = settings.append("company_config", {"company": company})
		cfg.tax_account_head = row.get("tax_account_head")
		cfg.shipping_account_head = row.get("shipping_account_head")

	settings.save()
	return {"ok": True}


@frappe.whitelist()
def save_features(taxjar_enabled, flags=None):
	"""Set the master switch and each company's Calculate/File flags. Flags for
	a company without an existing company_config row are silently skipped —
	the Accounts step must run first to create that row."""
	frappe.has_permission(SETTINGS, "write", throw=True)

	flags = frappe.parse_json(flags) if isinstance(flags, str) else (flags or [])

	settings = frappe.get_single(SETTINGS)
	settings.taxjar_enabled = cint(taxjar_enabled)

	existing = {cfg.company: cfg for cfg in (settings.company_config or [])}
	for row in flags:
		cfg = existing.get(row.get("company"))
		if not cfg:
			continue
		cfg.taxjar_calculate_tax = cint(row.get("calculate"))
		cfg.taxjar_create_transactions = cint(row.get("file"))

	settings.save()
	return {"ok": True}


@frappe.whitelist()
def remove_company(company):
	"""Drop a company from the guided setup entirely — its credential and (if
	any) its company_config row — so it disappears from every later step too
	rather than leaving an orphaned config with no credential behind it."""
	frappe.has_permission(SETTINGS, "write", throw=True)

	settings = frappe.get_single(SETTINGS)
	settings.set("table_hvjw", [c for c in (settings.table_hvjw or []) if c.company != company])
	settings.set("company_config", [c for c in (settings.company_config or []) if c.company != company])
	settings.save()

	return {"ok": True}


@frappe.whitelist()
def fetch_nexus():
	"""Pull nexus regions from TaxJar for every configured company (wraps the
	doctype's own update_nexus_list, which also saves) and return them grouped
	by company, same shape as get_setup_state()'s nexus_by_company."""
	frappe.has_permission(SETTINGS, "write", throw=True)

	settings = frappe.get_single(SETTINGS)
	if not settings.company_config:
		frappe.throw(_("Please add at least one company's accounts before fetching nexus."))

	settings.update_nexus_list()

	return {"ok": True, "nexus_by_company": _nexus_by_company(settings)}


@frappe.whitelist()
def finish_setup():
	"""Mark setup complete. Saving runs the doctype's own validate(), so an
	incomplete/invalid configuration surfaces its error instead of being marked done.
	"""
	frappe.has_permission(SETTINGS, "write", throw=True)

	settings = frappe.get_single(SETTINGS)
	settings.setup_complete = 1
	settings.save()

	return {"ok": True, "setup_complete": True}
