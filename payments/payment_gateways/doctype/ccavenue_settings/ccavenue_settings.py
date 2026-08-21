# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hashlib
from datetime import timedelta
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
    get_datetime,
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


CCAVENUE_SUCCESS_STATUSES = {"Success", "Successful", "Shipped"}


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
        log_webhook_response(
            status="Error",
            raw_payload=kwargs,
            error=frappe.get_traceback(),
        )
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
        log_webhook_response(
            status="Error",
            decrypted_response=plain_response,
            error="Missing order identifier",
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

    log_webhook_response(
        order_id=order_id,
        status=(
            "Success"
            if plain_response.get("order_status") in CCAVENUE_SUCCESS_STATUSES
            else "Failed"
        ),
        decrypted_response=plain_response,
    )

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
            reference_doc = frappe.get_doc(
                transaction_data.reference_doctype,
                transaction_data.reference_docname,
            )
            # An order_id can be a retry of an earlier failed attempt under the
            # same booking (multiple Integration Requests/Event Payments per
            # booking). Hooks on on_payment_authorized have no other way to
            # know which specific attempt this call is about, so flag it -
            # see frappe_koradi_temple's record_online_payment_status.
            reference_doc.flags.payment_gateway_order_id = order_id
            custom_redirect_to = reference_doc.run_method(
                "on_payment_authorized", payment_status
            )
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


def _handle_ccavenue_den_notification(error_log_context):
    """
    Shared handler for CCAvenue Dynamic Event Notification (DEN) endpoints.
    Every DEN event type (Order Status, Order Reconciliation Status, ...) is
    registered as its own URL in the CCAvenue merchant panel, but they all POST
    the same encrypted 'encResp' envelope identified by 'order_id'.
    """
    # 1. Enforce that it is processing incoming server data
    if frappe.request.method != "POST":
        frappe.throw(_("Only POST requests are allowed"), frappe.PermissionError)

    # 2. Fetch the configuration parameters
    ccavenue_config = get_ccavenue_config()

    # CCAvenue sends parameters as form-encoded data in a POST request
    enc_response = frappe.local.form_dict.get("encResp")

    if not enc_response:
        frappe.log_error(
            f"{error_log_context} payload missing 'encResp'", "CCAvenue Webhook Error"
        )
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
        frappe.log_error(
            frappe.get_traceback(), f"{error_log_context} Decryption Failed"
        )
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
            f"{error_log_context} payload missing order_id. Data: {cstr(plain_response)}",
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
        frappe.log_error(
            frappe.get_traceback(), f"{error_log_context} Processing Failed"
        )
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
            "Success"
            if plain_response.get("order_status") in CCAVENUE_SUCCESS_STATUSES
            else "Failed"
        ),
        http_status_code=200,
        decrypted_response=plain_response,
    )
    return "OK"


@frappe.whitelist(allow_guest=True)
def ccavenue_webhook():
    return _handle_ccavenue_den_notification("CCAvenue Webhook")


@frappe.whitelist(allow_guest=True)
def ccavenue_reconciliation_webhook():
    return _handle_ccavenue_den_notification("CCAvenue Reconciliation Webhook")


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
                reference_doc = frappe.get_doc(
                    transaction_data.reference_doctype,
                    transaction_data.reference_docname,
                )
                # An order_id can be a retry of an earlier failed attempt
                # under the same booking (multiple Integration
                # Requests/Event Payments per booking). Hooks on
                # on_payment_authorized have no other way to know which
                # specific attempt this call is about, so flag it - see
                # frappe_koradi_temple's record_online_payment_status.
                reference_doc.flags.payment_gateway_order_id = order_id
                # Trigger hooks attached to standard document processing
                reference_doc.run_method("on_payment_authorized", payment_status)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(), "Webhook Document Hook Exception"
                )

    request.db_set("status", "Completed" if is_success else "Failed")


