frappe.provide("taxjar_integration");

// The "Bulk Action" dropdown that sits beside the tab titles.
//
// It is always rendered, never hidden - a control that disappears teaches the
// user nothing about why. Disabled it explains itself instead: hovering or
// focusing it says to select a record first.
//
// Disabled state is data-disabled + title, NOT pointer-events: none. The latter
// suppresses the very tooltip that explains the disabled state, and takes the
// button out of the keyboard path with it. The click handler guards instead.
// Same approach frappe's own es-pill group uses.
taxjar_integration.BulkActionButton = class BulkActionButton {
	constructor(options) {
		Object.assign(this, options);
		this.label = this.label || __("Bulk Action");
		this.disabled_title = this.disabled_title || __("Select one or more records to run an action");
		this.items = [];
		this.render();
	}

	render() {
		this.$button_group = $(`
			<div class="taxjar-bulk-action btn-group">
				<button type="button" class="btn btn-default btn-sm dropdown-toggle" data-toggle="dropdown"
					aria-haspopup="true" aria-expanded="false">
					${frappe.utils.escape_html(this.label)}
					<span class="caret"></span>
				</button>
				<ul class="dropdown-menu dropdown-menu-right"></ul>
			</div>
		`).appendTo(this.$wrapper);

		this.$toggle = this.$button_group.find(".dropdown-toggle");
		this.$menu = this.$button_group.find(".dropdown-menu");

		// bootstrap opens the menu on its own; swallow the click while disabled
		// so an empty menu never appears.
		this.$toggle.on("click", (e) => {
			if (this.is_disabled()) {
				e.preventDefault();
				e.stopImmediatePropagation();
			}
		});

		this.set_items([]);
	}

	is_disabled() {
		return this.$toggle.is("[data-disabled]");
	}

	// items: [{ label, action }] or { divider: true }. An empty list disables
	// the button - there is nothing meaningful to offer.
	set_items(items) {
		this.items = items || [];
		this.$menu.empty();

		this.items.forEach((item, index) => {
			if (item.divider) {
				$('<li class="dropdown-divider"></li>').appendTo(this.$menu);
				return;
			}

			$(`<li><a class="dropdown-item" href="#" data-index="${index}">${frappe.utils.escape_html(
				item.label
			)}</a></li>`)
				.appendTo(this.$menu)
				.on("click", (e) => {
					e.preventDefault();
					item.action();
				});
		});

		this.toggle_disabled(!this.items.some((item) => !item.divider));
	}

	toggle_disabled(disabled) {
		if (disabled) {
			this.$toggle.attr({
				"data-disabled": "",
				"aria-disabled": "true",
				title: this.disabled_title,
			});
		} else {
			this.$toggle.removeAttr("data-disabled").removeAttr("aria-disabled").removeAttr("title");
		}
	}
};
