import hashlib
import json
import traceback

import frappe
import taxjar
from frappe import _
from frappe.contacts.doctype.address.address import get_company_address
from frappe.utils import cint, flt
from frappe.utils.password import get_decrypted_password

from erpnext import get_region

SUPPORTED_COUNTRY_CODES = [
	"AT",
	"AU",
	"BE",
	"BG",
	"CA",
	"CY",
	"CZ",
	"DE",
	"DK",
	"EE",
	"ES",
	"FI",
	"FR",
	"GB",
	"GR",
	"HR",
	"HU",
	"IE",
	"IT",
	"LT",
	"LU",
	"LV",
	"MT",
	"NL",
	"PL",
	"PT",
	"RO",
	"SE",
	"SI",
	"SK",
	"US",
]
SUPPORTED_STATE_CODES = [
	"AL",
	"AK",
	"AZ",
	"AR",
	"CA",
	"CO",
	"CT",
	"DE",
	"DC",
	"FL",
	"GA",
	"HI",
	"ID",
	"IL",
	"IN",
	"IA",
	"KS",
	"KY",
	"LA",
	"ME",
	"MD",
	"MA",
	"MI",
	"MN",
	"MS",
	"MO",
	"MT",
	"NE",
	"NV",
	"NH",
	"NJ",
	"NM",
	"NY",
	"NC",
	"ND",
	"OH",
	"OK",
	"OR",
	"PA",
	"RI",
	"SC",
	"SD",
	"TN",
	"TX",
	"UT",
	"VT",
	"VA",
	"WA",
	"WV",
	"WI",
	"WY",
]

# Description used to identify TaxJar-managed rows in the taxes table.
# Any row with this description is owned by TaxJar and will be replaced on recalculation.
TAXJAR_ROW_DESCRIPTION = "TaxJar Sales Tax"


def _get_taxjar_logger():
	return frappe.logger("taxjar_integration", allow_site=True, file_count=20)


def _safe_json(data):
	try:
		return json.loads(json.dumps(data, default=str))
	except Exception:
		return str(data)


def _taxjar_response_payload(response):
	if response is None:
		return None

	for attr in ("full_response", "__dict__"):
		value = getattr(response, attr, None)
		if value:
			return _safe_json(value)

	return _safe_json(response)


def _write_taxjar_ui_log(log_data):
	if not frappe.db.exists("DocType", "TaxJar API Log"):
		return

	reference_doctype = (log_data.get("context") or {}).get("doctype")
	reference_name = (log_data.get("context") or {}).get("name")

	frappe.get_doc(
		{
			"doctype": "TaxJar API Log",
			"action": log_data.get("action"),
			"status": log_data.get("status"),
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"payload": json.dumps(log_data.get("payload"), default=str)
			if log_data.get("payload") is not None
			else None,
			"response": json.dumps(log_data.get("response"), default=str)
			if log_data.get("response") is not None
			else None,
			"error": json.dumps(log_data.get("error"), default=str)
			if log_data.get("error") is not None
			else None,
		}
	# ignore_permissions: log writes must succeed regardless of the triggering user's role.
	# TaxJar API Log is read-restricted to System Manager; this does not grant the user read access.
	).insert(ignore_permissions=True)


def _is_taxjar_logging_enabled():
	cached_value = getattr(frappe.flags, "taxjar_logging_enabled", None)
	if cached_value is not None:
		return cached_value

	try:
		enabled = cint(frappe.db.get_single_value("TaxJar Settings", "enable_taxjar_logging") or 1)
	except Exception:
		enabled = 1

	frappe.flags.taxjar_logging_enabled = enabled
	return enabled


def log_taxjar_call(action, status, payload=None, response=None, error=None, context=None):
	if not _is_taxjar_logging_enabled():
		return

	log_data = {
		"action": action,
		"status": status,
		"context": context or {},
		"payload": _safe_json(payload) if payload is not None else None,
		"response": _taxjar_response_payload(response),
		"error": _safe_json(error) if error is not None else None,
	}

	logger = _get_taxjar_logger()
	message = json.dumps(log_data, default=str)
	if status == "error":
		logger.error(message)
	else:
		logger.info(message)

	try:
		_write_taxjar_ui_log(log_data)
	except Exception:
		logger.error("Failed to write TaxJar API Log DocType entry")
		logger.error(traceback.format_exc())


def get_company_config(company):
	"""Return the TaxJar Company Config row for the given company, or None."""
	taxjar_settings = frappe.get_single("TaxJar Settings")
	for config in taxjar_settings.company_config or []:
		if config.company == company:
			return config
	return None


def get_client(company=None):
	taxjar_settings = frappe.get_single("TaxJar Settings")
	is_sandbox = taxjar_settings.api_mode == "Sandbox"
	api_url = taxjar.SANDBOX_API_URL if is_sandbox else taxjar.DEFAULT_API_URL
	token_field = "sandbox_token" if is_sandbox else "live_token"

	api_key = None
	for cred in taxjar_settings.table_hvjw or []:
		if not company or cred.company == company:
			if getattr(cred, token_field, None):
				api_key = get_decrypted_password("TaxJar API Credential", cred.name, token_field)
			break

	if api_key and api_url:
		client = taxjar.Client(api_key=api_key, api_url=api_url)
		client.set_api_config("headers", {"x-api-version": "2022-01-24"})
		return client


