if (!window.taxjar_integration) {
	window.taxjar_integration = {};
}

// ── Shared geography constants ──
// Single source of truth for US state + Canadian province codes used across the
// Address and Customer forms and the TaxJar Customers configuration page.
taxjar_integration.US_STATE_NAMES = {
	AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas",
	CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware",
	DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii",
	ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa",
	KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine",
	MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota",
	MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska",
	NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico",
	NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio",
	OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island",
	SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas",
	UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington",
	WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
};
taxjar_integration.US_STATE_CODES = Object.keys(taxjar_integration.US_STATE_NAMES);
taxjar_integration.CA_PROVINCE_CODES = [
	"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
];

taxjar_integration.check_shipping_address = function (frm) {
	if (frm.doc.shipping_address_name) {
		return;
	}

	let party_type = frm.doc.quotation_to || "Customer";
	let party_name = frm.doc.party_name || frm.doc.customer;

	if (!party_name || party_type !== "Customer") {
		return;
	}

	return frappe.xcall(
		"taxjar_integration.taxjar_integration.taxjar_integration.get_customer_addresses",
		{ customer: party_name }
	).then(function (addresses) {
		addresses = addresses || [];

		if (addresses.length) {
			frappe.validated = false;
			taxjar_integration.show_address_picker_dialog(frm, addresses);
			return;
		}

		if (frm.doc.doctype === "Quotation") {
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
					taxjar_integration._open_new_address(frm);
					frappe.msg_dialog.hide();
				},
			},
		});
	});
};

