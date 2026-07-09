// TaxJar guided setup — desk page shell (Phase 1).
//
// Layout: a horizontal milestone rail across the top (connected nodes, navigation
// only) and a single-step panel below that swaps its entire content on Save &
// continue — only one step is ever shown at a time, rendered as plain page flow
// (no card/box around it) rather than a widget embedded in the desk. Phase 1 wires
// the shell to real server state via get_setup_state, plus the Review step's
// finish action. The per-step native controls (Connect / Accounts / Features /
// Nexus) are wired in later phases — see docs/guided-setup-plan.md.

// Milestone status icons — same Lucide set (circle-check / circle-dot-dashed /
// circle-dashed) ERPNext's own Getting Started onboarding uses for step status,
// inlined so the outline reads as first-party rather than a bespoke widget.
const TS_ICONS = {
	check: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><path d="m9 12 2 2 4-4"></path></svg>`,
	dot_dashed: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10.1 2.18a9.93 9.93 0 0 1 3.8 0"></path><path d="M17.6 3.71a9.95 9.95 0 0 1 2.69 2.7"></path><path d="M21.82 10.1a9.93 9.93 0 0 1 0 3.8"></path><path d="M20.29 17.6a9.95 9.95 0 0 1-2.7 2.69"></path><path d="M13.9 21.82a9.94 9.94 0 0 1-3.8 0"></path><path d="M6.4 20.29a9.95 9.95 0 0 1-2.69-2.7"></path><path d="M2.18 13.9a9.93 9.93 0 0 1 0-3.8"></path><path d="M3.71 6.4a9.95 9.95 0 0 1 2.7-2.69"></path><circle cx="12" cy="12" r="1"></circle></svg>`,
	dashed: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10.1 2.182a10 10 0 0 1 3.8 0"></path><path d="M13.9 21.818a10 10 0 0 1-3.8 0"></path><path d="M17.609 3.721a10 10 0 0 1 2.69 2.7"></path><path d="M2.182 13.9a10 10 0 0 1 0-3.8"></path><path d="M20.279 17.609a10 10 0 0 1-2.7 2.69"></path><path d="M21.818 10.1a10 10 0 0 1 0 3.8"></path><path d="M3.721 6.391a10 10 0 0 1 2.7-2.69"></path><path d="M6.391 20.279a10 10 0 0 1-2.69-2.7"></path></svg>`,
};

frappe.pages["taxjar-setup"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("TaxJar Setup"),
		single_column: true,
	});
	new TaxJarSetup(page);
};

const SETUP_STEPS = [
	{ key: "welcome", label: __("Welcome"), title: __("Let’s connect TaxJar"),
	  sub: __("A quick, guided setup — about 5 minutes. You can leave and resume any time."), skip: false },
	{ key: "connect", label: __("Connect API"), title: __("Connect your TaxJar account"),
	  sub: __("Choose a mode, enter each company’s token, and verify the connection."), skip: false },
	{ key: "accounts", label: __("Company accounts"), title: __("Where should tax be posted?"),
	  sub: __("Map the tax and shipping accounts TaxJar writes to on each Sales Invoice."), skip: false },
	{ key: "features", label: __("Features"), title: __("What should TaxJar do?"),
	  sub: __("A master switch, then Calculate Sales Tax and File Transactions per company."), skip: true },
	{ key: "nexus", label: __("Nexus"), title: __("Where do you collect tax?"),
	  sub: __("Nexus comes from TaxJar. Fetch it once — it stays current on its own."), skip: false },
	{ key: "review", label: __("Review"), title: __("Review & activate"),
	  sub: __("Everything you chose, grouped. Activating writes it to TaxJar Settings."), skip: false },
];

class TaxJarSetup {
	constructor(page) {
		this.page = page;
		this.cur = 0;
		this.reached = 0;
		this.state = null;
		this._build_shell();
		this._load_state();
	}

	_build_shell() {
		this.$root = $(`
			<div class="taxjar-setup">
				<nav class="ts-outline">
					<ol class="ts-milestones"></ol>
				</nav>
				<section class="ts-panel">
					<header class="ts-head">
						<div class="ts-kick"></div>
						<h2 class="ts-title"></h2>
						<p class="ts-sub"></p>
						<ol class="ts-progress ts-intervals" role="progressbar" aria-valuemin="1" aria-valuemax="${SETUP_STEPS.length}"></ol>
					</header>
					<div class="ts-body"></div>
					<footer class="ts-foot">
						<button class="btn btn-default ts-back">${__("Back")}</button>
						<span class="ts-grow"></span>
						<button class="btn btn-default ts-skip hide">${__("Skip for now")}</button>
						<button class="btn btn-dark ts-next"></button>
					</footer>
				</section>
			</div>
		`).appendTo(this.page.main);

		this.$milestones = this.$root.find(".ts-milestones");
		this.$intervals = this.$root.find(".ts-intervals");
		this.$body = this.$root.find(".ts-body");
		this.$root.find(".ts-back").on("click", () => this._go(this.cur - 1));
		this.$root.find(".ts-skip").on("click", () => this._advance());
		this.$root.find(".ts-next").on("click", () => this._on_next());

		this.$milestones.html(SETUP_STEPS.map((s, i) => `
			<li class="ts-milestone" data-i="${i}">
				<button class="ts-node-btn">
					<span class="ts-node"></span>
					<span class="ts-node-label">${frappe.utils.escape_html(s.label)}</span>
				</button>
			</li>
		`).join(""));
		this.$milestones.find(".ts-node-btn").on("click", (e) => {
			this._go(+$(e.currentTarget).closest(".ts-milestone").data("i"));
		});

		// Progress bar — frappe-ui's <Progress intervals> segments (one per step), each
		// carrying its own trailing caption instead of a single overall value/hint.
		this.$intervals.html(SETUP_STEPS.map((s, i) => `
			<li class="ts-interval" data-i="${i}">
				<span class="ts-interval-bar"></span>
				<span class="ts-interval-label">${frappe.utils.escape_html(s.label)}</span>
			</li>
		`).join(""));
	}

