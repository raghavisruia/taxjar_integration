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
	if (frm.doc.shipping_address_name || frm.doc.customer_address) {
		frm.layout.show_message("");
		return;
	}

	let party_name = frm.doc.party_name || frm.doc.customer;
	if (!party_name) return;

	frm.layout.show_message(
		__("Taxes are not calculated, as address is not set to assess nexus."),
		"orange"
	);
};

taxjar_integration._open_new_address = function (frm) {
	let party_name = frm.doc.party_name || frm.doc.customer;
	frappe.new_doc("Address", {
		address_title: party_name,
		address_type: "Shipping",
		links: [{ link_doctype: "Customer", link_name: party_name }],
	});
};
