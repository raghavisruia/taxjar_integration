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
	},

	taxjar_exemption_type(frm) {
		if (!frm.doc.taxjar_exemption_type || frm.doc.taxjar_exemption_type === "Non Exempt") {
			frm.clear_table("taxjar_exempt_regions");
			frm.refresh_field("taxjar_exempt_regions");
		}
	},
});
