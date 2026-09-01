"""Paytm Wireless POS (ECR) — settings, client, and controller.

Single source of truth for Paytm POS integration.  Consumer apps should only
import from this module.
"""

import json
import re
from datetime import datetime

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate, nowdate
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

_logger = frappe.logger("paytm_pos", allow_site=True, max_size=5, file_count=10)


# ---------------------------------------------------------------------------
# Checksum / HTTP helpers (formerly paytm_pos.py)
# ---------------------------------------------------------------------------


def _generate_checksum(body: dict, key: str) -> str:
	"""Generate Paytm checksum."""
	return generateSignature(body, key)


def verify_checksum(body: dict, key: str, checksum: str) -> bool:
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
	txn_id = f"{alnum}{ts}"
	txn_id = txn_id[:32] if len(txn_id) > 32 else txn_id
	if len(txn_id) < 8:
		txn_id = txn_id.ljust(8, "0")
	return txn_id


def _checksum_body(body: dict) -> dict:
	"""Flat dict for checksum — skip nested dicts/lists, all values must be strings."""
	result = {}
	for k, v in body.items():
		if isinstance(v, dict | list):
			continue
		result[k] = str(v)
	return result


def _build_head(config: dict, body: dict) -> dict:
	return {
		"requestTimeStamp": _now_string(),
		"channelId": config["channel_id"],
		"checksum": _generate_checksum(_checksum_body(body), config["merchant_key"]),
		"version": "1.0",
	}


def _call(endpoint: str, head: dict, body: dict) -> dict:
	"""Attaches the request payload as ``_request`` key so callers can log it."""
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


