The manifest enables auto-install when its two dependencies are installed together.
For an existing database, refresh the Apps list and explicitly install
`l10n_ae_edi_pint_peppol`. Updating previously installed UAE modules alone did not
install the newly discovered addon in the disposable upgrade rehearsal.

A Community installation without native Peppol does not need it. No new credentials,
permissions or configuration are introduced.

The bridge checks the accounting fiscal country; native Peppol applicability follows
Odoo's physical company country and registration state. In qualified public Odoo 20
(`65fca92369e515db7e340abef7dad47686c3da46`), AE is absent from `PEPPOL_LIST` in
`account/models/company.py`. A UAE fiscal company with an eligible foreign physical
country can still enter the native delegation branch. This distinction does not
establish UAE transport or network acceptance. Configuring an external ASP does not
enable Odoo's native verifier.
