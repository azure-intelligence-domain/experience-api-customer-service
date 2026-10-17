"""[Channel] Customer Service Experience API (openapi.yaml): what a customer service agent (a person
in the customer portal, or an AI agent through the agent gateway) needs about any customer and any
order: the order with everything every domain knows about it, and the commands support may give.
All through the gateway, as this API's runtime identity (ADR 0017). The order domain decides.
"""

import azure.functions as func

import kit
import order_view

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)  # Easy Auth guards the door

ALLOWED = ("cancel", "changeDeliveryAddress", "reschedule", "putOnHold", "release")
OPEN = {"placed", "confirmed", "onHold", "readyForShipment", "shipped", "delivered"}


def gateway(journey: kit.Journey) -> kit.Platform:
    return kit.Platform.from_environment(journey)


def ok(answer: kit.Answer, req: func.HttpRequest) -> dict:
    if not answer.ok:
        raise kit.passed_on(answer, req)
    return answer.body


def customer(a: dict) -> dict:
    keep = ("customerId", "name", "industry", "shippingAddress", "currency")
    return {"_links": {"self": {"href": f"/customers/{a['customerId']}"}, "orders": {"href": f"/orders?customerId={a['customerId']}"}},
            **{k: a[k] for k in keep if a.get(k) is not None}}


def the_order(platform: kit.Platform, order: dict, req: func.HttpRequest) -> dict:
    """Everything every domain knows about the order: invoices and sales order (finance), the
    reservation (supply), the booking and shipment (logistics), read at once."""
    refs = order.get("references", {})
    shipment_id, reservation_id = order_view.shipment_id(order), order_view.reservation_id(order)
    calls = {"invoices": ("GET", "/sap/invoices", None, {"orderId": order["orderId"]}), "products": ("GET", "/salesforce/products")}
    if refs.get("erpOrderNumber"):
        calls["salesOrder"] = ("GET", f"/sap/sales-orders/{refs['erpOrderNumber']}")
    if reservation_id:
        calls["reservation"] = ("GET", f"/wms/stock-reservations/{reservation_id}")
    if shipment_id:
        calls["booking"] = ("GET", f"/3pl/shipment-bookings/{shipment_id}")
        calls["shipped"] = ("GET", f"/3pl/shipments/{shipment_id}")
    answers = dict(zip(calls, platform.all(*calls.values())))
    found = {name: a.body for name, a in answers.items() if a.ok}
    invoices = ok(answers["invoices"], req)["_embedded"]["invoices"]
    products = {p["productId"]: p for p in ok(answers["products"], req)["_embedded"]["products"]}
    out = order_view.view(order, invoices, found.get("booking"), found.get("shipped"), products, ALLOWED)
    out["references"] = refs
    if "salesOrder" in found:
        keep = ("erpOrderNumber", "status", "netValue", "refusalReason", "createdAt", "cancellation")
        out["salesOrder"] = {k: found["salesOrder"][k] for k in keep if k in found["salesOrder"]}
    if "reservation" in found:
        keep = ("reservationId", "warehouseId", "lifecycleStatus", "orderLines", "reservedAt", "releasedAt", "releaseReason")
        out["reservation"] = {k: found["reservation"][k] for k in keep if k in found["reservation"]}
    return out


def order_of(platform: kit.Platform, req: func.HttpRequest) -> dict:
    answer = platform.call("GET", f"/order/orders/{req.route_params['orderId']}")
    if answer.status == 404:
        raise kit.refused(404, "Order Not Found", f"No order {req.route_params['orderId']} exists.", "not-found", req)
    return ok(answer, req)


# --- Queries ------------------------------------------------------------------------------------