	_load_state() {
		this.$body.html(`<div class="ts-loading text-muted">${__("Loading…")}</div>`);
		frappe.call({
			method: "taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup.get_setup_state",
			callback: (r) => {
				this.state = r.message || {};
				this._render();
			},
		});
	}

	_go(i) {
		if (i < 0 || i >= SETUP_STEPS.length || i > this.reached) return;
		this.cur = i;
		this.reached = Math.max(this.reached, i);
		this._render();
	}

	_advance() {
		if (this.cur < SETUP_STEPS.length - 1) {
			this.reached = Math.max(this.reached, this.cur + 1);
			this._go(this.cur + 1);
		}
	}

	_on_next() {
		if (this.cur === SETUP_STEPS.length - 1) return this._finish();
		// Phase 2 will collect + save the current step here before advancing.
		this._advance();
	}

	_finish() {
		const $btn = this.$root.find(".ts-next").prop("disabled", true);
		frappe.call({
			method: "taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup.finish_setup",
			callback: () => {
				frappe.show_alert({ message: __("TaxJar setup complete."), indicator: "green" }, 5);
				frappe.set_route("Form", "TaxJar Settings");
			},
			always: () => $btn.prop("disabled", false),
		});
	}

	_render() {
		const step = SETUP_STEPS[this.cur];

		// Panel shows exactly one step's content, swapped in full on navigate.
		this.$root.find(".ts-kick").text(__("Step {0} of {1}", [this.cur + 1, SETUP_STEPS.length]));
		this.$root.find(".ts-title").text(step.title);
		this.$root.find(".ts-sub").text(step.sub);
		this.$intervals.attr("aria-valuenow", this.cur + 1);
		this.$intervals.find(".ts-interval").each((i, el) => {
			const $el = $(el);
			$el.toggleClass("filled", i <= this.cur).toggleClass("active", i === this.cur);
		});
		this.$root.find(".ts-back").toggleClass("hide", this.cur === 0);
		this.$root.find(".ts-skip").toggleClass("hide", !step.skip);
		this.$root.find(".ts-next").text(this.cur === SETUP_STEPS.length - 1 ? __("Finish & activate") : __("Save & continue"));

		// Outline: pure navigation — done (check), active (dot-dashed, current), upcoming (dashed).
		this.$milestones.find(".ts-milestone").each((i, el) => {
			const $el = $(el);
			const done = i < this.cur;
			const active = i === this.cur;
			$el.toggleClass("done", done).toggleClass("active", active).toggleClass("upcoming", !done && !active);
			$el.find(".ts-node-btn").prop("disabled", i > this.reached);
			$el.find(".ts-node").html(done ? TS_ICONS.check : active ? TS_ICONS.dot_dashed : TS_ICONS.dashed);
		});

		this.$body.empty();
		this[`_render_${step.key}`]();
	}

	// ── step bodies (single step rendered into ts-body at a time) ──
	_render_welcome() {
		this.$body.html(`
			<p class="ts-lede">${__("This takes about 5 minutes. Have these ready:")}</p>
			<ul class="ts-check">
				<li><b>${__("A TaxJar API token")}</b> — ${__("from TaxJar → Account → API access.")}</li>
				<li><b>${__("Which companies collect sales tax")}</b> — ${__("one token & account mapping per company.")}</li>
				<li><b>${__("Your tax & shipping GL accounts")}</b> — ${__("where calculated tax and freight are posted.")}</li>
			</ul>
			<p class="text-muted small">${__("Nothing is saved to your live settings until you finish.")}</p>
		`);
	}

	_render_connect() { this._placeholder(__("Connect API")); }
	_render_accounts() { this._placeholder(__("Company accounts")); }
	_render_features() { this._placeholder(__("Features")); }
	_render_nexus() { this._placeholder(__("Nexus")); }

	_render_review() {
		const s = this.state || {};
		const companies = s.companies || [];
		const rows = companies.map((c) => `
			<div class="ts-kv"><span>${frappe.utils.escape_html(c.company)}</span>
				<span>${c.calculate ? __("Calculate tax") : ""}${c.calculate && c.file ? " · " : ""}${c.file ? __("File") : ""}${!c.calculate && !c.file ? __("Off") : ""}</span></div>
		`).join("") || `<div class="text-muted small">${__("No companies configured yet.")}</div>`;

		this.$body.html(`
			<div class="ts-card">
				<div class="ts-kv"><span>${__("TaxJar")}</span><span>${s.taxjar_enabled ? __("Enabled") : __("Disabled")}</span></div>
				<div class="ts-kv"><span>${__("API mode")}</span><span>${frappe.utils.escape_html(s.api_mode || "—")}</span></div>
			</div>
			<div class="ts-card">${rows}</div>
			<p class="text-muted small">${__("Activating writes this configuration to TaxJar Settings.")}</p>
		`);
	}

	_placeholder(label) {
		this.$body.html(`<div class="ts-placeholder text-muted">${__("{0} — coming in the next phase.", [frappe.utils.escape_html(label)])}</div>`);
	}
}
