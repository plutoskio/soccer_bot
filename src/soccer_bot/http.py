from __future__ import annotations

from dataclasses import dataclass
import gzip
import json
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        body = self.body
        if self.headers.get("content-encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))


class HttpClient:
    def __init__(self, user_agent: str = "soccer-bot-source-validation/0.1") -> None:
        self.user_agent = user_agent

    def get(
        self,
        base_url: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        query = urlencode(
            [(key, str(value)) for key, value in (params or {}).items() if value is not None]
        )
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        request_headers.update(headers or {})
        request = Request(url, headers=request_headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    url=url,
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except HTTPError as error:
            return HttpResponse(
                url=url,
                status=error.code,
                headers={key.lower(): value for key, value in error.headers.items()},
                body=error.read(),
            )
        except URLError as error:
            raise RuntimeError(f"Network request failed for {base_url}{path}: {error.reason}") from error

    def post_json(
        self,
        base_url: str,
        path: str,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        request = Request(
            url,
            headers=request_headers,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    url=url,
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except HTTPError as error:
            return HttpResponse(
                url=url,
                status=error.code,
                headers={key.lower(): value for key, value in error.headers.items()},
                body=error.read(),
            )
        except URLError as error:
            raise RuntimeError(f"Network request failed for {base_url}{path}: {error.reason}") from error
