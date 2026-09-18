frappe.provide("taxjar_integration");

// The "Export" button that sits beside the tab's bulk action.
//
// It sends the tab's own filters and scope to a page endpoint that answers with
// a file, so the sheet holds every row the tab holds - not the one page of rows
// on screen. The request is a form POST, not frappe.xcall: the response is
// workbook bytes with a filename, which the browser downloads and a JSON call
// could not.
//
// It is always rendered, never hidden - a control that disappears teaches the
// reader nothing about why. Disabled it explains itself instead: there is
// nothing to export, or there is too much of it to send in one request.
//
// Disabled state is data-disabled + title, NOT the button's native disabled
// attribute - the latter would also drop it out of the keyboard tab order and
// suppress the very title that explains why. Same reasoning, and the same
// capture-phase click guard, as BulkActionButton.
taxjar_integration.ExportButton = class ExportButton {
	// options: $wrapper, method (the dotted path of the export endpoint),
	// get_args (a function returning { filters, scope } for the open tab).
	constructor(options) {
		Object.assign(this, options);
		this.label = this.label || __("Export");
		this.total = 0;
		this.limit = 0;
		this.render();
	}

	render() {
		this.$button = frappe.ui
			.button({
				label: this.label,
				icon: "share",
				variant: "outline",
				css_class: "taxjar-export",
				onclick: () => this.download(),
			})
			.appendTo(this.$wrapper);

		// Runs before the click handler frappe.ui.button binds, which only fires
		// in the bubble phase - a capture-phase listener on the same element
		// always runs first.
		this.$button[0].addEventListener(
			"click",
			(e) => {
				if (this.is_disabled()) {
					e.preventDefault();
					e.stopImmediatePropagation();
				}
			},
			true
		);

		this.set_state(null);
	}

	is_disabled() {
		return this.$button.is("[data-disabled]");
	}

	// state: the page envelope the table was just rendered from - it carries
	// both how many rows the filters match and how many one export may hold, so
	// the button knows what it would be asking for before it is pressed.
	set_state(state) {
		this.total = state?.total || 0;
		this.limit = state?.export_limit || 0;
		this.toggle_disabled();
	}

	toggle_disabled() {
		const reason = this.blocked_reason();
		if (reason) {
			this.$button.attr({ "data-disabled": "", "aria-disabled": "true", title: reason });
		} else {
			this.$button
				.removeAttr("data-disabled")
				.removeAttr("aria-disabled")
				.removeAttr("title");
		}
	}

	// Why the button cannot be pressed, in words the reader can act on. An
	// empty string means it can.
	blocked_reason() {
		if (!this.total) return __("There is nothing here to export");

		if (this.limit && this.total > this.limit) {
			return __(
				"This tab holds {0} records. One export carries at most {1} - narrow the filters first.",
				[this.total, this.limit]
			);
		}

		return "";
	}

	download() {
		open_url_post(`/api/method/${this.method}`, this.get_args());
	}
};
