import frappe

from taxjar_integration.taxjar_integration.taxjar_integration import (
	TAXJAR_MAX_SYNC_RETRIES,
	_customer_sync_companies,
	_is_taxjar_enabled,
	company_scope,
	get_catalogue_client,
	recover_stuck_customer_syncs,
)
from taxjar_integration.taxjar_integration.doctype.taxjar_settings.taxjar_settings import (
	fetch_and_insert_categories,
)


def purge_old_api_logs():
	retention_days = frappe.db.get_single_value("TaxJar Settings", "log_retention_days")
	if not retention_days:
		return
	cutoff = frappe.utils.add_days(frappe.utils.today(), -int(retention_days))
	frappe.db.delete("TaxJar API Log", {"creation": ("<", cutoff)})


def sync_nexus_list():
	"""Daily job: refresh nexus regions from TaxJar for all configured companies."""
	doc = frappe.get_doc("TaxJar Settings", "TaxJar Settings")

	if not _is_taxjar_enabled(doc):
		return
	if not doc.company_config:
		return

	try:
		doc.update_nexus_list()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "TaxJar: Nexus sync failed")


def sync_product_tax_categories():
	"""Weekly job: pull TaxJar's current category list and insert any new ones.

	TaxJar doesn't publish a fixed update cadence for categories (added on an ongoing
	basis, not on a schedule), so weekly polling rather than daily. Reuses
	fetch_and_insert_categories() (shared with the manual "Update Product Tax
	Category List" button), which only inserts categories missing by
	product_tax_code - existing rows (and any Item already linked to them) are
	never touched.
	"""
	if not _is_taxjar_enabled():
		return

	client = get_catalogue_client()
	if not client:
		return

	try:
		fetch_and_insert_categories(client)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "TaxJar: Product tax category sync failed")


def retry_failed_taxjar_syncs():
	"""Every 15 min: re-enqueue Failed Sales Invoices for companies that still have
	transaction filing enabled.

	Only invoices whose last failure was classified retryable - a timeout, a rate
	limit, a TaxJar outage (classify_taxjar_error) - are picked up. A rejection
	the request itself caused, such as a duplicate transaction_id or an exemption
	that contradicts the tax rows, cannot clear on its own; re-sending it every 15
	minutes burns API quota and keeps rewriting Sync Error with whatever TaxJar
	objects to that hour, which reads as an error that "keeps changing". Those wait
	for the Retry button on the Transactions page instead.

	Also capped at TAXJAR_MAX_SYNC_RETRIES consecutive Failed outcomes
	(taxjar_sync_retry_count, bumped by _set_sync_status) - a retryable failure
	that keeps recurring stops being auto-retried too, rather than being
	re-enqueued every 15 minutes forever. The Retry button is unaffected.
	"""
	if not _is_taxjar_enabled():
		return

	failed_invoices = frappe.get_all(
		"Sales Invoice",
		filters={
			"taxjar_sync_status": "Failed",
			"taxjar_sync_retryable": 1,
			"taxjar_sync_retry_count": ("<", TAXJAR_MAX_SYNC_RETRIES),
			"docstatus": ("in", (1, 2)),
		},
		fields=["name", "company"],
		limit=50,
	)

	for invoice in failed_invoices:
		if not company_scope(invoice.company).files:
			continue
		frappe.enqueue(
			"taxjar_integration.taxjar_integration.taxjar_integration.sync_transaction_to_taxjar",
			invoice_name=invoice.name,
			queue="short",
			job_id=f"taxjar_retry_{invoice.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)


def retry_failed_taxjar_customer_syncs():
	"""Every 15 min: re-enqueue Customers whose last TaxJar sync failed in a way a
	retry could clear - see retry_failed_taxjar_syncs() for why the rest are left
	alone, and for the same TAXJAR_MAX_SYNC_RETRIES cap on consecutive failures.

	Customers stranded at "Queued" are swept into that same set first, before
	the query below reads it, so a row recovered on this tick is also retried
	on this tick rather than waiting another fifteen minutes.
	"""
	if not _is_taxjar_enabled():
		return

	recover_stuck_customer_syncs()

	failed_customers = frappe.get_all(
		"Customer",
		filters={
			"taxjar_customer_sync_status": "Failed",
			"taxjar_customer_sync_retryable": 1,
			"taxjar_customer_sync_retry_count": ("<", TAXJAR_MAX_SYNC_RETRIES),
		},
		pluck="name",
		limit=50,
	)

	# One shared definition of "a company a customer sync goes to". This loop
	# used to read the two feature flags directly, which skips the
	# United-States test company_scope() applies - so the cron could re-send a
	# customer for a company the save hook itself would never have queued.
	companies = _customer_sync_companies()

	for customer_name in failed_customers:
		for company in companies:
			frappe.enqueue(
				"taxjar_integration.taxjar_integration.taxjar_integration.sync_customer_to_taxjar",
				customer_name=customer_name,
				company=company,
				queue="short",
				# deduplicate is safe here, unlike on the save path: this cron
				# re-reads the Failed rows every fifteen minutes, so an enqueue
				# dropped now is simply made again on the next tick. The status
				# it would leave behind is Failed, which is a state something
				# still looks at.
				job_id=f"taxjar_customer_retry_{customer_name}_{company}",
				deduplicate=True,
				enqueue_after_commit=True,
			)
