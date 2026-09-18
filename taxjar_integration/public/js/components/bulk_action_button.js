frappe.provide("taxjar_integration");

// The "Bulk Action" dropdown that sits beside the tab titles.
//
// It is always rendered, never hidden - a control that disappears teaches the
// user nothing about why. Disabled it explains itself instead: hovering or
// focusing it says to select a record first.
//
// Disabled state is data-disabled + title, NOT the button's native disabled
// attribute - the latter would also drop it out of the keyboard tab order
// and suppress the very title that explains why. A capture-phase click
// listener swallows the click before frappe.ui.Dropdown's own (bubble-phase)
// listener gets it, so an empty menu never opens.
//
// Public API (set_items, toggle_disabled, disabled_title) is unchanged from
// this class's previous Bootstrap-dropdown implementation - only what's
// underneath it changed, to frappe.ui.dropdown (the Espresso replacement for
// bootstrap's data-toggle="dropdown", already used elsewhere in the desk).
taxjar_integration.BulkActionButton = class BulkActionButton {
	constructor(options) {
		Object.assign(this, options);
		this.label = this.label || __("Bulk Action");
		this.disabled_title = this.disabled_title || __("Select one or more records to run an action");
		this.items = [];
		this.render();
	}

	render() {
		this.dropdown = new frappe.ui.Dropdown({
			button: { label: this.label, variant: "outline", css_class: "taxjar-bulk-action" },
			options: [],
			align: "end",
		});
		this.$toggle = this.dropdown.$trigger.appendTo(this.$wrapper);

		// Runs before frappe.ui.Dropdown's own click handler (bound in its
		// constructor, above), which only fires in the bubble phase - a
		// capture-phase listener on the same element always runs first.
		this.trigger_el = this.$toggle[0];
		this.trigger_el.addEventListener("click", (e) => {
			if (this.is_disabled()) {
				e.preventDefault();
				e.stopImmediatePropagation();
			}
		}, true);

		this.set_items([]);
	}

	is_disabled() {
		return this.$toggle.is("[data-disabled]");
	}

	// items: [{ label, action }] or { divider: true }. An empty list disables
	// the button - there is nothing meaningful to offer.
	//
	// frappe.ui.Dropdown has no divider row. A line between two actions is a
	// section border, which .es-menu__group + .es-menu__group draws for the
	// second section onwards. An unknown { divider: true } item reached the
	// menu as a row with no label: an empty band that took the pointer
	// highlight and read as something you could press. So a divider ends one
	// section and starts the next, each with its heading hidden. The menu
	// drops an empty section, so a divider at either end costs nothing.
	set_items(items) {
		this.items = items || [];

		const sections = [];
		let section = null;
		for (const item of this.items) {
			if (item.divider) {
				section = null;
				continue;
			}
			if (!section) {
				section = { group: "", hide_label: true, options: [] };
				sections.push(section);
			}
			section.options.push({ label: item.label, onclick: item.action });
		}

		this.dropdown.set_options(sections);
		this.toggle_disabled(!sections.length);
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
