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

// ISO 3166-2:CA - https://en.wikipedia.org/wiki/ISO_3166-2:CA
taxjar_integration.CA_PROVINCE_NAMES = {
	AB: "Alberta", BC: "British Columbia", MB: "Manitoba", NB: "New Brunswick",
	NL: "Newfoundland and Labrador", NS: "Nova Scotia", NT: "Northwest Territories",
	NU: "Nunavut", ON: "Ontario", PE: "Prince Edward Island", QC: "Quebec",
	SK: "Saskatchewan", YT: "Yukon",
};
taxjar_integration.CA_PROVINCE_CODES = Object.keys(taxjar_integration.CA_PROVINCE_NAMES);

taxjar_integration.REGION_NAMES_BY_COUNTRY = {
	US: taxjar_integration.US_STATE_NAMES,
	CA: taxjar_integration.CA_PROVINCE_NAMES,
};

taxjar_integration.region_full_name = function (country, code) {
	return (taxjar_integration.REGION_NAMES_BY_COUNTRY[country] || {})[code] || code;
};

// ── Sync failure dialog ──
// Some of taxjar_integration.py's classify_taxjar_error() messages (e.g. an
// invalid API token) point the user at the guided setup wizard by name -
// turn that phrase into an actual link wherever the message reaches an
// interactive dialog. The stored Sync Error field itself stays plain text
// (it's a Small Text field, which renders as text, not HTML) - only this
// dialog rendering gets the link.
taxjar_integration.show_taxjar_sync_error = function (title, message) {
	const html = frappe.utils
		.escape_html(message)
		.replace(/guided setup/i, `<a href="/app/taxjar-setup">${__("guided setup")}</a>`);
	frappe.msgprint({ title, message: html, indicator: "red" });
};

// One frappe.ui.form MultiCheck field per country (US states, CA provinces),
// each with its own built-in "Select All"/"Unselect All" buttons - real desk
// controls rather than a hand-built checkbox grid. `selected` is a Set of
// "US:TX"-style keys. Returns dialog field defs to splice into a Dialog's
// `fields` array, right after the Exemption Type Select. Shared by the
// Customers page's bulk dialog and the Customer form's per-customer dialog.
taxjar_integration.build_region_multicheck_fields = function (selected) {
	taxjar_integration._inject_multicheck_column_styles();

	const to_options = (codes, country) =>
		codes.map((code) => ({
			label: taxjar_integration.region_full_name(country, code),
			value: `${country}:${code}`,
			checked: selected.has(`${country}:${code}`),
		}));

	return [
		// Named so update_visibility can hide the section itself (not just
		// its individual fields) whenever the grid has nothing to show -
		// otherwise the section's own divider and padding are left behind as
		// bare whitespace with nothing inside it.
		{ fieldtype: "Section Break", fieldname: "taxjar_regions_section" },
		{
			fieldtype: "MultiCheck",
			fieldname: "taxjar_us_states",
			label: __("US States"),
			options: to_options(taxjar_integration.US_STATE_CODES, "US"),
			columns: 3,
			select_all: true,
		},
		{
			fieldtype: "MultiCheck",
			fieldname: "taxjar_ca_provinces",
			label: __("CA Provinces"),
			options: to_options(taxjar_integration.CA_PROVINCE_CODES, "CA"),
			columns: 2,
			select_all: true,
		},
		{
			fieldtype: "HTML",
			fieldname: "taxjar_regions_warning",
			options: `<p class="taxjar-region-requirement-warning text-danger small" style="display:none;">
				${__("Select at least one region for this exemption type.")}
			</p>`,
		},
	];
};

// MultiCheck's own `columns` option relies on this rule (multicheck.js sets
// --checkbox-options-columns inline and expects `.checkbox-options { columns:
// var(...) }` to consume it) - frappe only ships that rule in its website/
// portal stylesheet (templates/styles/standard.css), not the desk bundle, so
// a MultiCheck field inside a desk dialog renders as one long single column
// without it. Injected once.
taxjar_integration._inject_multicheck_column_styles = function () {
	if (document.getElementById("taxjar-multicheck-column-styles")) return;
	const style = document.createElement("style");
	style.id = "taxjar-multicheck-column-styles";
	style.textContent = `
		.checkbox-options {
			columns: var(--checkbox-options-columns);
		}
	`;
	document.head.appendChild(style);
};

