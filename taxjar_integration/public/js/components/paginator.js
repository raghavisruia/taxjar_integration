frappe.provide("taxjar_integration");

// Numbered pagination for the TaxJar desk pages.
//
// The tables render at their natural height with no inner scrollbar, so the
// page size is what keeps a page to roughly a screenful - hence the size
// picker sitting next to the page numbers rather than buried in a menu.
//
// Long ranges collapse to first / neighbours / last with ellipses, so the
// control stays the same width whether there are 3 pages or 300.
taxjar_integration.Paginator = class Paginator {
	constructor(options) {
		Object.assign(this, options);
		this.page_sizes = this.page_sizes || [20, 50, 100];
		this.$wrapper.addClass("taxjar-paginator");
	}

	// state: { page, total_pages, page_size }
	//
	// Prev / Next are always rendered, disabled at the ends rather than removed
	// - controls that come and go make the row jump and leave you unsure
	// whether there is more to see or the button simply vanished.
	render(state) {
		this.$wrapper.empty();
		if (!state) return;

		this.render_size_picker(state);

		const total_pages = Math.max(state.total_pages || 1, 1);

		// "Page 2 of 7" sits above the controls: where you are, then the means
		// of moving. The numbered buttons alone say which page is current but
		// not how many there are once the range collapses behind an ellipsis.
		const $nav = $('<div class="taxjar-paginator-nav"></div>').appendTo(this.$wrapper);
		$(`<div class="taxjar-paginator-status">${__("Page {0} of {1}", [state.page, total_pages])}</div>`).appendTo(
			$nav
		);

		const $pages = $('<div class="taxjar-paginator-pages"></div>').appendTo($nav);

		this.add_step($pages, { label: __("Prev"), icon: "chevron-left" }, state.page - 1, state.page <= 1);

		// Numbers only earn their place once there is a choice to make.
		if (total_pages > 1) {
			for (const entry of this.get_page_entries(state.page, total_pages)) {
				if (entry === "…") {
					$('<span class="taxjar-paginator-gap">…</span>').appendTo($pages);
					continue;
				}
				$pages.append(frappe.ui.button({
					label: String(entry),
					variant: entry === state.page ? "solid" : "subtle",
					onclick: () => entry !== state.page && this.on_page(entry),
				}));
			}
		}

		this.add_step(
			$pages,
			{ label: __("Next"), icon_right: "chevron-right" },
			state.page + 1,
			state.page >= total_pages
		);
	}

	// The same segmented group the list view uses for its page length: the
	// sizes are few and worth comparing at a glance, which a collapsed select
	// hides. The current size is disabled - picking it again only reloads the
	// page you are already on.
	render_size_picker(state) {
		const $picker = $('<div class="taxjar-paginator-size"></div>').appendTo(this.$wrapper);
		const $group = $('<div class="btn-group"></div>').appendTo($picker);

		this.page_sizes.forEach((size) => {
			const current = size === state?.page_size;
			$(`<button type="button" class="btn btn-default btn-sm btn-paging ${
				current ? "btn-info" : ""
			}" data-value="${size}" ${current ? "disabled" : ""}>${size}</button>`).appendTo($group);
		});

		// Changing the size reshuffles every boundary, so the old page number
		// is meaningless - the caller resets to 1.
		$group.on("click", ".btn-paging", (e) =>
			this.on_page_size(parseInt($(e.currentTarget).data("value"), 10))
		);
	}

	add_step($pages, opts, page, disabled) {
		$pages.append(frappe.ui.button({
			...opts, variant: "subtle", disabled,
			onclick: () => this.on_page(page),
		}));
	}

	// First, last, and the current page with a neighbour either side; ellipses
	// for whatever that skips.
	get_page_entries(page, total) {
		const entries = new Set([1, total, page, page - 1, page + 1]);
		const pages = [...entries].filter((n) => n >= 1 && n <= total).sort((a, b) => a - b);

		return pages.reduce((out, n, i) => {
			if (i && n - pages[i - 1] > 1) out.push("…");
			out.push(n);
			return out;
		}, []);
	}
};
