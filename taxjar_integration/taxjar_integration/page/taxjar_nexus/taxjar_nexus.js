// The Nexus & Product Category page (/app/taxjar-nexus) - the same two
// summaries the TaxJar Settings form's "Nexus & Product Category" tab shows,
// on a page of their own, because both are read far more often than the
// credentials they sit behind.
//
// This page draws its own cards instead of the shared table renderers in
// public/js/taxjar_utils.js. Every card here carries its own title, subtitle,
// "Synced ..." caption and refresh control, and the settings form already
// supplies all four from its section headers and its labelled buttons. The
// form keeps the shared table renderers, so it is unchanged.
//
// Two layouts, picked by how many companies hold nexus regions:
//   one company    - the nexus card above the category card, in a column only
//                    as wide as its own content.
//   two or more    - one card per company in a grid, under a page level bar,
//                    with the category count in a card of its own below.
//
// Boxes come from the desk component library (frappe.ui.badge, .button,
// .empty_state) and the shared utility classes in
// frappe/public/scss/common/utilities.scss, so every colour, size and radius
// is a desk token and the page follows the dark theme. taxjar_nexus.css holds
// only what neither of those can express - see the note at the top of it.
//
// Data fetch lives in on_page_show, not the constructor - see the comment at
// the top of taxjar_transactions.js for why (cached desk pages, and the first
// on_page_show firing straight after on_page_load).
frappe.pages["taxjar-nexus"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Nexus & Product Category"),
		single_column: true,
	});

	wrapper.taxjar_nexus = new TaxJarNexusSummary(page);
};

frappe.pages["taxjar-nexus"].on_page_show = function (wrapper) {
	wrapper.taxjar_nexus.refresh();
};

const API = "taxjar_integration.taxjar_integration.page.taxjar_nexus.taxjar_nexus";

class TaxJarNexusSummary {
	constructor(page) {
		this.page = page;
		this.$body = $('<div class="taxjar-nexus-page flex flex-col gap-8"></div>').appendTo(
			page.main
		);
	}

	// frappe.xcall, not frappe.call: the latter hands back a jQuery jqXHR, and a
	// jQuery 3 Deferred has .always() but no .finally() - so the sync() chain
	// below threw on it and left the button spinning forever. xcall wraps the
	// same request in a native Promise (and resolves to r.message), which is
	// what every other page in this app already calls.
	refresh() {
		frappe.xcall(`${API}.get_summary`).then((summary) => this.render(summary));
	}

	// The layout follows the data, so the body is rebuilt on every render rather
	// than patched in place. Both refresh controls are wired again here.
	render(summary) {
		summary = summary || {};
		const companies = this._group_by_company(summary.nexus || []);
		const categories = summary.product_tax_categories || {};

		this.$body.empty();

		// One width for the whole page, not one per section: the widest section
		// sets it and the rest inherit, so every section and every refresh
		// control lines up on the same right edge. taxjar_nexus.css turns the
		// count into that width - see --taxjar-cards there.
		this.$body[0].style.setProperty("--taxjar-cards", String(Math.max(companies.length, 1)));

		if (companies.length > 1) {
			this._render_company_grid(companies, summary.nexus_last_synced);
			this._render_category_section(this.$body, categories);
			return;
		}

		// One company keeps both cards in a column only as wide as its content.
		const $column = $('<div class="taxjar-nexus-solo flex flex-col gap-8"></div>').appendTo(
			this.$body
		);

		this._render_nexus_card($column, companies[0], summary.nexus_last_synced);
		this._render_category_card($column, categories);
	}

	// ── data ──

	// The TaxJar Settings nexus child table, as one entry per company holding
	// that company's regions grouped by country. Insertion order is kept, so the
	// page lists companies and countries in the order the server returned them.
	_group_by_company(rows) {
		const companies = [];

		for (const row of rows) {
			const name = row.company || __("(No Company)");
			let company = companies.find((c) => c.name === name);
			if (!company) {
				company = { name, regions: [], countries: [] };
				companies.push(company);
			}
			company.regions.push(row);

			const country = row.country || __("Unknown");
			let group = company.countries.find((g) => g.country === country);
			if (!group) {
				group = { country, regions: [] };
				company.countries.push(group);
			}
			group.regions.push(row);
		}

		return companies;
	}

	// ── layouts ──

	// One company, or none at all: the nexus card, with the company name above
	// its own regions.
	_render_nexus_card($parent, company, last_synced) {
		const $card = this._card().appendTo($parent);
		const $head = this._render_head($card, {
			title: __("State Nexus"),
			subtitle: __("Auto-updated at midnight"),
			css_class: "px-5 py-4",
			tooltip: __("Fetch nexus regions from TaxJar"),
			onclick: () => this._sync_nexus($card),
		});
		this._render_synced($head, last_synced);

		const $card_body = $(
			'<div class="border-t border-outline-gray-1 px-5 py-4 flex flex-col gap-4"></div>'
		).appendTo($card);

		if (!company) {
			$card_body.append(this._nexus_empty_state());
			return;
		}

		$('<div class="text-base-medium text-ink-gray-8"></div>')
			.text(company.name)
			.appendTo($card_body);
		this._render_countries($card_body, company.countries);
	}

