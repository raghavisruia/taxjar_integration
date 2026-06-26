frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		_render_taxjar_response(frm);
		taxjar_integration.render_tax_breakdown(frm);
		_add_taxjar_buttons(frm);
		taxjar_integration.render_status_cards(frm);
		taxjar_integration.render_addresses(frm);
		taxjar_integration.show_no_address_tax_message(frm);
	},

	validate(frm) {
		return taxjar_integration.check_shipping_address(frm);
	},

	shipping_address_name(frm) {
		if (frm.doc.shipping_address_name) {
			frappe.call({
				method: "taxjar_integration.taxjar_integration.taxjar_integration.check_nexus",
				args: { shipping_address_name: frm.doc.shipping_address_name },
				callback(r) {
					if (r.message) {
						let msg = __("The state {0} ({1}) is not in your TaxJar Nexus list.", [r.message.state, r.message.state_code]);
						msg += "<br><br>";
						msg += __("Please add it to your TaxJar account at {0} to enable tax calculation for this state.", [
							'<a href="https://app.taxjar.com/account#states" target="_blank">https://app.taxjar.com/account#states</a>'
						]);
						frappe.msgprint({ title: __("Nexus Missing"), message: msg, indicator: "orange" });
					}
				}
			});
		}
	}
});

frappe.ui.form.on("Sales Invoice Item", {
	form_render(frm, cdt, cdn) {
		taxjar_integration.render_single_item_breakdown(frm, cdn, "Sales Invoice Item");
	},
});

function _render_taxjar_response(frm) {
	if (!frm.fields_dict.taxjar_response_html) return;
	if (frm.doc.taxjar_sync_status !== "Synced") {
		frm.fields_dict.taxjar_response_html.$wrapper.html("");
		return;
	}

	frappe.call({
		method: "taxjar_integration.taxjar_integration.taxjar_integration.get_taxjar_response_html",
		args: { invoice_name: frm.doc.name },
		callback(r) {
			if (r.message) {
				frm.fields_dict.taxjar_response_html.$wrapper.html(r.message);
			}
		}
	});
}

// Run a TaxJar invoice action: freeze the form, call the server method with
// invoice_name, then alert + reload on success. Shared by the buttons below.
function _taxjar_invoice_action(frm, { method, freeze_message, success_message }) {
	frappe.call({
		method: `taxjar_integration.taxjar_integration.taxjar_integration.${method}`,
		args: { invoice_name: frm.doc.name },
		freeze: true,
		freeze_message: freeze_message,
		callback() {
			frappe.show_alert({ message: success_message, indicator: "green" }, 5);
			frm.reload_doc();
		}
	});
}

function _add_taxjar_buttons(frm) {
	if (!frm.doc.docstatus || !frm.fields_dict.taxjar_sync_status) return;

	const status = frm.doc.taxjar_sync_status;

	if (status === "Failed" || status === "Not Applicable") {
		frm.add_custom_button(__("Sync to TaxJar"), function () {
			_taxjar_invoice_action(frm, {
				method: "sync_transaction_to_taxjar",
				freeze_message: __("Syncing to TaxJar..."),
				success_message: __("Sync complete"),
			});
		}, __("TaxJar"));
	}

	if (status === "Synced") {
		frm.add_custom_button(__("Fetch from TaxJar"), function () {
			_taxjar_invoice_action(frm, {
				method: "fetch_transaction_from_taxjar",
				freeze_message: __("Fetching from TaxJar..."),
				success_message: __("Data refreshed from TaxJar"),
			});
		}, __("TaxJar"));
	}

	if (status === "Synced" && frm.doc.docstatus === 2) {
		frm.add_custom_button(__("Delete from TaxJar"), function () {
			frappe.confirm(
				__("Are you sure you want to delete this transaction from TaxJar?"),
				function () {
					_taxjar_invoice_action(frm, {
						method: "delete_transaction_manual",
						freeze_message: __("Deleting from TaxJar..."),
						success_message: __("Deleted from TaxJar"),
					});
				}
			);
		}, __("TaxJar"));
	}
}