# ---------------------------------------------------------------------------
# Gateway API calls
# ---------------------------------------------------------------------------


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
	if reference_no:
		body["merchantReferenceNo"] = reference_no
	if payment_mode and payment_mode != "ALL":
		body["merchantExtendedInfo"] = {"paymentMode": payment_mode}
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
		"checksum": _generate_checksum(_checksum_body(body), config["merchant_key"]),
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
		"checksum": _generate_checksum(_checksum_body(body), config["merchant_key"]),
		"version": "1.0",
	}
	response = _call(config["refund_status_endpoint"], head, body)
	_logger.info("Paytm POS Refund Status %s -> %s", order_id, _result(response)["result_status"])
	return response


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class PayTMPOSSettings(Document):
	def validate(self):
		if not (self.merchant_id or "").strip():
			frappe.throw(_("Merchant ID is required"))

		if not (self.channel_id or "").strip():
			frappe.throw(_("Channel ID is required"))

		if not (self.merchant_key or "").strip():
			frappe.throw(_("Merchant Key is required"))

		if self.timeout_configuration:
			if not (1 <= cint(self.timeout_configuration) <= 60):
				frappe.throw(_("Timeout Configuration must be between 1 and 60 seconds"))

		seen_terminal_ids = set()
		for row in self.pos_devices:
			if row.enabled and row.terminal_id in seen_terminal_ids:
				frappe.throw(_("Terminal ID {0} is duplicated in POS Devices").format(row.terminal_id))
			seen_terminal_ids.add(row.terminal_id)

	def _require_api(self, api_name: str):
		"""Throw if the given API toggle is off."""
		if not is_api_enabled(api_name):
			frappe.throw(_("Paytm POS {0} API is disabled").format(api_name.replace("_", " ").title()))

	def start_sale(
		self,
		amount_paise: int,
		reference_doctype: str,
		reference_docname: str,
		payment_key: str | None = None,
		terminal: str | None = None,
		payment_mode: str = "ALL",
	) -> dict:
		"""Create Integration Request, fire POS terminal, return status.

		Returns ``{order_id, merchant_txn_id, status}`` where *status* is one of
		``success``, ``failed``, ``pending``.
		"""
		self._require_api("sale_api")

		merchant_txn_id = _generate_merchant_txn_id(reference_docname)

		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_type": "Remote",
				"integration_request_service": "PayTM POS",
				"status": "Queued",
				"reference_doctype": reference_doctype,
				"reference_docname": reference_docname,
				"data": frappe.as_json(
					{
						"merchantTransactionId": merchant_txn_id,
						"amount": amount_paise,
						"terminal": terminal,
						"payment_mode": payment_mode,
						"payment_gateway": "PayTM POS",
						"payment": payment_key,
					}
				),
			}
		)
		ir.insert(ignore_permissions=True)
		frappe.db.commit()

		try:
			response = sale_request(amount_paise, reference_docname, terminal, payment_mode)
			status = result_status(response)

			body = response.get("body") or {}
			remote_txn_id = body.get("merchantTransactionId") or merchant_txn_id

			ir_status = "Authorized" if status == "success" else "Failed"
			frappe.db.set_value(
				"Integration Request",
				ir.name,
				{
					"status": ir_status,
					"output": frappe.as_json(response),
					"data": frappe.as_json(
						{
							"merchantTransactionId": remote_txn_id,
							"amount": amount_paise,
							"terminal": terminal,
							"payment_mode": payment_mode,
							"payment_gateway": "PayTM POS",
							"payment": payment_key,
						}
					),
				},
				update_modified=False,
			)
			frappe.db.commit()

			return {
				"order_id": ir.name,
				"merchant_txn_id": remote_txn_id,
				"status": status,
			}
		except Exception:
			frappe.db.set_value(
				"Integration Request",
				ir.name,
				{
					"status": "Failed",
					"output": frappe.as_json({"error": frappe.get_traceback()}),
				},
				update_modified=False,
			)
			frappe.db.commit()
			raise

	def poll_sale(self, order_id: str) -> str:
		"""Poll gateway for transaction status, advance IR, run success bridge.

		Returns normalised status string.
		"""
		ir = frappe.get_doc("Integration Request", order_id)
		data = frappe.parse_json(ir.data) if ir.data else {}
		merchant_txn_id = data.get("merchantTransactionId")
		terminal = data.get("terminal")

		if not merchant_txn_id:
			frappe.throw(_("Integration Request {0} has no merchantTransactionId").format(order_id))

		response = status_enquiry(merchant_txn_id, terminal)
		status = result_status(response)

		if status == "success":
			frappe.db.set_value(
				"Integration Request",
				ir.name,
				{"status": "Completed", "output": frappe.as_json(response)},
				update_modified=False,
			)
			frappe.db.commit()
			self._run_success_bridge(ir)
		elif status in ("failed", "expired"):
			frappe.db.set_value(
				"Integration Request",
				ir.name,
				{"status": "Failed", "output": frappe.as_json(response)},
				update_modified=False,
			)
			frappe.db.commit()
		# pending → no state change

		return status

	def void_sale(self, order_id: str) -> str:
		"""Void a same-day successful sale. Must be completed + same-day."""
		status = self.poll_sale(order_id)
		if status != "success":
			frappe.throw(_("Cannot void: transaction status is {0}, expected success").format(status))

		ir = frappe.get_doc("Integration Request", order_id)
		output = frappe.parse_json(ir.output) if ir.output else {}
		txn_datetime = (output.get("body") or {}).get("transactionDateTime") or ""

		if txn_datetime and getdate(txn_datetime[:10]) != getdate(nowdate()):
			frappe.throw(_("Cannot void: transaction is not from today"))

		data = frappe.parse_json(ir.data) if ir.data else {}
		merchant_txn_id = data.get("merchantTransactionId")
		terminal = data.get("terminal")

		response = void_transaction(merchant_txn_id, terminal)
		void_status = result_status(response)

		new_ir_status = "Cancelled" if void_status == "success" else ir.status
		frappe.db.set_value(
			"Integration Request",
			ir.name,
			{"status": new_ir_status, "output": frappe.as_json(response)},
			update_modified=False,
		)
		frappe.db.commit()

		return void_status

	def do_refund(self, order_id: str, amount: str, note: str | None = None) -> dict:
		"""Initiate refund for a completed transaction."""
		self._require_api("refund_api")

		ir = frappe.get_doc("Integration Request", order_id)
		data = frappe.parse_json(ir.data) if ir.data else {}

		response = refund_request(
			order_id,
			data.get("merchantTransactionId", ""),
			data.get("merchantTransactionId", ""),
			str(amount),
		)

		frappe.db.set_value(
			"Integration Request",
			ir.name,
			{"output": frappe.as_json(response)},
			update_modified=False,
		)
		frappe.db.commit()

		return _result(response)

	def refund_status_for(self, order_id: str) -> dict:
		"""Check status of a refund request."""
		self._require_api("refund_status_api")

		ir = frappe.get_doc("Integration Request", order_id)
		data = frappe.parse_json(ir.data) if ir.data else {}

		response = refund_status(order_id, data.get("merchantTransactionId", ""))

		return _result(response)

	def _run_success_bridge(self, ir):
		"""Call on_payment_authorized on the reference document."""
		if not (ir.reference_doctype and ir.reference_docname):
			return
		try:
			ref_doc = frappe.get_doc(ir.reference_doctype, ir.reference_docname)
			if hasattr(ref_doc, "run_method"):
				ref_doc.run_method("on_payment_authorized", "Completed")
		except Exception:
			frappe.log_error(f"PayTM POS success bridge failed for {ir.name}")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def poll_pending_pos_sales():
	"""Scheduler entry: poll all IRs stuck in Queued/Authorized."""
	if not is_api_enabled("status_enquiry_api"):
		return

	irs = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": "PayTM POS",
			"status": ["in", ["Queued", "Authorized"]],
		},
		pluck="name",
	)

	settings = frappe.get_single("PayTM POS Settings")
	for ir_name in irs:
		try:
			settings.poll_sale(ir_name)
		except Exception:
			frappe.log_error(f"PayTM POS poll failed for {ir_name}")


