// The same two summaries the TaxJar Settings form's "Nexus & Product Category"
// tab shows, on a page of their own - both are read far more often than the
// credentials they sit behind. The tables are shared with that tab
// (taxjar_integration.render_nexus_cards / render_product_tax_category_summary
// in public/js/taxjar_utils.js), so the two can't drift apart. The section
// headers are this page's own, and follow the guided setup wizard's Sync Nexus
// step rather than the form's labelled buttons: an icon-only refresh control
// on the right with "Synced <when>" beside it.
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
		this.make_layout();
	}

	make_layout() {
		const $body = $(`
			<div class="taxjar-nexus-page">
				<section class="taxjar-nexus-section" data-section="nexus">
					<div class="taxjar-nexus-section-head">
						<div class="taxjar-nexus-section-title">${__("State Nexus")}</div>
						<span class="text-muted taxjar-nexus-synced"></span>
						<div class="taxjar-nexus-sync-mount"></div>
					</div>
					<div class="taxjar-nexus-list"></div>
				</section>
				<section class="taxjar-nexus-section" data-section="categories">
					<div class="taxjar-nexus-section-head">
						<div class="taxjar-nexus-section-title">${__("Product Tax Category")}</div>
						<span class="text-muted taxjar-nexus-synced"></span>
						<div class="taxjar-nexus-sync-mount"></div>
					</div>
					<div class="taxjar-product-category-summary"></div>
				</section>
			</div>
		`).appendTo(this.page.main);

		this.$nexus = $body.find('[data-section="nexus"]');
		this.$categories = $body.find('[data-section="categories"]');

		this._mount_sync_button(this.$nexus, __("Fetch nexus regions from TaxJar"), () =>
			this.sync(this.$nexus, "update_nexus_list", __("Nexus regions fetched and updated."))
		);
		this._mount_sync_button(this.$categories, __("Fetch product tax categories from TaxJar"), () =>
			this.sync(this.$categories, "refresh_product_tax_categories", __("Product tax categories fetched and updated."))
		);
	}

	_mount_sync_button($section, title, onclick) {
		$section.find(".taxjar-nexus-sync-mount").append(frappe.ui.button({
			icon: "refresh-cw", variant: "outline", title, onclick,
		}));
	}

	// frappe.xcall, not frappe.call: the latter hands back a jQuery jqXHR, and a
	// jQuery 3 Deferred has .always() but no .finally() - so the sync() chain
	// below threw on it and left the button spinning forever. xcall wraps the
	// same request in a native Promise (and resolves to r.message), which is
	// what every other page in this app already calls.
	refresh() {
		frappe.xcall(`${API}.get_summary`).then((summary) => this.render(summary));
	}

	render(summary) {
		summary = summary || {};
		const categories = summary.product_tax_categories || {};

		taxjar_integration.render_nexus_cards(
			this.$nexus.find(".taxjar-nexus-list"), summary.nexus
		);
		this._render_synced(this.$nexus, summary.nexus_last_synced);

		// The header carries the "Synced ..." caption for both sections, so the
		// summary box below is asked not to repeat it - unlike on the settings
		// form, where the box is the only place it can go.
		taxjar_integration.render_product_tax_category_summary(
			this.$categories.find(".taxjar-product-category-summary"),
			categories,
			{ show_last_updated: false }
		);
		this._render_synced(this.$categories, categories.last_updated);
	}

	// Blank until a sync has actually happened - "Synced never" is noise next
	// to a section that already says nothing has been fetched yet.
	//
	// comment_when() is prettyDate, which returns "" for any timestamp it works
	// out to be in the future (pretty_date.js:21) - which is what a just-written
	// row looks like whenever the site's System Settings timezone runs ahead of
	// the browser's. The absolute date is the fallback rather than a blank.
	//
	// html(), not text(): comment_when() hands back a whole
	// <span class="frappe-timestamp" title="<absolute date>"> element, not a
	// bare string, so as text it renders as visible markup.
	_render_synced($section, when) {
		const relative = when
			? frappe.datetime.comment_when(when) || taxjar_integration.format_last_synced(when)
			: "";
		$section.find(".taxjar-nexus-synced").html(relative ? __("Synced {0}", [relative]) : "");
	}

	// Both syncs are a TaxJar round trip per company. The button carries the
	// progress itself (same as the wizard's Sync Nexus step) rather than
	// freezing the whole desk behind a dialog.
	sync($section, method, success_message) {
		const $button = $section.find(".taxjar-nexus-sync-mount .es-button")
			.attr("aria-busy", "true")
			.prop("disabled", true);

		frappe.xcall(`${API}.${method}`).then((summary) => {
			this.render(summary);
			frappe.show_alert({ message: success_message, indicator: "green" }, 5);
		}).catch(() => {
			// frappe.call's own error handler has already shown the server's
			// message; this is only here so the button below still resets.
		}).finally(() => {
			$button.removeAttr("aria-busy").prop("disabled", false);
		});
	}
}
