On a fresh database, the bridge installs automatically when both UAE PINT and native
Sales are installed. An accounting installation without Sales keeps the bridge absent;
this module does not make Sales mandatory for UAE accounting.

For an existing database, refresh the Apps list and explicitly install
`l10n_ae_edi_sale` if it is absent. Updating already installed UAE modules alone does
not guarantee activation of a newly discovered bridge. Before preparing final Sales
invoices that deduct taxed advances, confirm that **UAE Electronic Invoicing Sales** is
installed.

Configure the native advance liability account and review the company binding's billing
invoice/credit capabilities. The bridge preserves native Sales and Accounting
calculations and captures the actual taxed advance references during new preparation.
Existing frozen fiscal evidence is not rewritten by installing the bridge. Review any
historical final invoice with missing advance references separately.
