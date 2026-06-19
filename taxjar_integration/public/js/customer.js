frappe.ui.form.on("Customer", {
	refresh(frm) {
		if (frm.fields_dict["taxjar_customer_id"] && !frm.doc.taxjar_customer_id && !frm.is_new()) {
			frm.set_value("taxjar_customer_id", frm.doc.name);
		}
	},

	taxjar_exemption_type(frm) {
		if (!frm.doc.taxjar_exemption_type || frm.doc.taxjar_exemption_type === "Non Exempt") {
			frm.clear_table("taxjar_exempt_regions");
			frm.refresh_field("taxjar_exempt_regions");
		}
	},
});
