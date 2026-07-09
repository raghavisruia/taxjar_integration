"""Server APIs for the TaxJar guided setup desk page (/app/taxjar-setup).

Phase 1 covers the read state the wizard hydrates from (``get_setup_state``) and
the completion write (``finish_setup``). The per-step save/test/fetch APIs land in
later phases (see docs/guided-setup-plan.md). Every endpoint is permission-guarded
against TaxJar Settings; nothing here bypasses the doctype's own validate/on_update.
"""

import frappe
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


@frappe.whitelist()
def get_setup_state():
	"""Return the current TaxJar Settings slice the guided setup renders from."""
	frappe.has_permission(SETTINGS, "read", throw=True)

	settings = frappe.get_single(SETTINGS)
	mode = settings.api_mode or "Sandbox"
	token_field = "sandbox_token" if mode == "Sandbox" else "live_token"

	creds = {c.company: c for c in (settings.table_hvjw or [])}

	companies = []
	for cfg in settings.company_config or []:
		companies.append({
			"company": cfg.company,
			"tax_account_head": cfg.tax_account_head,
			"shipping_account_head": cfg.shipping_account_head,
			"calculate": bool(cfg.taxjar_calculate_tax),
			"file": bool(cfg.taxjar_create_transactions),
			"token_last4": _token_last4(creds.get(cfg.company), token_field),
		})

	nexus_by_company = {}
	for row in settings.nexus or []:
		nexus_by_company.setdefault(row.company or "", []).append({
			"region": row.region,
			"region_code": row.region_code,
			"country": row.country,
			"country_code": row.country_code,
		})

	return {
		"api_mode": mode,
		"taxjar_enabled": bool(settings.taxjar_enabled),
		"enable_taxjar_logging": bool(settings.enable_taxjar_logging),
		"log_retention_days": settings.log_retention_days,
		"setup_complete": bool(settings.setup_complete),
		"companies": companies,
		"nexus_by_company": nexus_by_company,
	}


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