// Reads both MultiCheck fields built by build_region_multicheck_fields back
// into {country, state} rows, the shape configure_exemption expects.
taxjar_integration.get_selected_regions = function (dialog) {
	const values = [
		...(dialog.get_value("taxjar_us_states") || []),
		...(dialog.get_value("taxjar_ca_provinces") || []),
	];
	return values.map((value) => {
		const [country, state] = value.split(":");
		return { country, state };
	});
};

// Types that require at least one exempt region on file - kept in lockstep
// with _EXEMPTION_TYPES_REQUIRING_REGIONS in taxjar_integration.py, the real
// enforcement point. This is a live UX layer on top of that server throw,
// not a replacement for it.
const EXEMPTION_TYPES_REQUIRING_REGIONS = new Set(["Wholesale", "Government", "Other"]);

// Wires a dialog built from build_region_multicheck_fields: shows the region
// fields only once a region-scoped type is chosen, and disables the primary
// action with a warning while such a type has no regions checked. Returns an
// `update()` function the caller invokes on exemption_type change and once
// after dialog.show().
taxjar_integration.wire_exemption_dialog = function (dialog) {
	const $warning = dialog.fields_dict.taxjar_regions_warning.$wrapper.find(
		".taxjar-region-requirement-warning"
	);

	// MultiCheck.toggle() forces a full refresh(), which re-derives its
	// checkboxes from the field's original df.options (construction-time
	// `checked` flags) and overwrites selected_options from those - calling
	// it on every checkbox click would silently undo the very click that
	// triggered it. So visibility is only touched here, driven off whether
	// "enabled" actually flipped, never on every requirement check.
	let regions_visible = null;
	const update_visibility = () => {
		const type = dialog.get_value("exemption_type");
		const enabled = EXEMPTION_TYPES_REQUIRING_REGIONS.has(type);

		// The section itself (its divider + padding) must disappear along
		// with its contents for blank or Non Exempt, both of which have
		// nothing to show here - otherwise it's left behind as bare
		// whitespace between Exemption Type and Apply. Section isn't a
		// Control subclass - .show()/.hide() are its own API, not the
		// MultiCheck-specific toggle()-driven refresh() risk below.
		if (enabled) {
			dialog.fields_dict.taxjar_regions_section.show();
		} else {
			dialog.fields_dict.taxjar_regions_section.hide();
		}

		if (enabled === regions_visible) return;
		regions_visible = enabled;

		dialog.fields_dict.taxjar_us_states.toggle(enabled);
		dialog.fields_dict.taxjar_ca_provinces.toggle(enabled);
	};

	const update_requirement = () => {
		const type = dialog.get_value("exemption_type");
		const has_region = taxjar_integration.get_selected_regions(dialog).length > 0;
		const blocked = EXEMPTION_TYPES_REQUIRING_REGIONS.has(type) && !has_region;

		$warning.toggle(blocked);
		if (blocked) {
			dialog.disable_primary_action();
		} else {
			dialog.enable_primary_action();
		}
	};

	// Switching to a type that doesn't take regions (blank, or Non Exempt)
	// must drop whatever was checked for the PREVIOUS type - otherwise
	// regions picked for e.g. Wholesale silently ride along underneath,
	// invisible once update_visibility hides the grid for either of these.
	// select_all(true) is the same call the "Unselect All" button makes - a
	// direct checkbox update, not the destructive toggle()-driven refresh()
	// above.
	const clear_regions_if_not_required = () => {
		if (EXEMPTION_TYPES_REQUIRING_REGIONS.has(dialog.get_value("exemption_type"))) return;
		dialog.fields_dict.taxjar_us_states.select_all(true);
		dialog.fields_dict.taxjar_ca_provinces.select_all(true);
	};

	// MultiCheck's Select All/Unselect All buttons set checkbox.checked
	// directly (not via .prop()), which never dispatches a "change" event -
	// on_change is the control's own hook, fired for both that and a single
	// checkbox click, so it's the only reliable place to catch every case.
	// Only the (non-destructive) requirement check runs from here.
	dialog.fields_dict.taxjar_us_states.df.on_change = update_requirement;
	dialog.fields_dict.taxjar_ca_provinces.df.on_change = update_requirement;

	return () => {
		clear_regions_if_not_required();
		update_visibility();
		update_requirement();
	};
};

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

