// An export: a sale delivered to a country TaxJar does not price.
//
// The form used to report one as "Nexus not configured for null" - a sentence
// about a state the address does not have, ending in a link to a page where
// nexus is declared, which could not have changed the outcome. It also blocked
// the save to collect a shipping address nothing was going to read.
//
// Three behaviours, one fact: the strip, the shipping-address prompt and the
// exemption override all ask the same endpoint and all agree.

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
	US_CALC,
	US_FILE,
	answer_xcall,
	flush,
	install_desk,
	load_taxjar_utils,
	make_frm,
	message_html,
	message_text,
	sidebar_section,
} from "./helpers/desk.js";

const MOD = "taxjar_integration.taxjar_integration.taxjar_integration";
const SCOPE_METHOD = `${MOD}.get_company_scope`;
const EXPORT_METHOD = `${MOD}.check_export_destination`;
const REGION_METHOD = `${MOD}.get_region_exemption`;
const NEXUS_METHOD = `${MOD}.check_nexus`;
const ADDRESSES_METHOD = `${MOD}.get_customer_addresses`;

const EXEMPTION_FIELDS = ["taxjar_transaction_exempt", "taxjar_transaction_exemption_type"];

let frappe;
let taxjar;

beforeEach(() => {
	frappe = install_desk();
	taxjar = load_taxjar_utils();
});

/** A saved invoice for a calculating company, with one address on it. */
function open_invoice(doc = {}) {
	return make_frm({
		company: US_CALC.company,
		fields: EXEMPTION_FIELDS,
		doc: { customer: "Acme Corp", ...doc },
	});
}

describe("the message strip", () => {
	it("names the country of an export", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[EXPORT_METHOD]: { country: "India" },
		});

		const frm = open_invoice({
			customer_address: "ADDR-IN",
			taxjar_has_nexus: 0,
			taxjar_nexus_reason: "Destination is in India, which TaxJar does not price",
		});
		await taxjar.show_no_address_tax_message(frm);
		await flush();

		expect(message_text(frm)).toContain("India");
		expect(message_text(frm)).not.toContain("null");
		expect(message_text(frm)).toContain("no taxes are charged");
	});

	it("offers no nexus link on an export", () => {
		// Nexus is a registration with a United States state. No amount of it
		// makes a sale to Mumbai taxable.
		const frm = open_invoice({ customer_address: "ADDR-IN" });
		taxjar._show_outside_coverage_message(frm, "India");

		expect(message_html(frm)).not.toContain(taxjar.TAXJAR_NEXUS_URL);
	});

	it("still reports a missing nexus for a United States address", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[EXPORT_METHOD]: {},
			[NEXUS_METHOD]: { state: "New Jersey", state_code: "NJ", country_code: "US" },
		});

		const frm = make_frm({
			company: US_CALC.company,
			is_new: true,
			fields: EXEMPTION_FIELDS,
			doc: { customer: "Acme Corp", customer_address: "ADDR-NJ" },
		});
		await taxjar.show_no_address_tax_message(frm);
		await flush();

		expect(message_text(frm)).toContain("New Jersey");
		expect(message_html(frm)).toContain(taxjar.TAXJAR_NEXUS_URL);
	});
});

