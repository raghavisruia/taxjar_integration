// Data fetch lives in on_page_show, not the constructor - see the comment at
// the top of taxjar_transactions.js for why (cached desk pages, and the first
// on_page_show firing straight after on_page_load).
frappe.pages["taxjar-customers"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("TaxJar Customer Configuration"),
		single_column: true,
	});

	wrapper.taxjar_customers = new TaxJarCustomerConfig(page);
	$(wrapper).on("hide", () => wrapper.taxjar_customers.on_hide());
};

frappe.pages["taxjar-customers"].on_page_show = function (wrapper) {
	wrapper.taxjar_customers.on_show();
};

const EXEMPTION_OPTIONS = ["", "Wholesale", "Government", "Non Exempt", "Other"];

// Defined once in taxjar_utils.js (loaded globally via the app bundle, which is
// present on every desk page including this one).
const US_STATES = taxjar_integration.US_STATE_CODES;
const CA_PROVINCES = taxjar_integration.CA_PROVINCE_CODES;

const STATUS_COLORS = {
	Synced: "green",
	Failed: "red",
	Queued: "blue",
};

const SYNC_UPDATE_EVENT = "taxjar_customers_update";

const REGIONS_BLOCKED_MESSAGE = __("Set an exemption type first, then pick its exempt regions.");

// Whether an exemption is configured is the tab, not a filter - so there is
// only ever one way to express it.
const ALL_TAB = "all";
const EXEMPT_TAB = "exempt";
const NOT_CONFIGURED_TAB = "not_configured";

const TABS = [
	{ name: ALL_TAB, label: __("All"), is_active: true },
	{ name: EXEMPT_TAB, label: __("Exempted Customers") },
	{ name: NOT_CONFIGURED_TAB, label: __("Exemption Not Configured") },
];

// Header search fields. Every one is a LIKE term the server resolves against
// its own allowlist (_SEARCHABLE_COLUMNS), so the fieldname must match the
// Customer column it searches.
const SEARCH_FIELDS = [
	{ fieldname: "customer_name", label: __("Customer Name"), fieldtype: "Data" },
	{
		fieldname: "customer_group",
		label: __("Customer Group"),
		fieldtype: "Link",
		options: "Customer Group",
	},
	{ fieldname: "taxjar_customer_id", label: __("Customer ID (TaxJar)"), fieldtype: "Data" },
];

class TaxJarCustomerConfig {
	constructor(page) {
		this.page = page;
		this.current_page = 1;
		this.page_size = 20;
		this.active_tab = ALL_TAB;
		this.status_filter = null;
		this.selected = new Set();

		// Same reasoning as taxjar_transactions.js: one stable reference so
		// off() can detach it, debounced so a bulk sync of N customers doesn't
		// fire N refreshes.
		this._on_sync_update = frappe.utils.debounce(() => this.refresh(), 500);

		this.make_filters();
		this.make_summary();
		this.make_tabs();
		this.make_not_configured_panel();
	}

	on_show() {
		frappe.realtime.doctype_subscribe("Customer");
		frappe.realtime.off(SYNC_UPDATE_EVENT, this._on_sync_update);
		frappe.realtime.on(SYNC_UPDATE_EVENT, this._on_sync_update);
		this.refresh();
	}

	// Detaches the handler without doctype_unsubscribe - see on_hide() in
	// taxjar_transactions.js for why leaving the room would be unsafe.
	on_hide() {
		frappe.realtime.off(SYNC_UPDATE_EVENT, this._on_sync_update);
		this._on_sync_update.cancel();
	}

	// ── Shell ─────────────────────────────────────────────────────────────

	// page.add_field() puts these in the desk's own header filter row. They
	// narrow the whole result set server-side, not the loaded page - with
	// 20-row pages, searching a customer who is on page 3 would otherwise
	// silently report nothing found.
	make_filters() {
		this.filter_controls = {};

		for (const df of SEARCH_FIELDS) {
			this.filter_controls[df.fieldname] = this.page.add_field({
				...df,
				change: () => {
					this.current_page = 1;
					this.reset_selection();
					this.refresh();
				},
			});
		}
	}

	make_summary() {
		this.summary_area = $('<div class="taxjar-summary"></div>').appendTo(this.page.main);
	}

