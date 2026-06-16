// Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

function _render_nexus_html(frm) {
	const wrapper = frm.fields_dict.nexus_html.$wrapper;
	const rows = frm.doc.nexus || [];

	if (!rows.length) {
		wrapper.html(`
			<div style="
				padding: 24px;
				text-align: center;
				color: var(--text-muted);
				font-size: 13px;
				border: 1px dashed var(--border-color);
				border-radius: var(--border-radius-md);
				margin: 8px 0;
			">
				No nexus regions loaded. Click <strong>Update Nexus List</strong> to fetch from TaxJar.
			</div>
		`);
		return;
	}

	// Group rows by company
	const by_company = {};
	for (const row of rows) {
		const key = row.company || '(No Company)';
		if (!by_company[key]) by_company[key] = [];
		by_company[key].push(row);
	}

	const card_style = `
		border: 1px solid var(--border-color);
		border-radius: var(--border-radius-md);
		margin-bottom: 16px;
		overflow: hidden;
	`;
	const header_style = `
		background: var(--subtle-fg);
		padding: 10px 16px;
		font-weight: 600;
		font-size: 13px;
		color: var(--heading-color);
		border-bottom: 1px solid var(--border-color);
	`;
	const table_wrap_style = `overflow-x: auto;`;
	const table_style = `
		width: 100%;
		border-collapse: collapse;
		font-size: 12px;
	`;
	const th_style = `
		text-align: left;
		padding: 8px 16px;
		color: var(--text-muted);
		font-weight: 500;
		border-bottom: 1px solid var(--border-color);
		white-space: nowrap;
	`;
	const td_style = `
		padding: 8px 16px;
		border-bottom: 1px solid var(--border-color);
		white-space: nowrap;
	`;
	const td_last_style = `
		padding: 8px 16px;
		white-space: nowrap;
	`;

	let html = '';
	for (const [company, company_rows] of Object.entries(by_company)) {
		const badge = `<span style="
			background: var(--bg-blue);
			color: var(--text-on-blue);
			border-radius: 10px;
			padding: 1px 8px;
			font-size: 11px;
			font-weight: 500;
			margin-left: 8px;
			vertical-align: middle;
		">${company_rows.length}</span>`;

		const rows_html = company_rows.map((r, idx) => {
			const is_last = idx === company_rows.length - 1;
			const cell = is_last ? td_last_style : td_style;
			return `
				<tr>
					<td style="${cell}">${frappe.utils.escape_html(r.region || '—')}</td>
					<td style="${cell}"><code>${frappe.utils.escape_html(r.region_code || '—')}</code></td>
					<td style="${cell}">${frappe.utils.escape_html(r.country || '—')}</td>
					<td style="${cell}"><code>${frappe.utils.escape_html(r.country_code || '—')}</code></td>
				</tr>`;
		}).join('');

		html += `
			<div style="${card_style}">
				<div style="${header_style}">${frappe.utils.escape_html(company)}${badge}</div>
				<div style="${table_wrap_style}">
					<table style="${table_style}">
						<thead>
							<tr>
								<th style="${th_style}">Region</th>
								<th style="${th_style}">Code</th>
								<th style="${th_style}">Country</th>
								<th style="${th_style}">Country Code</th>
							</tr>
						</thead>
						<tbody>${rows_html}</tbody>
					</table>
				</div>
			</div>`;
	}

	wrapper.html(html);
}

const API_MODE_DESCRIPTIONS = {
	Sandbox: 'Requests & response payloads are validated. Tax rates might be stale and no transactions are recorded. API calls does not consume plan quota.',
	Live: 'Sales tax calculations based on nexus, transactions are recorded for reporting and auto-filing. API calls consume plan quota.',
};

function _set_api_mode_description(frm) {
	const desc = API_MODE_DESCRIPTIONS[frm.doc.api_mode]
		|| 'Select a mode to use TaxJar API Features';
	frm.set_df_property('api_mode', 'description', desc);
}

frappe.ui.form.on('TaxJar Settings', {
	refresh(frm) {
		_render_nexus_html(frm);
		_set_api_mode_description(frm);

		const account_query = (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { company: row.company, is_group: 0 } };
		};
		frm.set_query('tax_account_head', 'company_config', account_query);
		frm.set_query('shipping_account_head', 'company_config', account_query);
	},

	api_mode(frm) {
		_set_api_mode_description(frm);
	},

	update_nexus_list_btn(frm) {
		frm.call({
			doc: frm.doc,
			method: 'update_nexus_list',
			callback: () => {
				frm.refresh();
				_render_nexus_html(frm);
				frappe.show_alert({ message: __('Nexus regions fetched and updated.'), indicator: 'green' }, 5);
			},
		});
	},
});