describe("the shipping address prompt", () => {
	it("is skipped for an export", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[EXPORT_METHOD]: { country: "India" },
		});
		taxjar._prompt_for_shipping_address = vi.fn(() => Promise.resolve());

		const frm = open_invoice({ customer_address: "ADDR-IN" });
		await taxjar.check_shipping_address(frm);
		await flush();

		expect(taxjar._prompt_for_shipping_address).not.toHaveBeenCalled();
		expect(frappe.validated).toBe(true);
	});

	it("still asks for a United States destination", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[EXPORT_METHOD]: {},
			[ADDRESSES_METHOD]: [],
		});
		taxjar._prompt_for_shipping_address = vi.fn(() => Promise.resolve());

		const frm = open_invoice({ customer_address: "ADDR-NJ" });
		await taxjar.check_shipping_address(frm);
		await flush();

		expect(taxjar._prompt_for_shipping_address).toHaveBeenCalled();
	});

	it("asks when there is no address at all to read a country off", async () => {
		answer_xcall(frappe, { [SCOPE_METHOD]: US_CALC });
		taxjar._prompt_for_shipping_address = vi.fn(() => Promise.resolve());

		const frm = open_invoice();
		await taxjar.check_shipping_address(frm);
		await flush();

		expect(taxjar._prompt_for_shipping_address).toHaveBeenCalled();
	});
});

describe("the exemption override", () => {
	it("is pre-set to exempt for Other on an export, and locked", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[REGION_METHOD]: {},
			[EXPORT_METHOD]: { country: "India" },
		});

		const frm = open_invoice({ customer_address: "ADDR-IN" });
		await taxjar.apply_region_exemption(frm);
		await flush();

		expect(frm.doc.taxjar_transaction_exempt).toBe(1);
		expect(frm.doc.taxjar_transaction_exemption_type).toBe("Other");
		EXEMPTION_FIELDS.forEach((f) => expect(frm.fields_dict[f].df.read_only).toBe(1));
	});

	it("leaves a United States sale alone", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[REGION_METHOD]: {},
			[EXPORT_METHOD]: {},
		});

		const frm = open_invoice({ customer_address: "ADDR-NJ" });
		await taxjar.apply_region_exemption(frm);
		await flush();

		expect(frm.doc.taxjar_transaction_exempt).toBeUndefined();
		EXEMPTION_FIELDS.forEach((f) => expect(frm.fields_dict[f].df.read_only).toBe(0));
	});

	it("prefers the customer's own exemption, which TaxJar applies itself", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[REGION_METHOD]: { exemption_type: "Wholesale", state: null },
			// Answered, but never asked for: the customer's own type wins.
			[EXPORT_METHOD]: { country: "India" },
		});

		const frm = open_invoice({ customer_address: "ADDR-IN" });
		await taxjar.apply_region_exemption(frm);
		await flush();

		expect(frm.doc.taxjar_transaction_exemption_type).toBe("Wholesale");
	});

	it("does not write on a submitted document", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[REGION_METHOD]: {},
			[EXPORT_METHOD]: { country: "India" },
		});

		const frm = open_invoice({ customer_address: "ADDR-IN" });
		frm.doc.docstatus = 1;
		await taxjar.apply_region_exemption(frm);
		await flush();

		expect(frm.set_value).not.toHaveBeenCalled();
		EXEMPTION_FIELDS.forEach((f) => expect(frm.fields_dict[f].df.read_only).toBe(1));
	});
});

describe("the endpoint is asked once per address", () => {
	it("caches the answer, the way the company scope is cached", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_CALC,
			[EXPORT_METHOD]: { country: "India" },
		});

		await taxjar.export_destination("ADDR-IN");
		await taxjar.export_destination("ADDR-IN");
		await flush();

		const calls = frappe.xcall.mock.calls.filter(([method]) => method === EXPORT_METHOD);
		expect(calls).toHaveLength(1);
	});

	it("does not ask about a document with no address", async () => {
		answer_xcall(frappe, { [SCOPE_METHOD]: US_CALC });

		expect(await taxjar.export_destination(undefined)).toBeNull();
		expect(frappe.xcall).not.toHaveBeenCalled();
	});
});

