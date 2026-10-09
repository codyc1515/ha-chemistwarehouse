"""Verify HTTP behavior without accessing a real account."""

from unittest.mock import AsyncMock

import aiohttp
import pytest
from yarl import URL

from custom_components.chemistwarehouse.client import AuthenticationError, ChemistWarehouseClient
from custom_components.chemistwarehouse.const import BASE_URL
from custom_components.chemistwarehouse.parser import Order, ParseError


async def test_account_fetch_updates_orders_and_clears_removed_repeats(pages):
    async with aiohttp.ClientSession() as session:
        client = ChemistWarehouseClient(session, "test", "secret")
        account_with_repeats = pages["account"].split('<table class="table')[0] + pages["repeats"]
        client._authenticated_get = AsyncMock(side_effect=[account_with_repeats, pages["account"]])
        assert len(await client.async_orders()) == 1
        assert [repeat.remaining for repeat in client.repeats] == [1, 2]
        client._authenticated_get.assert_awaited_once_with(BASE_URL + "/YourAccount")

        assert len(await client.async_orders()) == 1
        assert client.repeats == ()


async def test_login_and_session_expiry(pages):
    async with aiohttp.ClientSession() as session:
        client = ChemistWarehouseClient(session, "test", "secret")
        client._request = AsyncMock(
            side_effect=[
                pages["login"],
                pages["account"],
                pages["account"],
                pages["login"],
                pages["login"],
                pages["account"],
                pages["account"],
                pages["account"],
            ]
        )
        await client.async_login()
        assert client.authenticated
        orders = await client.async_orders()
        assert len(orders) == 1
        assert client._request.await_count == 8
        assert client._request.await_args_list[1].args[0] == "POST"


async def test_invalid_credentials(pages):
    async with aiohttp.ClientSession() as session:
        client = ChemistWarehouseClient(session, "test", "wrong")
        client._request = AsyncMock(side_effect=[pages["login"], pages["login"]])
        with pytest.raises(AuthenticationError):
            await client.async_login()
        assert not client.authenticated


async def test_sso_cookie_is_completed_before_account_verification(pages):
    async with aiohttp.ClientSession() as session:
        session.cookie_jar.update_cookies({"ssoGuid": "test-guid"}, URL(BASE_URL))
        client = ChemistWarehouseClient(session, "test", "secret")
        client._request = AsyncMock(
            side_effect=[
                pages["login"],
                pages["account"],
                "{}",
                pages["account"],
            ]
        )
        await client.async_login()
        assert client._request.await_args_list[2].args == (
            "GET",
            BASE_URL + "/AMS/Handlers/SSO.ashx?g=test-guid",
        )
        assert client.authenticated


async def test_cross_origin_login_action_is_rejected(pages):
    async with aiohttp.ClientSession() as session:
        client = ChemistWarehouseClient(session, "test", "secret")
        html = pages["login"].replace("./login?", "https://evil.example/login?")
        client._request = AsyncMock(return_value=html)
        with pytest.raises(ParseError):
            await client.async_login()
        assert client._request.await_count == 1


async def test_request_does_not_follow_unsafe_redirect():
    class Response:
        status = 302
        headers = {"Location": "https://evil.example/login"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def request(self, *args, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response()

    with pytest.raises(ParseError):
        await ChemistWarehouseClient(Session(), "test", "secret")._request(
            "POST", BASE_URL + "/login", {"secret": "value"}
        )


async def test_invoice_fetches_only_associated_prescription_details(pages):
    async with aiohttp.ClientSession() as session:
        client = ChemistWarehouseClient(session, "test", "secret")
        client._authenticated_get = AsyncMock(return_value=pages["invoice"])
        order = Order(
            "1001", "", "", "", BASE_URL + "/cmsglobalfiles/pages/invoice.aspx?OrderID=1001&ID=test"
        )
        meds = await client.async_medications(order)
        assert len(meds) == 1
        assert meds[0].name == "Example Medicine 10mg 30 tablets"
        client._authenticated_get.assert_awaited_once_with(order.invoice_url)


@pytest.mark.parametrize(
    "invoice_url",
    [
        BASE_URL + "/cmsglobalfiles/pages/invoice.aspx?OrderID=1002&ID=test",
        "https://evil.example/cmsglobalfiles/pages/invoice.aspx?OrderID=1001",
        BASE_URL + "/c/tracking?OrderID=1001",
    ],
)
async def test_invoice_rejects_wrong_order_or_origin_without_requests(invoice_url):
    async with aiohttp.ClientSession() as session:
        client = ChemistWarehouseClient(session, "test", "secret")
        client._authenticated_get = AsyncMock()
        with pytest.raises(ParseError):
            await client.async_medications(Order("1001", "", "", "", invoice_url))
        client._authenticated_get.assert_not_awaited()
