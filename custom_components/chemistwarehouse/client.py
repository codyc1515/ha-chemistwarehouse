"""Authenticated, read-only website client."""

from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
from yarl import URL

from .const import BASE_URL
from .parser import (
    ParseError,
    is_login,
    login_form,
    parse_invoice,
    parse_orders,
    parse_repeats,
    parse_tracking,
    safe_url,
)


class AuthenticationError(Exception):
    """The saved credentials are no longer accepted."""


class CannotConnect(Exception):
    """The site is unreachable or is blocking automated access."""


class ChemistWarehouseClient:
    """Own an isolated cookie jar for each account."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str):
        self.session = session
        self.username = username
        self.password = password
        self.authenticated = False
        self.repeats = ()

    async def _request(self, method: str, url: str, data: dict | None = None) -> str:
        """Validate each redirect and never include private URLs in errors."""
        for _ in range(10):
            url = safe_url(url)
            try:
                async with self.session.request(
                    method,
                    url,
                    data=data,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise CannotConnect("Website returned an invalid redirect")
                        url = safe_url(location, url)
                        if response.status == 303 or (
                            response.status in (301, 302) and method == "POST"
                        ):
                            method, data = "GET", None
                        continue
                    if response.status == 401:
                        raise AuthenticationError("Website session expired")
                    if response.status >= 400:
                        raise CannotConnect(f"Website returned HTTP {response.status}")
                    return await response.text()
            except (aiohttp.ClientError, TimeoutError):
                # aiohttp exceptions can contain token-bearing request URLs.
                raise CannotConnect("Unable to contact Chemist Warehouse") from None
        raise CannotConnect("Website redirected too many times")

    async def async_login(self) -> None:
        """Submit fresh ASP.NET tokens and verify access to the account list."""
        self.authenticated = False
        page = await self._request("GET", BASE_URL + "/login")
        action, fields = login_form(page, self.username, self.password)
        result = await self._request("POST", action, fields)
        if is_login(result):
            raise AuthenticationError("Invalid username or password")
        # The supplied site's JavaScript completes SSO using this cookie.
        cookies = self.session.cookie_jar.filter_cookies(URL(BASE_URL))
        if "ssoGuid" in cookies and cookies["ssoGuid"].value:
            await self._request(
                "GET",
                BASE_URL + "/AMS/Handlers/SSO.ashx?" + urlencode({"g": cookies["ssoGuid"].value}),
            )
        page = await self._request("GET", BASE_URL + "/YourAccount")
        if is_login(page):
            raise AuthenticationError("Invalid username or password")
        parse_orders(page)
        self.authenticated = True

    async def _authenticated_get(self, url: str) -> str:
        if not self.authenticated:
            await self.async_login()
        try:
            page = await self._request("GET", url)
            if not is_login(page):
                return page
        except AuthenticationError:
            pass
        await self.async_login()
        page = await self._request("GET", url)
        if is_login(page):
            self.authenticated = False
            raise AuthenticationError("Website session expired")
        return page

    async def async_orders(self):
        page = await self._authenticated_get(BASE_URL + "/YourAccount")
        orders = parse_orders(page)
        repeats = parse_repeats(page)
        self.repeats = repeats
        return orders

    async def async_tracking(self, order):
        if urlsplit(order.tracking_url).path.casefold().rstrip("/") != "/track-your-order":
            raise ParseError("Unexpected tracking path")
        return parse_tracking(await self._authenticated_get(order.tracking_url), order.order_id)

    async def async_medications(self, order):
        """Read only the invoice belonging to a prescription order."""
        if not order.invoice_url:
            return ()
        parts = urlsplit(safe_url(order.invoice_url))
        if (
            parts.path.casefold() != "/cmsglobalfiles/pages/invoice.aspx"
            or parse_qs(parts.query).get("OrderID", [""])[0] != order.order_id
        ):
            raise ParseError("Invoice link does not match the prescription order")
        return parse_invoice(await self._authenticated_get(order.invoice_url), order.order_id)