	make_tabs() {
		this.tabs_wrapper = $('<div class="taxjar-page-tabs"></div>').appendTo(this.page.main);

		// hidden: false and parent are both load-bearing here - see the comment
		// on make_tabs() in taxjar_transactions.js for why a FieldGroup outside
		// a form needs them.
		const tab_fields = TABS.reduce(
			(fields, tab) => [
				...fields,
				{
					fieldtype: "Tab Break",
					fieldname: `${tab.name}_tab`,
					label: tab.label,
					parent: "TaxJar Customer Configuration",
					hidden: false,
					active: tab.is_active ? 1 : 0,
				},
				{ fieldtype: "HTML", fieldname: `${tab.name}_html` },
			],
			[]
		);

		this.tab_group = new frappe.ui.FieldGroup({
			fields: tab_fields,
			body: this.tabs_wrapper,
		});
		this.tab_group.make();

		this.tabs = Object.fromEntries(this.tab_group.tabs.map((tab) => [tab.df.fieldname, tab]));

		this.make_tab_actions();
		this.setup_tab_change();

		this.paginator = new taxjar_integration.Paginator({
			$wrapper: $('<div class="taxjar-pagination"></div>').appendTo(this.page.main),
			on_page: (page) => {
				this.current_page = page;
				this.reset_selection();
				this.refresh();
			},
			on_page_size: (size) => {
				this.page_size = size;
				this.current_page = 1;
				this.reset_selection();
				this.refresh();
			},
		});
	}

	// Built detached; render_table() moves it above the active tab's table.
	make_tab_actions() {
		this.$tab_actions = $('<div class="taxjar-tab-actions"></div>');
		this.$selection_count = $('<span class="taxjar-selection-count"></span>').appendTo(this.$tab_actions);

		this.bulk_action = new taxjar_integration.BulkActionButton({
			$wrapper: this.$tab_actions,
			label: __("Bulk Action"),
		});
	}

	// Bound per tab, directly on the nav-link - see setup_tab_change() in
	// taxjar_transactions.js for why an ancestor delegate never fires.
	setup_tab_change() {
		TABS.forEach((tab) => {
			this.tabs[`${tab.name}_tab`]?.tab_link.find(".nav-link").on("click", () => {
				if (tab.name === this.active_tab) return;
				this.enter_tab(tab.name);
				this.refresh();
			});
		});
	}

	enter_tab(name) {
		this.active_tab = name;
		this.current_page = 1;
		this.status_filter = null;
		this.reset_selection();
		// Each tab's own card is its indicator - the card keys are the tab names.
		this.summary?.set_active(name);
	}

	go_to_tab(name) {
		this.tabs[`${name}_tab`]?.set_active();
		this.enter_tab(name);
	}

	make_not_configured_panel() {
		this.not_configured_panel = $('<div class="taxjar-not-configured"></div>')
			.hide()
			.appendTo(this.page.main);
	}

	show_not_configured() {
		this.summary_area.hide();
		this.tabs_wrapper.hide();
		this.paginator.$wrapper.hide();
		taxjar_integration.render_not_configured_panel(this.not_configured_panel);
		this.not_configured_panel.show();
	}

	hide_not_configured() {
		this.not_configured_panel.hide();
		this.summary_area.show();
		this.tabs_wrapper.show();
		this.paginator.$wrapper.show();
	}

	// ── Data ──────────────────────────────────────────────────────────────

	// What the page is scoped to. The summary counts describe exactly this
	// population - the status drill-down is deliberately not part of it.
	get_scope_filters() {
		const filters = {};
		const search = {};

		for (const [fieldname, control] of Object.entries(this.filter_controls)) {
			const value = control.get_value();
			if (value) search[fieldname] = value;
		}

		if (Object.keys(search).length) filters.search = search;

		return filters;
	}

	// The scope plus the status drill-down, which only the table honours -
	// feeding it to the summary would collapse every other number to zero the
	// moment you clicked one.
	get_filters() {
		const filters = this.get_scope_filters();

		if (this.status_filter) filters.sync_status = this.status_filter;

		return filters;
	}

