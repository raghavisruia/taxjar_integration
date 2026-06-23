frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		_render_taxjar_response(frm);
		_render_tax_breakdown(frm);
		_add_taxjar_buttons(frm);
	},

	validate(frm) {
		return _check_shipping_address(frm);
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

function _build_transaction_table(rows, totals, currency) {
	const body = rows
		.map(
			(r) =>
				`<tr>
				<td>${frappe.utils.escape_html(r.jurisdiction)}</td>
				<td>${frappe.utils.escape_html(r.name || "")}</td>
				<td class="text-right">${(r.rate * 100).toFixed(3)}%</td>
				<td class="text-right">${format_currency(r.tax_amount, currency)}</td>
			</tr>`
		)
		.join("");

	return `
		<div class="tax-break-up" style="overflow-x: auto;">
			<table class="table table-bordered table-hover">
				<thead style="background-color: var(--subtle-fg);"><tr>
					<th class="text-left">${__("Jurisdiction")}</th>
					<th class="text-left">${__("Name")}</th>
					<th class="text-right">${__("Rate")}</th>
					<th class="text-right">${__("Tax Amount")}</th>
				</tr></thead>
				<tbody>${body}</tbody>
				<tfoot><tr style="font-weight:bold">
					<td>${__("Total")}</td>
					<td></td>
					<td class="text-right">${((totals.rate || 0) * 100).toFixed(3)}%</td>
					<td class="text-right">${format_currency(totals.amount_to_collect || 0, currency)}</td>
				</tr></tfoot>
			</table>
		</div>`;
}

function _build_item_table(rows, currency) {
	const body = rows
		.map(
			(r) =>
				`<tr>
				<td>${frappe.utils.escape_html(r.jurisdiction)}</td>
				<td class="text-right">${format_currency(r.exempt_or_non_taxable || 0, currency)}</td>
				<td class="text-right">${format_currency(r.taxable_amount || 0, currency)}</td>
				<td class="text-right">${(r.rate * 100).toFixed(3)}%</td>
				<td class="text-right">${format_currency(r.tax_amount, currency)}</td>
			</tr>`
		)
		.join("");

	return `
		<div class="tax-break-up" style="overflow-x: auto;">
			<table class="table table-bordered table-hover">
				<thead style="background-color: var(--subtle-fg);"><tr>
					<th class="text-left">${__("Jurisdiction")}</th>
					<th class="text-right">${__("Exempt/Non-Taxable")}</th>
					<th class="text-right">${__("Taxable")}</th>
					<th class="text-right">${__("Rate")}</th>
					<th class="text-right">${__("Tax Amount")}</th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table>
		</div>`;
}

function _no_breakdown_msg() {
	return `<p class="text-muted">${__("No TaxJar tax breakdown available for this transaction.")}</p>`;
}

function _multi_currency_note(data) {
	return `<p class="text-muted small">${
		__("This is a multi-currency transaction. Amounts were converted from {0} to USD at a rate of {1} ({2}) for TaxJar tax calculation.",
			[data.currency, data.exchange_rate, data.exchange_date])
	}</p>`;
}

function _render_tax_breakdown(frm) {
	if (!frm.fields_dict.taxjar_breakdown_html) return;
	const wrapper = frm.fields_dict.taxjar_breakdown_html.$wrapper;

	if (!frm.doc.taxjar_breakdown_json) {
		wrapper.html(_no_breakdown_msg());
		return;
	}

	let data;
	try {
		data = JSON.parse(frm.doc.taxjar_breakdown_json);
	} catch (e) {
		wrapper.html(_no_breakdown_msg());
		return;
	}

	let html = "";
	const currency = data.currency || frm.doc.currency;

	if (data.usd) {
		html += _multi_currency_note(data);
		html += `<p class="text-muted small" style="margin-bottom:4px"><strong>${__("Tax Calculation (USD)")}</strong></p>`;
		html += _build_transaction_table(data.usd.transaction || [], data.usd.totals || {}, "USD");
		html += `<p class="text-muted small" style="margin-top:12px;margin-bottom:4px"><strong>${
			__("Equivalent in Transaction Currency ({0})", [currency])
		}</strong></p>`;
	}

	html += _build_transaction_table(data.transaction || [], data.totals || {}, currency);
	wrapper.html(html);
}

function _render_single_item_breakdown(frm, cdn) {
	const row = frm.fields_dict.items?.grid?.grid_rows_by_docname[cdn];
	const field = row?.grid_form?.fields_dict?.taxjar_item_breakdown_html;
	if (!field) return;

	const item = frappe.get_doc("Sales Invoice Item", cdn);
	if (!item?.taxjar_item_breakdown_json) {
		field.$wrapper.html(_no_breakdown_msg());
		return;
	}

	let data;
	try {
		data = JSON.parse(item.taxjar_item_breakdown_json);
	} catch (e) {
		field.$wrapper.html(_no_breakdown_msg());
		return;
	}

	let html = "";
	const currency = data.currency || frm.doc.currency;

	if (data.usd) {
		html += `<p class="text-muted small" style="margin-bottom:4px"><strong>${__("Tax Calculation (USD)")}</strong></p>`;
		html += _build_item_table(data.usd.breakdown || [], "USD");
		html += `<p class="text-muted small" style="margin-top:12px;margin-bottom:4px"><strong>${
			__("Equivalent in Transaction Currency ({0})", [currency])
		}</strong></p>`;
	}

	html += _build_item_table(data.breakdown || [], currency);
	field.$wrapper.html(html);
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

function _check_shipping_address(frm) {
	if (frm.doc.shipping_address_name) return;

	let party_name = frm.doc.customer;
	if (!party_name) return;

	return frappe.xcall(
		"taxjar_integration.taxjar_integration.taxjar_integration.get_customer_addresses",
		{ customer: party_name }
	).then(function (addresses) {
		addresses = addresses || [];

		if (addresses.length) {
			frappe.validated = false;
			_show_address_picker(frm, addresses);
			return;
		}

		frappe.validated = false;
		frappe.msgprint({
			title: __("No Addresses Found"),
			message: __("No addresses found for this customer. Please add a shipping address before saving."),
			indicator: "red",
			primary_action: {
				label: __("Add New Address"),
				action() {
					frappe.new_doc("Address", {
						address_title: party_name,
						address_type: "Shipping",
						links: [{ link_doctype: "Customer", link_name: party_name }],
					});
					frappe.msg_dialog.hide();
				},
			},
		});
	});
}

function _show_address_picker(frm, addresses) {
	let selected = null;

	let table_rows = addresses
		.map((addr, idx) => {
			let parts = [addr.address_line1, addr.city, addr.state, addr.pincode]
				.filter(Boolean)
				.join(", ");
			let checked = idx === 0 ? "checked" : "";
			if (idx === 0) selected = addr.name;

			return `<tr data-address="${frappe.utils.escape_html(addr.name)}" style="cursor:pointer">
				<td style="width:30px;text-align:center">
					<input type="radio" name="taxjar_addr" value="${frappe.utils.escape_html(addr.name)}" ${checked}>
				</td>
				<td>${frappe.utils.escape_html(addr.address_title || addr.name)}</td>
				<td>${frappe.utils.escape_html(parts)}</td>
				<td>${frappe.utils.escape_html(addr.address_type || "")}</td>
				<td style="text-align:center">
					${addr.is_shipping_address ? '<span class="indicator-pill green">Yes</span>' : ""}
				</td>
			</tr>`;
		})
		.join("");

	let html = `
		<p class="text-muted">${__("A shipping address is required for sales tax calculation. Select an existing address or add a new one.")}</p>
		<div style="max-height:300px;overflow-y:auto;margin-top:10px">
			<table class="table table-bordered table-hover">
				<thead style="background-color:var(--subtle-fg)">
					<tr>
						<th style="width:30px"></th>
						<th>${__("Title")}</th>
						<th>${__("Address")}</th>
						<th>${__("Type")}</th>
						<th style="text-align:center">${__("Preferred Shipping")}</th>
					</tr>
				</thead>
				<tbody>${table_rows}</tbody>
			</table>
		</div>
	`;

	let d = new frappe.ui.Dialog({
		title: __("Select Shipping Address"),
		fields: [
			{ fieldtype: "HTML", fieldname: "address_table", options: html },
			{
				fieldtype: "Check",
				fieldname: "mark_as_shipping",
				label: __("Use this address as shipping address for future transactions"),
				default: 0,
			},
		],
		primary_action_label: __("Use Selected"),
		primary_action() {
			if (!selected) {
				frappe.show_alert({ message: __("Please select an address"), indicator: "orange" });
				return;
			}

			frm.set_value("shipping_address_name", selected);

			if (d.get_value("mark_as_shipping")) {
				frappe.xcall(
					"taxjar_integration.taxjar_integration.taxjar_integration.mark_address_as_shipping",
					{ address_name: selected }
				);
			}

			d.hide();
			frm.save();
		},
		secondary_action_label: __("Add New Address"),
		secondary_action() {
			d.hide();
			let party_name = frm.doc.customer;
			frappe.new_doc("Address", {
				address_title: party_name,
				address_type: "Shipping",
				links: [{ link_doctype: "Customer", link_name: party_name }],
			});
		},
	});

	d.$wrapper.on("click", "tr[data-address]", function () {
		let addr_name = $(this).data("address");
		d.$wrapper.find('input[name="taxjar_addr"]').each(function () {
			if ($(this).val() === addr_name) $(this).prop("checked", true);
		});
		selected = addr_name;
	});

	d.$wrapper.on("change", 'input[name="taxjar_addr"]', function () {
		selected = $(this).val();
	});

	d.show();
}
