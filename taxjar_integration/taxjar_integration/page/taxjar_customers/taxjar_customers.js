frappe.pages["taxjar-customers"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("TaxJar Customer Configuration"),
		single_column: true,
	});

	new TaxJarCustomerConfig(page);
};

const EXEMPTION_OPTIONS = ["", "Wholesale", "Government", "Non Exempt", "Other"];
const SYNC_STATUS_OPTIONS = ["", "Queued", "Synced", "Failed"];

const US_STATES = [
	"AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN",
	"IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH",
	"NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT",
	"VT","VA","WA","WV","WI","WY",
];

const CA_PROVINCES = [
	"AB","BC","MB","NB","NL","NS","NT","NU","ON","PE","QC","SK","YT",
];

const STATUS_COLORS = {
	Synced: "green",
	Failed: "red",
	Queued: "blue",
};

class TaxJarCustomerConfig {
	constructor(page) {
		this.page = page;
		this.filters = {};
		this.current_page = 1;
		this.selected = new Set();

		this.make_filters();
		this.make_bulk_actions();
		this.make_table();
		this.refresh();
	}

	make_filters() {
		this.filter_area = $('<div class="taxjar-filters"></div>').appendTo(this.page.main);

		this.filter_customer_name = this.page.add_field({
			fieldname: "customer_name",
			label: __("Customer Name"),
			fieldtype: "Data",
			change: () => { this.current_page = 1; this.refresh(); },
		});

		this.filter_customer_group = this.page.add_field({
			fieldname: "customer_group",
			label: __("Customer Group"),
			fieldtype: "Link",
			options: "Customer Group",
			change: () => { this.current_page = 1; this.refresh(); },
		});

		this.filter_exemption_type = this.page.add_field({
			fieldname: "exemption_type",
			label: __("Exemption Type"),
			fieldtype: "Select",
			options: [
				{ label: __("All"), value: "" },
				{ label: __("Wholesale"), value: "Wholesale" },
				{ label: __("Government"), value: "Government" },
				{ label: __("Non Exempt"), value: "Non Exempt" },
				{ label: __("Other"), value: "Other" },
				{ label: __("Not Set"), value: "__not_set" },
			],
			change: () => { this.current_page = 1; this.refresh(); },
		});

		this.filter_sync_status = this.page.add_field({
			fieldname: "sync_status",
			label: __("Sync Status"),
			fieldtype: "Select",
			options: [
				{ label: __("All"), value: "" },
				{ label: __("Synced"), value: "Synced" },
				{ label: __("Failed"), value: "Failed" },
				{ label: __("Queued"), value: "Queued" },
				{ label: __("Never Synced"), value: "__not_set" },
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
					<button class="btn btn-xs btn-default taxjar-bulk-set-exemption" disabled>
						${__("Set Exemption Type")}
					</button>
					<button class="btn btn-xs btn-default taxjar-bulk-clear" disabled>
						${__("Clear Exemption")}
					</button>
					<button class="btn btn-xs btn-primary taxjar-bulk-sync" disabled>
						${__("Sync to TaxJar")}
					</button>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.bulk_area.find(".taxjar-select-all").on("change", (e) => {
			const checked = e.target.checked;
			this.page.main.find(".taxjar-row-check").prop("checked", checked);
			if (checked) {
				(this.customers || []).forEach((c) => this.selected.add(c.name));
			} else {
				this.selected.clear();
			}
			this.update_bulk_state();
		});

		this.bulk_area.find(".taxjar-bulk-set-exemption").on("click", () => this.bulk_set_exemption());
		this.bulk_area.find(".taxjar-bulk-clear").on("click", () => this.bulk_clear());
		this.bulk_area.find(".taxjar-bulk-sync").on("click", () => this.bulk_sync());
	}

	make_table() {
		this.table_wrapper = $(`
			<div class="taxjar-table-wrapper" style="padding: 0 15px;">
				<table class="table table-bordered" style="margin-bottom: 0;">
					<thead>
						<tr>
							<th style="width: 30px;"></th>
							<th>${__("Customer Name")}</th>
							<th style="width: 160px;">${__("Customer Group")}</th>
							<th style="width: 180px;">${__("Exemption Type")}</th>
							<th style="width: 120px;">${__("Exempt Regions")}</th>
							<th style="width: 100px;">${__("Sync Status")}</th>
						</tr>
					</thead>
					<tbody class="taxjar-customers-body"></tbody>
				</table>
				<div class="taxjar-pagination" style="padding: 12px 0; display: flex; align-items: center; justify-content: space-between;"></div>
			</div>
		`).appendTo(this.page.main);

		this.tbody = this.table_wrapper.find(".taxjar-customers-body");
		this.pagination = this.table_wrapper.find(".taxjar-pagination");
	}

	get_filters() {
		const filters = {};
		const name_val = this.filter_customer_name?.get_value();
		if (name_val) filters.customer_name = name_val;

		const group_val = this.filter_customer_group?.get_value();
		if (group_val) filters.customer_group = group_val;

		const ex_val = this.filter_exemption_type?.get_value();
		if (ex_val) filters.exemption_type = ex_val;

		const sync_val = this.filter_sync_status?.get_value();
		if (sync_val) filters.sync_status = sync_val;

		return filters;
	}

	refresh() {
		const filters = this.get_filters();

		frappe.xcall(
			"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.get_customers",
			{ filters, page: this.current_page },
		).then((data) => {
			this.customers = data.customers;
			this.total = data.total;
			this.total_pages = data.total_pages;
			this.render_table();
			this.render_pagination(data);
			this.update_bulk_state();
			this.bulk_area.find(".taxjar-select-all").prop("checked", false);
		});
	}

	render_table() {
		this.tbody.empty();

		if (!this.customers || !this.customers.length) {
			this.tbody.append(`
				<tr><td colspan="6" class="text-muted text-center" style="padding: 40px;">
					${__("No customers found")}
				</td></tr>
			`);
			return;
		}

		this.customers.forEach((c) => {
			const checked = this.selected.has(c.name) ? "checked" : "";
			const status_color = STATUS_COLORS[c.taxjar_customer_sync_status] || "";
			const status_html = status_color
				? `<span class="indicator-pill ${status_color}">${__(c.taxjar_customer_sync_status)}</span>`
				: "";

			const region_count = c.exempt_region_count || 0;
			const region_label = region_count
				? `${region_count} <span class="text-muted" style="cursor:pointer;">✎</span>`
				: `<span class="text-muted" style="cursor:pointer;">✎</span>`;

			const options = EXEMPTION_OPTIONS.map((opt) => {
				const sel = (c.taxjar_exemption_type || "") === opt ? "selected" : "";
				const label = opt || __("(Not Set)");
				return `<option value="${frappe.utils.escape_html(opt)}" ${sel}>${frappe.utils.escape_html(label)}</option>`;
			}).join("");

			const row = $(`
				<tr data-customer="${frappe.utils.escape_html(c.name)}">
					<td><input type="checkbox" class="taxjar-row-check" ${checked}></td>
					<td><a href="/app/customer/${encodeURIComponent(c.name)}">${frappe.utils.escape_html(c.customer_name)}</a></td>
					<td>${frappe.utils.escape_html(c.customer_group || "")}</td>
					<td><select class="form-control input-xs taxjar-exemption-select">${options}</select></td>
					<td class="taxjar-regions-cell text-center" style="cursor: pointer;">${region_label}</td>
					<td>${status_html}</td>
				</tr>
			`);

			row.find(".taxjar-row-check").on("change", (e) => {
				if (e.target.checked) {
					this.selected.add(c.name);
				} else {
					this.selected.delete(c.name);
				}
				this.update_bulk_state();
			});

			row.find(".taxjar-exemption-select").on("change", (e) => {
				this.save_exemption(c.name, e.target.value);
			});

			row.find(".taxjar-regions-cell").on("click", () => {
				this.open_regions_dialog(c.name, c.customer_name);
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
				${__("Showing {0} - {1} of {2} customers", [start, end, data.total])}
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

		this.bulk_area.find(".taxjar-bulk-set-exemption").prop("disabled", disabled);
		this.bulk_area.find(".taxjar-bulk-clear").prop("disabled", disabled);
		this.bulk_area.find(".taxjar-bulk-sync").prop("disabled", disabled);
		this.bulk_area.find(".taxjar-selection-count").text(
			count ? __("{0} selected", [count]) : ""
		);
	}

	// ── Auto-save actions ──────────────────────────────────────────────

	save_exemption(customer, exemption_type) {
		frappe.xcall(
			"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.save_exemption_type",
			{ customer, exemption_type },
		).then(() => {
			frappe.show_alert({ message: __("Saved"), indicator: "green" });
			this.refresh();
		});
	}

	open_regions_dialog(customer, customer_name) {
		frappe.xcall(
			"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.get_exempt_regions",
			{ customer },
		).then((regions) => {
			this.show_regions_dialog(customer, customer_name, regions);
		});
	}

	show_regions_dialog(customer, customer_name, existing_regions) {
		const selected = new Set(existing_regions.map((r) => `${r.country}:${r.state}`));

		const make_checkboxes = (codes, country) => {
			return codes.map((code) => {
				const key = `${country}:${code}`;
				const checked = selected.has(key) ? "checked" : "";
				return `<label style="display:inline-flex;align-items:center;width:60px;margin:2px 4px;cursor:pointer;gap:2px;">
					<input type="checkbox" class="region-cb" data-country="${country}" data-state="${code}" ${checked}>
					<span style="font-size:var(--text-sm);">${code}</span>
				</label>`;
			}).join("");
		};

		const dialog = new frappe.ui.Dialog({
			title: __("Exempt Regions: {0}", [customer_name]),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "regions_html",
					options: `
						<div style="display:flex;gap:32px;">
							<div style="flex:1;">
								<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
									<strong>${__("US States")}</strong>
									<label style="cursor:pointer;font-weight:normal;font-size:var(--text-sm);display:flex;align-items:center;gap:4px;">
										<input type="checkbox" class="select-all-country" data-country="US">
										${__("Select All")}
									</label>
								</div>
								<div class="us-states-grid">${make_checkboxes(US_STATES, "US")}</div>
							</div>
							<div style="flex:1;">
								<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
									<strong>${__("CA Provinces")}</strong>
									<label style="cursor:pointer;font-weight:normal;font-size:var(--text-sm);display:flex;align-items:center;gap:4px;">
										<input type="checkbox" class="select-all-country" data-country="CA">
										${__("Select All")}
									</label>
								</div>
								<div class="ca-provinces-grid">${make_checkboxes(CA_PROVINCES, "CA")}</div>
							</div>
						</div>
					`,
				},
			],
			primary_action_label: __("Save"),
			primary_action: () => {
				const regions = [];
				dialog.$wrapper.find(".region-cb:checked").each(function () {
					regions.push({
						country: $(this).data("country"),
						state: $(this).data("state"),
					});
				});
				frappe.xcall(
					"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.save_exempt_regions",
					{ customer, regions },
				).then(() => {
					dialog.hide();
					frappe.show_alert({ message: __("Regions saved"), indicator: "green" });
					this.refresh();
				});
			},
		});

		dialog.show();

		dialog.$wrapper.find(".select-all-country").on("change", function () {
			const country = $(this).data("country");
			const checked = this.checked;
			dialog.$wrapper.find(`.region-cb[data-country="${country}"]`).prop("checked", checked);
		});
	}

	// ── Bulk actions ───────────────────────────────────────────────────

	bulk_set_exemption() {
		const d = new frappe.ui.Dialog({
			title: __("Set Exemption Type"),
			fields: [
				{
					fieldtype: "HTML",
					options: `<p class="text-muted">${__("Apply to {0} selected customers:", [this.selected.size])}</p>`,
				},
				{
					fieldname: "exemption_type",
					label: __("Exemption Type"),
					fieldtype: "Select",
					options: "Wholesale\nGovernment\nNon Exempt\nOther",
					reqd: 1,
				},
			],
			primary_action_label: __("Apply"),
			primary_action: (values) => {
				d.hide();
				frappe.xcall(
					"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.bulk_set_exemption_type",
					{ customers: Array.from(this.selected), exemption_type: values.exemption_type },
				).then((r) => {
					frappe.show_alert({ message: __("{0} customers updated", [r.updated]), indicator: "green" });
					this.selected.clear();
					this.refresh();
				});
			},
		});
		d.show();
	}

	bulk_clear() {
		frappe.confirm(
			__("Clear exemption type and exempt regions for {0} selected customers?", [this.selected.size]),
			() => {
				frappe.xcall(
					"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.bulk_clear_exemption",
					{ customers: Array.from(this.selected) },
				).then((r) => {
					frappe.show_alert({ message: __("{0} customers cleared", [r.updated]), indicator: "green" });
					this.selected.clear();
					this.refresh();
				});
			},
		);
	}

	bulk_sync() {
		frappe.xcall(
			"taxjar_integration.taxjar_integration.page.taxjar_customers.taxjar_customers.bulk_sync_to_taxjar",
			{ customers: Array.from(this.selected) },
		).then((r) => {
			frappe.show_alert({ message: __("{0} customers queued for sync", [r.queued]), indicator: "blue" });
			this.selected.clear();
			this.refresh();
		});
	}
}