// A foreign Sales Taxes and Charges row (a handling fee, a manual
// "Loyalty Discount" row, etc - anything that isn't our own tax row or the
// configured shipping row) is otherwise invisible to TaxJar. This gates
// save with a dialog showing exactly how each one will be treated, built
// from the same classifier get_tax_data() itself uses server-side (design
// doc §5) - what the dialog shows is guaranteed to match what gets sent.
taxjar_integration.confirm_foreign_tax_rows = function (frm) {
	return frappe
		.xcall("taxjar_integration.taxjar_integration.taxjar_integration.preview_foreign_tax_rows", {
			doc_json: JSON.stringify(frm.doc),
		})
		.then(function (result) {
			let rows = (result && result.foreign_rows) || [];
			if (!rows.length) {
				return;
			}

			// Only re-prompt when the foreign-row set has actually changed
			// since the last "Proceed" - otherwise every unrelated resave of
			// a draft carrying a foreign row would re-block on this dialog.
			let ack_hash = taxjar_integration._hash_foreign_rows(rows);
			if (frm._taxjar_foreign_rows_ack === ack_hash) {
				return;
			}

			return new Promise(function (resolve) {
				taxjar_integration._show_foreign_tax_rows_dialog(frm, rows, ack_hash, resolve);
			});
		});
};

taxjar_integration._hash_foreign_rows = function (rows) {
	// affected_item_count is part of the fingerprint too - a negative row's
	// rendered sentence ("...across {0} line item(s)...") changes when the
	// item set it's distributed across changes, even if the row's own
	// account_head/amount/treatment stay the same.
	return JSON.stringify(
		rows.map((row) => [row.account_head, row.amount, row.treatment, row.affected_item_count])
	);
};

