# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hashlib
from urllib.parse import parse_qsl, urlencode
import requests
import frappe
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import (
    add_to_date,
    call_hook_method,
    cint,
    cstr,
    flt,
    get_url,
    now_datetime,
)
from frappe.utils.password import get_decrypted_password

from payments.utils import create_payment_gateway
from payments.utils.utils import request_relative_url


class CCAvenueSettings(Document):
    supported_currencies = ("INR",)

    def validate(self):
        create_payment_gateway("CCAvenue")
        call_hook_method("payment_gateway_enabled", gateway="CCAvenue")

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. CCAvenue does not support transactions in currency '{0}'"
                ).format(currency)
            )

    def get_payment_url(self, **kwargs):
        """Return payment url with several params"""
        integration_request = create_request_log(kwargs, service_name="CCAvenue")
        kwargs.update(dict(order_id=integration_request.name))

        return get_url(f"./ccavenue_checkout?{urlencode(kwargs)}")

    def get_payment_payload(self, **kwargs):
        integration_request = create_request_log(kwargs, service_name="CCAvenue")
        ccavenue_config = get_ccavenue_config()
        ccavenue_params = get_ccavenue_params(
            kwargs, integration_request.name, ccavenue_config
        )

        return {
            "gateway_url": ccavenue_config.url,
            "encRequest": ccavenue_params["encRequest"],
            "access_code": ccavenue_params["access_code"],
            "order_id": integration_request.name,
        }


def get_ccavenue_config():
    """Returns CCAvenue config"""

    ccavenue_config = frappe.db.get_singles_dict("CCAvenue Settings")
    ccavenue_config.update(
        dict(
            working_key=get_decrypted_password(
                "CCAvenue Settings", "CCAvenue Settings", "working_key"
            )
        )
    )

    if cint(ccavenue_config.sandbox):
        ccavenue_config.update(
            dict(
                url="https://test.ccavenue.com/transaction/transaction.do?command=initiateTransaction"
            )
        )
    else:
        ccavenue_config.update(
            dict(
                url="https://secure.ccavenue.com/transaction/transaction.do?command=initiateTransaction"
            )
        )
    return ccavenue_config


# CCAvenue uses different success vocabularies across its APIs: the browser
# redirect/webhook (encResp) response reports "Success", while the Order Status
# Tracker API (used for manual verification) reports "Shipped" for a completed
# transaction instead.
CCAVENUE_SUCCESS_STATUSES = {"Success", "Shipped"}

# Order Status Tracker API statuses that mean CCAvenue itself hasn't reached a
# final outcome yet, so the request should be left as-is and re-checked later.
CCAVENUE_PENDING_TRACKER_STATUSES = {"Awaited", "Initiated"}


def _get_cipher(working_key: str):
    key = hashlib.md5(working_key.encode()).digest()
    iv = bytes(range(0, 16))
    return key, iv


def encrypt(plain_text: str, working_key: str) -> str:
    key, iv = _get_cipher(working_key)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plain_text.encode(), AES.block_size)
    return cipher.encrypt(padded).hex()


def decrypt(cipher_hex: str, working_key: str) -> str:
    key, iv = _get_cipher(working_key)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = cipher.decrypt(bytes.fromhex(cipher_hex))
    return unpad(padded, AES.block_size).decode()


def get_ccavenue_params(payment_details, order_id, ccavenue_config):
    callback_url = request_relative_url(
        "/api/method/payments.payment_gateways.doctype.ccavenue_settings.ccavenue_settings.verify_transaction"
    )

    ccavenue_params = {
        "merchant_id": ccavenue_config.merchant_id,
        "order_id": order_id,
        "currency": payment_details.get("currency"),
        "amount": cstr(flt(payment_details.get("amount"), 2)),
        "redirect_url": callback_url,
        "cancel_url": callback_url,
        "language": "EN",
        "billing_name": payment_details.get("billing_name")
        or payment_details.get("payer_name"),
        "billing_address": payment_details.get("billing_address"),
        "billing_city": payment_details.get("billing_city"),
        "billing_state": payment_details.get("billing_state"),
        "billing_zip": payment_details.get("billing_zip"),
        "billing_tel": payment_details.get("billing_tel"),
        "billing_email": payment_details.get("billing_email")
        or payment_details.get("payer_email"),
    }
    ccavenue_params = {k: v for k, v in ccavenue_params.items() if v}

    plain_text = urlencode(ccavenue_params)
    enc_request = encrypt(plain_text, ccavenue_config.working_key)

    return {"encRequest": enc_request, "access_code": ccavenue_config.access_code}


