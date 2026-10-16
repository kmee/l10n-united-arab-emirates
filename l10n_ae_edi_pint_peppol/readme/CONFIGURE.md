The manifest enables auto-install when its two dependencies are installed together.
For an existing database, refresh the Apps list and explicitly install
`l10n_ae_edi_pint_peppol`. Updating previously installed UAE modules alone did not
install the newly discovered addon in the disposable upgrade rehearsal.

A Community installation without native Peppol does not need it. No new credentials,
permissions or configuration are introduced.

Native applicability follows Odoo's supported-country list and registration state.
In the qualified public Odoo 20 source (`65fca92369e515db7e340abef7dad47686c3da46`),
AE is absent from `PEPPOL_LIST` in `account/models/company.py`. The native-applicable
AE branch is therefore forward compatibility, not a currently qualified UAE transport.
The bridge retains the native applicability check so future native UAE support can
delegate normally. Configuring an external ASP does not enable Odoo's native verifier.
