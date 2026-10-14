# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Process-configured HTTP ingress for KMEE's controlled event contract."""

import os
import re

from werkzeug.exceptions import RequestEntityTooLarge

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..tools.controlled_events import (
    ControlledEventBindingUnavailable,
    ControlledEventReceiver,
)


class ControlledEventIngress(http.Controller):
    @http.route(
        "/l10n_ae/controlled/events/<int:binding_id>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
        sitemap=False,
        # One extra byte detects chunked truncation before signature verification.
        max_content_length=65537,
    )
    def receive(self, binding_id, **_kwargs):
        def respond(body, status):
            return request.make_json_response(
                body, status=status, headers={"Cache-Control": "no-store"}
            )

        if request.httprequest.mimetype != "application/json":
            return respond({"error": "unsupported_media_type"}, 415)
        length = request.httprequest.content_length
        if length is not None and length > 65536:
            return respond({"error": "body_too_large"}, 413)
        try:
            raw = request.httprequest.get_data()
        except RequestEntityTooLarge:
            return respond({"error": "body_too_large"}, 413)
        if len(raw) > 65536:
            return respond({"error": "body_too_large"}, 413)
        prefix = f"KMEE_UAE_CONTROLLED_EVENTS_{binding_id}"
        database = os.environ.get(prefix + "_DATABASE")
        user = os.environ.get(prefix + "_USER_ID", "")
        company = os.environ.get(prefix + "_COMPANY_ID", "")
        secret = os.environ.get(prefix + "_SECRET", "").encode("utf-8")
        if (
            database != request.registry.db_name
            or not re.fullmatch(r"[1-9][0-9]{0,9}", user)
            or not re.fullmatch(r"[1-9][0-9]{0,9}", company)
            or int(user) > 2147483647
            or int(company) > 2147483647
            or len(secret) < 16
        ):
            return respond({"error": "receiving_unavailable"}, 503)
        receiver = ControlledEventReceiver(
            request.registry, int(user), int(company), secret
        )
        try:
            outcome = receiver.receive(
                binding_id, raw, request.httprequest.headers.get("X-KMEE-UAE-Signature")
            )
        except AccessError:
            return respond({"error": "receiving_forbidden"}, 403)
        except ControlledEventBindingUnavailable:
            return respond({"error": "receiving_unavailable"}, 503)
        except ValueError:
            return respond({"error": "invalid_event"}, 400)
        except UserError:
            return respond({"error": "event_requires_review"}, 409)
        return respond({"outcome": outcome}, 200)
