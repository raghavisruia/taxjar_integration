# TaxJar Integration

TaxJar Integration for ERPNext (Frappe/ERPNext v16).

This app calculates US sales tax using TaxJar, writes tax values back to ERPNext documents, and can create TaxJar order/refund transactions on submission/cancellation.

## Features

- Tax calculation hook on Quotation, Sales Order, and Sales Invoice validation.
- Optional TaxJar transaction creation on Sales Invoice submit.
- Optional TaxJar transaction deletion on Sales Invoice cancel.
- Product Tax Category support for item-level TaxJar product tax codes.
- Sandbox mode support.
- UI-visible API diagnostics via TaxJar API Log DocType.

## Version Compatibility

- Tested for Frappe/ERPNext v16.

## Installation

From your bench folder:

```bash
bench get-app taxjar_integration
bench --site <site-name> install-app taxjar_integration
bench --site <site-name> migrate
```

## Basic Configuration

Open TaxJar Settings and configure:

- Enable Tax Calculation
- Sandbox Mode (optional, for testing)
- Live API Key or Sandbox API Key
- Company
- Tax Account Head
- Shipping Account Head
- Create TaxJar Transaction (optional)

Notes:

- Tax Account Head should match the account where TaxJar tax is expected on invoices.
- If a static Sales Taxes and Charges template is auto-applied, it can override expected TaxJar behavior.

## How Tax Is Applied

- Hook entrypoint: validate event for Quotation, Sales Order, Sales Invoice.
- TaxJar payload uses company and customer shipping addresses plus item product tax codes.
- Returned tax is added/updated as an Actual tax row using Tax Account Head.
- Item fields tax_collectable and taxable_amount are populated from TaxJar breakdown where applicable.

## TaxJar API Log (UI)

Use TaxJar API Log in Desk to inspect calls and outcomes.

Each row captures:

- action: tax_for_order, create_order, create_refund, delete_order, etc.
- status: request, success, error, skipped
- reference_doctype/reference_name (when available)
- payload
- response
- error or skipped reason

Typical skipped reasons include:

- taxjar_calculate_tax is disabled
- company is not in a supported context for the request
- no nexus match for destination
- TaxJar client credentials not configured
- no matching sales tax amount for transaction creation

## Demo Script: Create a TaxJar-Friendly Sales Invoice

This repository includes a helper script that creates/submits a Sales Invoice intended to trigger TaxJar tax application in v16.

Module path:

- taxjar_integration.taxjar_integration.demo_invoice.create_taxjar_demo_sales_invoice

Run:

```bash
bench --site <site-name> execute taxjar_integration.taxjar_integration.demo_invoice.create_taxjar_demo_sales_invoice
```

Optional kwargs:

```bash
bench --site <site-name> execute taxjar_integration.taxjar_integration.demo_invoice.create_taxjar_demo_sales_invoice --kwargs "{'company':'Your Company','submit':1,'rate':800}"
```

The function returns a JSON summary with:

- created invoice id
- tax rows
- precondition changes it applied
- recent TaxJar logs for that invoice

## Verification Checklist

After creating a test invoice:

- Confirm taxes_and_charges is empty or not forcing a static template.
- Confirm invoice tax row is Actual and aligns with Tax Account Head.
- Confirm item has product_tax_category.
- Confirm TaxJar API Log shows request/success (or skipped/error with reason).

## Troubleshooting

If tax looks template-driven instead of TaxJar-driven:

1. Remove/default-disable static Sales Taxes and Charges template for the test case.
2. Ensure TaxJar Settings tax_account_head aligns with invoice tax account behavior.
3. Confirm destination address and nexus setup.
4. Confirm product tax category is present on item.
5. Check TaxJar API Log for request/response and skipped/error reason.

If code changes are not reflected:

```bash
bench --site <site-name> migrate
bench restart
```

## Security and Open Source Notes

- Do not commit live API keys, sandbox keys, or customer-sensitive payloads.
- Use sandbox credentials for demos and CI/testing.
- When sharing logs/issues publicly, redact secrets and personal data.

## Contributing

Contributions are welcome.

Recommended workflow:

1. Fork the repository.
2. Create a feature branch.
3. Add or update tests where possible.
4. Run bench migrate and validate on a v16 site.
5. Open a pull request with clear reproduction steps and expected behavior.

## License

MIT