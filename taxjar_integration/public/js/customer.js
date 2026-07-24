// State/province codes are defined once in taxjar_utils.js (loaded globally via
// the app bundle) so Address, Customer, and exempt-region forms stay in lockstep.
const US_STATES = taxjar_integration.US_STATE_CODES;
const CA_PROVINCES = taxjar_integration.CA_PROVINCE_CODES;

function _get_state_options(country) {
	const codes = country === "CA" ? CA_PROVINCES : US_STATES;
	return [""].concat(codes).join("\n");
}

function _apply_state_filter(frm, cdn) {
	const row = locals["TaxJar Customer Exempt Region"][cdn];
	if (!row) return;

	const grid_row = frm.fields_dict.taxjar_exempt_regions.grid.grid_rows_by_docname[cdn];
	if (!grid_row) return;

	const state_field = grid_row.docfields.find((df) => df.fieldname === "state");
	if (state_field) {
		state_field.options = _get_state_options(row.country);
	}

	// Clear state if it doesn't belong to the selected country
	const valid = row.country === "CA" ? CA_PROVINCES : US_STATES;
	if (row.state && !valid.includes(row.state)) {
		frappe.model.set_value(row.doctype, row.name, "state", "");
	}

	frm.refresh_field("taxjar_exempt_regions");
}

frappe.ui.form.on("Customer", {
	// Registered once per form load, not refresh - see sales_invoice.js's
	// identical setup(frm) listener for the full reasoning. on_customer_update
	// (not just a rare submit/cancel event) and the 15-min cron retry both
	// funnel through _set_customer_sync_status, which publishes this event.
	setup(frm) {
		frappe.realtime.on("taxjar_customer_sync_update", () => frm.reload_doc());
	},

	refresh(frm) {
		if (
			!frm.is_new() &&
			frm.doc.taxjar_exemption_type &&
			frm.fields_dict["taxjar_customer_id"]
		) {
			frm.add_custom_button(
				__("Sync to TaxJar"),
				() => {
					frappe.xcall(
						"taxjar_integration.taxjar_integration.taxjar_integration.sync_customer_to_taxjar",
						{ customer_name: frm.doc.name },
					).then(() => {
						frappe.show_alert({ message: __("Customer sync queued"), indicator: "green" });
						frm.reload_doc();
					});
				},
				__("TaxJar"),
			);
		}

		// Apply state filters for existing rows on load
		(frm.doc.taxjar_exempt_regions || []).forEach((row) => {
			_apply_state_filter(frm, row.name);
		});
	},

	taxjar_exemption_type(frm) {
		if (!frm.doc.taxjar_exemption_type || frm.doc.taxjar_exemption_type === "Non Exempt") {
			frm.clear_table("taxjar_exempt_regions");
			frm.refresh_field("taxjar_exempt_regions");
		}
	},
});

frappe.ui.form.on("TaxJar Customer Exempt Region", {
	country(frm, cdt, cdn) {
		_apply_state_filter(frm, cdn);
	},

	taxjar_exempt_regions_add(frm, cdt, cdn) {
		_apply_state_filter(frm, cdn);
	},
});