	// Two or more companies: the head becomes a page level bar above a grid that
	// holds one card per company, named and then listed. The bar says the same
	// thing the stacked card's head says.
	_render_company_grid(companies, last_synced) {
		const $section = this._section(this.$body);
		const $head = this._render_head($section, {
			title: __("State Nexus"),
			subtitle: __("Auto-updated at midnight"),
			tooltip: __("Fetch nexus regions from TaxJar"),
			onclick: () => this._sync_nexus($section),
		});
		this._render_synced($head, last_synced);

		const $grid = $('<div class="taxjar-nexus-grid"></div>').appendTo($section);

		for (const company of companies) {
			const $card = this._card().appendTo($grid);

			$('<div class="text-base-medium text-ink-gray-8 truncate p-4"></div>')
				.text(company.name)
				.appendTo($card);

			const $card_body = $(
				'<div class="border-t border-outline-gray-1 p-4 flex flex-col gap-4"></div>'
			).appendTo($card);
			this._render_countries($card_body, company.countries);
		}
	}

	// One company: the category count as a card below the nexus card, the same
	// width and the same shape - a head, a rule, then the count.
	_render_category_card($parent, categories) {
		const $card = this._card().appendTo($parent);
		const $head = this._render_head($card, {
			title: __("Product Tax Category"),
			subtitle: __("Updates are fetched automatically every week"),
			css_class: "px-5 py-4",
			tooltip: __("Fetch product tax categories from TaxJar"),
			onclick: () => this._sync_categories($card),
		});
		this._render_synced($head, categories.last_updated);

		const $card_body = $('<div class="border-t border-outline-gray-1 px-5 py-4"></div>').appendTo(
			$card
		);

		if (!categories.count) {
			$card_body.append(this._category_empty_state());
			return;
		}

		this._render_count($card_body, categories.count);
	}

	// Two or more companies: the same shape State Nexus takes in this layout - a
	// page level bar, then the count in a card of its own. The card sits in the
	// same grid the company cards use, so it is one column wide rather than
	// stretched across the page.
	_render_category_section($parent, categories) {
		const $section = this._section($parent);
		const $head = this._render_head($section, {
			title: __("Product Tax Category"),
			subtitle: __("Updates are fetched automatically every week"),
			tooltip: __("Fetch product tax categories from TaxJar"),
			onclick: () => this._sync_categories($section),
		});
		this._render_synced($head, categories.last_updated);

		const $card = this._card()
			.addClass("p-5")
			.appendTo($('<div class="taxjar-nexus-grid"></div>').appendTo($section));

		if (!categories.count) {
			$card.append(this._category_empty_state());
			return;
		}

		this._render_count($card, categories.count);
	}

	// ── pieces ──

	// A section is a bar and the cards it heads. Its width comes from the page,
	// which caps every section the same way - without that cap the bar stretched
	// the whole window, and its refresh control sat far to the right of the
	// cards it acts on.
	_section($parent) {
		return $('<div class="taxjar-nexus-section flex flex-col gap-4"></div>').appendTo($parent);
	}

	_card() {
		return $(
			'<div class="bg-surface-elevation-1 border border-outline-gray-1 rounded-lg shadow-sm"></div>'
		);
	}

	// The title with its description under it on the left, the "Synced ..."
	// caption and the icon only refresh control on the right. The same head
	// serves a card and a page level bar - only the padding differs, which the
	// caller passes in.
	//
	// The row does not wrap. Wrapping dropped the control below the title and
	// hard against the left edge, which reads as a control for whatever comes
	// next. The title block carries min-w-0 instead, so a long description
	// takes a second line of its own and the control stays on the right.
	_render_head($parent, opts) {
		const $head = $(`
			<div class="flex items-center justify-between gap-4 ${opts.css_class || ""}">
				<div class="flex flex-col gap-0.5 min-w-0">
					<div class="text-3xl-semibold text-ink-gray-8" data-head="title"></div>
					<div class="text-p-sm text-ink-gray-5" data-head="subtitle"></div>
				</div>
			</div>
		`).appendTo($parent);

		$head.find('[data-head="title"]').text(opts.title);
		$head.find('[data-head="subtitle"]').text(opts.subtitle);
		this._render_sync_controls($head, opts);

		return $head;
	}

	// The right hand end of every head: the "Synced ..." caption, then the
	// refresh control.
	_render_sync_controls($parent, opts) {
		const $controls = $('<div class="flex items-center gap-2 shrink-0"></div>').appendTo(
			$parent
		);

		$('<span class="text-p-xs text-ink-gray-5 whitespace-nowrap taxjar-nexus-synced"></span>').appendTo(
			$controls
		);

		$('<span class="taxjar-nexus-sync-mount"></span>')
			.append(
				frappe.ui.button({
					icon: "refresh-cw",
					variant: "outline",
					title: opts.tooltip,
					onclick: opts.onclick,
				})
			)
			.appendTo($controls);

		return $controls;
	}