# ---------------------------------------------------------------------------
# Config / discovery
# ---------------------------------------------------------------------------


def get_paytm_pos_config() -> dict:
	"""Return the PayTM POS Settings as a dict with the merchant key decrypted and
	the host/endpoints resolved from the staging flag."""
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
	"""Return the enabled POS Devices rows as a list of dicts."""
	settings = frappe.get_single("PayTM POS Settings")
	return [row.as_dict() for row in settings.pos_devices if row.enabled]


def get_terminal(terminal_name: str | None = None) -> dict:
	"""Resolve the terminal to use for a Sale request. If terminal_name is given it
	must match an enabled row's name or terminal_id; otherwise the first enabled terminal is used."""
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
	"""Check if a specific API is enabled in PayTM POS Settings."""
	config = frappe.db.get_singles_dict("PayTM POS Settings")
	return bool(cint(config.get(api_name)))


@frappe.whitelist()
def get_pos_terminals() -> list[dict]:
	"""Return all POS Devices as a list of dicts for use in linked fields."""
	terminals = get_enabled_terminals()
	return [
		{
			"terminal_id": t.get("terminal_id"),
			"terminal_name": t.get("terminal_name"),
			"enabled": t.get("enabled"),
		}
		for t in terminals
	]


@frappe.whitelist()
def get_pos_config() -> dict:
	"""Return POS configuration (without sensitive key) for frontend use."""
	config = frappe.db.get_singles_dict("PayTM POS Settings")
	return {
		"merchant_id": config.get("merchant_id"),
		"channel_id": config.get("channel_id"),
		"staging": cint(config.get("staging")),
		"sale_api": cint(config.get("sale_api")),
		"status_enquiry_api": cint(config.get("status_enquiry_api")),
		"void_api": cint(config.get("void_api")),
		"refund_api": cint(config.get("refund_api")),
		"refund_status_api": cint(config.get("refund_status_api")),
		"terminals": get_enabled_terminals(),
	}