	refresh() {
		const filters = this.get_filters();

		Promise.all([
			frappe.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.get_customers",
				{ filters, page: this.current_page, scope: this.active_tab, page_size: this.page_size }
			),
			frappe.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.get_summary",
				{ filters: this.get_scope_filters() }
			),
		]).then(([data, summary]) => {
			if (data.not_configured) {
				this.show_not_configured();
				return;
			}
			this.hide_not_configured();
			this.customers = data.customers;
			this.render_summary(summary);
			this.render_table();
			this.paginator.render(data);
			this.update_bulk_state();
		});
	}

	// The three group captions are the three tabs, so clicking one switches
	// tab; the sync statuses inside the Exemptions group filter within it.
	render_summary(summary) {
		const groups = [
			{
				label: __("Total Customers"),
				cards: [{ value: summary.total, value_key: ALL_TAB }],
			},
			{
				label: __("Exemptions"),
				cards: [
					{ label: __("Total"), value: summary.exempt.total, value_key: EXEMPT_TAB },
					{ divider: true },
					{ label: __("Synced"), value: summary.exempt.synced, value_key: "Synced", indicator: "green" },
					{ label: __("Queued"), value: summary.exempt.queued, value_key: "Queued", indicator: "blue" },
					{ label: __("Failed"), value: summary.exempt.failed, value_key: "Failed", indicator: "red" },
				],
			},
			{
				label: __("No exemption configured"),
				cards: [
					{ value: summary.not_configured, value_key: NOT_CONFIGURED_TAB },
				],
			},
		];

		if (!this.summary) {
			this.summary = new taxjar_integration.SummaryStrip({
				$wrapper: this.summary_area,
				groups,
				on_select: (card) => this.on_summary_select(card),
			});
			this.summary.set_active(this.active_tab);
			return;
		}

		const active = this.summary.active_key;
		this.summary.update(groups);
		this.summary.set_active(active);
	}

	on_summary_select(card) {
		const tab = TABS.map((t) => t.name).find((name) => name === card?.value_key);
		if (tab) {
			this.go_to_tab(tab);
			this.refresh();
			return;
		}

		// A sync status only describes an exempt customer, so drill into the
		// Exemptions tab alongside it.
		if (this.active_tab !== EXEMPT_TAB) this.go_to_tab(EXEMPT_TAB);
		this.status_filter = card?.value_key || null;
		this.summary.set_active(this.status_filter);
		this.current_page = 1;
		this.reset_selection();
		this.refresh();
	}

	// ── Table ─────────────────────────────────────────────────────────────

	get_columns() {
		const columns = [
			{
				label: __("Customer Name"),
				fieldname: "customer_name",
				_html: (value, row) =>
					`<a href="/app/customer/${encodeURIComponent(row.name)}">${frappe.utils.escape_html(
						value || row.name
					)}</a>`,
			},
			{ label: __("Customer Group"), fieldname: "customer_group", width: 160 },
			{
				label: __("Customer ID (TaxJar)"),
				fieldname: "taxjar_customer_id",
				width: 170,
				// Empty means no successful create in TaxJar yet, which is a
				// state worth naming rather than an empty cell.
				_html: (value) =>
					value
						? `<span class="taxjar-customer-id">${frappe.utils.escape_html(value)}</span>`
						: `<span class="text-muted">${__("Not synced yet")}</span>`,
			},
		];

		// Both columns would read "Not set" / "—" on every row of the
		// not-configured tab, so they only earn their width elsewhere.
		if (this.active_tab !== NOT_CONFIGURED_TAB) {
			columns.push(
				{
					label: __("Exemption Type"),
					fieldname: "taxjar_exemption_type",
					width: 180,
					// An always-visible Select rather than a read-only cell:
					// this page exists to change exemptions, and a cell you
					// have to discover is clickable makes it look like a report
					// you can only look at.
					_html: (value) => this.render_exemption_select(value),
				},
				{
					label: __("Regions"),
					fieldname: "exempt_region_count",
					width: 110,
					align: "center",
					_html: (value, row) => this.render_regions_cell(row),
				}
			);
		}

		columns.push({
			label: __("Sync Status"),
			fieldname: "taxjar_customer_sync_status",
			width: 120,
			_html: (value) => {
				const color = STATUS_COLORS[value];
				return color ? `<span class="indicator-pill ${color}">${__(value)}</span>` : "";
			},
		});

		return columns;
	}

	render_exemption_select(value) {
		const options = EXEMPTION_OPTIONS.map((opt) => {
			const selected = (value || "") === opt ? "selected" : "";
			const label = opt || __("(Not Set)");
			return `<option value="${frappe.utils.escape_html(opt)}" ${selected}>${frappe.utils.escape_html(
				label
			)}</option>`;
		}).join("");

		return `<select class="form-control input-xs taxjar-exemption-select">${options}</select>`;
	}

	// The pencil is on every row, including rows with no exemption type. An
	// affordance that disappears reads as "there is nothing to do here", when
	// the truth is "not yet" - so the blocked rows keep it and say why on
	// hover, and again on click (see bind_row_actions).
	//
	// The desk's own icon rather than a text glyph, whose size and baseline
	// shift from platform to platform. "square-pen", not "edit":
	// frappe.utils.icon() builds a <use href="#icon-{name}">, and an unknown
	// name resolves to nothing and renders blank rather than failing loudly -
	// which is exactly what "edit", absent from frappe's sprite, did here.
	render_regions_cell(row) {
		const configured = Boolean(row.taxjar_exemption_type);
		const title = configured ? __("Edit exempt regions") : REGIONS_BLOCKED_MESSAGE;

		return `<button type="button"
			class="taxjar-configure-link${configured ? "" : " taxjar-configure-link--blocked"}"
			data-customer="${frappe.utils.escape_html(row.name)}"
			title="${title}"
			>${configured ? row.exempt_region_count || 0 : ""}${frappe.utils.icon("square-pen", "sm")}</button>`;
	}

	// One table per tab: the columns differ between them, and each tab has its
	// own HTML field to render into.
	render_table() {
		this.table_wrappers = this.table_wrappers || {};
		if (!this.table_wrappers[this.active_tab]) {
			this.table_wrappers[this.active_tab] = this.make_table();
		}

		this.$table_wrapper = this.table_wrappers[this.active_tab];
		this.render_rows();

		// Move rather than copy, so the one instance of each - handlers and all
		// - follows whichever tab is showing. Both live inside the table
		// wrapper so they share its padding and line up with the table's edges
		// instead of merely happening to sit at the same inset.
		this.$tab_actions.prependTo(this.$table_wrapper);
		this.paginator.$wrapper.appendTo(this.$table_wrapper);
	}

	make_table() {
		const headers = this.get_columns()
			.map((col) => {
				const width = col.width ? ` style="width: ${col.width}px;"` : "";
				const align = col.align === "center" ? ' class="text-center"' : "";
				return `<th${align}${width}>${col.label}</th>`;
			})
			.join("");

		const $table_wrapper = $(`
			<div class="taxjar-table-wrapper">
				<table class="table table-bordered taxjar-customers-table">
					<thead>
						<tr>
							<th class="taxjar-check-cell">
								<input type="checkbox" class="taxjar-select-all" title="${__("Select All")}">
							</th>
							${headers}
						</tr>
					</thead>
					<tbody></tbody>
				</table>
			</div>
		`).appendTo(this.tab_group.get_field(`${this.active_tab}_html`).$wrapper);

		this.bind_row_actions($table_wrapper);

		return $table_wrapper;
	}

	render_rows() {
		const columns = this.get_columns();
		const $tbody = this.$table_wrapper.find("tbody").empty();

		if (!this.customers?.length) {
			$tbody.append(`
				<tr>
					<td colspan="${columns.length + 1}" class="taxjar-no-data text-muted text-center">
						${__("No customers found")}
					</td>
				</tr>
			`);
			this.sync_select_all();
			return;
		}

		for (const row of this.customers) {
			const cells = columns
				.map((col) => {
					const value = row[col.fieldname];
					const content = col._html
						? col._html(value, row)
						: frappe.utils.escape_html(value || "");
					return `<td${col.align === "center" ? ' class="text-center"' : ""}>${content}</td>`;
				})
				.join("");

			$tbody.append(`
				<tr data-customer="${frappe.utils.escape_html(row.name)}">
					<td class="taxjar-check-cell">
						<input type="checkbox" class="taxjar-row-check"
							${this.selected.has(row.name) ? "checked" : ""}>
					</td>
					${cells}
				</tr>
			`);
		}

		this.sync_select_all();
	}

	// Delegated once per table so they survive the tbody being re-rendered.
	// Both the Exemption Type and the Regions cell open onto the same decision,
	// which is why the Regions cell opens a dialog carrying the type as well.
	bind_row_actions($table_wrapper) {
		// .attr, not .data: jQuery coerces a numeric-looking data attribute to a
		// Number, and a Customer named by a numeric series would stop matching.
		const row_name = (el) => $(el).closest("tr").attr("data-customer");

		$table_wrapper.on("change", ".taxjar-select-all", (e) => this.toggle_all(e.target.checked));

		$table_wrapper.on("change", ".taxjar-row-check", (e) => {
			const name = row_name(e.currentTarget);
			if (e.currentTarget.checked) this.selected.add(name);
			else this.selected.delete(name);
			this.sync_select_all();
			this.update_bulk_state();
		});

		$table_wrapper.on("change", ".taxjar-exemption-select", (e) =>
			this.set_exemption_type(row_name(e.currentTarget), e.currentTarget.value)
		);

		$table_wrapper.on("click", ".taxjar-configure-link", (e) => {
			e.preventDefault();
			const name = $(e.currentTarget).attr("data-customer");
			const row = (this.customers || []).find((c) => c.name === name);
			if (!row) return;

			// Regions mean nothing without a type behind them, so the dialog
			// would have nothing it could usefully save. Say so rather than
			// opening it - the cue repeats the cell's own tooltip, for anyone
			// who clicked instead of hovering.
			if (!row.taxjar_exemption_type) {
				frappe.show_alert({ message: REGIONS_BLOCKED_MESSAGE, indicator: "orange" });
				return;
			}

			this.open_configure_dialog([row]);
		});
	}

	// Type only - regions are deliberately left alone here, since switching
	// Wholesale to Government must not silently discard a customer's configured
	// regions. See set_exemption_type on the server.
	set_exemption_type(customer, exemption_type) {
		frappe
			.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.set_exemption_type",
				{ customer, exemption_type }
			)
			.then(() => {
				frappe.show_alert({ message: __("Saved"), indicator: "green" });
				this.refresh();
			});
	}

	// ── Selection ─────────────────────────────────────────────────────────

	// Scoped to the rows on screen. A selection that outlived the page it was
	// made on would leave the count claiming rows nobody can see, and the bulk
	// actions acting on them - so every navigation clears it.
	reset_selection() {
		this.selected.clear();
	}

	get_checked() {
		return (this.customers || []).filter((c) => this.selected.has(c.name));
	}

	toggle_all(checked) {
		for (const c of this.customers || []) {
			if (checked) this.selected.add(c.name);
			else this.selected.delete(c.name);
		}

		this.$table_wrapper.find(".taxjar-row-check").prop("checked", checked);
		this.update_bulk_state();
	}

	sync_select_all() {
		const rows = this.customers || [];
		const all = rows.length > 0 && rows.every((c) => this.selected.has(c.name));
		this.$table_wrapper.find(".taxjar-select-all").prop("checked", all);
	}

	// ── Bulk actions ──────────────────────────────────────────────────────

	update_bulk_state() {
		const checked = this.get_checked();
		const failed = checked.filter((row) => row.taxjar_customer_sync_status === "Failed");

		this.$selection_count.text(checked.length ? __("{0} selected", [checked.length]) : "");

		if (!checked.length) {
			this.bulk_action.disabled_title = __("Select one or more records to run an action");
			this.bulk_action.set_items([]);
			return;
		}

		const items = [
			{
				label: __("Configure Exemption…"),
				action: () => this.open_configure_dialog(checked),
			},
		];

		// Clearing is a no-op on a tab where nothing has an exemption.
		if (this.active_tab !== NOT_CONFIGURED_TAB) {
			items.push({ label: __("Clear Exemption"), action: () => this.clear_exemption(checked) });
		}

		if (failed.length) {
			items.push({ divider: true });
			items.push({
				label: __("Resync with TaxJar"),
				action: () => this.retry_failed(failed),
			});
		}

		this.bulk_action.set_items(items);
	}

	clear_exemption(rows) {
		frappe
			.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.bulk_clear_exemption",
				{ customers: rows.map((r) => r.name) }
			)
			.then((r) => {
				frappe.show_alert({
					message: __("Exemption cleared for {0} customers", [r.updated]),
					indicator: "green",
				});
				this.after_bulk_action();
			});
	}

	retry_failed(rows) {
		frappe
			.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.bulk_sync_to_taxjar",
				{ customers: rows.map((r) => r.name) }
			)
			.then((r) => {
				frappe.show_alert({
					message: __("{0} customers queued for sync", [r.queued]),
					indicator: "blue",
				});
				this.after_bulk_action();
			});
	}

	after_bulk_action() {
		this.reset_selection();
		this.refresh();
	}

	// ── Configure Exemption dialog ────────────────────────────────────────

	// Type and regions in one dialog: they are one decision, and splitting
	// them is what previously let a customer keep exempt regions after its
	// exemption type was cleared.
	open_configure_dialog(rows) {
		const single = rows.length === 1 ? rows[0] : null;

		const prefill_regions = single
			? frappe.xcall(
					"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.get_exempt_regions",
					{ customer: single.name }
			  )
			: Promise.resolve([]);

		prefill_regions.then((regions) => {
			this.show_configure_dialog(rows, single?.taxjar_exemption_type || "", regions || []);
		});
	}

	show_configure_dialog(rows, exemption_type, existing_regions) {
		const selected = new Set(existing_regions.map((r) => `${r.country}:${r.state}`));
		const title = rows.length === 1
			? __("Configure Exemption: {0}", [rows[0].customer_name || rows[0].name])
			: __("Configure Exemption: {0} customers", [rows.length]);

		const make_checkboxes = (codes, country) =>
			codes
				.map((code) => {
					const checked = selected.has(`${country}:${code}`) ? "checked" : "";
					return `<label class="taxjar-region-option">
						<input type="checkbox" class="region-cb" data-country="${country}" data-state="${code}" ${checked}>
						<span>${code}</span>
					</label>`;
				})
				.join("");

		const dialog = new frappe.ui.Dialog({
			title,
			size: "large",
			fields: [
				{
					fieldtype: "Select",
					fieldname: "exemption_type",
					label: __("Exemption Type"),
					options: EXEMPTION_OPTIONS.map((opt) => ({
						label: opt || __("(Not Set)"),
						value: opt,
					})),
					default: exemption_type,
					change: () => toggle_regions(),
				},
				{ fieldtype: "Section Break" },
				{
					fieldtype: "HTML",
					fieldname: "regions_html",
					options: `
						<div class="taxjar-regions">
							${
								rows.length > 1
									? `<p class="text-muted small">${__(
											"Applying will replace the exempt regions on all {0} selected customers.",
											[rows.length]
									  )}</p>`
									: ""
							}
							<p class="taxjar-regions-hint text-muted small">${__(
								"Choose an exemption type to select exempt regions."
							)}</p>
							<div class="taxjar-regions-grids" style="display:flex;gap:32px;">
								<div style="flex:1;">
									<div class="taxjar-regions-head">
										<strong>${__("US States")}</strong>
										<label class="taxjar-region-select-all">
											<input type="checkbox" class="select-all-country" data-country="US">
											${__("Select All")}
										</label>
									</div>
									<div class="us-states-grid">${make_checkboxes(US_STATES, "US")}</div>
								</div>
								<div style="flex:1;">
									<div class="taxjar-regions-head">
										<strong>${__("CA Provinces")}</strong>
										<label class="taxjar-region-select-all">
											<input type="checkbox" class="select-all-country" data-country="CA">
											${__("Select All")}
										</label>
									</div>
									<div class="ca-provinces-grid">${make_checkboxes(CA_PROVINCES, "CA")}</div>
								</div>
							</div>
						</div>
					`,
				},
			],
			primary_action_label: __("Apply"),
			primary_action: () => {
				const type = dialog.get_value("exemption_type");
				const regions = type
					? dialog.$wrapper
							.find(".region-cb:checked")
							.map((_, cb) => ({
								country: $(cb).data("country"),
								state: $(cb).data("state"),
							}))
							.get()
					: [];

				dialog.hide();
				this.save_exemption(rows, type, regions);
			},
		});

		// The regions grid is only meaningful once a type is chosen - same rule
		// the Regions column follows, applied live as the Select changes.
		const toggle_regions = () => {
			const enabled = !!dialog.get_value("exemption_type");
			const $regions = dialog.$wrapper.find(".taxjar-regions-grids");
			$regions.toggleClass("taxjar-regions-disabled", !enabled);
			$regions.find("input").prop("disabled", !enabled);
			dialog.$wrapper.find(".taxjar-regions-hint").toggle(!enabled);
		};

		dialog.$wrapper.on("change", ".select-all-country", (e) => {
			const country = $(e.currentTarget).data("country");
			dialog.$wrapper
				.find(`.region-cb[data-country="${country}"]`)
				.prop("checked", e.currentTarget.checked);
		});

		dialog.show();
		toggle_regions();
	}

	save_exemption(rows, exemption_type, regions) {
		frappe
			.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.configure_exemption",
				{ customers: rows.map((r) => r.name), exemption_type, regions }
			)
			.then((r) => {
				frappe.show_alert({
					message: __("Updated {0} customers", [r.updated]),
					indicator: "green",
				});
				this.after_bulk_action();
			});
	}

}
