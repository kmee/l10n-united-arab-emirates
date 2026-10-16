The integration is under development. The following release gates remain open:

- Complete end-to-end acceptance for sending, authenticated signed-event ingress,
  receiving and self-billing, including scheduler authority and company isolation.
- Qualify multi-currency, multiple credit references and advances across complete
  business flows; confirm UUID assignment and correction policies with the chosen ASP.
- Complete operational recovery qualification, including bounded backoff, batch
  recovery and reconciliation after interruptions.
- Qualify the remaining security, load, backup/restore (RPO/RTO), upgrade/rollback and
  PINT-AE ruleset transition scenarios on Odoo 20.0, including Enterprise coexistence.
- Agree accredited ASP API contracts and replace the KMEE-controlled ClearTax and
  Suntech/TCA simulations with documented, provider-reviewed adapters. Validate
  authorized ASP sandbox/pilot flows and fiscal reporting before production use;
  controlled mock acceptance is not FTA confirmation.
- Complete and review the English and Brazilian Portuguese user guides and site, plus
  the ten complete training videos by role with visible navigation, English narrative
  captions and no audio narration.
- Complete hosted CI, reproducible release packaging and deployment runbooks, then
  prepare the contribution for community review and a proposed OCA umbrella repository.

Selected passing tests do not by themselves establish complete acceptance or production
readiness. Public publication and deployment require their own readiness review.