taxjar_integration.show_address_picker_dialog = function (frm, addresses) {
	let selected = null;

	let table_rows = addresses
		.map((addr, idx) => {
			let addr_parts = [addr.address_line1, addr.city, addr.state, addr.pincode]
				.filter(Boolean)
				.join(", ");
			let checked = idx === 0 ? "checked" : "";
			if (idx === 0) selected = addr.name;

			return `<tr data-address="${frappe.utils.escape_html(addr.name)}"
					style="cursor:pointer">
				<td style="width:30px;text-align:center">
					<input type="radio" name="taxjar_addr" value="${frappe.utils.escape_html(addr.name)}" ${checked}>
				</td>
				<td>${frappe.utils.escape_html(addr.address_title || addr.name)}</td>
				<td>${frappe.utils.escape_html(addr_parts)}</td>
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
			taxjar_integration._open_new_address(frm);
		},
	});

	d.$wrapper.on("click", "tr[data-address]", function () {
		let addr_name = $(this).data("address");
		d.$wrapper.find('input[name="taxjar_addr"][value="' + addr_name + '"]').prop("checked", true);
		selected = addr_name;
	});

	d.$wrapper.on("change", 'input[name="taxjar_addr"]', function () {
		selected = $(this).val();
	});

	d.show();
};

taxjar_integration.show_no_address_tax_message = function (frm) {
	if (!(frm.doc.shipping_address_name || frm.doc.customer_address)) {
		let party_name = frm.doc.party_name || frm.doc.customer;
		if (party_name) {
			frm.layout.show_message(
				__("Taxes are not calculated, as address is not set to assess nexus."),
				"orange"
			);
			return;
		}
	}

	if (frm.doc.taxjar_nexus_reason && !frm.doc.taxjar_has_nexus) {
		frm.layout.show_message(
			__("{0}, hence no taxes are charged.", [frm.doc.taxjar_nexus_reason]),
			"blue"
		);
		return;
	}

	frm.layout.show_message("");
};

taxjar_integration._open_new_address = function (frm) {
	let party_name = frm.doc.party_name || frm.doc.customer;
	frappe.new_doc("Address", {
		address_title: party_name,
		address_type: "Shipping",
		links: [{ link_doctype: "Customer", link_name: party_name }],
	});
};

// ── TaxJar Tab: Status Cards & Addresses ──

taxjar_integration.render_status_cards = function (frm) {
	if (!frm.fields_dict.taxjar_status_html) return;
	const wrapper = frm.fields_dict.taxjar_status_html.$wrapper;

	if (!frm.doc.taxjar_nexus_reason && !frm.doc.taxjar_customer_taxable_reason) {
		wrapper.html(`<p class="text-muted">${__("Tax status will be available after saving.")}</p>`);
		return;
	}

	const has_nexus = frm.doc.taxjar_has_nexus;
	const customer_taxable = frm.doc.taxjar_customer_taxable;
	// Only the transaction-level override (see taxjar_transaction_exempt) gets
	// a second pill - a master-level customer exemption or the plain taxable
	// case say nothing extra here, matching "rest don't show reasons at all".
	const overridden = (frm.doc.taxjar_customer_taxable_reason || "").startsWith("Overridden");

	const card1 = {
		question: __("Do you have a nexus here?"),
		answer: has_nexus ? __("Yes") : __("No"),
		color: has_nexus ? "green" : "orange",
	};

	let card2, card3;

	if (!has_nexus && frm.doc.taxjar_nexus_reason) {
		card2 = { question: __("Is the customer taxable?"), answer: __("Skipped"), color: "grey" };
		card3 = { question: __("Is the product taxable?"), answer: __("Skipped"), color: "grey" };
	} else if (!customer_taxable && frm.doc.taxjar_customer_taxable_reason) {
		card2 = { question: __("Is the customer taxable?"), answer: __("No"), color: "orange", overridden };
		card3 = { question: __("Is the product taxable?"), answer: __("Skipped"), color: "grey" };
	} else {
		card2 = {
			question: __("Is the customer taxable?"),
			answer: customer_taxable ? __("Yes") : __("No"),
			color: customer_taxable ? "green" : "orange",
			overridden,
		};

		const prod = frm.doc.taxjar_product_taxable;
		let prod_color = "grey";
		let prod_answer = __("Skipped");
		if (prod === "Yes") { prod_color = "green"; prod_answer = __("Yes"); }
		else if (prod === "No") { prod_color = "orange"; prod_answer = __("No"); }
		else if (prod === "Partially") { prod_color = "blue"; prod_answer = __("Partially"); }

		card3 = {
			question: __("Is the product taxable?"),
			answer: prod_answer,
			color: prod_color,
		};
	}

	taxjar_integration._inject_status_card_styles();

	const cards = [card1, card2, card3];
	let html = '<div class="taxjar-status-cards">';
	cards.forEach((card, i) => {
		html += `
			<div class="taxjar-status-card">
				<div class="text-muted taxjar-status-card-q">${card.question}</div>
				<div class="taxjar-status-card-a">
					<span class="indicator-pill ${card.color}">${card.answer}</span>
				</div>
				${card.overridden ? `<div class="taxjar-status-card-override"><span class="indicator-pill grey">${__("Overridden")}</span></div>` : ""}
			</div>
			${i < 2 ? '<div class="taxjar-status-arrow">→</div>' : ""}`;
	});
	html += "</div>";
	wrapper.html(html);
};

// Responsive layout for the status cards. On wide screens the three cards sit
// side by side with → connectors; on narrow screens they stack and the arrows
// rotate to point downward. Injected once.
taxjar_integration._inject_status_card_styles = function () {
	if (document.getElementById("taxjar-status-card-styles")) return;
	const style = document.createElement("style");
	style.id = "taxjar-status-card-styles";
	style.textContent = `
		.taxjar-status-cards {
			display: flex;
			gap: 12px;
			flex-wrap: wrap;
			align-items: stretch;
			margin-bottom: 16px;
		}
		.taxjar-status-card {
			flex: 1 1 200px;
			border: 1px solid var(--border-color);
			border-radius: var(--border-radius-lg);
			padding: 16px;
			background: var(--fg-color);
		}
		.taxjar-status-card-q {
			font-size: var(--text-sm);
			margin-bottom: 8px;
			font-style: italic;
		}
		.taxjar-status-card-a {
			font-size: var(--text-lg);
			font-weight: 600;
		}
		.taxjar-status-card-override {
			margin-top: 6px;
		}
		.taxjar-status-arrow {
			display: flex;
			align-items: center;
			font-size: 20px;
			color: var(--text-muted);
		}
		@media (max-width: 991px) {
			.taxjar-status-cards { flex-direction: column; }
			.taxjar-status-card { flex-basis: auto; }
			.taxjar-status-arrow { justify-content: center; transform: rotate(90deg); }
		}
	`;
	document.head.appendChild(style);
};

taxjar_integration.render_addresses = function (frm) {
	if (!frm.fields_dict.taxjar_addresses_html) return;
	const wrapper = frm.fields_dict.taxjar_addresses_html.$wrapper;

	if (!frm.doc.taxjar_ship_from && !frm.doc.taxjar_ship_to) {
		wrapper.html("");
		return;
	}

	const from_text = frm.doc.taxjar_ship_from || __("Not set");
	const to_text = frm.doc.taxjar_ship_to || __("Not set");

	wrapper.html(`
		<div style="display:flex;align-items:center;gap:16px;padding:8px 0">
			<div style="flex:1;text-align:center">
				<div class="text-muted" style="font-size:var(--text-sm)">${__("Ship From")}</div>
				<div style="font-weight:600;margin-top:4px">${frappe.utils.escape_html(from_text)}</div>
			</div>
			<div style="font-size:20px;color:var(--text-muted)">→</div>
			<div style="flex:1;text-align:center">
				<div class="text-muted" style="font-size:var(--text-sm)">${__("Ship To")}</div>
				<div style="font-weight:600;margin-top:4px">${frappe.utils.escape_html(to_text)}</div>
			</div>
		</div>
	`);
};

// ── TaxJar Tax Breakdown Rendering ──
// Shared by Sales Invoice / Sales Order / Quotation forms and their item grids.
// These render the jurisdiction-level tax breakdown stored on taxjar_breakdown_json
// (transaction) and taxjar_item_breakdown_json (per line item).

taxjar_integration._no_breakdown_msg = function (is_new) {
	const text = is_new
		? __("Please save to see tax breakdown.")
		: __("No TaxJar tax breakdown available for this transaction.");
	return `<p class="text-muted">${text}</p>`;
};

taxjar_integration.build_item_table = function (rows, currency) {
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
};

// Plain HTML field, rendered client-side straight off the already-loaded
// taxjar_freight_taxable Check field - no server round trip needed. Kept out
// of taxjar_breakdown_html on purpose: a read-only Text Editor field wraps
// its whole content in a boxed "like-disabled-input" background, which looks
// right around the table but wrong around a standalone indicator pill.
taxjar_integration.render_shipping_taxability = function (frm) {
	if (!frm.fields_dict.taxjar_freight_taxable_html) return;
	const wrapper = frm.fields_dict.taxjar_freight_taxable_html.$wrapper;

	if (frm.doc.taxjar_freight_taxable === undefined || frm.doc.taxjar_freight_taxable === null) {
		wrapper.html("");
		return;
	}

	const taxable = cint(frm.doc.taxjar_freight_taxable);
	const color = taxable ? "green" : "grey";
	const label = taxable ? __("Yes") : __("No");
	wrapper.html(`
		<div style="margin-bottom: 10px; font-size: var(--text-md);">
			<span class="text-muted">${__("Is shipping charges taxable?")}</span>
			<span class="indicator-pill ${color}" style="margin-left: 6px; font-size: var(--text-md);">${label}</span>
		</div>
	`);
};

// The table itself (plus, for multi-currency docs, the USD sub-table above
// it) is rendered server-side - see get_taxjar_breakdown_html() in
// taxjar_integration.py and templates/includes/taxjar_breakup.html - same
// tax-break-up/table-bordered/table-hover markup core ERPNext and
// india_compliance use for their own Tax Breakup / GST Breakup tables, which
// is also what makes it show up in Print/PDF. onload pushes the rendered
// HTML onto frm.doc.__onload (the browser already holds its own copy of the
// doc by the time onload runs, so it can't be written directly onto the
// field) - this just copies it across.
taxjar_integration.render_tax_breakdown = function (frm) {
	if (!frm.fields_dict.taxjar_breakdown_html) return;

	if (frm.is_new()) {
		frm.doc.taxjar_breakdown_html = taxjar_integration._no_breakdown_msg(true);
	} else {
		frm.doc.taxjar_breakdown_html = frm.doc.__onload?._taxjar_breakdown_html || taxjar_integration._no_breakdown_msg(false);
	}
	frm.refresh_field("taxjar_breakdown_html");
};

taxjar_integration.render_single_item_breakdown = function (frm, cdn, item_doctype) {
	const row = frm.fields_dict.items?.grid?.grid_rows_by_docname[cdn];
	const field = row?.grid_form?.fields_dict?.taxjar_item_breakdown_html;
	if (!field) return;

	const item = frappe.get_doc(item_doctype, cdn);
	if (!item?.taxjar_item_breakdown_json) {
		field.$wrapper.html(taxjar_integration._no_breakdown_msg());
		return;
	}

	let data;
	try {
		data = JSON.parse(item.taxjar_item_breakdown_json);
	} catch (e) {
		field.$wrapper.html(taxjar_integration._no_breakdown_msg());
		return;
	}

	let html = "";
	const currency = data.currency || frm.doc.currency;

	if (data.usd) {
		html += `<p class="text-muted small" style="margin-bottom:4px"><strong>${__("Tax Calculation (USD)")}</strong></p>`;
		html += taxjar_integration.build_item_table(data.usd.breakdown || [], "USD");
		html += `<p class="text-muted small" style="margin-top:12px;margin-bottom:4px"><strong>${
			__("Equivalent in Transaction Currency ({0})", [currency])
		}</strong></p>`;
	}

	html += taxjar_integration.build_item_table(data.breakdown || [], currency);
	field.$wrapper.html(html);
};

// ── "TaxJar not set up" panel ──
// Rendered by the Customers / Transactions pages when their read methods report
// not_configured (the TaxJar custom fields do not exist yet). Replaces the table
// area with a friendly call to action instead of an empty grid or a server error.
taxjar_integration.render_not_configured_panel = function ($container) {
	$container.html(`
		<div class="text-center text-muted" style="padding: 60px 20px;">
			<div style="font-size: var(--text-2xl); font-weight: 600; margin-bottom: 8px;">
				${__("TaxJar is not set up yet")}
			</div>
			<p style="max-width: 480px; margin: 0 auto 16px;">
				${__("Enable a TaxJar feature in TaxJar Settings to start configuring customers and syncing transactions.")}
			</p>
			<a class="btn btn-primary btn-sm" href="/app/taxjar-settings">
				${__("Open TaxJar Settings")}
			</a>
		</div>
	`);
};
