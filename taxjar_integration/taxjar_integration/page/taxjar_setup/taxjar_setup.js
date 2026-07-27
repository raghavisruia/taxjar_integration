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
	// Nothing is actually saved on this step (no form fields), so its button
	// just says "Continue" rather than the misleading "Save & continue".
	{ key: "welcome", label: __("Pre-requisites"), title: __("Let’s connect TaxJar"), skip: false, nextLabel: __("Continue") },
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
		// API Credentials starts expanded - it's a required step, so hiding it
		// by default would just cost an extra click every single time.
		this._credsExpanded = true;
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

		// Delegated once here (rather than rebound per step render) so it
		// keeps working for any .ts-info-btn added later by any step, without
		// needing to re-wire it every time this.$body.html() replaces its contents.
		this.$root.on("click", ".ts-info-btn", (e) => {
			e.stopPropagation(); // don't also trigger whatever the button sits inside (a pill, a heading)
			this._toggle_info_popover($(e.currentTarget));
		});
	}

	// Click-to-show popover for the Retry pill's failure reason - no backing
	// form field to hang frappe's native InfoCard off of here (API Mode and
	// Enable API logs use the real thing instead - see their
	// df.show_description_on_click), so this one spot stays hand-rolled.
	_info_btn_html(text) {
		return `<button type="button" class="ts-info-btn" data-info="${frappe.utils.escape_html(text)}">${frappe.utils.icon("triangle-alert", "md")}</button>`;
	}

	_toggle_info_popover($trigger) {
		const reopening = $trigger.hasClass("ts-info-btn-active");
		$(".ts-info-pop").remove();
		this.$root.find(".ts-info-btn").removeClass("ts-info-btn-active");
		$(document).off("click.tsInfoPop");
		if (reopening) return;

		$trigger.addClass("ts-info-btn-active");
		const $pop = $(`<div class="ts-info-pop">${frappe.utils.escape_html($trigger.attr("data-info") || "")}</div>`).appendTo("body");
		// position: fixed + getBoundingClientRect() are both viewport-relative,
		// so no scroll-offset math is needed here.
		const rect = $trigger[0].getBoundingClientRect();
		$pop.css({ top: rect.bottom + 6, left: rect.left });
		$(document).on("click.tsInfoPop", () => {
			$pop.remove();
			$trigger.removeClass("ts-info-btn-active");
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
		// Gated (e.g. Connect before anything's tested) stays a real, clickable
		// button rather than a native disabled one — a disabled button eats the
		// click silently, which just looks broken. Explain what's missing instead.
		if (this._nextGated) {
			frappe.show_alert({ message: this._nextGateMessage || __("Please complete this step first."), indicator: "orange" });
			return;
		}

		const step = SETUP_STEPS[this.cur];
		if (step.key === "review") return this._finish();

		// Steps with a save API collect -> save -> reload state -> advance;
		// steps without one (Welcome, Nexus — which persists via its own Fetch
		// action) just advance.
		const saver = this[`_save_${step.key}`];
		if (!saver) return this._advance();
		Promise.resolve(saver.call(this)).then((ok) => { if (ok) this._advance(); });
	}

	// Visually disabled but still clickable, so a click can explain why instead
	// of a native `disabled` button silently eating it.
	_set_next_gated(blocked, message) {
		this._nextGated = blocked;
		this._nextGateMessage = message;
		this.$root.find(".ts-next").toggleClass("ts-next-gated", blocked);
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
		const nextLabel = this.cur === SETUP_STEPS.length - 1
			? __("Finish & activate")
			: (step.nextLabel || __("Save & continue"));
		this.$root.find(".ts-next").text(nextLabel).prop("disabled", false);
		this._set_next_gated(false);

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
		const icon = frappe.utils.icon("external-link", "xs");
		const chartOfAccountsUrl = `${frappe.urllib.get_base_url()}/app/account/view/tree`;

		this.$body.html(`
			<p class="ts-lede">${__("Before proceeding,")}</p>
			<ul class="ts-check">
				<li>
					<a class="ts-check-link" href="https://app.taxjar.com/api_sign_up" target="_blank" rel="noopener noreferrer">
						<span class="ts-check-num">1</span>
						<span class="ts-check-text"><b>${__("Sign Up for TaxJar")}</b>${icon}</span>
					</a>
				</li>
				<li>
					<a class="ts-check-link" href="https://app.taxjar.com/account#api-access" target="_blank" rel="noopener noreferrer">
						<span class="ts-check-num">2</span>
						<span class="ts-check-text"><b>${__("Get API Token from TaxJar")}</b>${icon}</span>
					</a>
				</li>
				<li>
					<a class="ts-check-link" href="https://app.taxjar.com/account#states" target="_blank" rel="noopener noreferrer">
						<span class="ts-check-num">3</span>
						<span class="ts-check-text"><b>${__("Configure Nexus in TaxJar")}</b>${icon}</span>
					</a>
				</li>
				<li>
					<a class="ts-check-link" href="${chartOfAccountsUrl}" target="_blank" rel="noopener noreferrer">
						<span class="ts-check-num">4</span>
						<span class="ts-check-text">
							<b>${__("Review Ledger Accounts")}</b>${icon}
							<ul class="ts-check-sub">
								<li>${__("Sales Tax Payable")}</li>
								<li>${__("Shipping and Freight Income")}</li>
							</ul>
						</span>
					</a>
				</li>
			</ul>
		`);
	}

	// Two-button pill toggle (Sandbox | Live), a stand-in for a real frappe
	// control - exposes only the get_value/set_value subset the rest of this
	// file actually calls on this.controls.mode, and calls _on_mode_change()
	// itself on a real click rather than needing a $input "change" event.
	_render_mode_toggle($parent, initial) {
		const $wrap = $(`
			<div class="ts-segmented">
				<button type="button" class="ts-seg-btn" data-value="Sandbox">${__("Sandbox")}</button>
				<button type="button" class="ts-seg-btn" data-value="Live">${__("Live")}</button>
			</div>
		`).appendTo($parent);

		let value = initial;
		const setActive = () => {
			$wrap.find(".ts-seg-btn").each((_, el) => {
				const isActive = $(el).data("value") === value;
				$(el).toggleClass("ts-seg-active", isActive).attr("aria-pressed", isActive);
			});
		};
		setActive();

		$wrap.find(".ts-seg-btn").on("click", (e) => {
			const next = $(e.currentTarget).data("value");
			if (next === value) return;
			value = next;
			setActive();
			this._on_mode_change();
		});

		return {
			get_value: () => value,
			set_value: (v) => { value = v; setActive(); },
		};
	}

	// ── Step 2: Connect API ─────────────────────────────────────────
	_render_connect() {
		const s = this.state || {};
		const creds = (s.credentials && s.credentials.length) ? s.credentials : [{ company: null, token_last4: null }];

		this.$body.html(`
			<div class="ts-card">
				<div class="ts-card-b ts-mode-row">
					<div>
						<label class="control-label" style="margin:0">${__("API Mode")} <span class="ts-reqd">*</span></label>
						<p class="ts-fieldnote" style="margin:2px 0 0">${__("Live requests affect real filings.")}</p>
					</div>
					<div class="ts-field-mode"></div>
				</div>
			</div>

			<div class="ts-card" style="margin-top:20px">
				<div class="ts-card-h ts-cred-heading">
					<span class="ts-acc-chevron">${frappe.utils.icon("chevron-right", "sm")}</span>
					<b>${__("API Credentials")}</b>
					<span class="ts-grow"></span>
					<button class="btn btn-default btn-sm ts-add-cred">${__("+ Add another company")}</button>
				</div>
				<div class="ts-card-b ts-cred-rows"></div>
			</div>

			<div class="ts-card ts-logtoggle" style="margin-top:20px">
				<div class="ts-card-b">
					<div class="ts-field-logging"></div>
				</div>
				<div class="ts-card-b ts-retention-row">
					<div>
						<label class="control-label" style="margin:0">${__("Retention")}</label>
						<p class="ts-fieldnote" style="margin:2px 0 0">${__("Older logs are deleted automatically.")}</p>
					</div>
					<div class="ts-retention-wrap">
						<div class="ts-field-retention"></div>
						<span class="ts-retention-unit"></span>
					</div>
				</div>
			</div>
		`);

		// A two-option toggle, not a native Select - Sandbox/Live is a binary
		// choice worth showing both sides of at once rather than hiding one
		// behind a closed dropdown. Not a real frappe control, so it can't
		// get InfoCard for free the way Select did; the label+description
		// are hand-authored instead (same as Enable API logs' own reverted
		// plain-description treatment above).
		this.controls.mode = this._render_mode_toggle(this.$body.find(".ts-field-mode"), s.api_mode || "Live");

		// this.controls.mode.get_value() is safe to read synchronously here
		// (unlike a real frappe control's set_value(), the toggle above has no
		// frappe.run_serially step to resolve), but _modeIsLive stays its own
		// tracked flag regardless, since credential cards not yet built at
		// this point (see _add_credential_card below) need to read it too;
		// _on_mode_change() keeps it in sync from here on.
		this._modeIsLive = (s.api_mode || "Live") === "Live";

		this._connectCards = [];
		creds.forEach((cred) => this._add_credential_card(cred));
		// Reapplies whatever this instance's expand state already was (e.g. the
		// user collapsed it, then went Back and returned) rather than always
		// resetting to expanded on every re-render of this step.
		this._set_creds_expanded(this._credsExpanded);

		this.$body.find(".ts-cred-heading").on("click", () => this._set_creds_expanded(!this._credsExpanded));
		this.$body.find(".ts-add-cred").on("click", (e) => {
			e.stopPropagation(); // don't also toggle the heading's own collapse
			this._add_credential_card({ company: null, token_last4: null });
			this._set_creds_expanded(true);
		});

		// fieldtype "Switch" (frappe.ui.form.ControlSwitch, controls/switch.js)
		// is a real pill toggle already shipped and styled in frappe core
		// (frappe/public/scss/common/controls.scss's .switch-control/
		// .switch-visual/.switch-thumb, already part of the desk CSS bundle)
		// - reachable from a plain script exactly like Check is, no Vue/
		// frappe-ui package involved. Replaces the hand-rolled CSS checkbox
		// (appearance:none + ::before track/thumb), which rendered as a
		// broken grey ring instead of a clean switch in practice. Its own
		// native label+description replace the hand-authored .ts-togtext.
		this.controls.enableLogging = frappe.ui.form.make_control({
			parent: this.$body.find(".ts-field-logging"),
			df: {
				fieldtype: "Switch", fieldname: "enable_taxjar_logging",
				label: __("Enable API logs"),
				description: __("Records API requests, responses, and errors in TaxJar API Log."),
			},
			render_input: true,
		});
		this.controls.enableLogging.set_value(s.enable_taxjar_logging ? 1 : 0);

		// only_input: the unit text to the right already says what this
		// number means, so a separate field label would be redundant.
		this.controls.logRetention = frappe.ui.form.make_control({
			parent: this.$body.find(".ts-field-retention"),
			df: { fieldtype: "Int", fieldname: "log_retention_days" },
			only_input: true,
			render_input: true,
		});
		this.controls.logRetention.set_value(s.log_retention_days != null ? s.log_retention_days : 15);

		const $retentionUnit = this.$body.find(".ts-retention-unit");
		const syncRetentionUnit = () => {
			$retentionUnit.text(cint(this.controls.logRetention.get_value()) === 1 ? __("day") : __("days"));
		};
		// set_value() above resolves through frappe.run_serially, so reading
		// get_value() back synchronously right here would still see the
		// pre-set value on first render (same class of bug as _modeIsLive) -
		// derive the initial unit word from the already-known state/default
		// instead, and only trust get_value() from here on for the change event.
		$retentionUnit.text(
			(s.log_retention_days != null ? s.log_retention_days : 15) === 1 ? __("day") : __("days")
		);
		this.controls.logRetention.$input.on("input", syncRetentionUnit);

		// Log Retention only means anything once logging is on — mirrors the
		// doctype field's own depends_on: eval: doc.enable_taxjar_logging.
		// Hides the whole row (label + description + input), not just the
		// input - "Retention / Older logs are deleted automatically" with no
		// way to see or edit the day count would read as broken, not off.
		const $retentionField = this.$body.find(".ts-retention-row");
		const syncRetentionVisibility = () => {
			$retentionField.toggle(!!this.controls.enableLogging.get_value());
		};
		// Same asynchronous-set_value gotcha as above - use the known state
		// value for the initial visibility check, not a synchronous read.
		$retentionField.toggle(!!s.enable_taxjar_logging);
		this.controls.enableLogging.$input.on("change", syncRetentionVisibility);

		this._sync_connect_gate();
	}

	_set_creds_expanded(expanded) {
		this._credsExpanded = expanded;
		this.$body.find(".ts-cred-rows").css("display", expanded ? "flex" : "none");
		this.$body.find(".ts-cred-heading .ts-acc-chevron").toggleClass("ts-acc-chevron-open", expanded);
	}

	_add_credential_card(cred) {
		// No per-row header anymore - the Company field itself is always
		// visible in the row, so nothing else needs to identify which
		// company a row is for. Rows are separated with a divider instead
		// (see .ts-cred-row + .ts-cred-row in the CSS).
		// A company that already has a saved token starts already "tested" -
		// re-running the guided setup shouldn't visually demand a re-test of a
		// connection that was already verified and hasn't changed.
		const alreadySaved = !!cred.token_last4;
		const $card = $(`
			<div class="ts-cred-row">
				<div class="ts-field-company"></div>
				<div class="ts-field-token"></div>
				<div class="ts-cred-action"></div>
				<button class="ts-card-remove" title="${__("Remove")}">&times;</button>
			</div>
		`).appendTo(this.$body.find(".ts-cred-rows"));

		const entry = { company: cred.company, tested: alreadySaved, lastError: null, $card, controls: {} };
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
		// set_value() below (restoring an existing credential's company) fires
		// df.onchange itself as part of setting the value - not just real user
		// input - so without this guard, populating an already-saved card
		// immediately re-fired onchange and reset entry.tested straight back to
		// false right after alreadySaved had just set it true, wiping the
		// "Success" pill the moment the card rendered.
		let restoringInitialCompany = !!cred.company;
		companyControl.df.onchange = () => {
			if (restoringInitialCompany) {
				restoringInitialCompany = false;
				return;
			}
			entry.company = companyControl.get_value();
			entry.tested = false;
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

		// ControlLink builds its own <input> and (unlike ControlData) never sets
		// autocomplete="off" on it - harmless on its own, but this field sits
		// right above the Password field below, which is exactly the "text input
		// immediately before a password input" shape Chrome's login-manager
		// heuristic looks for. Left alone, Chrome offers to autofill the site's
		// saved login here, dropping "Administrator" into Company and the saved
		// password into the token field, which then fails Link validation.
		companyControl.$input.attr("autocomplete", "off");

		const tokenControl = frappe.ui.form.make_control({
			parent: $card.find(".ts-field-token"),
			df: {
				fieldtype: "Password", fieldname: "token",
				label: this._modeIsLive ? __("Live token") : __("Sandbox token"),
				reqd: !cred.token_last4,
				placeholder: cred.token_last4 ? __("•••••••••••• (ending in {0})", [cred.token_last4]) : "",
			},
			render_input: true,
		});
		// Chrome tends to ignore autocomplete="off" (set by ControlData) on
		// password inputs specifically, but does respect "new-password" - the
		// standard way to tell it this isn't a login field to offer saved
		// credentials for.
		tokenControl.$input.attr("autocomplete", "new-password");
		// A TaxJar token isn't a password being created — the strength meter (and
		// the request it fires on every keystroke) makes no sense here and was the
		// source of a 500 in this environment; this control never needed it.
		tokenControl.disable_password_checks();
		entry.controls.token = tokenControl;

		// A saved connection starts "tested" (see alreadySaved above), but that only
		// holds while the stored token is still what's in effect. The moment the
		// user actually types into this field, the value in play changes and the
		// previous verification no longer applies — require a fresh Connect
		// before Continue is ungated again.
		tokenControl.$input.on("input", () => {
			if (!entry.tested) return;
			entry.tested = false;
			this._reset_cred_status(entry);
			this._sync_connect_gate();
		});

		this._render_cred_action(entry);
		$card.find(".ts-card-remove").on("click", () => this._remove_credential_card(entry, cred));
	}

	// The action slot cycles through three states: an idle "Connect" button,
	// a transient "testing…" state (see _test_connection), and a result pill
	// (green Success / yellow Retry) that replaces the button once tested —
	// clicking the pill re-tests. Centralised here since every entry point
	// that can invalidate a previous test (edit company, edit token, switch
	// mode) needs to fall back to the same idle button.
	_render_cred_action(entry) {
		const $action = entry.$card.find(".ts-cred-action");
		if (entry.tested) {
			$action.html(`<span class="ts-chip ok ts-cred-pill"><span class="ts-chip-dot"></span> ${__("Success")}</span>`);
		} else if (entry.lastError) {
			// The warning icon sits outside the pill (not nested inside it, and
			// not routed through the pill's own click handler at all - they're
			// siblings, so clicking the icon can never also trigger a retry).
			// No native InfoCard here: it's not a real form field, so there's
			// no df/label for frappe's own control code to hang one off of -
			// this is the one spot still using the hand-rolled popover.
			$action.html(`
				${this._info_btn_html(entry.lastError)}
				<span class="ts-chip retry ts-cred-pill">${__("Retry")}</span>
			`);
		} else {
			$action.html(`<button type="button" class="btn btn-default ts-test">${__("Connect")}</button>`);
		}
		$action.find(".ts-test, .ts-cred-pill").on("click", () => this._test_connection(entry));
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
		entry.lastError = null;
		this._render_cred_action(entry);
	}

	_on_mode_change() {
		// Real user-driven change event — the control's value is accurate to read
		// synchronously here, unlike the set_value() call in _render_connect().
		const live = this.controls.mode.get_value() === "Live";
		this._modeIsLive = live;
		this._connectCards.forEach((entry) => {
			const tokenCtrl = entry.controls.token;
			// The last-4 hint / placeholder was computed for the previous mode's
			// token — sandbox and live tokens are different values, so we can't
			// carry it over without another round trip. Re-entry is required.
			tokenCtrl.df.label = live ? __("Live token") : __("Sandbox token");
			tokenCtrl.df.placeholder = "";
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
		// Transient state, not routed through _render_cred_action - nothing
		// about entry.tested/lastError has changed yet, this is just what the
		// action slot looks like while the request is in flight.
		entry.$card.find(".ts-cred-action").html(
			`<span class="ts-chip warn"><span class="ts-spin"></span> ${__("Contacting TaxJar…")}</span>`
		);

		this._call("test_connection", {
			company,
			token: entry.controls.token.get_value() || undefined,
			mode: this.controls.mode.get_value(),
		}).then((res) => {
			entry.tested = !!res.ok;
			entry.lastError = res.ok ? null : (res.message || __("Could not connect."));
			this._render_cred_action(entry);
			this._sync_connect_gate();
		}).catch(() => {
			entry.tested = false;
			entry.lastError = __("Something went wrong.");
			this._render_cred_action(entry);
		});
	}

	_sync_connect_gate() {
		// Reads c.company (kept in sync directly on the entry), deliberately
		// not the Company control's own get_value(). For a restored card, that
		// control is read-only and its value was just populated via
		// set_value(), which - like the mode label and retention-visibility
		// bugs - resolves asynchronously; reading it back from the control
		// synchronously right here (this runs immediately after every card is
		// added, on every render) could still see the pre-set value and
		// wrongly gate Continue on an already-saved, already-tested credential.
		//
		// Every company must test successfully, not just one - an untested or
		// failed credential left in the list here was reaching later steps
		// (Nexus fetch pulls nexus for every company in one request and used
		// to hard-crash with a raw 401 traceback the moment any one of them
		// had a bad token) with no way back to fix it. Naming the specific
		// company gives the user two concrete ways out: fix its token and
		// re-test, or remove it.
		const withCompany = this._connectCards.filter((c) => c.company);
		if (!withCompany.length) {
			this._set_next_gated(true, __("Add at least one company before continuing."));
			return;
		}
		const untested = withCompany.find((c) => !c.tested);
		this._set_next_gated(
			!!untested,
			untested ? __("Test the connection for {0} (or remove it) before continuing.", [untested.company]) : ""
		);
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
		return this._call("save_connection", {
			mode,
			credentials: rows,
			enable_taxjar_logging: this.controls.enableLogging.get_value() ? 1 : 0,
			log_retention_days: this.controls.logRetention.get_value(),
		})
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

			// Pre-fill whichever ledger is still blank from the standard US chart of
			// accounts (Sales Tax Payable / Shipping and Freight Income), so the admin
			// sees accounts already filled in and can still override before saving.
			if (!cfg.tax_account_head || !cfg.shipping_account_head) {
				this._call("get_default_ledgers", { company: cred.company }).then((defaults) => {
					defaults = defaults || {};
					if (!cfg.tax_account_head && defaults.tax_account_head) {
						taxControl.set_value(defaults.tax_account_head);
					}
					if (!cfg.shipping_account_head && defaults.shipping_account_head) {
						shipControl.set_value(defaults.shipping_account_head);
					}
				});
			}

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
	// No master switch here — taxjar_enabled is managed on the TaxJar Settings
	// doctype directly, not by this wizard. This step only ever touches the
	// per-company Calculate/File flags.
	_render_features() {
		const s = this.state || {};
		const companies = s.companies || [];

		this.$body.html(`
			<label class="control-label">${__("Per company")}</label>
			<div class="ts-cardgrid ts-feature-cards"></div>
		`);

		this._featureCards = [];
		if (!companies.length) {
			this.$body.find(".ts-feature-cards").html(
				`<div class="ts-placeholder text-muted">${__("Add company accounts first.")}</div>`
			);
		} else {
			companies.forEach((c) => this._add_feature_card(c));
		}
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

	_save_features() {
		// NOT sent as "flags" - frappe.call()'s get_newargs() unconditionally
		// strips any kwarg literally named "flags" from every whitelisted API
		// call (a security measure, unrelated to this doctype), so the server
		// param is company_flags instead. See save_features()'s docstring.
		const company_flags = (this._featureCards || []).map((c) => ({
			company: c.company,
			calculate: c.controls.calc.get_value() ? 1 : 0,
			file: c.controls.file.get_value() ? 1 : 0,
		}));

		const $next = this.$root.find(".ts-next").prop("disabled", true);
		return this._call("save_features", { company_flags })
			.then(() => this._reload_state())
			.then(() => true)
			.catch(() => false)
			.finally(() => $next.prop("disabled", false));
	}

	// ── Step 5: Nexus ───────────────────────────────────────────────
	_render_nexus() {
		const s = this.state || {};
		const nexusByCompany = s.nexus_by_company || {};

		this.$body.html(`
			<div class="ts-nexusaction">
				<button class="btn btn-default ts-fetch">${__("Fetch from TaxJar")}</button>
				<span class="ts-chip idle ts-fetchstatus">${__("Not fetched yet")}</span>
			</div>
			<div class="ts-nexusresult"></div>
			<div class="alert alert-warning ts-nexusnote" role="alert">
				${frappe.utils.icon("info", "sm")}
				<span>${__("Manage nexus in TaxJar — fetch on demand here for changes made there, or let it update automatically every night at midnight.")}</span>
			</div>
		`);

		this._render_nexus_groups(nexusByCompany);
		this.$body.find(".ts-fetch").on("click", () => this._fetch_nexus());

		// Opening this step always pulls the latest — no need to remember to
		// click Fetch just to see current nexus. The status chip above is
		// overwritten immediately by _fetch_nexus()'s own in-progress state.
		this._fetch_nexus();
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
			const regionWord = total === 1 ? __("region") : __("regions");
			const companyWord = companiesN === 1 ? __("company") : __("companies");
			$status.attr("class", "ts-chip ok ts-fetchstatus")
				.html(`<span class="ts-chip-dot"></span> ${__("Fetched {0} {1} across {2} {3}", [total, regionWord, companiesN, companyWord])}`);
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
		const nexusCompaniesN = Object.keys(nexusByCompany).length;

		// Company name and its accounts stack on their own lines — a company
		// name and "Tax head · Shipping head" side by side wraps unevenly and
		// never lines up cleanly in a two-column row. Tax Ledger / Shipping
		// Ledger get their own line each too, rather than being crammed
		// together on one line with no indication of which was which.
		const accountRows = companies.map((c) => `
			<div class="ts-accrow">
				<div class="ts-acc-company">${frappe.utils.escape_html(c.company)}</div>
				<div class="ts-acc-detail">${__("Tax Ledger")}: ${frappe.utils.escape_html(c.tax_account_head || "—")}</div>
				<div class="ts-acc-detail">${__("Shipping Ledger")}: ${frappe.utils.escape_html(c.shipping_account_head || "—")}</div>
			</div>
		`).join("") || `<div class="text-muted small">${__("No accounts configured yet.")}</div>`;

		const featureRows = companies.map((c) => `
			<div class="ts-kv"><span>${frappe.utils.escape_html(c.company)}</span>
				<span>${c.calculate && c.file ? __("Calculate tax · File") : c.calculate ? __("Calculate tax") : c.file ? __("File") : __("Off")}</span></div>
		`).join("") || `<div class="text-muted small">${__("No companies configured yet.")}</div>`;

		const mode = s.api_mode || "—";
		const modeDisplay = mode === "Live"
			? `<span class="indicator-pill green no-indicator-dot">${__("Live")}</span>`
			: frappe.utils.escape_html(mode);

		const retentionDays = s.log_retention_days;
		const logsDisplay = s.enable_taxjar_logging
			? `<span style="display:inline-flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
					<span class="indicator-pill green no-indicator-dot">${__("Enabled")}</span>
					<span class="indicator-pill green no-indicator-dot">${__("{0} {1} retention", [retentionDays, retentionDays === 1 ? __("day") : __("days")])}</span>
				</span>`
			: __("Off");

		this.$body.html(`
			<div class="ts-cardgrid">
				<div class="ts-card"><div class="ts-card-h"><b>${__("Connection")}</b></div>
					<div class="ts-card-b" style="gap:0">
						<div class="ts-kv"><span>${__("Mode")}</span><span>${modeDisplay}</span></div>
						<div class="ts-kv"><span>${__("API Logs")}</span><span>${logsDisplay}</span></div>
					</div></div>
				<div class="ts-card"><div class="ts-card-h"><b>${__("Nexus")}</b></div>
					<div class="ts-card-b" style="gap:0">
						<div class="ts-kv"><span>${__("Regions")}</span><span>${__("{0} across {1} {2}", [totalNexus, nexusCompaniesN, nexusCompaniesN === 1 ? __("company") : __("companies")])}</span></div>
						<div class="ts-kv"><span>${__("Auto-Refresh")}</span><span><span class="indicator-pill green no-indicator-dot">${__("Daily at midnight")}</span></span></div>
					</div></div>
				<div class="ts-card"><div class="ts-card-h"><b>${__("Accounts")}</b></div>
					<div class="ts-card-b" style="gap:0">${accountRows}</div></div>
				<div class="ts-card"><div class="ts-card-h"><b>${__("Features")}</b></div>
					<div class="ts-card-b" style="gap:0">${featureRows}</div></div>
			</div>
		`);
	}
}