@frappe.whitelist(allow_guest=True)
def verify_transaction(**kwargs):
    """Response URL target CCAvenue POSTs the encrypted result back to."""
    ccavenue_config = get_ccavenue_config()
    enc_response = kwargs.get("encResp")

    if not enc_response:
        frappe.respond_as_web_page(
            _("Payment Failed"),
            _(
                "Transaction response was not received. In case of any deductions, deducted amount will get refunded to your account."
            ),
            http_status_code=401,
            indicator_color="red",
        )
        return

    try:
        plain_response = dict(
            parse_qsl(decrypt(enc_response, ccavenue_config.working_key))
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "CCAvenue Payment Failed")
        frappe.respond_as_web_page(
            _("Payment Failed"),
            _("Unable to verify the payment response."),
            http_status_code=401,
            indicator_color="red",
        )
        return

    order_id = plain_response.get("order_id")
    if not order_id:
        frappe.log_error(
            f"Order unsuccessful. Response: {cstr(plain_response)}",
            "CCAvenue Payment Failed",
        )
        frappe.respond_as_web_page(
            _("Payment Failed"),
            _(
                "Transaction failed to complete. In case of any deductions, deducted amount will get refunded to your account."
            ),
            http_status_code=401,
            indicator_color="red",
        )
        return

    finalize_request(order_id, plain_response)


def finalize_request(order_id, transaction_response):
    request = frappe.get_doc("Integration Request", order_id)
    transaction_data = (
        request.data and frappe.parse_json(request.data) or frappe._dict()
    )
    redirect_message = transaction_data.get("redirect_message") or None

    request.db_set("output", frappe.as_json(transaction_response))

    is_success = transaction_response.get("order_status") in CCAVENUE_SUCCESS_STATUSES
    status = "Failed"

    redirect_to = (
        transaction_data.get("redirect_to")
        if is_success
        else (
            transaction_data.get("failed_redirect_to")
            or transaction_data.get("redirect_to")
        )
    ) or None

    if transaction_data.reference_doctype and transaction_data.reference_docname:
        payment_status = (
            "Completed"
            if is_success
            else (transaction_response.get("order_status") or "Failed")
        )
        try:
            custom_redirect_to = frappe.get_doc(
                transaction_data.reference_doctype,
                transaction_data.reference_docname,
            ).run_method("on_payment_authorized", payment_status)
            if is_success:
                status = "Completed"
            if custom_redirect_to:
                redirect_to = custom_redirect_to
        except Exception:
            frappe.log_error(frappe.get_traceback())

    request.db_set("status", status)

    if redirect_to:
        redirect_url = redirect_to
        if redirect_message:
            separator = "&" if "?" in redirect_url else "?"
            redirect_url += separator + urlencode(
                {"redirect_message": redirect_message}
            )
    else:
        redirect_url = get_url(
            "payment-success" if status == "Completed" else "payment-failed"
        )
        if redirect_message:
            redirect_url += "?" + urlencode({"redirect_message": redirect_message})

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = redirect_url


def get_gateway_controller(doctype, docname):
    reference_doc = frappe.get_doc(doctype, docname)
    return frappe.db.get_value(
        "Payment Gateway", reference_doc.payment_gateway, "gateway_controller"
    )


def log_webhook_response(
    order_id=None,
    status="Error",
    http_status_code=None,
    raw_payload=None,
    decrypted_response=None,
    error=None,
):
    """Persist every incoming CCAvenue webhook hit for auditing/debugging."""
    integration_request = (
        order_id
        if order_id and frappe.db.exists("Integration Request", order_id)
        else None
    )

    log = frappe.get_doc(
        {
            "doctype": "Webhook Response Log",
            "gateway": "CCAvenue",
            "order_id": order_id,
            "status": status,
            "http_status_code": http_status_code,
            "integration_request": integration_request,
            "raw_payload": frappe.as_json(raw_payload) if raw_payload else None,
            "decrypted_response": (
                frappe.as_json(decrypted_response) if decrypted_response else None
            ),
            "error": error,
        }
    )
    log.insert(ignore_permissions=True)
    frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit


