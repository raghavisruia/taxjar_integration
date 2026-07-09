// TaxJar guided setup — desk page shell.
//
// Layout: a single-step panel that swaps its entire content on Save & continue —
// only one step is ever shown at a time, rendered as plain page flow (no card/box
// around it) rather than a widget embedded in the desk. Each step's own header
// carries the progress rail directly below its heading — frappe-ui's
// <Progress :intervals="true"> internals, one segment per step, each with a
// clickable trailing caption that doubles as navigation.
//
// Every data field is a real Frappe control (frappe.ui.form.make_control) so
// Link search, get_query filtering and desk-consistent styling come for free —
// only the shell (rail, progress, cards, status chips) is custom CSS. Connect,
// Accounts and Features persist per step (Continue = collect -> save API ->
// reload state -> advance), so the guide is resumable; Nexus persists via its
// own Fetch action instead of a Continue save. See docs/guided-setup-plan.md.

const SETUP_MODULE = "taxjar_integration.taxjar_integration.page.taxjar_setup.taxjar_setup";

frappe.pages["taxjar-setup"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("TaxJar Setup"),
		single_column: true,
	});
	new TaxJarSetup(page);
};

const SETUP_STEPS = [
	{ key: "welcome", label: __("Welcome"), title: __("Let’s connect TaxJar"), skip: false },
	{ key: "connect", label: __("Connect API"), title: __("Connect your TaxJar account"), skip: false },
	{ key: "accounts", label: __("Company accounts"), title: __("Where should tax be posted?"), skip: false },
	{ key: "features", label: __("Features"), title: __("What should TaxJar do?"), skip: true },
	{ key: "nexus", label: __("Nexus"), title: __("Where do you collect tax?"), skip: true },
	{ key: "review", label: __("Review"), title: __("Review & activate"), skip: false },
];

class TaxJarSetup {
	constructor(page) {
		this.page = page;
		this.cur = 0;
		this.reached = 0;
		this.state = null;
		this.controls = {};
		this._build_shell();
		this._load_state();
	}