taxjar_integration._show_foreign_tax_rows_dialog = function (frm, rows, ack_hash, resolve) {
	let resolved = false;
	let settle = function (proceed) {
		if (resolved) return;
		resolved = true;
		if (!proceed) frappe.validated = false;
		resolve();
	};

	let table_rows = rows
		.map((row) => {
			let treatment =
				row.treatment === "taxable_line_item"
					? __("Added as an additional taxable line item: {0}", [
							frappe.utils.escape_html(row.description || ""),
					  ])
					: __("Applied as a discount across {0} line item(s) - consider using Additional Discount instead", [
							row.affected_item_count || 0,
					  ]);

			return `<tr>
				<td>${frappe.utils.escape_html(row.account_head)}</td>
				<td>${frappe.utils.escape_html(row.description || "")}</td>
				<td class="text-right">${format_currency(row.amount, frm.doc.currency)}</td>
				<td>${treatment}</td>
			</tr>`;
		})
		.join("");

	let html = `
		<p class="text-muted">${__("This document has Sales Taxes and Charges rows TaxJar doesn't already recognize as tax or shipping. Here's how they'll be treated for tax calculation:")}</p>
		<div style="max-height:300px;overflow-y:auto;margin-top:10px">
			<table class="table table-bordered table-hover">
				<thead style="background-color:var(--subtle-fg)">
					<tr>
						<th>${__("Ledger")}</th>
						<th>${__("Description")}</th>
						<th class="text-right">${__("Amount")}</th>
						<th>${__("Treatment")}</th>
					</tr>
				</thead>
				<tbody>${table_rows}</tbody>
			</table>
		</div>
	`;

	let d = new frappe.ui.Dialog({
		title: __("Confirm Tax Treatment for Extra Charges"),
		fields: [{ fieldtype: "HTML", fieldname: "foreign_rows_table", options: html }],
		primary_action_label: __("Proceed"),
		primary_action() {
			frm._taxjar_foreign_rows_ack = ack_hash;
			d.hide();
			settle(true);
		},
		secondary_action_label: __("Cancel"),
		secondary_action() {
			d.hide();
			settle(false);
		},
		on_hide() {
			// Dismissed via Escape/backdrop click, not a button - treat like
			// Cancel rather than silently letting the save through.
			settle(false);
		},
	});
	d.show();
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

const TAXJAR_MESSAGE_CLASS = "taxjar-form-message";

// Layout.show_message() *appends*; it only clears the container when called
// with nothing at all (layout.js:132-164). refresh() runs more than once per
// form load, so routing every call straight through it stacked a fresh copy of
// the same strip each time.
//
// Clearing the container instead would take frappe's own messages with it -
// "Submit this document to confirm" lives there too - so only our own block is
// replaced: tagged on the way in, removed by that tag on the way out.
taxjar_integration._set_tax_message = function (frm, text, color) {
	const $container = frm.layout.message;
	$container.find(`.${TAXJAR_MESSAGE_CLASS}`).remove();

	if (!text) {
		// Someone else's message may still be in there; only hide the container
		// once it is genuinely empty.
		if (!$container.children().length) $container.addClass("hidden");
		return;
	}

	frm.layout.show_message(text, color);
	$container.children().last().addClass(TAXJAR_MESSAGE_CLASS);
};

taxjar_integration.show_no_address_tax_message = function (frm) {
	if (!(frm.doc.shipping_address_name || frm.doc.customer_address)) {
		let party_name = frm.doc.party_name || frm.doc.customer;
		if (party_name) {
			taxjar_integration._set_tax_message(
				frm,
				__("Taxes are not calculated, as address is not set to assess nexus."),
				"orange"
			);
			return;
		}
	}

	if (frm.doc.taxjar_nexus_reason && !frm.doc.taxjar_has_nexus) {
		taxjar_integration._set_tax_message(
			frm,
			__("{0}, hence no taxes are charged.", [frm.doc.taxjar_nexus_reason]),
			"blue"
		);
		return;
	}

	taxjar_integration._set_tax_message(frm, "");
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

// ── Region-scoped customer exemption ──
// A customer can be exempt in some states and not others. Where the master's
// exemption covers this sale's destination, the transaction override is set
// from the master and locked: it is not the user's call, and leaving it blank
// would suggest the sale is taxable when it is not.
//
// Destination follows the same ship-to-then-bill-to fallback the server uses,
// so this re-runs whenever either address changes.
taxjar_integration.apply_region_exemption = function (frm) {
	const fields = ["taxjar_transaction_exempt", "taxjar_transaction_exemption_type"];
	const customer = frm.doc.party_name || frm.doc.customer;
	const address = frm.doc.shipping_address_name || frm.doc.customer_address;

	const unlock = () => fields.forEach((f) => frm.set_df_property(f, "read_only", 0));

	if (!customer || !address) {
		unlock();
		return;
	}

	return frappe
		.xcall("taxjar_integration.taxjar_integration.taxjar_integration.get_region_exemption", {
			customer,
			address,
		})
		.then((exemption) => {
			if (!exemption || !exemption.exemption_type) {
				unlock();
				return;
			}

			fields.forEach((f) => frm.set_df_property(f, "read_only", 1));

			// Only written on a draft, and only when it would actually change
			// something - set_value on an unchanged field still marks the form
			// dirty, which would make merely opening a saved invoice look edited.
			if (frm.doc.docstatus !== 0) return;

			if (!cint(frm.doc.taxjar_transaction_exempt)) {
				frm.set_value("taxjar_transaction_exempt", 1);
			}
			if (frm.doc.taxjar_transaction_exemption_type !== exemption.exemption_type) {
				frm.set_value("taxjar_transaction_exemption_type", exemption.exemption_type);
			}
		});
};

// True when the per-transaction override is both ticked and given a reason -
// the same test the server applies before it counts as real.
taxjar_integration._has_transaction_exemption = function (frm) {
	return Boolean(cint(frm.doc.taxjar_transaction_exempt) && frm.doc.taxjar_transaction_exemption_type);
};

taxjar_integration.render_status_cards = function (frm) {
	if (!frm.fields_dict.taxjar_status_html) return;
	const wrapper = frm.fields_dict.taxjar_status_html.$wrapper;

	if (!frm.doc.taxjar_nexus_reason && !frm.doc.taxjar_customer_taxable_reason) {
		wrapper.html(`<p class="text-muted">${__("Tax status will be available after saving.")}</p>`);
		return;
	}

	const has_nexus = frm.doc.taxjar_has_nexus;
	// The customer master's own answer. A transaction override no longer flips
	// this to "No" - that would hide the fact that the customer is taxable and
	// only this one sale is not. The override is appended to the answer instead.
	const customer_taxable = frm.doc.taxjar_customer_taxable;
	const transaction_exempt = taxjar_integration._has_transaction_exemption(frm);

	const card1 = {
		question: __("Do you have a nexus here?"),
		answer: has_nexus ? __("Yes") : __("No"),
		color: has_nexus ? "green" : "orange",
	};

	let card2, card3;

	const skipped = { answer: __("Skipped"), color: "grey" };

	if (!has_nexus && frm.doc.taxjar_nexus_reason) {
		// Nothing downstream is evaluated once there is no nexus.
		card2 = { question: __("Is the customer taxable?"), ...skipped };
		card3 = { question: __("Is the product taxable?"), ...skipped };
	} else {
		let answer = __("Yes");
		let color = "green";
		// Only this one answer is a sentence; every other pill on these cards
		// is a single word and keeps frappe's own shape untouched.
		let wrap = false;

		if (!customer_taxable) {
			answer = __("No");
			color = "orange";
		} else if (transaction_exempt) {
			answer = __("Yes, but transaction is marked as exempt");
			color = "orange";
			wrap = true;
		}

		card2 = { question: __("Is the customer taxable?"), answer, color, wrap };

		// Product taxability is moot once the sale is exempt either way.
		if (!customer_taxable || transaction_exempt) {
			card3 = { question: __("Is the product taxable?"), ...skipped };
		} else {
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
	}

	taxjar_integration._inject_status_card_styles();

	const cards = [card1, card2, card3];
	let html = '<div class="taxjar-status-cards">';
	cards.forEach((card, i) => {
		html += `
			<div class="taxjar-status-card">
				<div class="text-muted taxjar-status-card-q">${card.question}</div>
				<div class="taxjar-status-card-a">
					<span class="indicator-pill ${card.color}${card.wrap ? " taxjar-pill-wrap" : ""}">${card.answer}</span>
				</div>
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
		/* .indicator-pill is built for one short word: a fixed 20px height, a
		   fully round radius, and a dot centred on that fixed box
		   (frappe/public/scss/common/indicator.scss). "Yes, but transaction is
		   marked as exempt" wraps, so the height has to give and the dot has to
		   sit on the first line instead of halfway down the block, and the full
		   radius has to come down - it bows a two-line block out into a
		   lozenge.

		   Opt-in via .taxjar-pill-wrap rather than applied to every pill in
		   these cards: a single-word pill under these rules sizes to its
		   content instead of frappe's fixed 20px and lifts its dot off centre,
		   so "Yes" and "Skipped" would drift out of step with pills elsewhere
		   in the desk. */
		.taxjar-status-card .indicator-pill.taxjar-pill-wrap {
			height: auto;
			min-height: 20px;
			align-items: flex-start;
			white-space: normal;
			text-align: left;
			/* --radius-full on a two-line block bows the sides out into a
			   lozenge; a fixed radius keeps the corners round without the
			   stretch. Single-word pills keep the inherited full radius. */
			border-radius: 10px;
		}
		.taxjar-status-card .indicator-pill.taxjar-pill-wrap::before {
			/* centres the 6px dot on the first line rather than the whole block */
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

		.taxjar-addresses { display: flex; align-items: center; gap: 16px; padding: 8px 0; }
		/* Both cells carry the padding and a transparent border of the same
		   width, so lighting one up tints it in place instead of nudging the
		   other one sideways. */
		/* --radius-md, not --border-radius-md: this frappe ships the Espresso
		   --radius-* scale (css/espresso/radius.css) and never defines the old
		   --border-radius-* aliases, so that name resolves to nothing and the
		   corners come out square. Dashed rather than dotted for a longer
		   segment - dotted renders as round dots at 1px. */
		.taxjar-address {
			flex: 1;
			text-align: center;
			padding: 10px 12px;
			border: 1px dashed transparent;
			border-radius: var(--radius-md);
		}
		.taxjar-address-label { font-size: var(--text-sm); color: var(--text-muted); }
		.taxjar-address-value { font-weight: 600; margin-top: 4px; }
		.taxjar-address-arrow { font-size: 20px; color: var(--text-muted); }
		/* --bg-blue / --text-on-blue are frappe's own indicator-pill tokens
		   (indicator.scss), redefined per theme - so this tracks light/dark
		   without a hex of ours, and stays clear of the green/orange/grey
		   verdict pills on the status cards above. */
		.taxjar-address-lit { background: var(--bg-blue); border-color: var(--text-on-blue); }
		/* Not blue: the box carries the colour, and colouring its caption too
		   would read as a second signal rather than as the words for the one
		   already there. --text-light (ink-gray-5) rather than --text-muted
		   (ink-gray-6), a step lighter again, so it also sits below the
		   "Ship To" label it shares the box with rather than competing with
		   it. Both track the theme. */
		.taxjar-address-note {
			margin-top: 4px;
			font-size: var(--text-sm);
			color: var(--text-light);
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

	taxjar_integration._inject_status_card_styles();

	const from_text = frm.doc.taxjar_ship_from || __("Not set");
	const to_text = frm.doc.taxjar_ship_to || __("Not set");

	// Which end of the shipment set the rate, off TaxJar's own tax_source.
	// Only trusted while the document actually has nexus: the early returns in
	// set_sales_tax that stop before calculating (no nexus, exempt, no payload)
	// leave the stored value untouched, so a document that was taxed once would
	// otherwise keep advertising a rule that no longer applies to it. Same
	// guard shape as _has_no_nexus. With no source, neither side is tinted or
	// captioned - the row reads exactly as it did before.
	const source = frm.doc.taxjar_has_nexus ? (frm.doc.taxjar_tax_source || "") : "";
	const origin = source === "origin";
	const destination = source === "destination";

	// The tint alone says "this one" without saying why, so the rule is named
	// inside the box it applies to - no separate legend to read across to, and
	// nothing rendered at all on the side that didn't source the rate. One
	// argument drives both the tint and the caption, so they cannot disagree.
	// Brackets live in the markup, not in the translatable - a translator gets
	// the phrase to translate, not punctuation to reproduce.
	const cell = (label, text, note) => `
		<div class="taxjar-address${note ? " taxjar-address-lit" : ""}">
			<div class="taxjar-address-label">${label}</div>
			<div class="taxjar-address-value">${frappe.utils.escape_html(text)}</div>
			${note ? `<div class="taxjar-address-note">(${note})</div>` : ""}
		</div>`;

	wrapper.html(`
		<div class="taxjar-addresses">
			${cell(__("Ship From"), from_text, origin ? __("Origin based tax") : "")}
			<div class="taxjar-address-arrow">→</div>
			${cell(__("Ship To"), to_text, destination ? __("Destination based tax") : "")}
		</div>
	`);
};

// ── TaxJar Tax Breakdown Rendering ──
// Shared by the Sales Invoice / Sales Order / Quotation forms. Renders the
// jurisdiction-level tax breakdown stored on taxjar_breakdown_json.

taxjar_integration._no_breakdown_msg = function (is_new, frm) {
	let text;
	if (is_new) {
		text = __("Save transaction to fetch sales tax & view breakup.");
	} else if (frm && taxjar_integration._has_no_nexus(frm)) {
		// There is no breakdown and there never will be - say why, rather than
		// reporting an absence the user cannot act on.
		text = __("{0}, hence no taxes are charged.", [frm.doc.taxjar_nexus_reason]);
	} else {
		text = __("No TaxJar tax breakdown available for this transaction.");
	}
	return `<p class="text-muted">${text}</p>`;
};

// Nexus was actually assessed and came back negative. taxjar_has_nexus alone
// is 0 both for "no nexus" and "not evaluated yet", so the reason has to be
// present too.
taxjar_integration._has_no_nexus = function (frm) {
	return Boolean(frm.doc.taxjar_nexus_reason) && !frm.doc.taxjar_has_nexus;
};

// Plain HTML field, rendered client-side straight off the already-loaded
// taxjar_freight_taxable Check field - no server round trip needed. Kept out
// of taxjar_breakdown_html on purpose: a read-only Text Editor field wraps
// its whole content in a boxed "like-disabled-input" background, which looks
// right around the table but wrong around a standalone indicator pill.
taxjar_integration.render_shipping_taxability = function (frm) {
	if (!frm.fields_dict.taxjar_freight_taxable_html) return;
	const wrapper = frm.fields_dict.taxjar_freight_taxable_html.$wrapper;

	// Nothing is taxed without a nexus, so shipping taxability is moot - the
	// pill would answer a question that does not arise.
	//
	// hide() rather than just emptying: the field's own wrapper keeps its
	// margins when it holds no content, leaving a blank band above the
	// breakdown on an unsaved doc.
	if (
		frm.doc.taxjar_freight_taxable === undefined ||
		frm.doc.taxjar_freight_taxable === null ||
		taxjar_integration._has_no_nexus(frm)
	) {
		wrapper.empty().hide();
		return;
	}

	const taxable = cint(frm.doc.taxjar_freight_taxable);
	const color = taxable ? "green" : "grey";
	const label = taxable ? __("Yes") : __("No");
	wrapper.show().html(`
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
		frm.doc.taxjar_breakdown_html =
			frm.doc.__onload?._taxjar_breakdown_html || taxjar_integration._no_breakdown_msg(false, frm);
	}
	frm.refresh_field("taxjar_breakdown_html");

	// The field carries no label - the section heading above already names it -
	// but frappe renders the label block regardless and only hides it on
	// request (base_input.js:22-25, toggle_label). Left alone it is an empty
	// row of whitespace between the heading and the table. After
	// refresh_field(), which re-renders the value beneath it.
	frm.fields_dict.taxjar_breakdown_html.toggle_label?.(false);
};

// ── TaxJar Sync Status: sidebar pill (Sales Invoice) ──
// Inserted right after .sidebar-meta-details (the title/doc-id block), so it
// sits below the doc id and above the Assign/Attachments/Tags/Share list -
// its own border-bottom draws the line separating it from Assign below,
// same as .sidebar-meta-details already does above it.
// Same color mapping and hover-popover interaction as the Sync Status column
// on the TaxJar Transaction Sync page (taxjar_transactions.js) - kept as its
// own small copy here rather than shared, since that page's version is bound
// to its own table/class instance and not meant to be called standalone.

taxjar_integration.SYNC_STATUS_COLORS = {
	Synced: "green",
	Failed: "red",
	Queued: "blue",
	Excluded: "grey",
};

taxjar_integration._hide_sync_sidebar_pop = function () {
	if (taxjar_integration._sync_sidebar_pop) {
		taxjar_integration._sync_sidebar_pop.remove();
		taxjar_integration._sync_sidebar_pop = null;
	}
	$(document).off("click.taxjarSyncSidebarPop");
};

taxjar_integration._show_sync_sidebar_pop = function ($trigger) {
	taxjar_integration._hide_sync_sidebar_pop();
	const text = $trigger.attr("data-info") || "";
	if (!text) return;

	const $pop = $(`
		<div class="taxjar-sidebar-sync-pop" style="position: fixed; z-index: 1000; max-width: 280px; padding: 8px 12px; border-radius: var(--border-radius-md); background: var(--fg-color); border: 1px solid var(--border-color); box-shadow: var(--shadow-md); font-size: var(--text-sm); color: var(--text-color);">
			${frappe.utils.escape_html(text)}
		</div>
	`).appendTo("body");

	// position: fixed + getBoundingClientRect() are both viewport-relative,
	// so no scroll-offset math is needed - same approach as the Transactions
	// page's popover, clamped back on-screen for a pill near the sidebar edge.
	const rect = $trigger[0].getBoundingClientRect();
	const pop_width = $pop.outerWidth();
	const left = Math.min(rect.left, window.innerWidth - pop_width - 12);
	$pop.css({ top: rect.bottom + 6, left: Math.max(12, left) });

	taxjar_integration._sync_sidebar_pop = $pop;
	$(document).on("click.taxjarSyncSidebarPop", () => taxjar_integration._hide_sync_sidebar_pop());
};

taxjar_integration.render_sync_status_sidebar_pill = function (frm) {
	$(document).find(".form-sidebar .taxjar-sync-sidebar-pill-section").remove();
	taxjar_integration._hide_sync_sidebar_pop();

	if (!frm.fields_dict.taxjar_sync_status || !frm.doc.company) return;

	const docname = frm.doc.name;

	// Checked live on every refresh rather than cached on the transaction doc -
	// a stored flag would go stale for a Draft left unsaved, or worse for a
	// Cancelled doc (which is never saved again), once the company's TaxJar
	// config changes after the doc was last written.
	frappe.call({
		method: "taxjar_integration.taxjar_integration.taxjar_integration.is_taxjar_enabled_for_company",
		args: { company: frm.doc.company },
		callback: (r) => {
			if (frm.doc.name !== docname) return;

			if (!r.message) {
				taxjar_integration._render_taxjar_not_enabled_link(frm);
			} else {
				taxjar_integration._render_taxjar_sync_status_pill(frm);
			}
		},
	});
};

// TaxJar isn't configured for this company at all - there's no sync state to
// report, so no "TaxJar Status" label and no pill, just a plain link to go
// fix it. Distinct from the "Excluded" pill (below), which covers a
// company that IS enabled but hasn't reached _set_sync_status yet.
taxjar_integration._render_taxjar_not_enabled_link = function (frm) {
	const icon = frappe.utils.icon("external-link", "xs", "", "", "", true);
	const $section = $(`
		<div class="sidebar-section taxjar-sync-sidebar-pill-section border-bottom">
			<a
				href="/app/taxjar-setup"
				class="taxjar-not-enabled-link"
				style="display: inline-flex; align-items: center; gap: 4px; text-decoration: underline dotted; text-underline-offset: 3px;"
			>${__("Configure TaxJar")}${icon}</a>
		</div>
	`);
	$(document).find(".form-sidebar .sidebar-meta-details").after($section);
};

taxjar_integration._render_taxjar_sync_status_pill = function (frm) {
	// "Synced"/"Failed" are written by both the on_submit sync path and the
	// on_cancel delete path (see _set_sync_status), so docstatus === 2 is
	// what turns those two into the cancel-flow wording below.
	const cancelled = frm.doc.docstatus === 2;
	const status = frm.doc.taxjar_sync_status || "Excluded";
	let label, color, info_text;

	if (frm.doc.docstatus === 0) {
		label = __("Submit to Sync");
		color = "yellow";
	} else if (status === "Queued") {
		label = __("Queued");
		color = taxjar_integration.SYNC_STATUS_COLORS[status];
		info_text = __("Queued for sync");
	} else if (status === "Synced") {
		label = cancelled ? __("Cancelled") : __("Synced");
		color = cancelled ? "grey" : taxjar_integration.SYNC_STATUS_COLORS[status];
		if (frm.doc.taxjar_last_synced) {
			info_text = __("Last synced: {0}", [frappe.datetime.prettyDate(frm.doc.taxjar_last_synced)]);
		}
	} else if (status === "Failed") {
		label = cancelled ? __("Failed to Cancel") : __("Failed");
		color = taxjar_integration.SYNC_STATUS_COLORS[status];
		info_text = frm.doc.taxjar_sync_error || __("Unknown error");
	} else {
		// TaxJar is enabled for the company but enqueue_taxjar_sync/
		// enqueue_taxjar_delete hasn't reached _set_sync_status for this doc
		// yet (e.g. no API credential even though "create transactions" is on).
		label = __(status);
		color = taxjar_integration.SYNC_STATUS_COLORS[status];
	}

	const $badge = $(`
		<span
			class="indicator-pill no-indicator-dot ${color}"
			${info_text ? `data-info="${frappe.utils.escape_html(info_text)}" style="cursor:pointer;"` : ""}
		>${label}</span>
	`);

	const $pill = $(`
		<div class="sidebar-section taxjar-sync-sidebar-pill-section border-bottom">
			<div class="text-muted" style="font-weight: 600; margin-bottom: 6px;">${__("TaxJar Status")}</div>
		</div>
	`);
	$pill.append($badge);

	$(document).find(".form-sidebar .sidebar-meta-details").after($pill);

	if (info_text) {
		$badge.on("mouseenter", (e) => taxjar_integration._show_sync_sidebar_pop($(e.currentTarget)));
		$badge.on("mouseleave", () => taxjar_integration._hide_sync_sidebar_pop());
		$badge.on("click", (e) => {
			e.stopPropagation();
			taxjar_integration._show_sync_sidebar_pop($(e.currentTarget));
		});
	}
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
