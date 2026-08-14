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
			<div class="report-summary" style="padding: 20px 15px; display: flex; border-bottom: 1px solid var(--border-color);"></div>
		`).appendTo(this.page.main);
	}

	make_table() {
		this.table_wrapper = $(`
			<div class="taxjar-table-wrapper" style="padding: 0 15px;">
				<table class="table table-bordered" style="margin-bottom: 0;">
					<thead>
						<tr>
							<th style="width: 30px;"></th>
							<th style="width: 110px;">${__("Posting Date")}</th>
							<th>${__("Transaction ID")}</th>
							<th style="width: 180px;">${__("Customer")}</th>
							<th style="width: 110px;">${__("Type")}</th>
							<th style="width: 120px;">${__("Grand Total")}</th>
							<th style="width: 100px;">${__("Doc Status")}</th>
							<th style="width: 140px;">${__("Sync Status")}</th>
						</tr>
					</thead>
					<tbody class="taxjar-transactions-body"></tbody>
				</table>
				<div class="taxjar-pagination" style="padding: 12px 0; display: flex; align-items: center; justify-content: space-between;"></div>
			</div>
		`).appendTo(this.page.main);

		this.tbody = this.table_wrapper.find(".taxjar-transactions-body");
		this.pagination = this.table_wrapper.find(".taxjar-pagination");

		this.not_configured_panel = $('<div class="taxjar-not-configured"></div>')
			.hide()
			.appendTo(this.page.main);

		// Delegated once here (rather than rebound on every render_table()
		// call) so it keeps working across re-renders without re-wiring.
		// Shows immediately on hover - no native-tooltip delay - and also
		// toggles on click, since hover never fires on touch devices.
		this.tbody.on("mouseenter", ".taxjar-sync-trigger", (e) => {
			this._show_sync_popover($(e.currentTarget));
		});
		this.tbody.on("mouseleave", ".taxjar-sync-trigger", () => this._hide_sync_popover());
		this.tbody.on("click", ".taxjar-sync-trigger", (e) => {
			e.stopPropagation();
			this._show_sync_popover($(e.currentTarget));
		});
	}

	_show_sync_popover($trigger) {
		this._hide_sync_popover();
		const text = $trigger.attr("data-info") || "";
		const $pop = $(`<div class="taxjar-sync-pop">${frappe.utils.escape_html(text)}</div>`).appendTo("body");
		// position: fixed + getBoundingClientRect() are both viewport-relative,
		// so no scroll-offset math is needed here. Sync Status is the table's
		// last column, right up against the viewport edge, so the popover's
		// own width is clamped back on-screen rather than just using rect.left.
		const rect = $trigger[0].getBoundingClientRect();
		const pop_width = $pop.outerWidth();
		const left = Math.min(rect.left, window.innerWidth - pop_width - 12);
		$pop.css({ top: rect.bottom + 6, left: Math.max(12, left) });
		this._active_pop = $pop;
		$(document).on("click.taxjarSyncPop", () => this._hide_sync_popover());
	}

	_hide_sync_popover() {
		if (this._active_pop) {
			this._active_pop.remove();
			this._active_pop = null;
		}
		$(document).off("click.taxjarSyncPop");
	}

	show_not_configured() {
		this.bulk_area.hide();
		this.summary_area.hide();
		this.table_wrapper.hide();
		taxjar_integration.render_not_configured_panel(this.not_configured_panel);
		this.not_configured_panel.show();
	}

	hide_not_configured() {
		this.not_configured_panel.hide();
		this.bulk_area.show();
		this.summary_area.show();
		this.table_wrapper.show();
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
			if (data.not_configured) {
				this.show_not_configured();
				return;
			}
			this.hide_not_configured();
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
			{ label: __("Total Invoices"), value: summary.total, color: "" },
			{ label: __("Synced"), value: summary.synced, color: "green" },
			{ label: __("Failed"), value: summary.failed, color: "red" },
			{ label: __("Queued"), value: summary.queued, color: "blue" },
		];

		this.summary_area.empty();
		items.forEach((item) => {
			const value_color = item.color ? `var(--text-on-${item.color})` : "var(--text-color)";
			this.summary_area.append(`
				<div class="summary-item" style="flex: 1; text-align: center;">
					<div class="text-muted" style="font-size: var(--text-sm);">${item.label}</div>
					<div style="font-size: var(--text-2xl); font-weight: 600; color: ${value_color}; margin-top: 4px;">
						${item.value}
					</div>
				</div>
			`);
		});
	}

	render_table() {
		this.tbody.empty();

		if (!this.invoices || !this.invoices.length) {
			this.tbody.append(`
				<tr><td colspan="8" class="text-muted text-center" style="padding: 40px;">
					${__("No transactions found")}
				</td></tr>
			`);
			return;
		}

		this.invoices.forEach((inv) => {
			const checked = this.selected.has(inv.name) ? "checked" : "";

			const row = $(`
				<tr data-invoice="${frappe.utils.escape_html(inv.name)}">
					<td><input type="checkbox" class="taxjar-row-check" ${checked}></td>
					<td>${frappe.utils.escape_html(inv.posting_date)}</td>
					<td><a href="/app/sales-invoice/${encodeURIComponent(inv.name)}">${frappe.utils.escape_html(inv.name)}</a></td>
					<td>${frappe.utils.escape_html(inv.customer_name || "")}</td>
					<td>${frappe.utils.escape_html(inv.transaction_type || "")}</td>
					<td style="text-align: right;">${frappe.format(inv.grand_total, { fieldtype: "Currency" })}</td>
					<td>${frappe.utils.escape_html(inv.doc_status || "")}</td>
					<td>${this.render_sync_status_cell(inv)}</td>
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

	// Synced rows carry their detail (last-synced time) as a hover/click
	// popover on the pill itself - no separate info icon needed since the
	// pill's own text already says everything else. Queued and Failed still
	// pair the pill with a separate info icon (never nested inside the pill)
	// for their popover, since the pill text alone doesn't carry the detail.
	// Retrying a failed row goes through the checkbox + "Retry Selected" bulk
	// action rather than a dedicated button here, so Failed reads the same as
	// every other status.
	render_sync_status_cell(inv) {
		const status = inv.taxjar_sync_status;
		if (!status) return "";

		const color = STATUS_COLORS[status] || "grey";
		const label = status === "Not Applicable" ? __("NA") : __(status);

		if (status === "Synced") {
			if (!inv.taxjar_last_synced) {
				return `<span class="indicator-pill ${color}">${label}</span>`;
			}
			const info_text = __("Last synced: {0}", [frappe.datetime.prettyDate(inv.taxjar_last_synced)]);
			return `<span class="indicator-pill ${color} taxjar-sync-trigger" data-info="${frappe.utils.escape_html(info_text)}">${label}</span>`;
		}

		const pill = `<span class="indicator-pill ${color}">${label}</span>`;

		let info_text = "";
		if (status === "Queued") {
			info_text = __("Queued for sync");
		} else if (status === "Failed") {
			info_text = inv.taxjar_sync_error || __("Unknown error");
		}
		if (!info_text) return pill;

		const icon = `<button type="button" class="taxjar-sync-icon taxjar-sync-trigger" data-info="${frappe.utils.escape_html(info_text)}">${frappe.utils.icon("info", "sm")}</button>`;
		return `${pill}${icon}`;
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