	_build_shell() {
		this.$root = $(`
			<div class="taxjar-setup">
				<section class="ts-panel">
					<header class="ts-head">
						<h2 class="ts-title"></h2>
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

		this.$intervals = this.$root.find(".ts-intervals");
		this.$body = this.$root.find(".ts-body");
		this.$root.find(".ts-back").on("click", () => this._go(this.cur - 1));
		this.$root.find(".ts-skip").on("click", () => this._advance());
		this.$root.find(".ts-next").on("click", () => this._on_next());

		// The progress rail is the only navigation element — frappe-ui's <Progress
		// intervals> segments (one per step), each with a clickable trailing caption
		// standing in for the step it represents.
		this.$intervals.html(SETUP_STEPS.map((s, i) => `
			<li class="ts-interval" data-i="${i}">
				<button class="ts-interval-btn">
					<span class="ts-interval-bar"></span>
					<span class="ts-interval-label">${frappe.utils.escape_html(s.label)}</span>
				</button>
			</li>
		`).join(""));
		this.$intervals.find(".ts-interval-btn").on("click", (e) => {
			this._go(+$(e.currentTarget).closest(".ts-interval").data("i"));
		});
	}

	// ── server calls ────────────────────────────────────────────────
	_call(method, args) {
		return frappe.xcall(`${SETUP_MODULE}.${method}`, args);
	}

	_reload_state() {
		return this._call("get_setup_state", {}).then((state) => { this.state = state; });
	}

	_load_state() {
		this.$body.html(`<div class="ts-loading text-muted">${__("Loading…")}</div>`);
		this._reload_state().then(() => this._render());
	}

	// ── navigation ───────────────────────────────────────────────────
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
		const step = SETUP_STEPS[this.cur];
		if (step.key === "review") return this._finish();

		// Steps with a save API collect -> save -> reload state -> advance;
		// steps without one (Welcome, Nexus — which persists via its own Fetch
		// action) just advance.
		const saver = this[`_save_${step.key}`];
		if (!saver) return this._advance();
		Promise.resolve(saver.call(this)).then((ok) => { if (ok) this._advance(); });
	}

	_finish() {
		const $btn = this.$root.find(".ts-next").prop("disabled", true);
		this._call("finish_setup", {})
			.then(() => {
				frappe.show_alert({ message: __("TaxJar setup complete."), indicator: "green" }, 5);
				frappe.set_route("Form", "TaxJar Settings");
			})
			.finally(() => $btn.prop("disabled", false));
	}

	// ── render ───────────────────────────────────────────────────────
	_render() {
		const step = SETUP_STEPS[this.cur];

		// Panel shows exactly one step's content, swapped in full on navigate.
		this.$root.find(".ts-title").text(step.title);
		this.$root.find(".ts-back").toggleClass("hide", this.cur === 0);
		this.$root.find(".ts-skip").toggleClass("hide", !step.skip);
		this.$root.find(".ts-next")
			.text(this.cur === SETUP_STEPS.length - 1 ? __("Finish & activate") : __("Save & continue"))
			.prop("disabled", false);

		// Rail: pure navigation — filled up to and including the current step,
		// each segment's caption clickable once reached.
		this.$intervals.attr("aria-valuenow", this.cur + 1);
		this.$intervals.find(".ts-interval").each((i, el) => {
			const $el = $(el);
			$el.toggleClass("filled", i <= this.cur).toggleClass("active", i === this.cur);
			$el.find(".ts-interval-btn").prop("disabled", i > this.reached);
		});

		this.$body.empty();
		this[`_render_${step.key}`]();
	}

	// ── Step 1: Welcome ──────────────────────────────────────────────
	_render_welcome() {
		this.$body.html(`
			<p class="ts-lede">${__("This takes about 5 minutes. Have these ready:")}</p>
			<ul class="ts-check">
				<li><b>${__("A TaxJar API token")}</b> — ${__("from TaxJar → Account → API access.")}</li>
				<li><b>${__("Which companies collect sales tax")}</b> — ${__("one token & account mapping per company.")}</li>
				<li><b>${__("Your tax & shipping GL accounts")}</b> — ${__("where calculated tax and freight are posted.")}</li>
			</ul>
		`);
	}

	// ── Step 2: Connect API ─────────────────────────────────────────
	_render_connect() {
		const s = this.state || {};
		this.$body.html(`
			<div class="ts-field ts-field-mode" style="max-width:280px"></div>
			<p class="ts-fieldnote">${__("Sandbox uses TaxJar’s test environment — safe for setup. Switch to Live when you’re ready to bill.")}</p>
			<p class="ts-sectionlabel">${__("API credentials — one token per company")}</p>
			<div class="ts-cardgrid ts-cred-cards"></div>
			<button class="btn btn-default btn-sm ts-add-cred">${__("+ Add another company")}</button>
		`);

		this.controls.mode = frappe.ui.form.make_control({
			parent: this.$body.find(".ts-field-mode"),
			df: { fieldtype: "Select", fieldname: "api_mode", label: __("API Mode"), options: ["Sandbox", "Live"], reqd: 1 },
			render_input: true,
		});
		this.controls.mode.set_value(s.api_mode || "Sandbox");
		this.controls.mode.$input.on("change", () => this._on_mode_change());

		this._connectCards = [];
		const creds = (s.credentials && s.credentials.length) ? s.credentials : [{ company: null, token_last4: null }];
		creds.forEach((cred) => this._add_credential_card(cred));

		this.$body.find(".ts-add-cred").on("click", () => this._add_credential_card({ company: null, token_last4: null }));

		this._sync_connect_gate();
	}

	_add_credential_card(cred) {
		// Header is just the company name + status — the Company field itself lives
		// in the card body, so a long status message never has to share a row with
		// a full-width form control (that's what was overflowing before).
		const $card = $(`
			<div class="ts-card">
				<div class="ts-card-h">
					<b class="ts-cred-name">${cred.company ? frappe.utils.escape_html(cred.company) : __("New company")}</b>
					<div class="ts-card-h-right">
						<span class="ts-chip idle">${__("Not tested")}</span>
						<button class="ts-card-remove" title="${__("Remove")}">&times;</button>
					</div>
				</div>
				<div class="ts-card-b">
					<div class="ts-field-company"></div>
					<div class="ts-field-token"></div>
					<button class="btn btn-default btn-sm ts-test">${__("Test connection")}</button>
				</div>
			</div>
		`).appendTo(this.$body.find(".ts-cred-cards"));

		const entry = { company: cred.company, tested: !!cred.token_last4, $card, controls: {} };
		this._connectCards.push(entry);

		const otherCompanies = () => this._connectCards
			.filter((c) => c !== entry)
			.map((c) => c.controls.company && c.controls.company.get_value())
			.filter(Boolean);

		const companyControl = frappe.ui.form.make_control({
			parent: $card.find(".ts-field-company"),
			df: {
				fieldtype: "Link", fieldname: "company", options: "Company", label: __("Company"), reqd: 1,
				get_query: () => ({ filters: { name: ["not in", otherCompanies()] } }),
			},
			render_input: true,
		});
		companyControl.df.onchange = () => {
			entry.company = companyControl.get_value();
			entry.tested = false;
			$card.find(".ts-cred-name").text(entry.company || __("New company"));
			this._reset_cred_status(entry);
			this._sync_connect_gate();
		};
		if (cred.company) {
			companyControl.set_value(cred.company);
			// Company is the key save_connection upserts on — locked once a token is
			// already stored for it, so Continue can't silently orphan that row.
			companyControl.df.read_only = 1;
			companyControl.refresh();
		}
		entry.controls.company = companyControl;

		const tokenControl = frappe.ui.form.make_control({
			parent: $card.find(".ts-field-token"),
			df: {
				fieldtype: "Password", fieldname: "token",
				label: this.controls.mode.get_value() === "Live" ? __("Live token") : __("Sandbox token"),
				reqd: !cred.token_last4,
				placeholder: cred.token_last4 ? __("•••••••••••• (ending in {0})", [cred.token_last4]) : "",
				description: cred.token_last4 ? __("Leave blank to keep the saved token.") : "",
			},
			render_input: true,
		});
		// A TaxJar token isn't a password being created — the strength meter (and
		// the request it fires on every keystroke) makes no sense here and was the
		// source of a 500 in this environment; this control never needed it.
		tokenControl.disable_password_checks();
		entry.controls.token = tokenControl;

		$card.find(".ts-test").on("click", () => this._test_connection(entry));
		$card.find(".ts-card-remove").on("click", () => this._remove_credential_card(entry, cred));
	}

	_remove_credential_card(entry, cred) {
		const drop = () => {
			this._connectCards = this._connectCards.filter((c) => c !== entry);
			entry.$card.remove();
			this._sync_connect_gate();
		};

		if (!cred.company) {
			// Never saved — nothing server-side to clean up.
			drop();
			return;
		}

		frappe.confirm(
			__("Remove {0} and its saved token? This also clears any accounts or features already configured for it.", [cred.company]),
			() => this._call("remove_company", { company: cred.company }).then(() => this._reload_state()).then(drop)
		);
	}

	_reset_cred_status(entry) {
		entry.$card.find(".ts-card-h .ts-chip").attr("class", "ts-chip idle").text(__("Not tested"));
	}

	_on_mode_change() {
		const live = this.controls.mode.get_value() === "Live";
		this._connectCards.forEach((entry) => {
			const tokenCtrl = entry.controls.token;
			// The last-4 hint / placeholder was computed for the previous mode's
			// token — sandbox and live tokens are different values, so we can't
			// carry it over without another round trip. Re-entry is required.
			tokenCtrl.df.label = live ? __("Live token") : __("Sandbox token");
			tokenCtrl.df.placeholder = "";
			tokenCtrl.df.description = "";
			tokenCtrl.df.reqd = 1;
			tokenCtrl.refresh();
			entry.tested = false;
			this._reset_cred_status(entry);
		});
		this._sync_connect_gate();
	}

	_test_connection(entry) {
		const company = entry.controls.company.get_value();
		if (!company) {
			frappe.show_alert({ message: __("Select a company first."), indicator: "orange" });
			return;
		}
		const $status = entry.$card.find(".ts-card-h .ts-chip");
		const $btn = entry.$card.find(".ts-test");
		$status.attr("class", "ts-chip warn").html(`<span class="ts-spin"></span> ${__("Contacting TaxJar…")}`);
		$btn.prop("disabled", true);

		this._call("test_connection", {
			company,
			token: entry.controls.token.get_value() || undefined,
			mode: this.controls.mode.get_value(),
		}).then((res) => {
			entry.tested = !!res.ok;
			if (res.ok) {
				$status.attr("class", "ts-chip ok").html(`<span class="ts-chip-dot"></span> ${__("Success")}`);
			} else {
				$status.attr("class", "ts-chip err").text(res.message || __("Could not connect."));
			}
			this._sync_connect_gate();
		}).catch(() => {
			entry.tested = false;
			$status.attr("class", "ts-chip err").text(__("Something went wrong."));
		}).finally(() => $btn.prop("disabled", false));
	}

	_sync_connect_gate() {
		const anyTested = this._connectCards.some((c) => c.tested && c.controls.company.get_value());
		this.$root.find(".ts-next").prop("disabled", !anyTested);
	}

	_save_connect() {
		const mode = this.controls.mode.get_value();
		const rows = this._connectCards
			.map((c) => ({ company: c.controls.company.get_value(), token: c.controls.token.get_value() }))
			.filter((r) => r.company);

		if (!rows.length) {
			frappe.show_alert({ message: __("Add at least one company."), indicator: "orange" });
			return false;
		}

		const $next = this.$root.find(".ts-next").prop("disabled", true);
		return this._call("save_connection", { mode, credentials: rows })
			.then(() => this._reload_state())
			.then(() => true)
			.catch(() => false)
			.finally(() => $next.prop("disabled", false));
	}

	// ── Step 3: Company accounts ────────────────────────────────────
	_render_accounts() {
		const s = this.state || {};
		const creds = s.credentials || [];

		if (!creds.length) {
			this.$body.html(`<div class="ts-placeholder text-muted">${__("Add a company on the Connect step first.")}</div>`);
			this._accountCards = [];
			return;
		}

		this.$body.html(`
			<p class="ts-fieldnote">${__("TaxJar posts calculated tax and shipping to these accounts on each Sales Invoice.")}</p>
			<div class="ts-cardgrid ts-account-cards"></div>
		`);

		const configByCompany = {};
		(s.companies || []).forEach((c) => { configByCompany[c.company] = c; });

		this._accountCards = [];
		creds.forEach((cred) => {
			const cfg = configByCompany[cred.company] || {};
			const $card = $(`
				<div class="ts-card">
					<div class="ts-card-h"><b>${frappe.utils.escape_html(cred.company)}</b></div>
					<div class="ts-card-b">
						<div class="ts-field-tax"></div>
						<div class="ts-field-ship"></div>
					</div>
				</div>
			`).appendTo(this.$body.find(".ts-account-cards"));

			const taxControl = frappe.ui.form.make_control({
				parent: $card.find(".ts-field-tax"),
				df: {
					fieldtype: "Link", fieldname: "tax_account_head", options: "Account",
					label: __("Tax account head"), reqd: 1,
					get_query: () => ({ filters: { company: cred.company, is_group: 0 } }),
				},
				render_input: true,
			});
			if (cfg.tax_account_head) taxControl.set_value(cfg.tax_account_head);

			const shipControl = frappe.ui.form.make_control({
				parent: $card.find(".ts-field-ship"),
				df: {
					fieldtype: "Link", fieldname: "shipping_account_head", options: "Account",
					label: __("Shipping account head"), reqd: 1,
					get_query: () => ({ filters: { company: cred.company, is_group: 0 } }),
				},
				render_input: true,
			});
			if (cfg.shipping_account_head) shipControl.set_value(cfg.shipping_account_head);

			this._accountCards.push({ company: cred.company, controls: { tax: taxControl, ship: shipControl } });
		});
	}

	_save_accounts() {
		const rows = (this._accountCards || []).map((c) => ({
			company: c.company,
			tax_account_head: c.controls.tax.get_value(),
			shipping_account_head: c.controls.ship.get_value(),
		}));

		if (!rows.length || rows.some((r) => !r.tax_account_head || !r.shipping_account_head)) {
			frappe.show_alert({ message: __("Fill in both accounts for every company."), indicator: "orange" });
			return false;
		}

		const $next = this.$root.find(".ts-next").prop("disabled", true);
		return this._call("save_company_accounts", { rows })
			.then(() => this._reload_state())
			.then(() => true)
			.catch(() => false)
			.finally(() => $next.prop("disabled", false));
	}

	// ── Step 4: Features ────────────────────────────────────────────
	_render_features() {
		const s = this.state || {};
		const companies = s.companies || [];

		this.$body.html(`
			<div class="ts-togrow ts-togrow-master">
				<div class="ts-field-master"></div>
				<div class="ts-togtext"><b>${__("Enable TaxJar")}</b>
					<p>${__("Master switch. Off means TaxJar does nothing for any company — the toggles below are disabled.")}</p></div>
			</div>
			<div class="ts-featcards">
				<p class="ts-sectionlabel">${__("Per company")}</p>
				<div class="ts-cardgrid ts-feature-cards"></div>
			</div>
		`);

		this.controls.master = frappe.ui.form.make_control({
			parent: this.$body.find(".ts-field-master"),
			df: { fieldtype: "Check", fieldname: "taxjar_enabled" },
			render_input: true,
		});
		this.controls.master.set_value(s.taxjar_enabled ? 1 : 0);
		this.controls.master.$input.on("change", () => this._sync_master_lock());

		this._featureCards = [];
		if (!companies.length) {
			this.$body.find(".ts-feature-cards").html(
				`<div class="ts-placeholder text-muted">${__("Add company accounts first.")}</div>`
			);
		} else {
			companies.forEach((c) => this._add_feature_card(c));
		}

		this._sync_master_lock();
	}

	_add_feature_card(c) {
		const $card = $(`
			<div class="ts-card">
				<div class="ts-card-h"><b>${frappe.utils.escape_html(c.company)}</b></div>
				<div class="ts-card-b ts-cotog">
					<div class="ts-togrow">
						<div class="ts-field-calc"></div>
						<div class="ts-togtext"><b>${__("Calculate sales tax")}</b><p>${__("Tax on invoices + Product Tax Category on Items.")}</p></div>
					</div>
					<div class="ts-togrow">
						<div class="ts-field-file"></div>
						<div class="ts-togtext"><b>${__("File transactions")}</b><p>${__("Push submitted invoices to TaxJar.")}</p></div>
					</div>
				</div>
			</div>
		`).appendTo(this.$body.find(".ts-feature-cards"));

		const calc = frappe.ui.form.make_control({
			parent: $card.find(".ts-field-calc"), df: { fieldtype: "Check", fieldname: "calculate" }, render_input: true,
		});
		calc.set_value(c.calculate ? 1 : 0);

		const file = frappe.ui.form.make_control({
			parent: $card.find(".ts-field-file"), df: { fieldtype: "Check", fieldname: "file" }, render_input: true,
		});
		file.set_value(c.file ? 1 : 0);

		this._featureCards.push({ company: c.company, controls: { calc, file } });
	}

	_sync_master_lock() {
		// Toggling df.read_only + refresh() here would be the natural approach, but
		// Frappe's refresh_input() unconditionally re-applies the control's stored
		// value on every refresh — for a Check control that means a checkbox the
		// user just ticked can get silently reset back to its pre-click state the
		// next time this runs. Flipping the native `disabled` attribute directly
		// changes interactivity without ever touching the control's value/DOM sync.
		const on = !!this.controls.master.get_value();
		this.$body.find(".ts-featcards").toggleClass("locked", !on);
		(this._featureCards || []).forEach((c) => {
			c.controls.calc.$input.prop("disabled", !on);
			c.controls.file.$input.prop("disabled", !on);
		});
	}

	_save_features() {
		const taxjar_enabled = this.controls.master.get_value() ? 1 : 0;
		const flags = (this._featureCards || []).map((c) => ({
			company: c.company,
			calculate: c.controls.calc.get_value() ? 1 : 0,
			file: c.controls.file.get_value() ? 1 : 0,
		}));

		const $next = this.$root.find(".ts-next").prop("disabled", true);
		return this._call("save_features", { taxjar_enabled, flags })
			.then(() => this._reload_state())
			.then(() => true)
			.catch(() => false)
			.finally(() => $next.prop("disabled", false));
	}

	// ── Step 5: Nexus ───────────────────────────────────────────────
	_render_nexus() {
		const s = this.state || {};
		const nexusByCompany = s.nexus_by_company || {};
		const hasNexus = Object.keys(nexusByCompany).length > 0;

		this.$body.html(`
			<div class="ts-nexusaction">
				<button class="btn btn-default ts-fetch">${__("Fetch from TaxJar")}</button>
				<span class="ts-chip idle ts-fetchstatus">${hasNexus ? __("Fetched previously") : __("Not fetched yet")}</span>
			</div>
			<div class="ts-nexusresult"></div>
			<div class="ts-banner">
				<b>${__("Read from TaxJar — never written back")}</b>
				<p>${__("Nexus is fetched per company from its TaxJar account; this list only mirrors it. It refreshes automatically every night at midnight, and you can pull the latest any time with Update Nexus List on TaxJar Settings.")}</p>
			</div>
		`);

		this._render_nexus_groups(nexusByCompany);
		this.$body.find(".ts-fetch").on("click", () => this._fetch_nexus());
	}

	_render_nexus_groups(nexusByCompany) {
		const $result = this.$body.find(".ts-nexusresult");
		const companies = Object.keys(nexusByCompany).filter((c) => nexusByCompany[c].length);
		if (!companies.length) { $result.empty(); return; }

		$result.html(companies.map((company) => {
			const regions = nexusByCompany[company];
			const pills = regions.map((r) => `
				<span class="ts-pill">${frappe.utils.escape_html(r.region || r.region_code)}
					<span class="ts-pillcode">${frappe.utils.escape_html(r.region_code)}</span></span>
			`).join("");
			return `
				<div class="ts-card">
					<div class="ts-card-h"><b>${frappe.utils.escape_html(company)}</b>
						<span class="ts-chip idle">${regions.length} ${__("regions")}</span></div>
					<div class="ts-card-b"><div class="ts-pills">${pills}</div></div>
				</div>
			`;
		}).join(""));
	}

	_fetch_nexus() {
		const $status = this.$body.find(".ts-fetchstatus");
		const $btn = this.$body.find(".ts-fetch").prop("disabled", true);
		$status.attr("class", "ts-chip warn ts-fetchstatus").html(`<span class="ts-spin"></span> ${__("Fetching from TaxJar…")}`);

		this._call("fetch_nexus", {}).then((res) => {
			this.state.nexus_by_company = res.nexus_by_company;
			const companiesN = Object.keys(res.nexus_by_company).length;
			const total = Object.values(res.nexus_by_company).reduce((n, arr) => n + arr.length, 0);
			$status.attr("class", "ts-chip ok ts-fetchstatus")
				.html(`<span class="ts-chip-dot"></span> ${__("Fetched {0} regions across {1} companies", [total, companiesN])}`);
			this._render_nexus_groups(res.nexus_by_company);
		}).catch(() => {
			$status.attr("class", "ts-chip err ts-fetchstatus").text(__("Could not fetch nexus."));
		}).finally(() => $btn.prop("disabled", false));
	}

	// ── Step 6: Review ──────────────────────────────────────────────
	_render_review() {
		const s = this.state || {};
		const companies = s.companies || [];
		const nexusByCompany = s.nexus_by_company || {};
		const totalNexus = Object.values(nexusByCompany).reduce((n, arr) => n + arr.length, 0);

		const accountRows = companies.map((c) => `
			<div class="ts-kv"><span>${frappe.utils.escape_html(c.company)}</span>
				<span>${frappe.utils.escape_html(c.tax_account_head || "—")} · ${frappe.utils.escape_html(c.shipping_account_head || "—")}</span></div>
		`).join("") || `<div class="text-muted small">${__("No accounts configured yet.")}</div>`;

		const featureRows = companies.map((c) => `
			<div class="ts-kv"><span>${frappe.utils.escape_html(c.company)}</span>
				<span>${c.calculate && c.file ? __("Calculate tax · File") : c.calculate ? __("Calculate tax") : c.file ? __("File") : __("Off")}</span></div>
		`).join("") || `<div class="text-muted small">${__("No companies configured yet.")}</div>`;

		this.$body.html(`
			<div class="ts-cardgrid">
				<div class="ts-card"><div class="ts-card-h"><b>${__("Connection")}</b></div>
					<div class="ts-card-b" style="gap:0">
						<div class="ts-kv"><span>${__("Mode")}</span><span>${frappe.utils.escape_html(s.api_mode || "—")}</span></div>
						<div class="ts-kv"><span>${__("TaxJar")}</span><span>${s.taxjar_enabled ? __("Enabled") : __("Disabled")}</span></div>
					</div></div>
				<div class="ts-card"><div class="ts-card-h"><b>${__("Nexus")}</b></div>
					<div class="ts-card-b" style="gap:0">
						<div class="ts-kv"><span>${__("Regions")}</span><span>${__("{0} across {1} companies", [totalNexus, Object.keys(nexusByCompany).length])}</span></div>
						<div class="ts-kv"><span>${__("Refresh")}</span><span>${__("Nightly")}</span></div>
					</div></div>
				<div class="ts-card"><div class="ts-card-h"><b>${__("Accounts")}</b></div>
					<div class="ts-card-b" style="gap:0">${accountRows}</div></div>
				<div class="ts-card"><div class="ts-card-h"><b>${__("Features")}</b></div>
					<div class="ts-card-b" style="gap:0">${featureRows}</div></div>
			</div>
		`);
	}
}
