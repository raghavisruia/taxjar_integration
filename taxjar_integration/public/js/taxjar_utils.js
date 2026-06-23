if (!window.taxjar_integration) {
	window.taxjar_integration = {};
}

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

	const card1 = {
		question: __("Do you have a nexus here?"),
		answer: has_nexus ? __("Yes") : __("No"),
		reason: frm.doc.taxjar_nexus_reason || "",
		color: has_nexus ? "green" : "orange",
	};

	let card2, card3;

	if (!has_nexus && frm.doc.taxjar_nexus_reason) {
		card2 = { question: __("Is the customer taxable?"), answer: __("N/A"), reason: __("Not evaluated — no nexus"), color: "grey" };
		card3 = { question: __("Is the product taxable?"), answer: __("N/A"), reason: __("Not evaluated — no nexus"), color: "grey" };
	} else if (!customer_taxable && frm.doc.taxjar_customer_taxable_reason) {
		card2 = { question: __("Is the customer taxable?"), answer: __("No"), reason: frm.doc.taxjar_customer_taxable_reason || "", color: "orange" };
		card3 = { question: __("Is the product taxable?"), answer: __("N/A"), reason: __("Not evaluated — customer exempt"), color: "grey" };
	} else {
		card2 = {
			question: __("Is the customer taxable?"),
			answer: customer_taxable ? __("Yes") : __("No"),
			reason: frm.doc.taxjar_customer_taxable_reason || "",
			color: customer_taxable ? "green" : "orange",
		};

		const prod = frm.doc.taxjar_product_taxable;
		let prod_color = "grey";
		let prod_answer = __("N/A");
		if (prod === "Yes") { prod_color = "green"; prod_answer = __("Yes"); }
		else if (prod === "No") { prod_color = "orange"; prod_answer = __("No"); }
		else if (prod === "Partially") { prod_color = "blue"; prod_answer = __("Partially"); }

		card3 = {
			question: __("Is the product taxable?"),
			answer: prod_answer,
			reason: frm.doc.taxjar_product_taxable_reason || "",
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
