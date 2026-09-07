// Desk pages are built once and cached in frappe.pages[name]; revisiting the
// route only un-hides the existing DOM and fires on_page_show. So the data
// fetch lives in on_page_show, not the constructor - otherwise the page keeps
// showing whatever it loaded the first time until a browser reload. The
// constructor deliberately does NOT fetch: on first visit the "show" handler is
// bound before container.change_to() runs, so on_page_show already fires right
// after on_page_load and doing both would double-fetch every load.
frappe.pages["taxjar-transactions"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("TaxJar Transaction Sync"),
		single_column: true,
	});

	wrapper.taxjar_transactions = new TaxJarTransactionSync(page);

	// Frappe has no on_page_hide page event; container.js triggers a plain
	// "hide" on the outgoing container div, which is this wrapper.
	$(wrapper).on("hide", () => wrapper.taxjar_transactions.on_hide());
};

frappe.pages["taxjar-transactions"].on_page_show = function (wrapper) {
	wrapper.taxjar_transactions.on_show();
};

const STATUS_COLORS = {
	Synced: "green",
	Failed: "red",
	Queued: "blue",
	Excluded: "gray",
};

const DOC_STATUS_COLORS = { Draft: "gray", Submitted: "blue", Cancelled: "red" };

const SYNC_UPDATE_EVENT = "taxjar_transactions_update";

// One tab per state a transaction can be in, ordered by how much attention it
// wants: what needs fixing first, what is still moving, then the two resting
// states and finally the one that needs nothing. Together they partition the
// table - every invoice in range is in exactly one - and each is the population
// of the summary card above it, so the strip and the tabs cannot disagree.
const FAILED_TAB = "failed";
const QUEUED_TAB = "queued";
const NOT_APPLICABLE_TAB = "not_applicable";
const DRAFT_TAB = "draft";
const SYNCED_TAB = "synced";

const TABS = [
	{ name: FAILED_TAB, label: __("Failed"), is_active: true },
	{ name: QUEUED_TAB, label: __("Queued") },
	{ name: NOT_APPLICABLE_TAB, label: __("Not Applicable") },
	{ name: DRAFT_TAB, label: __("Draft") },
	{ name: SYNCED_TAB, label: __("Synced") },
];

// Submitted is the normal resting state and Draft the one before it, so both
// stay quiet; Cancelled is the one worth noticing in a list that mixes them.

class TaxJarTransactionSync {
	constructor(page) {
		this.page = page;
		this.current_page = 1;
		this.page_size = 20;
		this.active_tab = FAILED_TAB;
		this.column_search = {};

		// Built once so on_hide() has the same reference to pass to
		// frappe.realtime.off(). Debounced because a bulk retry of N invoices
		// publishes N events and each refresh() is two server calls - without
		// this, retrying 500 rows would fire 500 refreshes.
		this._on_sync_update = frappe.utils.debounce(() => this.refresh(), 500);

		this.make_filters();
		this.make_summary();
		this.make_tabs();
		this.make_not_configured_panel();
	}

	on_show() {
		// Joining is idempotent, and the server permission-checks the join.
		frappe.realtime.doctype_subscribe("Sales Invoice");
		// off() first so a stray double-show can't stack the same handler.
		frappe.realtime.off(SYNC_UPDATE_EVENT, this._on_sync_update);
		frappe.realtime.on(SYNC_UPDATE_EVENT, this._on_sync_update);
		this.refresh();
	}

	// Detaches our handler but deliberately does NOT doctype_unsubscribe: the
	// Sales Invoice list view subscribes to the same room and only sets itself
	// up once (guarded by its realtime_events_setup flag), so leaving the room
	// on our behalf could silently kill its auto-refresh.
	on_hide() {
		frappe.realtime.off(SYNC_UPDATE_EVENT, this._on_sync_update);
		this._on_sync_update.cancel();
		this._hide_sync_popover();
	}

	// ── Shell ─────────────────────────────────────────────────────────────