@frappe.whitelist(allow_guest=True)
def ccavenue_webhook():
    """
    Background Server-to-Server Webhook / Dynamic Event Notification endpoint.
    CCAvenue sends a POST request with the 'encResp' argument.
    """
    # 1. Enforce that it is processing incoming server data
    if frappe.request.method != "POST":
        frappe.throw(_("Only POST requests are allowed"), frappe.PermissionError)

    # 2. Fetch the configuration parameters
    ccavenue_config = get_ccavenue_config()

    # CCAvenue sends parameters as form-encoded data in a POST request
    enc_response = frappe.local.form_dict.get("encResp")

    if not enc_response:
        frappe.log_error("Webhook payload missing 'encResp'", "CCAvenue Webhook Error")
        # Return a status code so CCAvenue knows it reached the server but failed
        frappe.local.response["http_status_code"] = 400
        log_webhook_response(
            status="Error",
            http_status_code=400,
            raw_payload=frappe.local.form_dict,
            error="Missing encrypted payload",
        )
        return "Missing encrypted payload"

    try:
        # 3. Decrypt the response string and parse key-value pairs
        decrypted_str = decrypt(enc_response, ccavenue_config.working_key)
        plain_response = dict(parse_qsl(decrypted_str))
    except Exception:
        frappe.log_error(frappe.get_traceback(), "CCAvenue Webhook Decryption Failed")
        frappe.local.response["http_status_code"] = 400
        log_webhook_response(
            status="Error",
            http_status_code=400,
            raw_payload=frappe.local.form_dict,
            error=frappe.get_traceback(),
        )
        return "Decryption error"

    order_id = plain_response.get("order_id")
    if not order_id:
        frappe.log_error(
            f"Webhook payload missing order_id. Data: {cstr(plain_response)}",
            "CCAvenue Webhook Error",
        )
        frappe.local.response["http_status_code"] = 400
        log_webhook_response(
            status="Error",
            http_status_code=400,
            decrypted_response=plain_response,
            error="Missing order identifier",
        )
        return "Missing order identifier"

    # 4. Process the background transaction state update
    try:
        process_webhook_payment(order_id, plain_response)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "CCAvenue Webhook Processing Failed")
        frappe.local.response["http_status_code"] = 500
        log_webhook_response(
            order_id=order_id,
            status="Error",
            http_status_code=500,
            decrypted_response=plain_response,
            error=frappe.get_traceback(),
        )
        return "Internal processing failed"

    # 5. Tell CCAvenue the event was successfully acknowledged
    frappe.local.response["http_status_code"] = 200
    log_webhook_response(
        order_id=order_id,
        status=(
            "Success" if plain_response.get("order_status") == "Success" else "Failed"
        ),
        http_status_code=200,
        decrypted_response=plain_response,
    )
    return "OK"


def process_webhook_payment(order_id, transaction_response):
    """Updates database parameters silently without attempting UI user-redirection loops."""
    if not frappe.db.exists("Integration Request", order_id):
        frappe.log_error(
            f"Integration Request {order_id} not found for webhook.",
            "CCAvenue Webhook Error",
        )
        return

    request = frappe.get_doc("Integration Request", order_id)

    # Do not re-process if already marked Completed
    if request.status == "Completed":
        return

    request.db_set("output", frappe.as_json(transaction_response))

    is_success = transaction_response.get("order_status") in CCAVENUE_SUCCESS_STATUSES
    status = (
        "Completed"
        if is_success
        else (transaction_response.get("order_status") or "Failed")
    )

    if transaction_data := (
        request.data and frappe.parse_json(request.data) or frappe._dict()
    ):
        if transaction_data.reference_doctype and transaction_data.reference_docname:
            payment_status = "Completed" if is_success else status
            try:
                # Trigger hooks attached to standard document processing
                frappe.get_doc(
                    transaction_data.reference_doctype,
                    transaction_data.reference_docname,
                ).run_method("on_payment_authorized", payment_status)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(), "Webhook Document Hook Exception"
                )

    request.db_set("status", "Completed" if is_success else "Failed")


