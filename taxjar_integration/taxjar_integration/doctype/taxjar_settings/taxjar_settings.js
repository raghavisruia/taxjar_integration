// Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('TaxJar Settings', {
	update_nexus_list_btn: (frm) => {
		frm.call({
			doc: frm.doc,
			method: 'update_nexus_list',
			callback: () => frm.refresh()
		});
	},
});