	make_filters() {
		this.filter_area = $('<div class="taxjar-filters"></div>').appendTo(this.page.main);

		this.filter_company = this.add_filter({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			// Same default every ERPNext report opens with, so the page lands on
			// the company you actually work in rather than every company at once.
			default: frappe.defaults.get_user_default("Company"),
		});

		this.filter_from_date = this.add_filter({
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
		});

		this.filter_to_date = this.add_filter({
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		});

		this.filter_transaction_type = this.add_filter({
			fieldname: "transaction_type",
			label: __("Transaction Type"),
			fieldtype: "Select",
			options: [
				{ label: __("All"), value: "" },
				{ label: __("Sales Invoice"), value: "Sales Invoice" },
				{ label: __("Credit Note"), value: "Credit Note" },
				{ label: __("Debit Note"), value: "Debit Note" },
			],
		});
	}

	// frappe.ui.form.make_control() directly, rather than page.add_field(),
	// since add_field() hardcodes only_input: true and drops the label,
	// leaving just a placeholder - fine for the standard page toolbar, not
	// for a filter row that needs visible labels.
	add_filter(df) {
		df.change = () => {
			this.current_page = 1;
			this.refresh();
		};
		const control = frappe.ui.form.make_control({
			df,
			parent: this.filter_area,
			only_input: false,
		});
		control.refresh();
		if (df.default) control.set_input(df.default);
		return control;
	}

	make_summary() {
		this.summary_area = $('<div class="taxjar-summary"></div>').appendTo(this.page.main);
	}

	// frappe.ui.tabs, not a frappe.ui.FieldGroup of Tab Break fields - the
	// latter needed a form Layout to fight (hidden-field workarounds, an
	// event-delegation dodge, see the pre-Espresso comments this replaced),
	// none of which a real tabs component needs. Each tab's `content` is a
	// function that builds and caches this page's own table wrapper div,
	// mirroring the lazy build-once-per-tab pattern this page already used.
	make_tabs() {
		this.tabs_wrapper = $('<div class="taxjar-page-tabs"></div>').appendTo(this.page.main);
		this.tab_content_wrappers = {};

		this.tabsInstance = new frappe.ui.Tabs({
			tabs: TABS.map((tab) => ({
				label: tab.label,
				content: () => {
					const $wrapper = $('<div class="taxjar-tab-panel"></div>');
					this.tab_content_wrappers[tab.name] = $wrapper;
					return $wrapper;
				},
			})),
			on_change: (index) => {
				this.enter_tab(TABS[index].name);
				this.refresh();
			},
		});
		this.tabs_wrapper.append(this.tabsInstance.$el);

		this.make_tab_actions();

		this.paginator = new taxjar_integration.Paginator({
			$wrapper: $('<div class="taxjar-pagination"></div>').appendTo(this.page.main),
			on_page: (page) => {
				this.current_page = page;
				this.refresh();
			},
			on_page_size: (size) => {
				// Every page boundary moves, so the old page number is meaningless.
				this.page_size = size;
				this.current_page = 1;
				this.refresh();
			},
		});
	}

	// Selection count + Bulk Action. Built detached and moved into the active
	// tab's table wrapper by render_table(), so it sits directly above the rows
	// it acts on and inherits that wrapper's padding.
	make_tab_actions() {
		this.$tab_actions = $('<div class="taxjar-tab-actions"></div>');
		this.$selection_count = $('<span class="taxjar-selection-count"></span>').appendTo(this.$tab_actions);

		this.bulk_action = new taxjar_integration.BulkActionButton({
			$wrapper: this.$tab_actions,
			label: __("Bulk Action"),
		});
	}

	// State side of a tab switch, without touching frappe's tab UI - safe to
	// call both from a user click (where frappe already switched) and from
	// go_to_tab (which drives the UI first).
	enter_tab(name) {
		this.active_tab = name;
		this.current_page = 1;
		// Each card counts exactly one tab's population, so the strip follows
		// the tabs rather than filtering them.
		this.summary?.set_active(name);
	}

