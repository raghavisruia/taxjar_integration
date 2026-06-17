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


def create_transaction(doc, method):
	"""Create an order transaction in TaxJar"""
	TAXJAR_CREATE_TRANSACTIONS = frappe.db.get_single_value(
		"TaxJar Settings", "taxjar_create_transactions"
	)

	if not TAXJAR_CREATE_TRANSACTIONS:
		log_taxjar_call(
			action="create_transaction",
			status="skipped",
			error="taxjar_create_transactions is disabled",
			context={"doctype": doc.doctype, "name": doc.name},
		)
		return

	client = get_client(doc.company)

	if not client:
		log_taxjar_call(
			action="create_transaction",
			status="skipped",
			error="TaxJar client is not configured",
			context={"doctype": doc.doctype, "name": doc.name},
		)
		return

	sales_tax = sum(
		tax.tax_amount for tax in doc.taxes if tax.description == TAXJAR_ROW_DESCRIPTION
	)

	if not sales_tax:
		log_taxjar_call(
			action="create_transaction",
			status="skipped",
			error="No TaxJar-managed sales tax row found on document",
			context={"doctype": doc.doctype, "name": doc.name},
		)
		return

	tax_dict = get_tax_data(doc)

	if not tax_dict:
		log_taxjar_call(
			action="create_transaction",
			status="skipped",
			error="No TaxJar payload generated",
			context={"doctype": doc.doctype, "name": doc.name},
		)
		return

	tax_dict["transaction_id"] = doc.name
	tax_dict["transaction_date"] = frappe.utils.today()
	tax_dict["sales_tax"] = sales_tax
	tax_dict["amount"] = doc.total + tax_dict["shipping"]

	try:
		if doc.is_return:
			log_taxjar_call(
				action="create_refund",
				status="request",
				payload=tax_dict,
				context={"doctype": doc.doctype, "name": doc.name},
			)
			response = client.create_refund(tax_dict)
			log_taxjar_call(
				action="create_refund",
				status="success",
				payload=tax_dict,
				response=response,
				context={"doctype": doc.doctype, "name": doc.name},
			)
		else:
			log_taxjar_call(
				action="create_order",
				status="request",
				payload=tax_dict,
				context={"doctype": doc.doctype, "name": doc.name},
			)
			response = client.create_order(tax_dict)
			log_taxjar_call(
				action="create_order",
				status="success",
				payload=tax_dict,
				response=response,
				context={"doctype": doc.doctype, "name": doc.name},
			)
	except taxjar.exceptions.TaxJarResponseError as err:
		log_taxjar_call(
			action="create_transaction",
			status="error",
			payload=tax_dict,
			error=getattr(err, "full_response", str(err)),
			context={"doctype": doc.doctype, "name": doc.name},
		)
		frappe.throw(_(sanitize_error_response(err)))
	except Exception:
		log_taxjar_call(
			action="create_transaction",
			status="error",
			payload=tax_dict,
			error=traceback.format_exc(),
			context={"doctype": doc.doctype, "name": doc.name},
		)
		_get_taxjar_logger().error(traceback.format_exc())


def delete_transaction(doc, method):
	"""Delete an existing TaxJar order transaction"""
	TAXJAR_CREATE_TRANSACTIONS = frappe.db.get_single_value(
		"TaxJar Settings", "taxjar_create_transactions"
	)

	if not TAXJAR_CREATE_TRANSACTIONS:
		return

	client = get_client(doc.company)

	if not client:
		return

	try:
		log_taxjar_call(
			action="delete_order",
			status="request",
			payload={"transaction_id": doc.name},
			context={"doctype": doc.doctype, "name": doc.name},
		)
		response = client.delete_order(doc.name)
		log_taxjar_call(
			action="delete_order",
			status="success",
			payload={"transaction_id": doc.name},
			response=response,
			context={"doctype": doc.doctype, "name": doc.name},
		)
	except taxjar.exceptions.TaxJarResponseError as err:
		log_taxjar_call(
			action="delete_order",
			status="error",
			payload={"transaction_id": doc.name},
			error=getattr(err, "full_response", str(err)),
			context={"doctype": doc.doctype, "name": doc.name},
		)
		raise
	except Exception:
		log_taxjar_call(
			action="delete_order",
			status="error",
			payload={"transaction_id": doc.name},
			error=traceback.format_exc(),
			context={"doctype": doc.doctype, "name": doc.name},
		)
		raise


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

	tax_dict = dict(
		id=item.get("idx"),
		quantity=item.get("qty"),
		unit_price=item.get("rate"),
		product_tax_code=product_tax_code,
	)

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

	tax_data = validate_tax_request(tax_dict)
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


def _remove_taxjar_rows(doc, company_config):
	"""Remove all sales tax rows owned by TaxJar for this company."""
	doc.taxes = [
		tax for tax in doc.taxes
		if tax.account_head != company_config.tax_account_head
	]


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
		doc.run_method("calculate_taxes_and_totals")
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


@frappe.whitelist()
def sync_customer_to_taxjar(customer_name, company=None):
	"""Create or update a customer record in TaxJar.

	Designed to run via frappe.enqueue (background) or called directly
	from the client-side "Sync to TaxJar" button.
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
	exemption_type = customer_doc.get("taxjar_exemption_type") or "non_exempt"

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

	try:
		log_taxjar_call(action="update_customer", status="request", payload=customer_data, context=ctx)
		response = client.update_customer(customer_name, customer_data)
		log_taxjar_call(action="update_customer", status="success", payload=customer_data, response=response, context=ctx)
	except taxjar.exceptions.TaxJarResponseError as err:
		full = getattr(err, "full_response", {}) or {}
		if full.get("status") == 404:
			try:
				log_taxjar_call(action="create_customer", status="request", payload=customer_data, context=ctx)
				response = client.create_customer(customer_data)
				log_taxjar_call(action="create_customer", status="success", payload=customer_data, response=response, context=ctx)
			except Exception:
				log_taxjar_call(action="create_customer", status="error", payload=customer_data, error=traceback.format_exc(), context=ctx)
				_get_taxjar_logger().error(traceback.format_exc())
				return
		else:
			log_taxjar_call(action="update_customer", status="error", payload=customer_data, error=getattr(err, "full_response", str(err)), context=ctx)
			_get_taxjar_logger().error(traceback.format_exc())
			return
	except Exception:
		log_taxjar_call(action="sync_customer", status="error", payload=customer_data, error=traceback.format_exc(), context=ctx)
		_get_taxjar_logger().error(traceback.format_exc())
		return

	frappe.db.set_value("Customer", customer_name, "taxjar_customer_id", customer_name, update_modified=False)
	frappe.db.set_value("Customer", customer_name, "taxjar_last_synced", frappe.utils.now(), update_modified=False)


def on_customer_update(doc, method):
	"""Enqueue TaxJar customer sync when exemption fields are present."""
	if not (
		frappe.db.get_single_value("TaxJar Settings", "taxjar_calculate_tax")
		or frappe.db.get_single_value("TaxJar Settings", "taxjar_create_transactions")
	):
		return

	exemption_type = doc.get("taxjar_exemption_type")
	if not exemption_type:
		return

	taxjar_settings = frappe.get_single("TaxJar Settings")
	for config in taxjar_settings.company_config or []:
		frappe.enqueue(
			"taxjar_integration.taxjar_integration.taxjar_integration.sync_customer_to_taxjar",
			customer_name=doc.name,
			company=config.company,
			queue="short",
			deduplicate=True,
			now=frappe.flags.in_test,
		)