@kit.refusals
def retrieve_list_of_customers(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    platform = platform_of(kit.journey(req))
    accounts = ok(platform.call("GET", "/salesforce/accounts"), req)["_embedded"]["accounts"]
    customers = [customer(a) for a in accounts]
    return kit.hal(200, {"_links": {"self": {"href": "/customers"}}, "totalCount": len(customers), "_embedded": {"customers": customers}})


@kit.refusals
def retrieve_customer(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    """A customer at a glance: its orders by status and what it still owes."""
    platform = platform_of(kit.journey(req))
    customer_id = req.route_params["customerId"]
    account, orders = platform.all(("GET", f"/salesforce/accounts/{customer_id}"),
                                   ("GET", "/order/orders", None, {"customerId": customer_id, "limit": 100}))
    if account.status == 404:
        raise kit.refused(404, "Customer Not Found", f"No customer {customer_id} exists.", "not-found", req)
    found = ok(orders, req)["_embedded"]["orders"]
    by_status: dict[str, int] = {}
    for o in found:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
    owed = [o["totalAmount"] for o in found if o["status"] in OPEN and o["paymentStatus"] != "paid"]
    out = {**customer(ok(account, req)), "orderCount": ok(orders, req)["totalCount"], "ordersByStatus": by_status,
           "openAmount": {"value": round(sum(m["value"] for m in owed), 2), "currency": owed[0]["currency"] if owed else "EUR"},
           "recentOrders": [order_view.summary(o) for o in found[:5]]}
    return kit.hal(200, out)


@kit.refusals
def retrieve_list_of_orders(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    platform = platform_of(kit.journey(req))
    query = {name: req.params.get(name) for name in ("customerId", "status", "offset", "limit")}
    found = ok(platform.call("GET", "/order/orders", query=query), req)
    orders = [order_view.summary(o) for o in found["_embedded"]["orders"]]
    return kit.hal(200, {"_links": found["_links"], "totalCount": found["totalCount"], "_embedded": {"orders": orders}})


@kit.refusals
def retrieve_order(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    platform = platform_of(kit.journey(req))
    return kit.hal(200, the_order(platform, order_of(platform, req), req))


# --- Commands: passed to the order domain, answered with the order as it is now -----------------


def command(req: func.HttpRequest, platform_of, method: str, suffix: str, body=None, query=None) -> func.HttpResponse:
    platform = platform_of(kit.journey(req))
    answer = platform.call(method, f"/order/orders/{req.route_params['orderId']}{suffix}", body, query)
    return kit.hal(200, the_order(platform, ok(answer, req), req))


@kit.refusals
def cancel_order(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    reason = req.params.get("reason", "")
    if not reason or len(reason) > 200:
        raise kit.invalid("Query parameter reason is required, at most 200 characters.", req)
    return command(req, platform_of, "DELETE", "", query={"reason": reason})


@kit.refusals
def put_order_on_hold(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    return command(req, platform_of, "PUT", "/hold", kit.body_of(req, "holdRequest"))


@kit.refusals
def release_order(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    return command(req, platform_of, "DELETE", "/hold")


@kit.refusals
def reschedule_delivery(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    return command(req, platform_of, "PUT", "/requested-delivery-date", kit.body_of(req, "requestedDeliveryDateChange"))


@kit.refusals
def change_delivery_address(req: func.HttpRequest, platform_of=gateway) -> func.HttpResponse:
    return command(req, platform_of, "PUT", "/delivery-address", kit.body_of(req, "deliveryAddress"))


# --- Routes (host.json: no route prefix, so the paths are the contract's) -----------------------


@app.route(route="customers", methods=["GET"])
def retrieveListOfCustomers(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_list_of_customers(req)


@app.route(route="customers/{customerId}", methods=["GET"])
def retrieveCustomer(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_customer(req)


@app.route(route="orders", methods=["GET"])
def retrieveListOfOrders(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_list_of_orders(req)


@app.route(route="orders/{orderId}", methods=["GET"])
def retrieveOrder(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_order(req)


@app.route(route="orders/{orderId}", methods=["DELETE"])
def cancelOrder(req: func.HttpRequest) -> func.HttpResponse:
    return cancel_order(req)


@app.route(route="orders/{orderId}/hold", methods=["PUT"])
def putOrderOnHold(req: func.HttpRequest) -> func.HttpResponse:
    return put_order_on_hold(req)


@app.route(route="orders/{orderId}/hold", methods=["DELETE"])
def releaseOrder(req: func.HttpRequest) -> func.HttpResponse:
    return release_order(req)


@app.route(route="orders/{orderId}/requested-delivery-date", methods=["PUT"])
def rescheduleDelivery(req: func.HttpRequest) -> func.HttpResponse:
    return reschedule_delivery(req)


@app.route(route="orders/{orderId}/delivery-address", methods=["PUT"])
def changeDeliveryAddress(req: func.HttpRequest) -> func.HttpResponse:
    return change_delivery_address(req)
