import json

import azure.functions as func

import function_app as api
from contracts_test import assert_matches
from fakes import ORDER, FakePlatform


def request(method, url, body=None, route=None, params=None, headers=None):
    return func.HttpRequest(method, url, headers={"Correlation-Id": "ord-4711", **(headers or {})}, route_params=route or {},
                            params=params or {}, body=json.dumps(body).encode() if body is not None else b"")


ORDER_4711 = {"orderId": "4711"}


def test_an_order_with_what_every_domain_knows():
    order = assert_matches(api.retrieve_order(request("GET", "/orders/4711", route=ORDER_4711), FakePlatform()), "retrieveOrder")
    assert order["salesOrder"]["erpOrderNumber"] == "8000043217" and order["reservation"]["lifecycleStatus"] == "reserved"
    assert order["references"]["trackingNumber"] == "SPD4711BE" and order["shipment"]["status"] == "dispatched"
    assert set(order["_links"]) == {"self"}  # shipped: nothing left for support to change


def test_an_order_before_references_still_finds_its_shipment():
    """Orders placed before process-api-order 1.2.0 have no references: the systems' own numbering finds them."""
    old = {**ORDER, "references": None, "timeline": None}
    old = {k: v for k, v in old.items() if v is not None}
    platform = FakePlatform({("GET", "/order/orders/4711"): (200, old)})
    order = assert_matches(api.retrieve_order(request("GET", "/orders/4711", route=ORDER_4711), platform), "retrieveOrder")
    assert order["shipment"]["shipmentId"] == "SPD-004711" and order["reservation"]["reservationId"] == "RSV-4711"
    assert [p["reached"] for p in order["progress"]] == [True, True, True, True, False, False] and order["timeline"] == []


def test_a_customer_at_a_glance():
    orders = [{**ORDER, "orderId": str(n), "status": s, "paymentStatus": p} for n, s, p in
              [(4711, "shipped", "invoiced"), (4712, "cancelled", "invoiced"), (4713, "completed", "paid")]]
    platform = FakePlatform({("GET", "/order/orders"): (200, {"_links": {"self": {"href": "/orders"}}, "totalCount": 3, "_embedded": {"orders": orders}})})
    response = api.retrieve_customer(request("GET", "/customers/ACME", route={"customerId": "ACME"}), platform)
    overview = assert_matches(response, "retrieveCustomer")
    assert overview["ordersByStatus"] == {"shipped": 1, "cancelled": 1, "completed": 1} and overview["openAmount"]["value"] == 84000.0
    assert platform.calls[1][3] == {"customerId": "ACME", "limit": 100}
    assert api.retrieve_customer(request("GET", "/customers/X", route={"customerId": "X"}), FakePlatform()).status_code == 404


def test_hold_release_reschedule_go_to_the_order_domain():
    confirmed = {**ORDER, "status": "confirmed", "references": {"erpOrderNumber": "8000043217"}, "timeline": ORDER["timeline"][:3]}
    held = {**confirmed, "status": "onHold", "holdReason": "Customer asked to wait."}
    platform = FakePlatform({("PUT", "/order/orders/4711/hold"): (200, held), ("DELETE", "/order/orders/4711/hold"): (200, confirmed),
                             ("PUT", "/order/orders/4711/requested-delivery-date"): (200, confirmed)})
    on_hold = assert_matches(api.put_order_on_hold(request("PUT", "/x", {"reason": "Customer asked to wait."}, ORDER_4711), platform), "putOrderOnHold")
    assert on_hold["status"] == "onHold" and set(on_hold["_links"]) == {"self", "cancel", "changeDeliveryAddress", "reschedule", "release"}
    released = assert_matches(api.release_order(request("DELETE", "/x", route=ORDER_4711), platform), "releaseOrder")
    assert "putOnHold" in released["_links"]
    assert_matches(api.reschedule_delivery(request("PUT", "/x", {"requestedDeliveryDate": "2026-10-14"}, ORDER_4711), platform), "rescheduleDelivery")
    assert [c[:3] for c in platform.calls if c[0] != "GET"] == [("PUT", "/order/orders/4711/hold", {"reason": "Customer asked to wait."}),
                                                                ("DELETE", "/order/orders/4711/hold", None),
                                                                ("PUT", "/order/orders/4711/requested-delivery-date", {"requestedDeliveryDate": "2026-10-14"})]


def test_cancel_needs_a_reason():
    response = api.cancel_order(request("DELETE", "/orders/4711", route=ORDER_4711), FakePlatform())
    assert response.status_code == 400 and assert_matches(response, "cancelOrder")["title"] == "Invalid Request"


def test_orders_of_any_customer():
    page = assert_matches(api.retrieve_list_of_orders(request("GET", "/orders"), FakePlatform()), "retrieveListOfOrders")
    assert page["_embedded"]["orders"][0]["trackingNumber"] == "SPD4711BE" and page["_embedded"]["orders"][0]["lineCount"] == 1
