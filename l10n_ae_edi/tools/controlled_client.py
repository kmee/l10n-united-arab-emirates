# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""A client for KMEE's simulation contract, not a vendor's UAE API."""

import hashlib
import json
import re
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlencode, urlsplit


class ControlledAspError(Exception):
    """A classified result without credentials or invoice data in its message."""

    def __init__(self, kind, status=None):
        self.kind = kind
        self.status = status
        super().__init__("Controlled ASP result: " + kind)


class ControlledAspClient:
    """Transport-only SDK. Call outside accounting/database transactions."""

    def __init__(self, base_url, token, provider, timeout=5):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ControlledAspError("nonlocal_mock_endpoint")
        if provider not in ("controlled", "cleartax", "suntech_tca"):
            raise ControlledAspError("unsupported_mock_provider")
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.token, self.provider, self.timeout = token, provider, timeout

    def _request(self, method, path, body=None, key=None, require_remote_id=True):
        headers = {"Authorization": "Bearer " + self.token}
        if key:
            headers["Idempotency-Key"] = key
        content = None
        if body is not None:
            content = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
            headers["Content-Type"] = "application/json"
        connection = HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request(
                method, "/mock/v1/" + self.provider + path, content, headers
            )
            response = connection.getresponse()
            if response.status == 404 and method == "GET":
                return None
            if response.status not in (200, 201):
                kinds = {
                    401: "authentication",
                    403: "authentication",
                    409: "conflict",
                    429: "throttled",
                }
                raise ControlledAspError(
                    kinds.get(
                        response.status,
                        "rejected" if response.status < 500 else "unknown",
                    ),
                    response.status,
                )
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ControlledAspError("unknown")
            result = json.loads(raw)
            if (
                result.get("contract_version") != "kmee-controlled-1"
                or result.get("simulation") is not True
                or require_remote_id
                and not result.get("remote_id")
            ):
                raise ControlledAspError("unknown")
            return result
        except (OSError, HTTPException, ValueError, TypeError, AttributeError) as error:
            raise ControlledAspError("unknown") from error
        finally:
            connection.close()

    def submit(self, idempotency_key, document):
        """Send the already frozen envelope with a stable request key."""
        return self._request("POST", "/submissions", document, idempotency_key)

    def lookup(self, tin, idempotency_key):
        """Resolve an ambiguous result before attempting another POST."""
        query = urlencode({"tin": tin, "key": idempotency_key})
        return self._request("GET", "/submissions/by-key?" + query)

    def lookup_inbound(self, tin, remote_id, event_id=None):
        """Retrieve an original XML envelope from KMEE's controlled inbox."""
        if event_id is not None and (
            not isinstance(event_id, str) or not 1 <= len(event_id) <= 256
        ):
            raise ControlledAspError("invalid_event_request")
        params = {"tin": tin, "id": remote_id}
        if event_id is not None:
            params["event_id"] = event_id
        query = urlencode(params)
        result = self._request("GET", "/inbound/by-id?" + query)
        if result is None:
            return None
        if (
            result.get("provider") != self.provider
            or result.get("tin") != tin
            or result.get("remote_id") != remote_id
            or event_id is not None
            and result.get("event_id") != event_id
            or not isinstance(result.get("xml"), str)
            or hashlib.sha256(result["xml"].encode()).hexdigest()
            != result.get("xml_sha256")
        ):
            raise ControlledAspError("correlation")
        return result

    def list_inbound(self, tin, cursor="", limit=20):
        """List retained originals using a route-scoped KMEE simulation cursor."""
        if (
            not isinstance(tin, str)
            or not re.fullmatch(r"[0-9]{10}", tin)
            or not isinstance(cursor, str)
            or cursor
            and not re.fullmatch(r"[1-9][0-9]{0,18}\.[0-9a-f]{64}", cursor)
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ControlledAspError("invalid_page_request")
        query = urlencode({"tin": tin, "cursor": cursor, "limit": limit})
        result = self._request("GET", "/inbound?" + query, require_remote_id=False)
        if (
            not isinstance(result, dict)
            or result.get("provider") != self.provider
            or result.get("tin") != tin
            or result.get("cursor") != cursor
            or type(result.get("has_more")) is not bool
            or not isinstance(result.get("next_cursor"), str)
            or not isinstance(result.get("documents"), list)
        ):
            raise ControlledAspError("correlation")
        documents, next_cursor = result["documents"], result["next_cursor"]
        if (
            len(documents) > limit
            or documents
            and (
                not re.fullmatch(r"[1-9][0-9]{0,18}\.[0-9a-f]{64}", next_cursor)
                or next_cursor == cursor
            )
            or not documents
            and (next_cursor != cursor or result["has_more"])
            or result["has_more"]
            and len(documents) != limit
        ):
            raise ControlledAspError("correlation")
        seen = set()
        for document in documents:
            if (
                not isinstance(document, dict)
                or not isinstance(document.get("remote_id"), str)
                or not 1 <= len(document["remote_id"]) <= 256
                or "event_id" in document
                and (
                    not isinstance(document["event_id"], str)
                    or not 1 <= len(document["event_id"]) <= 256
                )
                or not isinstance(document.get("xml_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", document["xml_sha256"])
            ):
                raise ControlledAspError("correlation")
            identity = (
                ("event", document["event_id"])
                if "event_id" in document
                else ("document", document["remote_id"])
            )
            if identity in seen:
                raise ControlledAspError("correlation")
            seen.add(identity)
        return result