def get_logged_payment_attempts(order_id: str) -> list[dict]:
    logs = frappe.get_all(
        "Webhook Response Log",
        filters={"order_id": order_id},
        fields=["name", "status", "decrypted_response", "creation"],
        order_by="creation asc",
        ignore_permissions=True,
    )

    attempts = {}
    for log in logs:
        try:
            data = (
                frappe.parse_json(log.decrypted_response)
                if log.decrypted_response
                else {}
            )
        except Exception:
            data = {}
        if not isinstance(data, dict):
            continue
        attempt_key = data.get("tracking_id") or data.get("reference_no") or log.name
        # Keep the most recent log entry seen for a given attempt (tracking_id).
        attempts[attempt_key] = {
            "tracking_id": data.get("tracking_id") or data.get("reference_no"),
            "order_status": data.get("order_status"),
            "amount": data.get("amount") or data.get("order_amt"),
            "bank_ref_no": data.get("bank_ref_no") or data.get("order_bank_ref_no"),
            "payment_mode": data.get("payment_mode"),
            "failure_message": data.get("failure_message"),
            "trans_date": data.get("trans_date") or data.get("order_date_time"),
            "webhook_log": log.name,
            "logged_status": log.status,
        }

    return list(attempts.values())


def get_order_lookup_attempts(order_id: str) -> list[dict]:
    ccavenue_config = get_ccavenue_config()

    if cint(ccavenue_config.sandbox):
        api_url = "https://apitest.ccavenue.com/apis/servlet/DoWebTrans"
    else:
        api_url = "https://api.ccavenue.com/apis/servlet/DoWebTrans"

    creation = get_datetime(
        frappe.db.get_value("Integration Request", order_id, "creation")
        or now_datetime()
    )
    from_date = (creation - timedelta(days=1)).strftime("%d-%m-%Y")
    to_date = (now_datetime() + timedelta(days=1)).strftime("%d-%m-%Y")

    query_params = {
        "order_no": order_id,
        "from_date": from_date,
        "to_date": to_date,
        "page_number": 1,
    }
    plain_text = frappe.as_json(query_params)
    enc_request = encrypt(plain_text, ccavenue_config.working_key)

    payload = {
        "enc_request": enc_request,
        "access_code": ccavenue_config.access_code,
        "command": "orderLookup",
        "request_type": "JSON",
        "response_type": "JSON",
        "version": "1.2",
    }

    try:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = requests.post(api_url, data=payload, headers=headers, timeout=20)
        if response.status_code != 200:
            return []

        response_dict = dict(parse_qsl(response.text.strip()))
        if "enc_response" not in response_dict:
            return []

        decrypted_str = decrypt(
            response_dict["enc_response"].strip(), ccavenue_config.working_key
        )
        data = frappe.parse_json(decrypted_str)
        if not isinstance(data, dict):
            return []

        orders = data.get("order_Status_List") or []
        if isinstance(orders, dict):
            orders = [orders]
        if not isinstance(orders, list):
            return []

        attempts = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            attempts.append(
                {
                    "tracking_id": order.get("reference_no"),
                    "order_status": order.get("order_status"),
                    "amount": order.get("order_amt"),
                    "bank_ref_no": order.get("order_bank_ref_no"),
                    "payment_mode": order.get("order_option_type"),
                    "trans_date": order.get("order_date_time"),
                    "order_status_date_time": order.get("order_status_date_time"),
                    "source": "ccavenue_order_lookup",
                }
            )
        return attempts
    except Exception:
        frappe.log_error(frappe.get_traceback(), "CCAvenue Order Lookup Failed")
        return []


def _pick_latest_successful_attempt(attempts):
    """Out of a check_payment_status_by_id attempts list, return the successful
    one - the most recently dated if more than one attempt against the same
    order_id succeeded (e.g. a declined retry followed by a real payment).
    """
    successful = [
        a for a in attempts if a.get("order_status") in CCAVENUE_SUCCESS_STATUSES
    ]
    if not successful:
        return None
    if len(successful) == 1:
        return successful[0]

    def attempt_datetime(attempt):
        for key in ("order_status_date_time", "trans_date"):
            value = attempt.get(key)
            if not value:
                continue
            try:
                return get_datetime(value)
            except Exception:
                continue
        return get_datetime("1970-01-01")

    return max(successful, key=attempt_datetime)


