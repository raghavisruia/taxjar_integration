frappe.query_reports["TaxJar Transaction Sync"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "sync_status",
			label: __("Sync Status"),
			fieldtype: "Select",
			options: "\nNot Applicable\nQueued\nSynced\nFailed",
		},
		{
			fieldname: "transaction_type",
			label: __("Transaction Type"),
			fieldtype: "Select",
			options: "\nInvoice\nCredit Note\nDebit Note",
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (column.fieldname === "taxjar_sync_status" && data) {
			const indicators = {
				Synced: "green",
				Failed: "red",
				Queued: "blue",
				"Not Applicable": "grey",
			};
			const color = indicators[data.taxjar_sync_status] || "grey";
			value = `<span class="indicator-pill ${color}">${value}</span>`;
		}

		return value;
	},

	onload(report) {
		report.page.add_inner_button(__("Retry All Failed"), function () {
			frappe.call({
				method: "taxjar_integration.taxjar_integration.taxjar_integration.retry_all_failed_syncs",
				freeze: true,
				freeze_message: __("Queuing retry for all failed transactions..."),
				callback(r) {
					frappe.show_alert({
						message: __("{0} transactions queued for retry", [r.message]),
						indicator: "green",
					}, 5);
					report.refresh();
				}
			});
		});
	},

	get_datatable_options(options) {
		return Object.assign(options, {
			checkboxColumn: false,
			events: {
				onCheckRow(data) {
					// no-op
				}
			}
		});
	}
};
