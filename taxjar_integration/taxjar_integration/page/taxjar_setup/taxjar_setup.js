// TaxJar guided setup — desk page shell (Phase 1).
//
// Phase 1 delivers the wizard shell (left stepper, progress, step navigation) wired
// to real server state via get_setup_state, plus the Review step's finish action.
// The per-step native controls (Connect / Accounts / Features / Nexus) are wired in
// later phases — see docs/guided-setup-plan.md.

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
	  sub: __("A quick, guided setup — about 5 minutes. You can leave and resume any time."), pct: 8, skip: false },
	{ key: "connect", label: __("Connect API"), title: __("Connect your TaxJar account"),
	  sub: __("Choose a mode, enter each company’s token, and verify the connection."), pct: 33, skip: false },
	{ key: "accounts", label: __("Company accounts"), title: __("Where should tax be posted?"),
	  sub: __("Map the tax and shipping accounts TaxJar writes to on each Sales Invoice."), pct: 50, skip: false },
	{ key: "features", label: __("Features"), title: __("What should TaxJar do?"),
	  sub: __("A master switch, then Calculate Sales Tax and File Transactions per company."), pct: 66, skip: true },
	{ key: "nexus", label: __("Nexus"), title: __("Where do you collect tax?"),
	  sub: __("Nexus comes from TaxJar. Fetch it once — it stays current on its own."), pct: 83, skip: false },
	{ key: "review", label: __("Review"), title: __("Review & activate"),
	  sub: __("Everything you chose, grouped. Activating writes it to TaxJar Settings."), pct: 100, skip: false },
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
				<nav class="ts-rail">
					<div class="ts-steps"></div>
				</nav>
				<section class="ts-panel">
					<header class="ts-head">
						<div class="ts-kick"></div>
						<h2 class="ts-title"></h2>
						<p class="ts-sub"></p>
						<div class="ts-progress"><div class="ts-track"><i></i></div><span class="ts-pct"></span></div>
					</header>
					<div class="ts-body"></div>
					<footer class="ts-foot">
						<button class="btn btn-default ts-back">${__("Back")}</button>
						<span class="ts-grow"></span>
						<button class="btn btn-default ts-skip hide">${__("Skip for now")}</button>
						<button class="btn btn-primary ts-next"></button>
					</footer>
				</section>
			</div>
		`).appendTo(this.page.main);

		this.$steps = this.$root.find(".ts-steps");
		this.$body = this.$root.find(".ts-body");
		this.$root.find(".ts-back").on("click", () => this._go(this.cur - 1));
		this.$root.find(".ts-skip").on("click", () => this._advance());
		this.$root.find(".ts-next").on("click", () => this._on_next());

		this.$steps.html(SETUP_STEPS.map((s, i) =>
			`<button class="ts-step" data-i="${i}"><span class="ts-dot">${i + 1}</span> ${frappe.utils.escape_html(s.label)}</button>`
		).join(""));
		this.$steps.find(".ts-step").on("click", (e) => this._go(+e.currentTarget.dataset.i));
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
		this.$root.find(".ts-kick").text(__("Step {0} of {1}", [this.cur + 1, SETUP_STEPS.length]));
		this.$root.find(".ts-title").text(step.title);
		this.$root.find(".ts-sub").text(step.sub);
		this.$root.find(".ts-track i").css("width", step.pct + "%");
		this.$root.find(".ts-pct").text(step.pct + "%");
		this.$root.find(".ts-back").toggleClass("hide", this.cur === 0);
		this.$root.find(".ts-skip").toggleClass("hide", !step.skip);
		this.$root.find(".ts-next").text(this.cur === SETUP_STEPS.length - 1 ? __("Finish & activate") : __("Save & continue"));

		this.$steps.find(".ts-step").each((i, el) => {
			const $el = $(el);
			$el.toggleClass("active", i === this.cur).toggleClass("done", i < this.cur).prop("disabled", i > this.reached);
			$el.find(".ts-dot").html(i < this.cur ? "✓" : i + 1);
		});

		this[`_render_${step.key}`]();
	}

	// ── step bodies ──────────────────────────────────────────────
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
