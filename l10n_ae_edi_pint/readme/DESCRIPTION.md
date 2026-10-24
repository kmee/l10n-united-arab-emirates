Extend the public Community UBL/PINT generator for UAE identifiers and document processes. Freeze the generated XML at preparation and validate it using pinned UBL XSD plus shared and UAE Schematron rules. Reviewed incoming AED billing and self-billing documents use the public UBL21 importer with existing counterparties and taxes, protected original evidence and economic accounting roles. Additional scenario fields, foreign currency and the full conformance matrix remain under development. This module does not submit directly to Peppol or the FTA.

Received evidence packages preserve the exact original XML, processing evidence and a
versioned retention appendix. Reviewed obligations and active/released holds remain
portable without processing the original or creating accounting entries.

Each processing-history entry records the selected PINT package and manifest digest
separately from its validation report. A missing validation resource still retains the
selected package for a safe retry. Older history without a recorded package remains
unpinned and is never assigned the current default.