@frappe.whitelist()
def check_payment_status_by_id(order_id: str) -> dict:
    """
    Queries CCAvenue Order Status Tracker API using an internal order_id.
    Decrypts the response and returns the status mapping dictionary.
    """
    if not order_id:
        frappe.throw(_("Please provide a valid Order ID"))

    ccavenue_config = get_ccavenue_config()

    # Determine correct endpoint based on Environment settings
    if cint(ccavenue_config.sandbox):
        api_url = "https://apitest.ccavenue.com/apis/servlet/DoWebTrans"
    else:
        api_url = "https://api.ccavenue.com/apis/servlet/DoWebTrans"

    # 1. Structure the parameter payload for the API
    query_params = {
        "order_no": order_id,
        "reference_no": "",  # Leave blank since we are using order_no/order_id
    }

    # 2. Convert to JSON text and encrypt
    plain_text = frappe.as_json(query_params)
    enc_request = encrypt(plain_text, ccavenue_config.working_key)

    # 3. Setup standard POST body structural arguments required by CCAvenue
    payload = {
        "enc_request": enc_request,
        "access_code": ccavenue_config.access_code,
        "command": "orderStatusTracker",
        "request_type": "JSON",
        "response_type": "JSON",
        "version": "1.2",
    }

    try:
        # 4. Perform synchronous server-to-server request
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = requests.post(api_url, data=payload, headers=headers, timeout=20)

        if response.status_code != 200:
            frappe.throw(_("Failed to connect to CCAvenue verification servers."))

        # 5. Extract status from standard plain string formats returned
        response_text = response.text.strip()
        response_dict = dict(parse_qsl(response_text))

        if "enc_response" in response_dict:
            decrypted_str = decrypt(
                response_dict["enc_response"].strip(), ccavenue_config.working_key
            )
            final_status_data = frappe.parse_json(decrypted_str)
            return final_status_data
        else:
            frappe.log_error(
                f"CCAvenue status tracking raw failure: {response_text}",
                "CCAvenue API Error",
            )
            return {
                "status": "Error",
                "message": "No encrypted response returned by CCAvenue",
            }

    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "CCAvenue Status Query Exception Failed"
        )
        return {"status": "Error", "message": "Failed to connect or decrypt payload"}


# CCAvenue's hosted page lets a customer retry after a decline without a new
# order_id: several transaction attempts can end up under one order_no.
# orderStatusTracker queried with a blank reference_no has been observed to
# return an EARLIER attempt instead of the latest one for such orders, so a
# single failure-looking read is not trustworthy. Re-check this many cron
# cycles (15 min apart) before accepting a failure as final; a clear success
# is still finalized immediately.
CCAVENUE_MAX_VERIFICATION_ATTEMPTS = 6


def verify_pending_payments():
    """Cron (every 15 min): reconcile CCAvenue Integration Requests that never
    received a redirect/webhook callback (e.g. the customer closed the tab
    after paying) by polling the Order Status Tracker API via
    check_payment_status_by_id, then finalizing the request the same way the
    webhook handler does.
    """
    pending_order_ids = frappe.get_all(
        "Integration Request",
        filters={
            "integration_request_service": "CCAvenue",
            "status": ["in", ("Queued", "Authorized")],
            "creation": ["<", add_to_date(now_datetime(), minutes=-10)],
            "modified": [">=", add_to_date(now_datetime(), days=-3)],
        },
        pluck="name",
        limit=200,
    )

    for order_id in pending_order_ids:
        try:
            status_response = check_payment_status_by_id(order_id)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"CCAvenue Payment Verification Failed for {order_id}",
            )
            continue

        order_status = status_response.get("order_status") if status_response else None

        if order_status in CCAVENUE_SUCCESS_STATUSES:
            try:
                process_webhook_payment(order_id, status_response)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"CCAvenue Payment Verification Finalize Failed for {order_id}",
                )
            continue

        if not order_status or order_status in CCAVENUE_PENDING_TRACKER_STATUSES:
            # Still not final on CCAvenue's side, re-check on the next run.
            continue

        try:
            previous_output = (
                frappe.parse_json(
                    frappe.db.get_value("Integration Request", order_id, "output")
                    or "{}"
                )
                or {}
            )
        except Exception:
            previous_output = {}

        attempts = cint(previous_output.get("_verification_attempts")) + 1

        if attempts < CCAVENUE_MAX_VERIFICATION_ATTEMPTS:
            # Looks failed, but it may be a stale read of an earlier attempt
            # under this order_id - hold off and check again next cycle
            # instead of locking in "Failed" right away.
            status_response["_verification_attempts"] = attempts
            frappe.db.set_value(
                "Integration Request",
                order_id,
                "output",
                frappe.as_json(status_response),
                update_modified=True,
            )
            continue

        try:
            process_webhook_payment(order_id, status_response)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"CCAvenue Payment Verification Finalize Failed for {order_id}",
            )