describe("the sidebar pill on a draft", () => {
	const SYNC_FIELDS = ["taxjar_sync_status"];

	/** An unsubmitted invoice for a company that files, with one address. */
	function open_draft(doc = {}) {
		return make_frm({
			company: US_FILE.company,
			fields: SYNC_FIELDS,
			doc: { customer: "Acme Corp", ...doc },
		});
	}

	it("says Excluded for an export, not Submit to Sync", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_FILE,
			[EXPORT_METHOD]: { country: "India" },
		});

		const frm = open_draft({ customer_address: "ADDR-IN" });
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		const text = sidebar_section().text();
		expect(text).toContain("Excluded");
		expect(text).not.toContain("Submit to Sync");
	});

	it("still says Submit to Sync for a United States destination", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_FILE,
			[EXPORT_METHOD]: {},
		});

		const frm = open_draft({ customer_address: "ADDR-NJ" });
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		expect(sidebar_section().text()).toContain("Submit to Sync");
	});

	it("reads the shipping address before the billing address", async () => {
		answer_xcall(frappe, {
			[SCOPE_METHOD]: US_FILE,
			[EXPORT_METHOD]: (args) => (args.address === "ADDR-IN" ? { country: "India" } : {}),
		});

		const frm = open_draft({ customer_address: "ADDR-NJ", shipping_address_name: "ADDR-IN" });
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		expect(sidebar_section().text()).toContain("Excluded");
	});

	it("does not read the address of a submitted document", async () => {
		answer_xcall(frappe, { [SCOPE_METHOD]: US_FILE });

		const frm = open_draft({ customer_address: "ADDR-IN", taxjar_sync_status: "Synced" });
		frm.doc.docstatus = 1;
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		const calls = frappe.xcall.mock.calls.filter(([method]) => method === EXPORT_METHOD);
		expect(calls).toHaveLength(0);
		expect(sidebar_section().text()).toContain("Synced");
	});

	// The answer lands after the user has moved on. Writing it then would put
	// one document's status on another's form.
	it("drops an answer that arrives after the form moved to another document", async () => {
		let settle;
		frappe.xcall.mockImplementation((method) => {
			if (method === SCOPE_METHOD) return Promise.resolve(US_FILE);
			return new Promise((resolve) => {
				settle = resolve;
			});
		});

		const frm = open_draft({ customer_address: "ADDR-IN" });
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		frm.doc.name = "SINV-0002";
		settle({ country: "India" });
		await flush();

		expect(sidebar_section().length).toBe(0);
	});

	// Both address fields repaint this pill, so two picks in quick succession
	// leave two lookups running. The one asked first can be the one to answer
	// last, and the pill would then describe the address the user left.
	it("drops an answer for an address the document no longer has", async () => {
		const settle = {};
		frappe.xcall.mockImplementation((method, args) => {
			if (method === SCOPE_METHOD) return Promise.resolve(US_FILE);
			return new Promise((resolve) => {
				settle[args.address] = resolve;
			});
		});

		const frm = open_draft({ customer_address: "ADDR-IN" });
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		frm.doc.customer_address = "ADDR-NJ";
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		// New Jersey answers first, then India - the address the form has left.
		settle["ADDR-NJ"]({});
		await flush();
		settle["ADDR-IN"]({ country: "India" });
		await flush();

		const text = sidebar_section().text();
		expect(text).toContain("Submit to Sync");
		expect(text).not.toContain("Excluded");
	});

	// The other order, which was always right, and has to stay right: the
	// answer for the address the document still has must paint.
	it("paints the answer for the address the document still has", async () => {
		const settle = {};
		frappe.xcall.mockImplementation((method, args) => {
			if (method === SCOPE_METHOD) return Promise.resolve(US_FILE);
			return new Promise((resolve) => {
				settle[args.address] = resolve;
			});
		});

		const frm = open_draft({ customer_address: "ADDR-NJ" });
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		frm.doc.customer_address = "ADDR-IN";
		taxjar.render_sync_status_sidebar_pill(frm);
		await flush();

		settle["ADDR-NJ"]({});
		await flush();
		settle["ADDR-IN"]({ country: "India" });
		await flush();

		expect(sidebar_section().text()).toContain("Excluded");
	});
});
