frappe.ui.form.on("Customer", {
	refresh(frm) {
		if (frm.fields_dict["taxjar_customer_id"] && !frm.doc.taxjar_customer_id && !frm.is_new()) {
			frm.set_value("taxjar_customer_id", frm.doc.name);
		}

		if (frm.fields_dict["taxjar_exemption_type"] && frm.doc.taxjar_exemption_type && !frm.is_new()) {
			frm.add_custom_button(__("Sync to TaxJar"), function () {
				frappe.call({
					method: "taxjar_integration.taxjar_integration.taxjar_integration.sync_customer_to_taxjar",
					args: { customer_name: frm.doc.name },
					freeze: true,
					freeze_message: __("Syncing customer to TaxJar..."),
					callback: function (r) {
						if (!r.exc) {
							frappe.show_alert({
								message: __("Customer synced to TaxJar successfully."),
								indicator: "green",
							}, 5);
							frm.reload_doc();
						}
					},
				});
			}, __("TaxJar"));
		}
	},

	taxjar_exemption_type(frm) {
		if (!frm.doc.taxjar_exemption_type || frm.doc.taxjar_exemption_type === "non_exempt") {
			frm.clear_table("taxjar_exempt_regions");
			frm.refresh_field("taxjar_exempt_regions");
		}
	},
});