	// The count, big, as the link into the list it counts, with its label beside
	// it. Both layouts draw it the same way - only the box around it changes.
	_render_count($parent, count) {
		const $row = $('<div class="flex items-baseline gap-2 flex-wrap"></div>').appendTo($parent);

		$('<a class="text-5xl-semibold text-ink-gray-8" href="/app/product-tax-category"></a>')
			.text(count)
			.appendTo($row);
		$('<span class="text-p-base text-ink-gray-6 whitespace-nowrap"></span>')
			.text(__("categories configured"))
			.appendTo($row);

		return $row;
	}

	_render_countries($parent, countries) {
		for (const group of countries) {
			const $group = $('<div class="flex flex-col gap-2"></div>').appendTo($parent);

			$('<div class="text-xs-medium text-ink-gray-6 whitespace-nowrap"></div>')
				.text(group.country)
				.appendTo($group);

			const $chips = $('<div class="flex flex-wrap gap-2"></div>').appendTo($group);
			group.regions.forEach((row) => this._region_chip(row).appendTo($chips));
		}
	}

	// The region name as a badge, with its region code beside the name inside the
	// same badge. frappe.ui.badge escapes its label, so the code is appended to
	// the finished badge rather than passed through as markup.
	//
	// The country code is not shown: the country name already labels the group
	// this chip sits in. It stays on the raw Nexus table on the settings form.
	_region_chip(row) {
		const $chip = frappe.ui.badge({
			label: row.region || "—",
			variant: "outline",
			// lg is the badge's own 13px step. The page sets no type size of its
			// own anywhere - every size on it comes from a component or from a
			// desk typography class.
			size: "lg",
			css_class: "taxjar-nexus-chip",
		});

		if (row.region_code) {
			$('<span class="text-xs-medium taxjar-nexus-code text-ink-violet-6"></span>')
				.text(row.region_code)
				.appendTo($chip);
		}

		return $chip;
	}

	// Same wording as the shared renderer the settings form still uses, so the
	// two places say the same thing when neither has been fetched yet.
	_nexus_empty_state() {
		return frappe.ui.empty_state({
			icon: "map-pin",
			title: __("No nexus regions loaded"),
			description: __("Fetch them from TaxJar using the button above."),
			css_class: "my-2",
		});
	}

	_category_empty_state() {
		return frappe.ui.empty_state({
			icon: "tag",
			title: __("No product tax categories loaded"),
			description: __("Fetch them from TaxJar using the button above."),
			css_class: "my-2",
		});
	}

	// Blank until a sync has actually happened - "Synced never" is noise next
	// to a card that already says nothing has been fetched yet.
	//
	// comment_when() is prettyDate, which returns "" for any timestamp it works
	// out to be in the future (pretty_date.js:21) - which is what a just-written
	// row looks like whenever the site's System Settings timezone runs ahead of
	// the browser's. The absolute date is the fallback rather than a blank.
	//
	// html(), not text(): comment_when() hands back a whole
	// <span class="frappe-timestamp" title="<absolute date>"> element, not a
	// bare string, so as text it renders as visible markup.
	_render_synced($head, when) {
		const relative = when
			? frappe.datetime.comment_when(when) || taxjar_integration.format_last_synced(when)
			: "";
		$head.find(".taxjar-nexus-synced").html(relative ? __("Synced {0}", [relative]) : "");
	}

	// ── syncs ──

	_sync_nexus($section) {
		this.sync($section, "update_nexus_list", __("Nexus regions fetched and updated."));
	}

	_sync_categories($section) {
		this.sync(
			$section,
			"refresh_product_tax_categories",
			__("Product tax categories fetched and updated.")
		);
	}

	// Both syncs are a TaxJar round trip per company. The button carries the
	// progress itself (same as the wizard's Sync Nexus step) rather than
	// freezing the whole desk behind a dialog.
	sync($section, method, success_message) {
		const $button = $section
			.find(".taxjar-nexus-sync-mount .es-button")
			.attr("aria-busy", "true")
			.prop("disabled", true);

		frappe
			.xcall(`${API}.${method}`)
			.then((summary) => {
				this.render(summary);
				frappe.show_alert({ message: success_message, indicator: "green" }, 5);
			})
			.catch(() => {
				// frappe.call's own error handler has already shown the server's
				// message; this is only here so the button below still resets.
			})
			.finally(() => {
				// A successful sync has already replaced this button along with the
				// rest of the body. Resetting a detached element does nothing, which
				// is what a failed sync needs it to do on the button still on screen.
				$button.removeAttr("aria-busy").prop("disabled", false);
			});
	}
}
