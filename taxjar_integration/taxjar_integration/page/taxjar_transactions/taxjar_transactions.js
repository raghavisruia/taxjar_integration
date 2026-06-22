frappe.pages["taxjar-transactions"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("TaxJar Transaction Sync"),
		single_column: true,
	});

	new TaxJarTransactionSync(page);
};

const STATUS_COLORS = {
	Synced: "green",
	Failed: "red",
	Queued: "blue",
	"Not Applicable": "grey",
};

class TaxJarTransactionSync {
	constructor(page) {
		this.page = page;
		this.filters = {};
		this.current_page = 1;
		this.selected = new Set();

		this.make_filters();
		this.make_bulk_actions();
		this.make_summary();
		this.make_table();
		this.refresh();
	}

	make_filters() {
		this.filter_area = $('<div class="taxjar-filters"></div>').appendTo(this.page.main);

		this.filter_company = this.page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			change: () => { this.current_page = 1; this.refresh(); },
		});

		this.filter_from_date = this.page.add_field({
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
			change: () => { this.current_page = 1; this.refresh(); },
		});

		this.filter_to_date = this.page.add_field({
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			change: () => { this.current_page = 1; this.refresh(); },
		});

		this.filter_sync_status = this.page.add_field({
			fieldname: "sync_status",
			label: __("Sync Status"),
			fieldtype: "Select",
			options: [
				{ label: __("All"), value: "" },
				{ label: __("Not Applicable"), value: "Not Applicable" },
				{ label: __("Queued"), value: "Queued" },
				{ label: __("Synced"), value: "Synced" },
				{ label: __("Failed"), value: "Failed" },
			],
			change: () => { this.current_page = 1; this.refresh(); },
		});

		this.filter_transaction_type = this.page.add_field({
			fieldname: "transaction_type",
			label: __("Transaction Type"),
			fieldtype: "Select",
			options: [
				{ label: __("All"), value: "" },
				{ label: __("Invoice"), value: "Invoice" },
				{ label: __("Credit Note"), value: "Credit Note" },
				{ label: __("Debit Note"), value: "Debit Note" },
			],
			change: () => { this.current_page = 1; this.refresh(); },
		});
	}

	make_bulk_actions() {
		this.bulk_area = $(`
			<div class="taxjar-bulk-actions" style="padding: 8px 15px; display: flex; align-items: center; gap: 8px;">
				<label style="margin: 0; display: flex; align-items: center; gap: 4px; cursor: pointer;">
					<input type="checkbox" class="taxjar-select-all"> ${__("Select All")}
				</label>
				<span class="taxjar-selection-count text-muted" style="margin-left: 4px;"></span>
				<div style="margin-left: auto; display: flex; gap: 8px;">
					<button class="btn btn-xs btn-primary taxjar-bulk-retry" disabled>
						${__("Retry Selected")}
					</button>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.bulk_area.find(".taxjar-select-all").on("change", (e) => {
			const checked = e.target.checked;
			this.page.main.find(".taxjar-row-check").prop("checked", checked);
			if (checked) {
				(this.invoices || []).forEach((inv) => this.selected.add(inv.name));
			} else {
				this.selected.clear();
			}
			this.update_bulk_state();
		});

		this.bulk_area.find(".taxjar-bulk-retry").on("click", () => this.bulk_retry());
	}

	make_summary() {
		this.summary_area = $(`
			<div class="taxjar-summary" style="padding: 8px 15px; display: flex; gap: 16px; flex-wrap: wrap;"></div>
		`).appendTo(this.page.main);
	}

	make_table() {
		this.table_wrapper = $(`
			<div class="taxjar-table-wrapper" style="padding: 0 15px;">
				<table class="table table-bordered" style="margin-bottom: 0;">
					<thead>
						<tr>
							<th style="width: 30px;"></th>
							<th>${__("Sales Invoice")}</th>
							<th style="width: 110px;">${__("Posting Date")}</th>
							<th style="width: 180px;">${__("Customer")}</th>
							<th style="width: 110px;">${__("Type")}</th>
							<th style="width: 120px;">${__("Grand Total")}</th>
							<th style="width: 120px;">${__("Sync Status")}</th>
							<th style="width: 150px;">${__("Last Synced")}</th>
							<th>${__("Error")}</th>
						</tr>
					</thead>
					<tbody class="taxjar-transactions-body"></tbody>
				</table>
				<div class="taxjar-pagination" style="padding: 12px 0; display: flex; align-items: center; justify-content: space-between;"></div>
			</div>
		`).appendTo(this.page.main);

		this.tbody = this.table_wrapper.find(".taxjar-transactions-body");
		this.pagination = this.table_wrapper.find(".taxjar-pagination");
	}

	get_filters() {
		const filters = {};

		const company = this.filter_company?.get_value();
		if (company) filters.company = company;

		const from_date = this.filter_from_date?.get_value();
		if (from_date) filters.from_date = from_date;

		const to_date = this.filter_to_date?.get_value();
		if (to_date) filters.to_date = to_date;

		const sync_status = this.filter_sync_status?.get_value();
		if (sync_status) filters.sync_status = sync_status;

		const transaction_type = this.filter_transaction_type?.get_value();
		if (transaction_type) filters.transaction_type = transaction_type;

		return filters;
	}

	refresh() {
		const filters = this.get_filters();

		Promise.all([
			frappe.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.get_transactions",
				{ filters, page: this.current_page },
			),
			frappe.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.get_summary",
				{ filters },
			),
		]).then(([data, summary]) => {
			this.invoices = data.invoices;
			this.total = data.total;
			this.total_pages = data.total_pages;
			this.render_summary(summary);
			this.render_table();
			this.render_pagination(data);
			this.update_bulk_state();
			this.bulk_area.find(".taxjar-select-all").prop("checked", false);
		});
	}

	render_summary(summary) {
		const items = [
			{ label: __("Total"), value: summary.total, color: "" },
			{ label: __("Synced"), value: summary.synced, color: "green" },
			{ label: __("Failed"), value: summary.failed, color: "red" },
			{ label: __("Queued"), value: summary.queued, color: "blue" },
		];

		this.summary_area.empty();
		items.forEach((item) => {
			const indicator = item.color ? `indicator-pill ${item.color}` : "text-muted";
			this.summary_area.append(`
				<span class="taxjar-summary-item">
					<span class="text-muted">${item.label}:</span>
					<span class="${indicator}" style="font-weight: 600;">${item.value}</span>
				</span>
			`);
		});
	}

	render_table() {
		this.tbody.empty();

		if (!this.invoices || !this.invoices.length) {
			this.tbody.append(`
				<tr><td colspan="9" class="text-muted text-center" style="padding: 40px;">
					${__("No transactions found")}
				</td></tr>
			`);
			return;
		}

		this.invoices.forEach((inv) => {
			const checked = this.selected.has(inv.name) ? "checked" : "";
			const status_color = STATUS_COLORS[inv.taxjar_sync_status] || "grey";
			const status_label = inv.taxjar_sync_status === "Not Applicable" ? __("NA") : __(inv.taxjar_sync_status);
			const status_html = inv.taxjar_sync_status
				? `<span class="indicator-pill ${status_color}">${status_label}</span>`
				: "";

			const last_synced = inv.taxjar_last_synced
				? frappe.datetime.prettyDate(inv.taxjar_last_synced)
				: "";

			const error_html = inv.taxjar_sync_error
				? `<span class="text-danger" title="${frappe.utils.escape_html(inv.taxjar_sync_error)}">${frappe.utils.escape_html(inv.taxjar_sync_error)}</span>`
				: "";

			const row = $(`
				<tr data-invoice="${frappe.utils.escape_html(inv.name)}">
					<td><input type="checkbox" class="taxjar-row-check" ${checked}></td>
					<td><a href="/app/sales-invoice/${encodeURIComponent(inv.name)}">${frappe.utils.escape_html(inv.name)}</a></td>
					<td>${frappe.utils.escape_html(inv.posting_date)}</td>
					<td>${frappe.utils.escape_html(inv.customer_name || "")}</td>
					<td>${frappe.utils.escape_html(inv.transaction_type || "")}</td>
					<td style="text-align: right;">${frappe.format(inv.grand_total, { fieldtype: "Currency" })}</td>
					<td>${status_html}</td>
					<td>${last_synced}</td>
					<td>${error_html}</td>
				</tr>
			`);

			row.find(".taxjar-row-check").on("change", (e) => {
				if (e.target.checked) {
					this.selected.add(inv.name);
				} else {
					this.selected.delete(inv.name);
				}
				this.update_bulk_state();
			});

			this.tbody.append(row);
		});
	}

	render_pagination(data) {
		this.pagination.empty();

		const start = (data.page - 1) * data.page_size + 1;
		const end = Math.min(data.page * data.page_size, data.total);

		this.pagination.append(`
			<span class="text-muted">
				${data.total ? __("Showing {0} - {1} of {2}", [start, end, data.total]) : __("No results")}
			</span>
			<div style="display: flex; align-items: center; gap: 8px;">
				<button class="btn btn-xs btn-default taxjar-prev" ${data.page <= 1 ? "disabled" : ""}>
					${__("← Prev")}
				</button>
				<span>${__("Page {0} of {1}", [data.page, data.total_pages])}</span>
				<button class="btn btn-xs btn-default taxjar-next" ${data.page >= data.total_pages ? "disabled" : ""}>
					${__("Next →")}
				</button>
			</div>
		`);

		this.pagination.find(".taxjar-prev").on("click", () => {
			if (this.current_page > 1) {
				this.current_page--;
				this.refresh();
			}
		});

		this.pagination.find(".taxjar-next").on("click", () => {
			if (this.current_page < data.total_pages) {
				this.current_page++;
				this.refresh();
			}
		});
	}

	update_bulk_state() {
		const count = this.selected.size;
		const disabled = count === 0;

		this.bulk_area.find(".taxjar-bulk-retry").prop("disabled", disabled);
		this.bulk_area.find(".taxjar-selection-count").text(
			count ? __("{0} selected", [count]) : ""
		);
	}

	bulk_retry() {
		const selected = Array.from(this.selected);
		const failed_selected = selected.filter((name) => {
			const inv = (this.invoices || []).find((i) => i.name === name);
			return inv && inv.taxjar_sync_status === "Failed";
		});

		if (!failed_selected.length) {
			frappe.msgprint(__("No failed transactions in selection."));
			return;
		}

		frappe.xcall(
			"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.bulk_retry",
			{ invoices: failed_selected },
		).then((r) => {
			frappe.show_alert({
				message: __("{0} transactions queued for retry", [r.queued]),
				indicator: "blue",
			});
			this.selected.clear();
			this.refresh();
		});
	}
}
