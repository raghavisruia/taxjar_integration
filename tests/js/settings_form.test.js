// The TaxJar Settings grid, where a company's ledgers are picked.
//
// Design doc §4.1. The account fields are filtered when the account is picked,
// and never again. Changing the company on a row would otherwise leave the
// previous company's ledgers sitting there, which is what reaches a transaction
// as "Account ... does not belong to company ...", naming neither TaxJar nor
// the row that caused it. The server refuses such a pair as well; this is the
// half that stops it being typed in the first place.

import { beforeEach, describe, expect, it } from "vitest";

import { add_grid_row, install_desk, load_taxjar_settings_form, load_taxjar_utils } from "./helpers/desk.js";

const CONFIG = "TaxJar Company Config";

let frappe;
let handlers;

beforeEach(() => {
	frappe = install_desk();
	load_taxjar_utils();
	handlers = load_taxjar_settings_form();
});

describe("changing the company on a configuration row", () => {
	function change_company(values) {
		const { cdt, cdn, row } = add_grid_row(CONFIG, "row-1", values);
		handlers[CONFIG].company({}, cdt, cdn);
		return row;
	}

	it("clears both ledgers", () => {
		const row = change_company({
			company: "US File Co",
			tax_account_head: "Sales Tax - US Calc Co",
			shipping_account_head: "Freight - US Calc Co",
		});

		expect(row.tax_account_head).toBeNull();
		expect(row.shipping_account_head).toBeNull();
	});

	it("clears the other ledger when only one was picked", () => {
		const row = change_company({
			company: "US File Co",
			tax_account_head: "Sales Tax - US Calc Co",
			shipping_account_head: null,
		});

		expect(row.tax_account_head).toBeNull();
		expect(row.shipping_account_head).toBeNull();
	});

	// Silence would leave the user to notice two fields had emptied themselves.
	it("says so, and names the company to pick from", () => {
		change_company({
			company: "US File Co",
			tax_account_head: "Sales Tax - US Calc Co",
			shipping_account_head: "Freight - US Calc Co",
		});

		expect(frappe.show_alert).toHaveBeenCalledTimes(1);
		const alert = frappe.show_alert.mock.calls[0][0];
		expect(alert.message).toContain("Ledger accounts cleared");
		expect(alert.message).toContain("US File Co");
		expect(alert.indicator).toBe("orange");
	});

	// A row that never had a ledger has nothing to clear, and an alert there
	// would report an event that did not happen.
	it("does nothing on a row with no ledgers yet", () => {
		const row = change_company({
			company: "US File Co",
			tax_account_head: null,
			shipping_account_head: null,
		});

		expect(frappe.model.set_value).not.toHaveBeenCalled();
		expect(frappe.show_alert).not.toHaveBeenCalled();
		expect(row.company).toBe("US File Co");
	});
});

describe("the ledger pickers", () => {
	// The server refuses a cross-company pair either way. This keeps the list
	// from offering one, which is the difference between a filter and a refusal.
	it("are filtered to the row's own company, and to single ledgers", () => {
		const queries = {};

		// Enough of the Settings form for its own refresh() to run all the way
		// through: it renders the nexus cards and the API mode description
		// before it registers these queries.
		const html_field = () => ({ $wrapper: $("<div></div>") });
		const frm = {
			doc: { setup_complete: 1, api_mode: "Sandbox", nexus: [] },
			fields_dict: {
				nexus_html: html_field(),
				product_tax_category_html: html_field(),
				update_nexus_list_btn: {
					$wrapper: $("<div class='input-max-width'></div>"),
					input_area: $("<div></div>")[0],
				},
			},
			set_intro() {},
			set_df_property() {},
			set_query(fieldname, parentfield, query) {
				queries[fieldname] = { parentfield, query };
			},
			call: () => Promise.resolve({}),
		};

		handlers["TaxJar Settings"].refresh(frm);

		const { cdt, cdn } = add_grid_row(CONFIG, "row-1", { company: "US Off Co" });

		for (const fieldname of ["tax_account_head", "shipping_account_head"]) {
			expect(queries[fieldname], `${fieldname} has no filter`).toBeTruthy();
			expect(queries[fieldname].parentfield).toBe("company_config");
			expect(queries[fieldname].query({}, cdt, cdn)).toEqual({
				filters: { company: "US Off Co", is_group: 0 },
			});
		}
	});
});
