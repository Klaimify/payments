# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hashlib
from urllib.parse import parse_qsl, urlencode

import frappe
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import (
    call_hook_method,
    cint,
    cstr,
    flt,
    get_request_site_address,
    get_url,
)
from frappe.utils.password import get_decrypted_password

from payments.utils import create_payment_gateway


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
    callback_url = (
        get_request_site_address(True)
        + "/api/method/payments.payment_gateways.doctype.ccavenue_settings.ccavenue_settings.verify_transaction"
    )

    ccavenue_params = {
        "merchant_id": ccavenue_config.merchant_id,
        "order_id": order_id,
        "currency": payment_details.get("currency"),
        "amount": cstr(flt(payment_details.get("amount"), 2)),
        "redirect_url": callback_url,
        "cancel_url": callback_url,
        "language": "EN",
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

    is_success = transaction_response.get("order_status") == "Success"
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
