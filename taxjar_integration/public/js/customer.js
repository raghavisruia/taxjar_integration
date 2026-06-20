const US_STATES = [
	"AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN",
	"IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH",
	"NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT",
	"VT","VA","WA","WV","WI","WY",
];

const CA_PROVINCES = [
	"AB","BC","MB","NB","NL","NS","NT","NU","ON","PE","QC","SK","YT",
];

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
