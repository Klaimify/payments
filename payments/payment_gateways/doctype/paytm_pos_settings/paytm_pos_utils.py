"""Paytm Wireless POS — utility helpers."""

import json
import re
from datetime import datetime

import frappe
import requests
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime, nowdate
from frappe.utils.password import get_decrypted_password
from paytmchecksum import generateSignature, verifySignature

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGING_HOST = "https://securegw-stage.paytm.in"
PRODUCTION_HOST = "https://securegw-edc.paytm.in"

STAGING_REFUND_HOST = "https://securestage.paytmpayments.com"
PRODUCTION_REFUND_HOST = "https://secure.paytmpayments.com"

SALE_PATH = "/ecr/payment/request"
STATUS_PATH = "/ecr/V2/payment/status"
VOID_PATH = "/ecr/void"
REFUND_PATH = "/refund/apply"
REFUND_STATUS_PATH = "/v2/refund/status"

IR_SERVICE = "PayTM POS"
IR_SERVICE_REFUND = "PayTM POS Refund"

IR_DESCRIPTION_SALE = "POS Payment (Paytm EDC)"
IR_DESCRIPTION_REFUND = "POS Refund (Paytm EDC)"

# Sale Registers as Failed after SALE_GRACE_MINUTES,
# After Acceptings Request on device
SALE_GRACE_MINUTES = 3
# A Sale still unresolved after this long (terminal never answered) is force
# failed by the scheduler.
SALE_EXPIRY_MINUTES = 15

_logger = frappe.logger("paytm_pos", allow_site=True, max_size=5, file_count=10)


# ---------------------------------------------------------------------------
# Checksum / HTTP helpers
# ---------------------------------------------------------------------------


def generate_checksum(body: dict, key: str) -> str:
	"""Generate Paytm checksum"""
	return generateSignature(body, key)


def verify_checksum(body: dict, key: str, checksum: str) -> bool:
	"""Verify Paytm checksum. Official lib raises on corrupt checksums.

	Reserved for an inbound EDC callback endpoint — not used by flow.
	"""
	try:
		return verifySignature(body, key, checksum)
	except Exception:
		return False


def now_string() -> str:
	return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def generate_merchant_txn_id(reference_docname: str) -> str:
	"""Unique alphanumeric merchantTransactionId (8-32 chars, no special chars).

	``<alnum(reference)><yymmddHHMMSS><rand4>`` — the random suffix guards
	against multiple attempts for the same reference landing in the same second
	"""
	alnum = re.sub(r"[^A-Za-z0-9]", "", reference_docname or "")
	ts = datetime.now().strftime("%y%m%d%H%M%S")
	rand = frappe.generate_hash(length=4)
	txn_id = f"{alnum}{ts}{rand}"[:32]
	if len(txn_id) < 8:
		txn_id = txn_id.ljust(8, "0")
	return txn_id


def checksum_body(body: dict) -> dict:
	"""dict for checksum.

	Paytm's lib lower-cases every value.
	"""
	result = {}
	for k, v in body.items():
		if isinstance(v, dict | list):
			continue
		result[k] = str(v)
	return result


def build_head(config: dict, body: dict) -> dict:
	return {
		"requestTimeStamp": now_string(),
		"channelId": config["channel_id"],
		"checksum": generate_checksum(checksum_body(body), config["merchant_key"]),
		"version": "1.0",
	}


def call(endpoint: str, head: dict, body: dict) -> dict:
	"""POST ``{head, body}`` as JSON. Attaches ``_request`` for logging."""
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
		frappe.throw(_("Could not reach the Paytm POS server. ({0})").format(str(exc)))


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------


def result(response: dict) -> dict:
	result_info = (response.get("body") or {}).get("resultInfo") or {}
	return {
		"result_status": result_info.get("resultStatus"),
		"result_code": result_info.get("resultCode"),
		"result_code_id": result_info.get("resultCodeId"),
		"result_msg": result_info.get("resultMsg"),
		"body": response.get("body") or {},
	}


def normalize_sale_body(response: dict) -> dict:
	"""Gateway summary of a successful Sale/Status response body."""
	body = response.get("body") or {}
	return {
		"payment_method": body.get("payMethod"),
		"acquirement_id": body.get("acquirementId"),
		"acquiring_bank": body.get("acquiringBank"),
		"amount": body.get("transactionAmount"),
		"transaction_datetime": body.get("transactionDateTime"),
		"result_msg": (body.get("resultInfo") or {}).get("resultMsg"),
	}


