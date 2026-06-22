frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		_render_tax_breakdown(frm);
	},
});

frappe.ui.form.on("Sales Order Item", {
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
		<div class="tax-break-up" style="overflow-x: auto; background-color: var(--subtle-fg); border-radius: var(--border-radius); padding: 1px;">
			<table class="table table-bordered table-hover">
				<thead><tr>
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
		<div class="tax-break-up" style="overflow-x: auto; background-color: var(--subtle-fg); border-radius: var(--border-radius); padding: 1px;">
			<table class="table table-bordered table-hover">
				<thead><tr>
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

	const item = frappe.get_doc("Sales Order Item", cdn);
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
