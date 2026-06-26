frappe.ui.form.on("Quotation", {
	refresh(frm) {
		taxjar_integration.render_tax_breakdown(frm);
		taxjar_integration.render_status_cards(frm);
		taxjar_integration.render_addresses(frm);
		taxjar_integration.show_no_address_tax_message(frm);
	},

	validate(frm) {
		return taxjar_integration.check_shipping_address(frm);
	},
});

frappe.ui.form.on("Quotation Item", {
	form_render(frm, cdt, cdn) {
		taxjar_integration.render_single_item_breakdown(frm, cdn, "Quotation Item");
	},
});