def enqueue_taxjar_sync(doc, method):
	"""on_submit hook: enqueue background TaxJar transaction sync."""
	if not cint(frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions")):
		return
	if not get_client(doc.company):
		return

	doc.db_set("taxjar_sync_status", "Queued", update_modified=False)

	frappe.enqueue(
		"taxjar_integration.taxjar_integration.taxjar_integration.sync_transaction_to_taxjar",
		invoice_name=doc.name,
		queue="short",
		enqueue_after_commit=True,
		job_id=f"taxjar_sync_{doc.name}",
		deduplicate=True,
		now=frappe.flags.in_test,
	)


def enqueue_taxjar_delete(doc, method):
	"""on_cancel hook: enqueue background TaxJar transaction deletion."""
	if not cint(frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions")):
		return
	if not get_client(doc.company):
		return

	doc.db_set("taxjar_sync_status", "Queued", update_modified=False)

	frappe.enqueue(
		"taxjar_integration.taxjar_integration.taxjar_integration.delete_transaction_from_taxjar",
		invoice_name=doc.name,
		queue="short",
		enqueue_after_commit=True,
		job_id=f"taxjar_delete_{doc.name}",
		deduplicate=True,
		now=frappe.flags.in_test,
	)


@frappe.whitelist()
def sync_transaction_to_taxjar(invoice_name):
	"""Background worker: create order/refund in TaxJar for a submitted Sales Invoice."""
	doc = frappe.get_doc("Sales Invoice", invoice_name)
	ctx = {"doctype": "Sales Invoice", "name": invoice_name}

	if doc.docstatus == 2:
		delete_transaction_from_taxjar(invoice_name)
		return

	client = get_client(doc.company)
	if not client:
		_set_sync_status(invoice_name, "Failed", error="TaxJar client is not configured")
		return

	sales_tax = sum(
		tax.tax_amount for tax in doc.taxes if tax.description == TAXJAR_ROW_DESCRIPTION
	)

	tax_dict = get_tax_data(doc)
	if not tax_dict:
		_set_sync_status(invoice_name, "Failed", error="No TaxJar payload generated")
		log_taxjar_call(action="create_transaction", status="skipped", error="No TaxJar payload generated", context=ctx)
		return

	tax_dict["transaction_id"] = doc.name
	tax_dict["transaction_date"] = str(doc.posting_date)
	tax_dict["sales_tax"] = sales_tax
	tax_dict["amount"] = doc.total + tax_dict["shipping"]
	tax_dict["provider"] = "ERPNext"

	if doc.is_return and doc.return_against:
		tax_dict["transaction_reference_id"] = doc.return_against

	try:
		if doc.is_return:
			log_taxjar_call(action="create_refund", status="request", payload=tax_dict, context=ctx)
			response = client.create_refund(tax_dict)
			log_taxjar_call(action="create_refund", status="success", payload=tax_dict, response=response, context=ctx)
		else:
			log_taxjar_call(action="create_order", status="request", payload=tax_dict, context=ctx)
			response = client.create_order(tax_dict)
			log_taxjar_call(action="create_order", status="success", payload=tax_dict, response=response, context=ctx)

		_set_sync_status(invoice_name, "Synced")
	except taxjar.exceptions.TaxJarConnectionError:
		log_taxjar_call(action="create_transaction", status="error", payload=tax_dict, error="TaxJar API is unreachable", context=ctx)
		_set_sync_status(invoice_name, "Failed", error="TaxJar API is unreachable")
	except taxjar.exceptions.TaxJarResponseError as err:
		error_msg = sanitize_error_response(err)
		log_taxjar_call(action="create_transaction", status="error", payload=tax_dict, error=getattr(err, "full_response", str(err)), context=ctx)
		_set_sync_status(invoice_name, "Failed", error=error_msg)
	except Exception:
		log_taxjar_call(action="create_transaction", status="error", payload=tax_dict, error=traceback.format_exc(), context=ctx)
		_set_sync_status(invoice_name, "Failed", error=traceback.format_exc())
		_get_taxjar_logger().error(traceback.format_exc())


def delete_transaction_from_taxjar(invoice_name):
	"""Background worker: delete order/refund from TaxJar for a cancelled Sales Invoice."""
	doc = frappe.get_doc("Sales Invoice", invoice_name)
	ctx = {"doctype": "Sales Invoice", "name": invoice_name}

	client = get_client(doc.company)
	if not client:
		_set_sync_status(invoice_name, "Failed", error="TaxJar client is not configured")
		return

	is_refund = doc.is_return
	action = "delete_refund" if is_refund else "delete_order"
	payload = {"transaction_id": doc.name}
	provider_params = {"provider": "ERPNext"}

	try:
		log_taxjar_call(action=action, status="request", payload=payload, context=ctx)
		if is_refund:
			response = client.delete_refund(doc.name, params=provider_params)
		else:
			response = client.delete_order(doc.name, params=provider_params)
		log_taxjar_call(action=action, status="success", payload=payload, response=response, context=ctx)
		_set_sync_status(invoice_name, "Synced")
	except taxjar.exceptions.TaxJarConnectionError:
		log_taxjar_call(action=action, status="error", payload=payload, error="TaxJar API is unreachable", context=ctx)
		_set_sync_status(invoice_name, "Failed", error="TaxJar API is unreachable")
	except taxjar.exceptions.TaxJarResponseError as err:
		error_msg = sanitize_error_response(err)
		log_taxjar_call(action=action, status="error", payload=payload, error=getattr(err, "full_response", str(err)), context=ctx)
		_set_sync_status(invoice_name, "Failed", error=error_msg)
	except Exception:
		log_taxjar_call(action=action, status="error", payload=payload, error=traceback.format_exc(), context=ctx)
		_set_sync_status(invoice_name, "Failed", error=traceback.format_exc())
		_get_taxjar_logger().error(traceback.format_exc())


def _set_sync_status(invoice_name, status, error=None):
	"""Update TaxJar sync status fields on a Sales Invoice via db_set."""
	frappe.db.set_value(
		"Sales Invoice", invoice_name,
		{
			"taxjar_sync_status": status,
			"taxjar_sync_error": error or "",
			"taxjar_last_synced": frappe.utils.now() if status == "Synced" else None,
		},
		update_modified=False,
	)


@frappe.whitelist()
def get_taxjar_response_html(invoice_name):
	"""Return HTML table rendering of the latest successful TaxJar API Log for an invoice."""
	if not frappe.db.exists("DocType", "TaxJar API Log"):
		return ""

	log_name = frappe.db.get_value(
		"TaxJar API Log",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice_name,
			"action": ("in", ("create_order", "create_refund", "show_order", "show_refund")),
			"status": "success",
		},
		fieldname="name",
		order_by="creation desc",
	)

	if not log_name:
		return ""

	response_json = frappe.db.get_value("TaxJar API Log", log_name, "response")
	if not response_json:
		return ""

	try:
		data = json.loads(response_json)
	except (json.JSONDecodeError, TypeError):
		return ""

	resp = data.get("__dict__", data) if isinstance(data, dict) else data

	rows = []
	field_map = [
		("Transaction ID", "transaction_id"),
		("Transaction Date", "transaction_date"),
		("Amount", "amount"),
		("Sales Tax", "sales_tax"),
		("Shipping", "shipping"),
		("From State", "from_state"),
		("To State", "to_state"),
		("Transaction Reference ID", "transaction_reference_id"),
		("Provider", "provider"),
	]

	for label, key in field_map:
		val = resp.get(key) if isinstance(resp, dict) else getattr(resp, key, None)
		if val is not None and val != "":
			rows.append(f"<tr><td><b>{frappe.utils.escape_html(label)}</b></td>"
						f"<td>{frappe.utils.escape_html(str(val))}</td></tr>")

	if not rows:
		return ""

	return (
		'<table class="table table-bordered table-sm">'
		+ "".join(rows)
		+ "</table>"
	)


@frappe.whitelist()
def retry_all_failed_syncs():
	"""Re-enqueue all Sales Invoices with Failed sync status."""
	failed = frappe.get_all(
		"Sales Invoice",
		filters={"taxjar_sync_status": "Failed", "docstatus": ("in", (1, 2))},
		pluck="name",
	)
	for name in failed:
		frappe.enqueue(
			"taxjar_integration.taxjar_integration.taxjar_integration.sync_transaction_to_taxjar",
			invoice_name=name,
			queue="short",
			job_id=f"taxjar_retry_{name}",
			deduplicate=True,
		)
	return len(failed)


@frappe.whitelist()
def fetch_transaction_from_taxjar(invoice_name):
	"""Pull current transaction state from TaxJar and return the response data."""
	doc = frappe.get_doc("Sales Invoice", invoice_name)
	client = get_client(doc.company)
	if not client:
		frappe.throw(_("TaxJar client is not configured for company {0}").format(doc.company))

	ctx = {"doctype": "Sales Invoice", "name": invoice_name}

	provider_params = {"provider": "ERPNext"}

	try:
		if doc.is_return:
			log_taxjar_call(action="show_refund", status="request", context=ctx)
			response = client.show_refund(doc.name, params=provider_params)
			log_taxjar_call(action="show_refund", status="success", response=response, context=ctx)
		else:
			log_taxjar_call(action="show_order", status="request", context=ctx)
			response = client.show_order(doc.name, params=provider_params)
			log_taxjar_call(action="show_order", status="success", response=response, context=ctx)
		return _taxjar_response_payload(response)
	except taxjar.exceptions.TaxJarResponseError as err:
		full = getattr(err, "full_response", {}) or {}
		status_code = full.get("status_code") if isinstance(full, dict) else None
		log_taxjar_call(action="show_transaction", status="error", error=getattr(err, "full_response", str(err)), context=ctx)
		if status_code == 404:
			frappe.throw(
				_("Transaction {0} was not found in TaxJar. It may have been created in a different API mode (Sandbox/Live) "
				  "or may not have been synced yet.").format(invoice_name)
			)
		frappe.throw(_("Failed to fetch from TaxJar: {0}").format(sanitize_error_response(err)))
	except taxjar.exceptions.TaxJarConnectionError:
		log_taxjar_call(action="show_transaction", status="error", error="TaxJar API is unreachable", context=ctx)
		frappe.throw(_("TaxJar API is unreachable. Please try again later."))
	except Exception as e:
		log_taxjar_call(action="show_transaction", status="error", error=str(e), context=ctx)
		frappe.throw(_("Failed to fetch from TaxJar: {0}").format(str(e)))


@frappe.whitelist()
def delete_transaction_manual(invoice_name):
	"""Manual deletion of a transaction from TaxJar (for cleanup)."""
	doc = frappe.get_doc("Sales Invoice", invoice_name)
	client = get_client(doc.company)
	if not client:
		frappe.throw(_("TaxJar client is not configured for company {0}").format(doc.company))

	ctx = {"doctype": "Sales Invoice", "name": invoice_name}
	is_refund = doc.is_return
	action = "delete_refund" if is_refund else "delete_order"

	provider_params = {"provider": "ERPNext"}

	try:
		log_taxjar_call(action=action, status="request", payload={"transaction_id": doc.name}, context=ctx)
		if is_refund:
			response = client.delete_refund(doc.name, params=provider_params)
		else:
			response = client.delete_order(doc.name, params=provider_params)
		log_taxjar_call(action=action, status="success", response=response, context=ctx)
		_set_sync_status(invoice_name, "Not Applicable")
		return {"success": True}
	except Exception as e:
		log_taxjar_call(action=action, status="error", error=str(e), context=ctx)
		frappe.throw(_("Failed to delete from TaxJar: {0}").format(str(e)))


def get_tax_data(doc):
	company_config = get_company_config(doc.company)
	if not company_config:
		return None

	from_address = get_company_address_details(doc)
	from_shipping_state = from_address.get("state")
	from_country_code = frappe.db.get_value("Country", from_address.country, "code", cache=True)
	from_country_code = from_country_code.upper()

	to_address = get_shipping_address_details(doc)
	to_shipping_state = to_address.get("state")
	to_country_code = frappe.db.get_value("Country", to_address.country, "code", cache=True)
	to_country_code = to_country_code.upper()

	shipping = sum(
		tax.tax_amount for tax in doc.taxes
		if tax.account_head == company_config.shipping_account_head
	)

	line_items = [get_line_item_dict(item, doc.docstatus) for item in doc.items]

	if from_shipping_state not in SUPPORTED_STATE_CODES:
		from_shipping_state = get_state_code(from_address, "Company")

	if to_shipping_state not in SUPPORTED_STATE_CODES:
		to_shipping_state = get_state_code(to_address, "Shipping")

	tax_dict = {
		"from_country": from_country_code,
		"from_zip": from_address.pincode,
		"from_state": from_shipping_state,
		"from_city": from_address.city,
		"from_street": from_address.address_line1,
		"to_country": to_country_code,
		"to_zip": to_address.pincode,
		"to_city": to_address.city,
		"to_street": to_address.address_line1,
		"to_state": to_shipping_state,
		"shipping": shipping,
		"amount": doc.net_total,
		"plugin": "erpnext",
		"line_items": line_items,
	}

	customer_name = _get_customer_name(doc)
	if customer_name:
		tax_dict["customer_id"] = customer_name

	return tax_dict


def get_state_code(address, location):
	if address is not None:
		state_code = get_iso_3166_2_state_code(address)
		if state_code not in SUPPORTED_STATE_CODES:
			frappe.throw(_("Please enter a valid State in the {0} Address").format(location))
	else:
		frappe.throw(_("Please enter a valid State in the {0} Address").format(location))

	return state_code


def get_line_item_dict(item, docstatus):
	# Prefer the value already on the line item (populated by fetch_from on Sales Invoice Item).
	# Fall back to the Item master for doctypes whose item table doesn't carry the custom field
	# (Quotation Item, Sales Order Item) and for programmatically created documents where the
	# client-side fetch_from never fired.
	product_tax_code = item.get("product_tax_category") or (
		frappe.db.get_value("Item", item.get("item_code"), "product_tax_category", cache=True)
		if item.get("item_code")
		else None
	)

	unit_price = flt(item.get("rate"))
	price_list_rate = flt(item.get("price_list_rate"))

	tax_dict = dict(
		id=item.get("idx"),
		quantity=item.get("qty"),
		product_tax_code=product_tax_code,
	)

	if price_list_rate and price_list_rate > unit_price:
		tax_dict["unit_price"] = price_list_rate
		tax_dict["discount"] = price_list_rate - unit_price
	else:
		tax_dict["unit_price"] = unit_price

	if docstatus == 1:
		tax_dict.update({"sales_tax": item.get("tax_collectable")})

	return tax_dict


def set_sales_tax(doc, method):
	TAXJAR_CALCULATE_TAX = frappe.db.get_single_value("TaxJar Settings", "taxjar_calculate_tax")

	if not TAXJAR_CALCULATE_TAX:
		log_taxjar_call(
			action="tax_for_order",
			status="skipped",
			error="taxjar_calculate_tax is disabled",
			context={"doctype": doc.doctype, "name": doc.name, "company": doc.company},
		)
		return

	if get_region(doc.company) != "United States":
		log_taxjar_call(
			action="tax_for_order",
			status="skipped",
			error="Company region is not United States",
			context={"doctype": doc.doctype, "name": doc.name, "company": doc.company},
		)
		return

	if not doc.items:
		log_taxjar_call(
			action="tax_for_order",
			status="skipped",
			error="Document has no items",
			context={"doctype": doc.doctype, "name": doc.name, "company": doc.company},
		)
		return

	company_config = get_company_config(doc.company)
	if not company_config:
		log_taxjar_call(
			action="tax_for_order",
			status="skipped",
			error="No TaxJar Company Config found for company {0}".format(doc.company),
			context={"doctype": doc.doctype, "name": doc.name, "company": doc.company},
		)
		return

	if check_sales_tax_exemption(doc, company_config):
		log_taxjar_call(
			action="tax_for_order",
			status="skipped",
			error="Document or customer is exempt from sales tax",
			context={"doctype": doc.doctype, "name": doc.name, "company": doc.company},
		)
		return

	tax_dict = get_tax_data(doc)

	if not tax_dict:
		log_taxjar_call(
			action="tax_for_order",
			status="skipped",
			error="No TaxJar payload generated from addresses/items",
			context={"doctype": doc.doctype, "name": doc.name, "company": doc.company},
		)
		_remove_taxjar_rows(doc, company_config)
		return

	# Check if delivering within a nexus; clears TaxJar rows if not
	if not check_for_nexus(doc, tax_dict):
		return

	cache_key = "taxjar_tax:" + hashlib.md5(
		json.dumps(tax_dict, sort_keys=True, default=str).encode()
	).hexdigest()
	cached = frappe.cache().get_value(cache_key)

	if cached is not None:
		tax_data = cached
	else:
		tax_data = validate_tax_request(tax_dict)
		if tax_data is not None:
			frappe.cache().set_value(cache_key, tax_data, expires_in_sec=300)
	if tax_data is not None:
		if not tax_data.amount_to_collect:
			_remove_taxjar_rows(doc, company_config)
		elif tax_data.amount_to_collect > 0:
			# Remove all existing rows for this company's tax account (template rows + previous TaxJar row)
			_remove_taxjar_rows(doc, company_config)

			doc.append(
				"taxes",
				{
					"charge_type": "Actual",
					"description": TAXJAR_ROW_DESCRIPTION,
					"account_head": company_config.tax_account_head,
					"tax_amount": tax_data.amount_to_collect,
				},
			)

			# Assign tax_collectable and taxable_amount per line item
			for item in tax_data.breakdown.line_items:
				doc.get("items")[cint(item.id) - 1].tax_collectable = item.tax_collectable
				doc.get("items")[cint(item.id) - 1].taxable_amount = item.taxable_amount

			doc.run_method("calculate_taxes_and_totals")
			doc.run_method("set_total_in_words")


def validate_return_against(doc, method):
	"""Enforce return_against on credit notes when TaxJar transaction reporting is enabled."""
	if not getattr(doc, "is_return", False):
		return
	if not cint(frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions")):
		return
	if not doc.return_against:
		frappe.throw(
			_(
				"Return Against is mandatory for credit notes when TaxJar transaction reporting is enabled. "
				"Please link the original Sales Invoice."
			),
			title=_("TaxJar: Missing Return Reference"),
		)


def _remove_taxjar_rows(doc, company_config):
	"""Remove all sales tax rows owned by TaxJar for this company and recalculate totals."""
	doc.taxes = [
		tax for tax in doc.taxes
		if tax.account_head != company_config.tax_account_head
	]
	doc.run_method("calculate_taxes_and_totals")
	doc.run_method("set_total_in_words")


def check_for_nexus(doc, tax_dict):
	"""Return True if the delivery is within a nexus. Clears TaxJar rows and returns False if not."""
	company_config = get_company_config(doc.company)
	in_nexus = frappe.db.get_value(
		"TaxJar Nexus",
		filters={"region_code": tax_dict["to_state"], "parent": "TaxJar Settings", "company": doc.company},
	)

	if not in_nexus:
		if company_config:
			_remove_taxjar_rows(doc, company_config)
		return False

	return True


def check_sales_tax_exemption(doc, company_config):
	"""Return True if the document or customer is blanket-exempt; remove TaxJar rows if so.

	State-specific exemptions (via TaxJar Customer API exempt_regions) are NOT
	handled here — they flow through to TaxJar via customer_id in the API payload.
	"""
	doc_exempt = hasattr(doc, "exempt_from_sales_tax") and doc.exempt_from_sales_tax

	customer_name = _get_customer_name(doc)
	customer_exempt = False
	if not doc_exempt and customer_name:
		customer_exempt = (
			frappe.db.has_column("Customer", "exempt_from_sales_tax")
			and frappe.db.get_value("Customer", customer_name, "exempt_from_sales_tax", cache=True)
		)

	if doc_exempt or customer_exempt:
		_remove_taxjar_rows(doc, company_config)
		return True

	return False


def validate_tax_request(tax_dict):
	"""Return the sales tax that should be collected for a given order."""

	client = get_client()

	if not client:
		log_taxjar_call(action="tax_for_order", status="skipped", error="TaxJar client is not configured")
		return

	try:
		log_taxjar_call(action="tax_for_order", status="request", payload=tax_dict)
		tax_data = client.tax_for_order(tax_dict)
	except taxjar.exceptions.TaxJarConnectionError:
		log_taxjar_call(
			action="tax_for_order",
			status="error",
			payload=tax_dict,
			error="TaxJar API is unreachable",
		)
		frappe.msgprint(
			_("TaxJar API is unreachable. Tax has not been calculated for this document."),
			indicator="orange",
			alert=True,
		)
		return None
	except taxjar.exceptions.TaxJarResponseError as err:
		log_taxjar_call(
			action="tax_for_order",
			status="error",
			payload=tax_dict,
			error=getattr(err, "full_response", str(err)),
		)
		frappe.throw(_(sanitize_error_response(err)))
	except Exception:
		log_taxjar_call(
			action="tax_for_order",
			status="error",
			payload=tax_dict,
			error=traceback.format_exc(),
		)
		raise
	else:
		log_taxjar_call(action="tax_for_order", status="success", payload=tax_dict, response=tax_data)
		return tax_data


def get_company_address_details(doc):
	"""Return company address details for the invoice's company."""
	from erpnext import get_default_company

	company = doc.company if hasattr(doc, "company") and doc.company else get_default_company()

	company_address = get_company_address(company).company_address

	if not company_address:
		frappe.throw(_("Please set a default address for the company {0}.").format(company))

	return frappe.get_doc("Address", company_address)


@frappe.whitelist()
def check_nexus(shipping_address_name):
	if not isinstance(shipping_address_name, str) or not shipping_address_name.strip():
		return

	TAXJAR_CALCULATE_TAX = frappe.db.get_single_value("TaxJar Settings", "taxjar_calculate_tax")
	if not TAXJAR_CALCULATE_TAX:
		return

	if not frappe.db.exists("Address", shipping_address_name):
		return

	try:
		address = frappe.get_doc("Address", shipping_address_name)
		state_code = get_iso_3166_2_state_code(address)

		if not frappe.db.get_value("TaxJar Nexus", filters={"region_code": state_code, "parent": "TaxJar Settings"}):
			return {"state": address.state, "state_code": state_code}
	except Exception:
		return


def get_shipping_address_details(doc):
	"""Return customer shipping address details"""

	if doc.shipping_address_name:
		shipping_address = frappe.get_doc("Address", doc.shipping_address_name)
	elif doc.customer_address:
		shipping_address = frappe.get_doc("Address", doc.customer_address)
	else:
		shipping_address = get_company_address_details(doc)

	return shipping_address


def get_iso_3166_2_state_code(address):
	import pycountry

	# Prefer the explicit TaxJar state code field when present (avoids pycountry guessing).
	taxjar_code = address.get("taxjar_state_code")
	if taxjar_code and taxjar_code in SUPPORTED_STATE_CODES:
		return taxjar_code

	state = address.get("state")
	if not state:
		frappe.throw(_("Please enter a valid State in the address"))

	country_code = frappe.db.get_value("Country", address.get("country"), "code", cache=True)

	error_message = _(
		"""{0} is not a valid state! Check for typos or enter the ISO code for your state."""
	).format(state)
	state = state.upper().strip()

	# The max length for ISO state codes is 3, excluding the country code
	if len(state) <= 3:
		# PyCountry returns state code as {country_code}-{state-code} (e.g. US-FL)
		address_state = (country_code + "-" + state).upper()

		states = pycountry.subdivisions.get(country_code=country_code.upper())
		states = [pystate.code for pystate in states]

		if address_state in states:
			return state

		frappe.throw(_(error_message))
	else:
		try:
			lookup_state = pycountry.subdivisions.lookup(state)
		except LookupError:
			frappe.throw(_(error_message))
		else:
			return lookup_state.code.split("-")[1]


def validate_address(doc, method):
	"""Enforce mandatory address fields for US and Canadian addresses."""
	if not doc.country:
		return

	country_code = (frappe.db.get_value("Country", doc.country, "code", cache=True) or "").upper()

	if country_code in ("US", "CA"):
		if not doc.state:
			frappe.throw(_("State/Province is mandatory for {0} addresses.").format(doc.country))

	if country_code == "US":
		if not doc.get("taxjar_state_code"):
			frappe.throw(_("State Code is mandatory for United States addresses."))
		if not doc.pincode:
			frappe.throw(_("Postal Code is mandatory for United States addresses."))

		if _is_taxjar_enabled():
			_validate_address_with_taxjar(doc)


def _is_taxjar_enabled():
	return cint(frappe.db.get_single_value("TaxJar Settings", "taxjar_calculate_tax")) or \
		cint(frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions"))


def _validate_address_with_taxjar(doc):
	"""Call TaxJar's address validation endpoint for US addresses."""
	client = get_client()
	if not client:
		return

	address_data = {
		"country": "US",
		"state": doc.get("taxjar_state_code") or "",
		"zip": doc.pincode or "",
		"city": doc.city or "",
		"street": doc.address_line1 or "",
	}

	ctx = {"doctype": "Address", "name": doc.name}

	try:
		log_taxjar_call(action="validate_address", status="request", payload=address_data, context=ctx)
		result = client.validate_address(address_data)
		log_taxjar_call(action="validate_address", status="success", payload=address_data, response=result, context=ctx)
	except taxjar.exceptions.TaxJarConnectionError:
		log_taxjar_call(action="validate_address", status="error", error="TaxJar API is unreachable", context=ctx)
		return
	except taxjar.exceptions.TaxJarResponseError as err:
		log_taxjar_call(action="validate_address", status="error", error=getattr(err, "full_response", str(err)), context=ctx)
		frappe.msgprint(
			_("TaxJar could not validate this address: {0}").format(sanitize_error_response(err)),
			indicator="orange",
		)
		return
	except Exception:
		log_taxjar_call(action="validate_address", status="error", error=traceback.format_exc(), context=ctx)
		return

	if hasattr(result, "__iter__"):
		for match in result:
			suggested = _format_address_suggestion(match)
			if suggested:
				frappe.msgprint(
					_("TaxJar suggests: {0}").format(suggested),
					indicator="blue",
					title=_("Address Verification"),
				)
				break


def _format_address_suggestion(match):
	parts = []
	for field in ("street", "city", "state", "zip", "country"):
		val = getattr(match, field, None)
		if val:
			parts.append(str(val))
	return ", ".join(parts) if parts else None


def _get_customer_name(doc):
	"""Return the Customer name for a transaction document, or None."""
	if doc.doctype == "Quotation":
		if getattr(doc, "quotation_to", None) == "Customer":
			return doc.party_name
		return None
	return getattr(doc, "customer", None)


def sanitize_error_response(response):
	full = getattr(response, "full_response", None) or {}
	detail = full.get("detail") or "An unexpected error occurred. Please try again."
	detail = detail.replace("_", " ")

	sanitized_responses = {
		"to zip": "Zipcode",
		"to city": "City",
		"to state": "State",
		"to country": "Country",
	}

	for k, v in sanitized_responses.items():
		detail = detail.replace(k, v)

	return detail


# ── TaxJar Customer API ──────────────────────────────────────────────────────

_EXEMPTION_TYPE_MAP = {
	"Wholesale": "wholesale",
	"Government": "government",
	"Marketplace": "marketplace",
	"Non Exempt": "non_exempt",
	"Other": "other",
}


def _map_exemption_type(label):
	"""Convert a human-readable exemption label to the TaxJar API value."""
	return _EXEMPTION_TYPE_MAP.get(label, "non_exempt")


def _set_customer_sync_status(customer_name, status, error=None):
	"""Update TaxJar sync status fields on a Customer."""
	frappe.db.set_value(
		"Customer", customer_name,
		{
			"taxjar_customer_sync_status": status,
			"taxjar_customer_sync_error": error or "",
		},
		update_modified=False,
	)


@frappe.whitelist()
def sync_customer_to_taxjar(customer_name, company=None):
	"""Create or update a customer record in TaxJar.

	Uses taxjar_customer_id to decide: empty → POST (create), set → PUT (update).
	Designed to run via frappe.enqueue (background).
	"""
	client = get_client(company)
	if not client:
		log_taxjar_call(
			action="sync_customer",
			status="skipped",
			error="TaxJar client is not configured",
			context={"doctype": "Customer", "name": customer_name, "company": company},
		)
		return

	customer_doc = frappe.get_doc("Customer", customer_name)
	exemption_type = _map_exemption_type(customer_doc.get("taxjar_exemption_type"))

	exempt_regions = [
		{"country": r.country, "state": r.state}
		for r in (customer_doc.get("taxjar_exempt_regions") or [])
	]

	customer_data = {
		"customer_id": customer_name,
		"exemption_type": exemption_type,
		"name": customer_doc.customer_name,
		"exempt_regions": exempt_regions,
	}

	ctx = {"doctype": "Customer", "name": customer_name, "company": company}
	existing_customer_id = customer_doc.get("taxjar_customer_id")

	try:
		if existing_customer_id:
			response = _update_taxjar_customer(client, customer_name, customer_data, ctx)
		else:
			response = _create_taxjar_customer(client, customer_data, ctx)
	except taxjar.exceptions.TaxJarConnectionError:
		log_taxjar_call(action="sync_customer", status="error", payload=customer_data, error="TaxJar API is unreachable", context=ctx)
		_set_customer_sync_status(customer_name, "Failed", error="TaxJar API is unreachable")
		return
	except Exception:
		log_taxjar_call(action="sync_customer", status="error", payload=customer_data, error=traceback.format_exc(), context=ctx)
		_get_taxjar_logger().error(traceback.format_exc())
		_set_customer_sync_status(customer_name, "Failed", error=traceback.format_exc())
		return

	if response is None:
		return

	_set_customer_sync_status(customer_name, "Synced")
	frappe.db.set_value("Customer", customer_name, "taxjar_customer_id", customer_name, update_modified=False)
	frappe.db.set_value("Customer", customer_name, "taxjar_last_synced", frappe.utils.now(), update_modified=False)


def _create_taxjar_customer(client, customer_data, ctx):
	"""POST a new customer to TaxJar. Returns the response or None on failure."""
	customer_name = customer_data["customer_id"]
	log_taxjar_call(action="create_customer", status="request", payload=customer_data, context=ctx)
	try:
		response = client.create_customer(customer_data)
	except taxjar.exceptions.TaxJarResponseError as err:
		log_taxjar_call(action="create_customer", status="error", payload=customer_data, error=getattr(err, "full_response", str(err)), context=ctx)
		_get_taxjar_logger().error(traceback.format_exc())
		_set_customer_sync_status(customer_name, "Failed", error=sanitize_error_response(err))
		return None
	log_taxjar_call(action="create_customer", status="success", payload=customer_data, response=response, context=ctx)
	return response


def _update_taxjar_customer(client, customer_id, customer_data, ctx):
	"""PUT an existing customer to TaxJar. Falls back to create on 404."""
	log_taxjar_call(action="update_customer", status="request", payload=customer_data, context=ctx)
	try:
		response = client.update_customer(customer_id, customer_data)
	except taxjar.exceptions.TaxJarResponseError as err:
		full = getattr(err, "full_response", {}) or {}
		if full.get("status_code") == 404:
			frappe.db.set_value("Customer", customer_id, "taxjar_customer_id", "", update_modified=False)
			return _create_taxjar_customer(client, customer_data, ctx)
		log_taxjar_call(action="update_customer", status="error", payload=customer_data, error=full, context=ctx)
		_get_taxjar_logger().error(traceback.format_exc())
		_set_customer_sync_status(customer_id, "Failed", error=sanitize_error_response(err))
		return None
	log_taxjar_call(action="update_customer", status="success", payload=customer_data, response=response, context=ctx)
	return response


def _has_taxjar_fields_changed(doc):
	"""Return True if any TaxJar-relevant field changed since last save."""
	if doc.has_value_changed("taxjar_exemption_type"):
		return True

	if doc.has_value_changed("customer_name"):
		return True

	previous = doc.get_doc_before_save()
	if not previous:
		return bool(doc.get("taxjar_exempt_regions"))

	old_regions = {(r.country, r.state) for r in (previous.get("taxjar_exempt_regions") or [])}
	new_regions = {(r.country, r.state) for r in (doc.get("taxjar_exempt_regions") or [])}
	return old_regions != new_regions


def on_customer_update(doc, method):
	"""Enqueue TaxJar customer sync when exemption fields change."""
	if not (
		frappe.db.get_single_value("TaxJar Settings", "taxjar_calculate_tax")
		or frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions")
	):
		return

	if not _has_taxjar_fields_changed(doc):
		return

	if not doc.get("taxjar_exemption_type") and not doc.get("taxjar_customer_id"):
		return

	doc.db_set("taxjar_customer_sync_status", "Queued", update_modified=False)

	taxjar_settings = frappe.get_single("TaxJar Settings")
	for config in taxjar_settings.company_config or []:
		frappe.enqueue(
			"taxjar_integration.taxjar_integration.taxjar_integration.sync_customer_to_taxjar",
			customer_name=doc.name,
			company=config.company,
			queue="short",
			deduplicate=True,
			job_id=f"sync_customer_taxjar_{doc.name}_{config.company}",
			now=frappe.flags.in_test,
		)


def on_customer_delete(doc, method):
	"""Delete customer from TaxJar when trashed in ERPNext."""
	taxjar_customer_id = doc.get("taxjar_customer_id")
	if not taxjar_customer_id:
		return

	if not (
		frappe.db.get_single_value("TaxJar Settings", "taxjar_calculate_tax")
		or frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions")
	):
		return

	taxjar_settings = frappe.get_single("TaxJar Settings")
	for config in taxjar_settings.company_config or []:
		try:
			delete_customer_from_taxjar(taxjar_customer_id, config.company)
		except Exception:
			_get_taxjar_logger().error(traceback.format_exc())
			log_taxjar_call(
				action="delete_customer",
				status="error",
				error=traceback.format_exc(),
				context={"doctype": "Customer", "name": doc.name, "company": config.company},
			)


def delete_customer_from_taxjar(taxjar_customer_id, company=None):
	"""Delete a customer record from TaxJar."""
	client = get_client(company)
	if not client:
		return

	ctx = {"doctype": "Customer", "name": taxjar_customer_id, "company": company}
	log_taxjar_call(action="delete_customer", status="request", context=ctx)
	try:
		response = client.delete_customer(taxjar_customer_id)
	except taxjar.exceptions.TaxJarResponseError as err:
		full = getattr(err, "full_response", {}) or {}
		if full.get("status_code") == 404:
			log_taxjar_call(action="delete_customer", status="success", context=ctx)
			return
		log_taxjar_call(action="delete_customer", status="error", error=getattr(err, "full_response", str(err)), context=ctx)
		raise
	except taxjar.exceptions.TaxJarConnectionError:
		log_taxjar_call(action="delete_customer", status="error", error="TaxJar API is unreachable", context=ctx)
		raise

	log_taxjar_call(action="delete_customer", status="success", response=response, context=ctx)
