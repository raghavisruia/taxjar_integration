frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		_render_taxjar_response(frm);
		_render_tax_breakdown(frm);
		_add_taxjar_buttons(frm);
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
		_render_single_item_breakdown(frm, cdn);
	},
});

function _render_single_item_breakdown(frm, cdn) {
	const row = frm.fields_dict.items?.grid?.grid_rows_by_docname[cdn];
	const field = row?.grid_form?.fields_dict?.taxjar_item_breakdown_html;
	if (!field) return;

	const item = frappe.get_doc("Sales Invoice Item", cdn);
	if (!item?.taxjar_item_breakdown_json) {
		field.$wrapper.html("");
		return;
	}

	let data;
	try {
		data = JSON.parse(item.taxjar_item_breakdown_json);
	} catch (e) {
		field.$wrapper.html("");
		return;
	}

	const rows = (data.breakdown || [])
		.map(
			(r) =>
				`<tr>
				<td>${frappe.utils.escape_html(r.jurisdiction)}</td>
				<td style="text-align:right">${format_currency(r.exempt_or_non_taxable || 0, frm.doc.currency)}</td>
				<td style="text-align:right">${format_currency(r.taxable_amount || 0, frm.doc.currency)}</td>
				<td style="text-align:right">${(r.rate * 100).toFixed(3)}%</td>
				<td style="text-align:right">${format_currency(r.tax_amount, frm.doc.currency)}</td>
			</tr>`
		)
		.join("");

	field.$wrapper.html(`
		<table class="table table-bordered table-sm">
			<thead><tr>
				<th>${__("Jurisdiction")}</th>
				<th style="text-align:right">${__("Exempt/Non-Taxable")}</th>
				<th style="text-align:right">${__("Taxable")}</th>
				<th style="text-align:right">${__("Rate")}</th>
				<th style="text-align:right">${__("Tax Amount")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
	`);
}

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

function _render_tax_breakdown(frm) {
	if (!frm.fields_dict.taxjar_breakdown_html) return;
	const wrapper = frm.fields_dict.taxjar_breakdown_html.$wrapper;

	if (!frm.doc.taxjar_breakdown_json) {
		wrapper.html("");
		return;
	}

	let data;
	try {
		data = JSON.parse(frm.doc.taxjar_breakdown_json);
	} catch (e) {
		wrapper.html("");
		return;
	}

	const rows = (data.transaction || [])
		.map(
			(r) =>
				`<tr>
				<td>${frappe.utils.escape_html(r.jurisdiction)}</td>
				<td>${frappe.utils.escape_html(r.name || "")}</td>
				<td style="text-align:right">${(r.rate * 100).toFixed(3)}%</td>
				<td style="text-align:right">${format_currency(r.tax_amount, frm.doc.currency)}</td>
			</tr>`
		)
		.join("");

	const totals = data.totals || {};
	wrapper.html(`
		<table class="table table-bordered table-sm" style="max-width:600px">
			<thead><tr>
				<th>${__("Jurisdiction")}</th>
				<th>${__("Name")}</th>
				<th style="text-align:right">${__("Rate")}</th>
				<th style="text-align:right">${__("Tax Amount")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
			<tfoot><tr style="font-weight:bold">
				<td>${__("Total")}</td>
				<td></td>
				<td style="text-align:right">${((totals.rate || 0) * 100).toFixed(3)}%</td>
				<td style="text-align:right">${format_currency(totals.amount_to_collect || 0, frm.doc.currency)}</td>
			</tr></tfoot>
		</table>
	`);
}

function _add_taxjar_buttons(frm) {
	if (!frm.doc.docstatus || !frm.fields_dict.taxjar_sync_status) return;

	const status = frm.doc.taxjar_sync_status;

	if (status === "Failed" || status === "Not Applicable") {
		frm.add_custom_button(__("Sync to TaxJar"), function () {
			frappe.call({
				method: "taxjar_integration.taxjar_integration.taxjar_integration.sync_transaction_to_taxjar",
				args: { invoice_name: frm.doc.name },
				freeze: true,
				freeze_message: __("Syncing to TaxJar..."),
				callback() {
					frappe.show_alert({ message: __("Sync complete"), indicator: "green" }, 5);
					frm.reload_doc();
				}
			});
		}, __("TaxJar"));
	}

	if (status === "Synced") {
		frm.add_custom_button(__("Fetch from TaxJar"), function () {
			frappe.call({
				method: "taxjar_integration.taxjar_integration.taxjar_integration.fetch_transaction_from_taxjar",
				args: { invoice_name: frm.doc.name },
				freeze: true,
				freeze_message: __("Fetching from TaxJar..."),
				callback() {
					frappe.show_alert({ message: __("Data refreshed from TaxJar"), indicator: "green" }, 5);
					frm.reload_doc();
				}
			});
		}, __("TaxJar"));
	}

	if (status === "Synced" && frm.doc.docstatus === 2) {
		frm.add_custom_button(__("Delete from TaxJar"), function () {
			frappe.confirm(
				__("Are you sure you want to delete this transaction from TaxJar?"),
				function () {
					frappe.call({
						method: "taxjar_integration.taxjar_integration.taxjar_integration.delete_transaction_manual",
						args: { invoice_name: frm.doc.name },
						freeze: true,
						freeze_message: __("Deleting from TaxJar..."),
						callback() {
							frappe.show_alert({ message: __("Deleted from TaxJar"), indicator: "green" }, 5);
							frm.reload_doc();
						}
					});
				}
			);
		}, __("TaxJar"));
	}
}
