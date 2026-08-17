"""Paytm Wireless POS (ECR) client.

Implements the Sale / Status Enquiry / Void APIs for Paytm's wireless POS
(EDC) integration. See:
https://www.paytmpayments.com/docs/pos-wireless-connection-overview

Every request carries a `head` block (metadata + checksum) and a `body`
block (transaction data). The checksum is generated over the JSON-serialized
request body using the standard Paytm checksum utility, matching how Paytm
validates the request server-side.
"""

import json
import re
from datetime import datetime

import frappe
import requests
from frappe import _
from paytmchecksum import generateSignature

from payments.payment_gateways.doctype.paytm_pos_settings.paytm_pos_settings import (
	get_paytm_pos_config,
	get_terminal,
)

_logger = frappe.logger("paytm_pos", allow_site=True, max_size=5, file_count=10)


def _now_string() -> str:
	return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _generate_merchant_txn_id(booking_name: str) -> str:
	"""Unique alphanumeric merchant transaction ID, 8-32 chars, no special chars.

	Generated once per transaction attempt and never reused across retries.
	"""
	alnum = re.sub(r"[^A-Za-z0-9]", "", booking_name)
	ts = datetime.now().strftime("%y%m%d%H%M%S")
	return f"JYOT{alnum}{ts}"[-32:]


def _build_head(config: dict, body: dict) -> dict:
	checksum = generateSignature(json.dumps(body, separators=(",", ":")), config["merchant_key"])
	return {
		"requestTimeStamp": _now_string(),
		"channelId": config["channel_id"],
		"checksum": checksum,
		"version": "1.0",
	}


def _call(endpoint: str, head: dict, body: dict) -> dict:
	payload = {"head": head, "body": body}
	try:
		response = requests.post(endpoint, json=payload, timeout=30)
		response.raise_for_status()
	except requests.exceptions.RequestException as exc:
		_logger.error("Paytm POS call to %s failed: %s", endpoint, exc, exc_info=True)
		frappe.throw(
			_("Could not reach the Paytm POS server. Please try again. ({0})").format(str(exc)),
			frappe.ValidationError,
		)

	try:
		result = response.json()
	except ValueError:
		_logger.error("Paytm POS non-JSON response from %s: %s", endpoint, response.text)
		frappe.throw(_("Unexpected response from the Paytm POS server."), frappe.ValidationError)

	_logger.info("Paytm POS %s -> %s", endpoint, frappe.as_json(result))
	return result


def _result_info(result: dict) -> dict:
	return ((result or {}).get("body") or {}).get("resultInfo") or {}


def sale_request(
	amount_paise: int,
	booking_name: str,
	terminal_name: str | None = None,
	payment_mode: str = "ALL",
	reference_no: str | None = None,
) -> dict:
	"""Send a payment request to a Paytm POS terminal.

	Returns a dict with the built `request` and the raw Paytm `response`.
	Use `result_status()` to interpret the outcome.
	"""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"transactionDateTime": _now_string(),
		"merchantTransactionId": _generate_merchant_txn_id(booking_name),
		"merchantReferenceNo": reference_no or booking_name,
		"transactionAmount": str(amount_paise),
		"merchantExtendedInfo": {"paymentMode": payment_mode},
	}
	if config.get("timeout_configuration"):
		body["timeoutConfig"] = int(config["timeout_configuration"])

	head = _build_head(config, body)
	return {
		"request": {"head": head, "body": body},
		"response": _call(config["sale_endpoint"], head, body),
	}


def status_enquiry(merchant_transaction_id: str, terminal_name: str | None = None) -> dict:
	"""Query the status of a sale/void transaction. Returns the raw Paytm response."""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"transactionDateTime": _now_string(),
		"merchantTransactionId": merchant_transaction_id,
	}
	head = _build_head(config, body)
	return {
		"request": {"head": head, "body": body},
		"response": _call(config["status_endpoint"], head, body),
	}


def void_transaction(merchant_transaction_id: str, terminal_name: str | None = None) -> dict:
	"""Cancel a successful same-day sale transaction. Returns the raw Paytm response."""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"merchantTransactionId": merchant_transaction_id,
		"transactionDateTime": _now_string(),
	}
	head = _build_head(config, body)
	return {
		"request": {"head": head, "body": body},
		"response": _call(config["void_endpoint"], head, body),
	}


def result_status(result: dict) -> str:
	"""Normalized result status: 'success', 'failed', 'pending' or 'expired'."""
	info = _result_info(result)
	status = (info.get("resultStatus") or "").upper()
	code = (info.get("resultCode") or "").strip()

	if status in ("S", "A") or code in ("0000", "0009"):
		return "success"
	if status == "F" or code in ("0330", "0233", "0007", "0011", "0090", "0180"):
		return "failed"
	if code == "0404":
		return "expired"
	if status in ("P", "U") or code in ("0010", "0030"):
		return "pending"
	return "pending"


def result_message(result: dict) -> str:
	return _result_info(result).get("resultMsg") or ""