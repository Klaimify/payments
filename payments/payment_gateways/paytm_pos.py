"""Paytm Wireless POS (ECR) API client.

Server-internal helpers for the Sale / Status Enquiry / Void / Refund calls
documented in the Paytm Wireless POS integration guide.
"""

import json
import re
from datetime import datetime

import frappe
import requests
from frappe import _
from paytmchecksum import generateSignature, verifySignature

from payments.payment_gateways.doctype.paytm_pos_settings.paytm_pos_settings import (
	get_paytm_pos_config,
	get_terminal,
)

_logger = frappe.logger("paytm_pos", allow_site=True, max_size=5, file_count=10)


def generate(body: dict, key: str) -> str:
	"""Generate Paytm checksum.
	Paytm's lib sorts keys, joins values with '|', hashes, then AES encrypts.
	"""
	return generateSignature(body, key)


def verify(body: dict, key: str, checksum: str) -> bool:
	"""Verify Paytm checksum. Official lib raises on corrupt checksums."""
	try:
		return verifySignature(body, key, checksum)
	except Exception:
		return False


def _now_string() -> str:
	return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _generate_merchant_txn_id(booking_name: str) -> str:
	"""Unique alphanumeric merchantTransactionId (8-32 chars, no special chars)."""
	alnum = re.sub(r"[^A-Za-z0-9]", "", booking_name)
	ts = datetime.now().strftime("%y%m%d%H%M%S")
	return f"JYOT{alnum}{ts}"[-32:]


def _checksum_body(body: dict) -> dict:
	"""Flat dict for checksum — all values must be strings (.lower() breaks on ints)."""
	return {k: str(v) for k, v in body.items()}


def _build_head(config: dict, body: dict) -> dict:
	return {
		"requestTimeStamp": _now_string(),
		"channelId": config["channel_id"],
		"checksum": generate(_checksum_body(body), config["merchant_key"]),
		"version": "1.0",
	}


def _call(endpoint: str, head: dict, body: dict) -> dict:
	""" Attaches the request payload as ``_request`` key so callers can log it.
	"""
	payload = {"head": head, "body": body}
	try:
		response = requests.post(
			endpoint,
			data=json.dumps(payload),
			headers={"Content-Type": "application/json"},
			timeout=30,
		)
		response.raise_for_status()
		result = response.json()
		result["_request"] = payload
		return result
	except requests.RequestException as exc:
		_logger.error("Paytm POS request to %s failed: %s", endpoint, exc, exc_info=True)
		frappe.throw(_("Could not reach the Paytm POS server. ({0})").format(exc))


def _result(response: dict) -> dict:
	result_info = (response.get("body") or {}).get("resultInfo") or {}
	return {
		"result_status": result_info.get("resultStatus"),
		"result_code": result_info.get("resultCode"),
		"result_code_id": result_info.get("resultCodeId"),
		"result_msg": result_info.get("resultMsg"),
		"body": response.get("body") or {},
	}


def sale_request(
	amount_paise: int,
	booking_name: str,
	terminal_name: str | None = None,
	payment_mode: str = "ALL",
	reference_no: str | None = None,
) -> dict:
	"""Send payment request to POS terminal. amount_paise in paise (integer)."""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"transactionDateTime": _now_string(),
		"merchantTransactionId": _generate_merchant_txn_id(booking_name),
		"transactionAmount": str(amount_paise),
	}
	timeout_val = int(config.get("timeout_configuration") or 0)
	if timeout_val:
		body["timeoutConfig"] = timeout_val

	head = _build_head(config, body)
	response = _call(config["sale_endpoint"], head, body)
	_logger.info("Paytm POS Sale %s -> %s", body["merchantTransactionId"], _result(response)["result_status"])
	return response


def status_enquiry(merchant_transaction_id: str, terminal_name: str | None = None) -> dict:
	"""Fetch status of a Sale or Void transaction."""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"transactionDateTime": _now_string(),
		"merchantTransactionId": merchant_transaction_id,
	}
	head = _build_head(config, body)
	response = _call(config["status_endpoint"], head, body)
	_logger.info("Paytm POS Status %s -> %s", merchant_transaction_id, _result(response)["result_status"])
	return response


def void_transaction(merchant_transaction_id: str, terminal_name: str | None = None) -> dict:
	"""Cancel a same-day successful Sale transaction."""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"merchantTransactionId": merchant_transaction_id,
		"transactionDateTime": _now_string(),
	}
	head = _build_head(config, body)
	response = _call(config["void_endpoint"], head, body)
	_logger.info("Paytm POS Void %s -> %s", merchant_transaction_id, _result(response)["result_status"])
	return response


def result_status(result: dict) -> str:
	"""Normalized status: 'success', 'failed', 'pending' or 'expired'."""
	info = (result.get("body") or {}).get("resultInfo") or {}
	status = (info.get("resultStatus") or "").upper()
	code = (info.get("resultCode") or "").strip()

	if status in ("S", "A", "SUCCESS") or code in ("0000", "0009"):
		return "success"
	if status in ("F", "FAILED") or code in ("0330", "0233", "0007", "0011", "0090", "0180"):
		return "failed"
	if code == "0404":
		return "expired"
	if status in ("P", "U", "PENDING", "UNKNOWN") or code in ("0010", "0030"):
		return "pending"
	return "pending"


def result_message(result: dict) -> str:
	return ((result.get("body") or {}).get("resultInfo") or {}).get("resultMsg") or ""


def refund_request(
	order_id: str,
	txn_id: str,
	ref_id: str,
	refund_amount: str,
) -> dict:
	"""Initiate refund for a successful transaction."""
	config = get_paytm_pos_config()

	body = {
		"mid": config["merchant_id"],
		"txnType": "REFUND",
		"orderId": order_id,
		"txnId": txn_id,
		"refId": ref_id,
		"refundAmount": refund_amount,
	}
	head = {
		"requestTimeStamp": _now_string(),
		"channelId": config["channel_id"],
		"checksum": generate(_checksum_body(body), config["merchant_key"]),
		"version": "1.0",
	}
	response = _call(config["refund_endpoint"], head, body)
	_logger.info("Paytm POS Refund %s -> %s", order_id, _result(response)["result_status"])
	return response


def refund_status(order_id: str, ref_id: str) -> dict:
	"""Check status of a refund request."""
	config = get_paytm_pos_config()

	body = {
		"mid": config["merchant_id"],
		"orderId": order_id,
		"refId": ref_id,
	}
	head = {
		"requestTimeStamp": _now_string(),
		"channelId": config["channel_id"],
		"checksum": generate(_checksum_body(body), config["merchant_key"]),
		"version": "1.0",
	}
	response = _call(config["refund_status_endpoint"], head, body)
	_logger.info("Paytm POS Refund Status %s -> %s", order_id, _result(response)["result_status"])
	return response
