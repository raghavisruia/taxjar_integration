frappe.ui.form.on("Sales Invoice", {
	// Registered once per form load (not refresh, which reruns repeatedly
	// and would stack duplicate listeners). The background sync job/cron
	// retry/bulk retry all funnel through _set_sync_status, which publishes
	// this event once the DB write is committed - frappe already scopes
	// delivery to clients viewing this exact document (doc:{doctype}/{name}
	// room, joined automatically on form load), so no docname check is
	// needed here.
	setup(frm) {
		frappe.realtime.on("taxjar_invoice_sync_update", () => frm.reload_doc());
	},

	refresh(frm) {
		taxjar_integration.toggle_taxjar_ui(frm);
		taxjar_integration.render_shipping_taxability(frm);
		taxjar_integration.render_tax_breakdown(frm);
		taxjar_integration.render_sync_status_sidebar_pill(frm);
		_add_taxjar_buttons(frm);
		taxjar_integration.render_status_cards(frm);
		taxjar_integration.render_addresses(frm);
		taxjar_integration.show_no_address_tax_message(frm);
		taxjar_integration.apply_region_exemption(frm);
	},

	// Destination decides whether the customer's region-scoped exemption
	// applies, so both address fields re-evaluate it - and, with no separate
	// shipping address, the billing address is also what nexus is judged on.
	customer_address(frm) {
		taxjar_integration.apply_region_exemption(frm);
		taxjar_integration.show_no_address_tax_message(frm);
	},

	validate(frm) {
		return taxjar_integration
			.confirm_foreign_tax_rows(frm)
			.then(() => taxjar_integration.check_shipping_address(frm));
	},

	// A missing nexus is reported in the form's own message strip rather than
	// a modal, so there is nothing here for a caller to await before saving -
	// the strip can appear while the save runs without racing anything.
	shipping_address_name(frm) {
		taxjar_integration.apply_region_exemption(frm);
		taxjar_integration.show_no_address_tax_message(frm);
	}
});

function _add_taxjar_buttons(frm) {
	if (!frm.doc.docstatus || !frm.fields_dict.taxjar_sync_status || !frm.doc.company) return;

	const status = frm.doc.taxjar_sync_status;
	if (status !== "Failed" && status !== "Excluded") return;

	// Every other exclusion is a switch someone can turn on, and the button is
	// how the document is filed once they have. An export is not: TaxJar prices
	// United States sales tax, this sale is delivered elsewhere, and the button
	// would only re-exclude the document it offered to file.
	if (frm.doc.taxjar_exclusion_reason === "Destination outside TaxJar coverage") return;

	// resync_transaction refuses to file for a company whose "create
	// transactions" flag is off, so the button is not offered there either -
	// it would otherwise sit directly beneath a sidebar pill saying this company
	// does not file, offering to do the one thing it cannot.
	//
	// Asked live for the same reason the sidebar pill asks live (see
	// render_sync_status_sidebar_pill): the flag can change after the document
	// was last written, and a cancelled document is never written again. The
	// answer lands after refresh() has finished, which is fine - add_inner_button
	// dedupes by label, so a late callback cannot stack a second button.
	const docname = frm.doc.name;
	taxjar_integration.scope(frm.doc.company).then((scope) => {
		if (!scope || !scope.files || frm.doc.name !== docname) return;
		_add_sync_button(frm);
	});
}

function _add_sync_button(frm) {
	frm.add_custom_button(__("Sync to TaxJar"), function () {
		frappe.call({
			method: "taxjar_integration.taxjar_integration.taxjar_integration.resync_transaction",
			args: { invoice_name: frm.doc.name },
			freeze: true,
			freeze_message: __("Syncing to TaxJar..."),
			callback() {
				frm.reload_doc().then(() => {
					if (frm.doc.taxjar_sync_status === "Failed") {
						taxjar_integration.show_taxjar_sync_error(
							__("TaxJar Sync Failed"),
							frm.doc.taxjar_sync_error || __("Sync failed.")
						);
					} else {
						frappe.show_alert({ message: __("Sync complete"), indicator: "green" }, 5);
					}
				});
			}
		});
	}, __("TaxJar"));
}