def result_status(response: dict, context: str = "sale") -> str:
	"""Normalized Sale/Status/Void status: 'success', 'failed', 'pending', 'expired'.

	``context`` — "sale" (default, also used for Void) or "status".  Maps the
	Paytm ECR "Response Codes & Messages" tables.  Every rejection there carries
	resultStatus FAIL / FAILED; in a Status Enquiry only 0000 / SUCCESS is a
	settled payment, while 0009 / ACCEPTED_SUCCESS there still means "not final".
	"""
	info = (response.get("body") or {}).get("resultInfo") or {}
	status = (info.get("resultStatus") or "").upper()
	code = (info.get("resultCode") or "").strip()

	# Merchant txn id not (yet) registered — caller applies a grace period.
	if code == "0404":
		return "expired"
	# Transient server error — retry, never a hard failure.
	if code == "0012":
		return "pending"

	success_statuses = {"S", "SUCCESS"}
	if context != "status":
		success_statuses |= {"A", "ACCEPTED_SUCCESS"}
	if status in success_statuses or code == "0000" or (context != "status" and code == "0009"):
		return "success"

	if status in ("F", "FAIL", "FAILED", "TXN_FAILURE") or code in (
		"0330",
		"0233",
		"0007",
		"0011",
		"0090",
		"0180",
		"0002",
		"0182",
		"0333",
		"1809",
		"1810",
		"0022",
		"0029",
		"9001",
		"0902",
	):
		return "failed"

	if status in ("P", "U", "PENDING", "UNKNOWN") or code in ("0010", "0030"):
		return "pending"
	return "pending"


def refund_result_status(response: dict) -> str:
	"""Normalized Refund / Refund-Status result: 'success', 'failed', 'pending'.

	resultStatus is authoritative (TXN_SUCCESS / PENDING / TXN_FAILURE); a few
	codes are overridden because Paytm labels them TXN_FAILURE while the intent
	is otherwise (629 = already refunded, 628 = pending at bank).
	"""
	info = (response.get("body") or {}).get("resultInfo") or {}
	status = (info.get("resultStatus") or "").upper()
	code = (info.get("resultCode") or "").strip()

	if code == "629":  # "Refund is already Successful"
		return "success"
	if code in ("501", "601", "628", "677"):  # raised / pending at PG or bank
		return "pending"
	if status in ("SUCCESS", "TXN_SUCCESS", "S") or code in ("10", "0000"):
		return "success"
	if status in ("PENDING", "P"):
		return "pending"
	if status in ("TXN_FAILURE", "FAILURE", "FAILED", "F"):
		return "failed"
	return "pending"


def result_message(response: dict) -> str:
	return ((response.get("body") or {}).get("resultInfo") or {}).get("resultMsg") or ""


# ---------------------------------------------------------------------------
# Integration Request helpers
# ---------------------------------------------------------------------------


def load_pos_ir(order_id: str, *, service: str = IR_SERVICE):
	"""Load an Integration Request and assert it belongs to this gateway."""
	ir = frappe.get_doc("Integration Request", order_id)
	if ir.integration_request_service != service:
		frappe.throw(_("Integration Request {0} is not a {1} request").format(order_id, service))
	return ir


def ir_age_minutes(ir) -> float:
	return (now_datetime() - get_datetime(ir.creation)).total_seconds() / 60.0


def check_pos_permission():
	if not frappe.has_permission("PayTM POS Settings", "write"):
		frappe.throw(_("You are not permitted to operate the Paytm POS terminal"), frappe.PermissionError)


def to_paise(amount) -> int:
	paise = int(round(flt(amount) * 100))
	if paise <= 0:
		frappe.throw(_("Payment amount must be greater than zero"))
	return paise


# ---------------------------------------------------------------------------
# Config / discovery
# ---------------------------------------------------------------------------


def get_paytm_pos_config() -> dict:
	"""PayTM POS Settings as a dict: merchant key decrypted, host/endpoints
	resolved from the ``staging`` flag."""
	config = frappe.db.get_singles_dict("PayTM POS Settings")
	config.update(
		{"merchant_key": get_decrypted_password("PayTM POS Settings", "PayTM POS Settings", "merchant_key")}
	)

	host = STAGING_HOST if cint(config.get("staging")) else PRODUCTION_HOST
	refund_host = STAGING_REFUND_HOST if cint(config.get("staging")) else PRODUCTION_REFUND_HOST
	config.update(
		{
			"host": host,
			"sale_endpoint": host + SALE_PATH,
			"status_endpoint": host + STATUS_PATH,
			"void_endpoint": host + VOID_PATH,
			"refund_endpoint": refund_host + REFUND_PATH,
			"refund_status_endpoint": refund_host + REFUND_STATUS_PATH,
		}
	)
	return config


def get_enabled_terminals() -> list[dict]:
	"""Enabled POS Devices rows as a list of dicts."""
	settings = frappe.get_single("PayTM POS Settings")
	return [row.as_dict() for row in settings.pos_devices if row.enabled]


def get_terminal(terminal_name: str | None = None) -> dict:
	"""Resolve the terminal for a request. ``terminal_name`` may match an
	enabled row's ``terminal_name`` or ``terminal_id``; otherwise the first
	enabled terminal is used."""
	terminals = get_enabled_terminals()
	if not terminals:
		frappe.throw(_("No POS terminal configured"))

	if terminal_name:
		for terminal in terminals:
			if terminal["terminal_name"] == terminal_name or terminal["terminal_id"] == terminal_name:
				return terminal
		frappe.throw(_("POS terminal {0} is not enabled or not found").format(terminal_name))

	return terminals[0]


def is_api_enabled(api_name: str) -> bool:
	"""Whether a specific API toggle is on in PayTM POS Settings."""
	config = frappe.db.get_singles_dict("PayTM POS Settings")
	return bool(cint(config.get(api_name)))