@frappe.whitelist()
def check_payment_status_by_id(order_id: str) -> dict:
    if not order_id:
        frappe.throw(_("Please provide a valid Order ID"))

    attempts_by_key = {
        (a.get("tracking_id") or a.get("webhook_log")): a
        for a in get_logged_payment_attempts(order_id)
    }
    for attempt in get_order_lookup_attempts(order_id):
        key = attempt.get("tracking_id") or len(attempts_by_key)
        attempts_by_key[key] = attempt
    attempts = list(attempts_by_key.values())

    ccavenue_config = get_ccavenue_config()

    # Determine correct endpoint based on Environment settings
    if cint(ccavenue_config.sandbox):
        api_url = "https://apitest.ccavenue.com/apis/servlet/DoWebTrans"
    else:
        api_url = "https://api.ccavenue.com/apis/servlet/DoWebTrans"

    # 1. Structure the parameter payload for the API
    query_params = {
        "order_no": order_id,
        "reference_no": "",
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
            if not isinstance(final_status_data, dict):
                final_status_data = {
                    "status": "Error",
                    "message": "Unexpected response shape from CCAvenue",
                }
            final_status_data["attempts"] = attempts

            if final_status_data.get("order_status") not in CCAVENUE_SUCCESS_STATUSES:
                # orderStatusTracker with a blank reference_no reflects
                # whichever attempt CCAvenue considers "latest", which is not
                # necessarily the one that actually succeeded - a declined
                # retry can outrank an earlier successful payment. Trust a
                # confirmed successful attempt (from webhook logs or
                # orderLookup) over that read.
                successful_attempt = _pick_latest_successful_attempt(attempts)
                if successful_attempt:
                    final_status_data["order_status"] = successful_attempt.get(
                        "order_status"
                    )
                    final_status_data["order_bank_ref_no"] = successful_attempt.get(
                        "bank_ref_no"
                    ) or final_status_data.get("order_bank_ref_no")
                    final_status_data["tracking_id"] = successful_attempt.get(
                        "tracking_id"
                    ) or final_status_data.get("tracking_id")
                    final_status_data["reference_no"] = final_status_data["tracking_id"]
                    final_status_data["order_status_date_time"] = (
                        successful_attempt.get("order_status_date_time")
                        or successful_attempt.get("trans_date")
                        or final_status_data.get("order_status_date_time")
                    )

            return final_status_data
        else:
            frappe.log_error(
                f"CCAvenue status tracking raw failure: {response_text}",
                "CCAvenue API Error",
            )
            return {
                "status": "Error",
                "message": "No encrypted response returned by CCAvenue",
                "attempts": attempts,
            }

    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "CCAvenue Status Query Exception Failed"
        )
        return {
            "status": "Error",
            "message": "Failed to connect or decrypt payload",
            "attempts": attempts,
        }


CCAVENUE_MAX_VERIFICATION_ATTEMPTS = 6


def verify_pending_payments():
    frappe.error_log(
        "CCAvenue verify_pending_payments started", "CCAvenue Payment Verification"
    )
    pending_requests = frappe.get_all(
        "Integration Request",
        filters={
            "integration_request_service": "CCAvenue",
            "status": ["in", ("Queued", "Authorized")],
            "creation": ["<", add_to_date(now_datetime(), minutes=-10)],
            "modified": [">=", add_to_date(now_datetime(), days=-3)],
        },
        fields=["name", "reference_doctype", "reference_docname"],
        limit=200,
    )

    for request in pending_requests:
        order_id = request.name

        if request.reference_doctype and request.reference_docname:
            already_settled = frappe.db.exists(
                "Integration Request",
                {
                    "reference_doctype": request.reference_doctype,
                    "reference_docname": request.reference_docname,
                    "status": "Completed",
                    "name": ["!=", order_id],
                },
            )
            if already_settled:
                continue

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

        if not order_status:
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