	// silent: true - enter_tab() runs the state side of the switch itself,
	// and every go_to_tab() caller already calls refresh() right after; without
	// it, set_active()'s on_change would fire a second, redundant refresh.
	go_to_tab(name) {
		this.tabsInstance.set_active(TABS.findIndex((t) => t.name === name), { silent: true });
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

	// What the page is scoped to: company, dates, type and the inline column
	// search. Which tab is open is NOT part of it - the tab is sent separately
	// as the scope, so the summary can count every tab's population under the
	// same filters while the table shows one of them.
	get_scope_filters() {
		const filters = {};

		const company = this.filter_company?.get_value();
		if (company) filters.company = company;

		const from_date = this.filter_from_date?.get_value();
		if (from_date) filters.from_date = from_date;

		const to_date = this.filter_to_date?.get_value();
		if (to_date) filters.to_date = to_date;

		const transaction_type = this.filter_transaction_type?.get_value();
		if (transaction_type) filters.transaction_type = transaction_type;

		// The datatable's inline filter row, resolved server-side so it
		// narrows the whole result set rather than the loaded page.
		if (Object.keys(this.column_search).length) filters.search = this.column_search;

		return filters;
	}

	refresh() {
		const filters = this.get_scope_filters();

		Promise.all([
			frappe.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.get_transactions",
				{ filters, page: this.current_page, scope: this.active_tab, page_size: this.page_size }
			),
			frappe.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.get_summary",
				{ filters }
			),
		]).then(([data, summary]) => {
			if (data.not_configured) {
				this.show_not_configured();
				return;
			}
			this.hide_not_configured();
			this.invoices = data.invoices;
			this.render_summary(summary);
			this.render_table();
			this.paginator.render(data);
			this.update_bulk_state();
		});
	}

	// Two captioned groups so "All Transactions" and "Draft" read as
	// separate totals. Every number drills the table down without disturbing
	// the company/date filters above.
	render_summary(summary) {
		const groups = [
			{
				label: __("Included"),
				cards: [
					{ label: __("Synced"), value: summary.submitted.synced, value_key: SYNCED_TAB, indicator: "green" },
					{ label: __("Queued"), value: summary.submitted.queued, value_key: QUEUED_TAB, indicator: "blue" },
					{ label: __("Failed"), value: summary.submitted.failed, value_key: FAILED_TAB, indicator: "red" },
				],
			},
			{
				// The two ways a transaction ends up outside TaxJar: not
				// submitted yet, or submitted and deliberately not sent.
				label: __("Excluded"),
				cards: [
					{ label: __("Draft"), value: summary.draft.total, value_key: DRAFT_TAB },
					{
						// The stored status is "Excluded" - the group heading
						// already says that, so the card names the case instead.
						label: __("Not Applicable"),
						value: summary.submitted.excluded,
						value_key: NOT_APPLICABLE_TAB,
						indicator: "grey",
					},
				],
			},
		];

		if (!this.summary) {
			this.summary = new taxjar_integration.SummaryStrip({
				$wrapper: this.summary_area,
				groups,
				on_select: (card) => this.on_summary_select(card),
			});
			return;
		}

		const active = this.summary.active_key;
		this.summary.update(groups);
		this.summary.set_active(active);
	}

	// Every card counts one tab, so clicking one opens that tab rather than
	// filtering the current one. The strip toggles its own selection off when
	// the active card is clicked again; there is no "no tab" state to go to,
	// so that just re-asserts where we already are.
	on_summary_select(card) {
		if (!card) {
			this.summary.set_active(this.active_tab);
			return;
		}

		this.go_to_tab(card.value_key);
		this.refresh();
	}

	// ── Table ─────────────────────────────────────────────────────────────

	get_columns() {
		const columns = [
			{
				label: __("Posting Date"),
				fieldname: "posting_date",
				fieldtype: "Date",
			},
			{
				label: __("Transaction ID"),
				fieldname: "name",
				_html: (value) =>
					`<a href="/app/sales-invoice/${encodeURIComponent(value)}">${frappe.utils.escape_html(
						value
					)}</a>`,
			},
			{ label: __("Customer"), fieldname: "customer_name" },
			{ label: __("Type"), fieldname: "transaction_type" },
			{
				label: __("Grand Total"),
				fieldname: "grand_total",
				fieldtype: "Currency",
				align: "right",
			},
		];

		// Both of these used to be dropped from the tabs where every row would
		// give the same answer - Transaction Status off Draft, Sync Status off
		// Draft and Excluded. It saved a repetitive column and cost more than it
		// saved: the columns moved under you as you crossed the tabs, so the
		// same reading sat in a different place on each one and the table stopped
		// being one table. A uniform row is worth a repeated word.
		columns.push({
			label: __("Transaction Status"),
			fieldname: "doc_status",
			_html: (value) =>
				value
					? frappe.ui.badge.html({ label: __(value), theme: DOC_STATUS_COLORS[value] || "gray" })
					: "",
		});

		columns.push({
			label: __("Sync Status"),
			fieldname: "taxjar_sync_status",
			_html: (value, row) => this.render_sync_status_cell(row),
		});

		return columns;
	}

	render_table() {
		const $wrapper = this.tab_content_wrappers[this.active_tab];
		const key = this.active_tab;

		// Columns differ per tab, and so does checkboxColumn - both are fixed
		// at construction in frappe.DataTable, so each tab keeps its own
		// instance rather than one being reconfigured on the fly.
		if (!this.datatables) this.datatables = {};

		if (!this.datatables[key]) {
			this.datatables[key] = new taxjar_integration.DataTableManager({
				$wrapper,
				columns: this.get_columns(),
				data: this.invoices,
				options: {
					// Retry is the only bulk action, so selection is offered
					// exactly where it can be run.
					checkboxColumn: key === FAILED_TAB,
					noDataMessage: __("No transactions found"),
				},
				on_check_row: () => this.update_bulk_state(),
				on_filter_change: (search) => {
					this.column_search = search;
					this.current_page = 1;
					this.refresh();
				},
			});
			this.bind_sync_popover($wrapper);
		} else {
			this.datatables[key].refresh(this.invoices);
		}

		// Move rather than copy, so the one instance of each - handlers and all
		// - follows whichever tab is showing. Both live inside the table's own
		// wrapper so they share its padding and line up with its edges instead
		// of merely happening to sit at the same inset.
		this.$tab_actions.prependTo($wrapper);
		this.paginator.$wrapper.appendTo($wrapper);
	}

	get datatable() {
		return this.datatables?.[this.active_tab];
	}

	// Synced rows carry their detail (last-synced time) as a hover/click
	// popover on the pill itself - no separate info icon needed since the
	// pill's own text already says everything else. Failed and Excluded each
	// pair the pill with a separate info icon (never nested inside the pill)
	// for its popover, since neither pill text carries what the reader wants:
	// the error, or why the document was kept out. Queued says all it has to
	// say in the pill.
	render_sync_status_cell(row) {
		// Nothing syncs before submit (see enqueue_taxjar_sync's on_submit
		// hook), so whatever the field happens to hold for a draft - "Excluded",
		// by its own default - is not a report about the document, and printing
		// it would say the one thing that is false. The invoice form names this
		// state "Submit to Sync" (see _render_taxjar_sync_status_pill); so does
		// this. No hover: unlike every other state here, the pill is an
		// instruction and complete in itself, and the form's matching sentence
		// repeated down a whole tab of drafts would be noise.
		if (row.docstatus === 0) {
			return frappe.ui.badge.html({ label: __("Submit to Sync"), theme: "amber" });
		}

		const status = row.taxjar_sync_status;
		if (!status) return "";

		const color = STATUS_COLORS[status] || "gray";

		// On a cancelled row, Failed means the cancellation never reached TaxJar -
		// the order is still filed there. That is the opposite of what a reader
		// takes "Failed" beside a Cancelled transaction status to mean, which is
		// that the invoice never got there in the first place. The Transaction
		// Status column says the document was cancelled; only this pill can say
		// which of the two operations was the one that failed.
		//
		// Worded exactly as the invoice form words it (_render_taxjar_sync_status_pill
		// in taxjar_utils.js), so one state is not called two things on two screens.
		const cancelled = row.docstatus === 2;
		const label = cancelled && status === "Failed" ? __("Failed to Cancel") : __(status);

		if (status === "Synced") {
			if (!row.taxjar_last_synced) {
				return frappe.ui.badge.html({ label, theme: color });
			}
			const info_text = __("Last synced: {0}", [frappe.datetime.prettyDate(row.taxjar_last_synced)]);
			return frappe.ui.badge.html({
				label, theme: color, css_class: "taxjar-sync-trigger",
				attrs: { "data-info": info_text },
			});
		}

		const pill = frappe.ui.badge.html({ label, theme: color });

		// Excluded may still have nothing to say - a row written before the
		// reason was recorded, for a company whose configuration no longer
		// explains it - and an icon promising a detail that does not exist is
		// worse than no icon, so the pill goes out on its own.
		let info_text = "";
		if (status === "Failed") {
			info_text = row.taxjar_sync_error || __("Unknown error");
		} else if (status === "Excluded") {
			info_text = taxjar_integration.exclusion_reason_text(
				row.taxjar_exclusion_reason,
				row.taxjar_exclusion_reason_is_current
			);
		}
		if (!info_text) return pill;

		const icon = `<button type="button" class="taxjar-sync-icon taxjar-sync-trigger" data-info="${frappe.utils.escape_html(
			info_text
		)}">${frappe.utils.icon("info", "sm")}</button>`;
		return `${pill}${icon}`;
	}

	// Delegated once per table (rather than rebound on every render) so it
	// keeps working across re-renders. Shows immediately on hover - no
	// native-tooltip delay - and also toggles on click, since hover never
	// fires on touch devices.
	bind_sync_popover($wrapper) {
		$wrapper.on("mouseenter", ".taxjar-sync-trigger", (e) =>
			this._show_sync_popover($(e.currentTarget))
		);
		$wrapper.on("mouseleave", ".taxjar-sync-trigger", () => this._hide_sync_popover());
		$wrapper.on("click", ".taxjar-sync-trigger", (e) => {
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

	// ── Bulk actions ──────────────────────────────────────────────────────

	get_checked() {
		return this.datatable?.get_checked_items().filter(Boolean) || [];
	}

	// Only the Failed tab offers selection, and every row on it is retryable -
	// the tab is the eligibility filter that the old "{n} retryable" counter
	// used to be, back when Failed rows sat mixed in with Synced and Queued.
	update_bulk_state() {
		if (this.active_tab !== FAILED_TAB) {
			this.$tab_actions.hide();
			return;
		}
		this.$tab_actions.show();

		const checked = this.get_checked();

		this.$selection_count.text(checked.length ? __("{0} selected", [checked.length]) : "");
		this.bulk_action.set_items(
			checked.length
				? [{ label: __("Resync with TaxJar"), action: () => this.bulk_retry(checked) }]
				: []
		);
		this.bulk_action.disabled_title = __("Select one or more records to run an action");
	}

	bulk_retry(rows) {
		frappe
			.xcall(
				"taxjar_integration.taxjar_integration.page.taxjar_transactions.taxjar_transactions.bulk_retry",
				{ invoices: rows.map((row) => row.name) }
			)
			.then((r) => {
				frappe.show_alert({
					message: __("{0} transactions queued for retry", [r.queued]),
					indicator: "blue",
				});
				this.datatable?.clear_checked_items();
				this.refresh();
			});
	}
}
