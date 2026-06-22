frappe.ui.form.on("Quotation", {
	refresh(frm) {
		_render_tax_breakdown(frm);
		_render_item_tax_breakdown(frm);
	},
});

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

function _render_item_tax_breakdown(frm) {
	for (const item of frm.doc.items || []) {
		const field = frm.fields_dict.items?.grid?.grid_rows_by_docname[item.name]
			?.grid_form?.fields_dict?.taxjar_item_breakdown_html;
		if (!field) continue;

		if (!item.taxjar_item_breakdown_json) {
			field.$wrapper.html("");
			continue;
		}

		let data;
		try {
			data = JSON.parse(item.taxjar_item_breakdown_json);
		} catch (e) {
			field.$wrapper.html("");
			continue;
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
}
