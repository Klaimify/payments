"""Paytm Wireless POS (ECR) — settings, client, and controller.

import from this module

Flow -->
-----------------
On a terminal-successful sale this module calls, on the reference document::

    reference_doc.run_method("on_payment_authorized", "Completed")

and stashes a gateway-neutral summary at ``Integration Request.data["result"]``
(keys: ``payment_method``, ``acquirement_id``, ``acquiring_bank``, ``amount``,
``transaction_datetime``).  On a terminal failure / expiry / void it calls::

    reference_doc.run_method("on_payment_failed", "<reason>")

"""

import json
import re
from datetime import datetime

import frappe
import requests
from frappe import _
from frappe.model.document import Document
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


def _generate_checksum(body: dict, key: str) -> str:
	"""Generate Paytm checksum"""
	return generateSignature(body, key)


def verify_checksum(body: dict, key: str, checksum: str) -> bool:
	"""Verify Paytm checksum. Official lib raises on corrupt checksums.

	Reserved for an inbound EDC callback endpoint — not used by the poll flow.
	"""
	try:
		return verifySignature(body, key, checksum)
	except Exception:
		return False


def _now_string() -> str:
	return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _generate_merchant_txn_id(reference_docname: str) -> str:
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


def _checksum_body(body: dict) -> dict:
	"""Flat dict for checksum — skip nested dicts/lists, stringify the rest.

	Paytm's lib lower-cases every value.
	"""
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


def _normalize_sale_body(response: dict) -> dict:
	"""Gateway-neutral summary of a successful Sale/Status response body."""
	body = response.get("body") or {}
	return {
		"payment_method": body.get("payMethod"),
		"acquirement_id": body.get("acquirementId"),
		"acquiring_bank": body.get("acquiringBank"),
		"amount": body.get("transactionAmount"),
		"transaction_datetime": body.get("transactionDateTime"),
		"result_msg": (body.get("resultInfo") or {}).get("resultMsg"),
	}


# ---------------------------------------------------------------------------
# Gateway API calls
# ---------------------------------------------------------------------------


def sale_request(
	amount_paise: int,
	reference_docname: str,
	merchant_transaction_id: str,
	terminal_name: str | None = None,
	payment_mode: str = "ALL",
	reference_no: str | None = None,
) -> dict:
	"""Send a payment request to the POS terminal.

	``merchant_transaction_id`` MUST be supplied by the caller and stored so
	the later Status Enquiry queries the exact same id.
	"""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"transactionDateTime": _now_string(),
		"merchantTransactionId": merchant_transaction_id,
		"transactionAmount": str(amount_paise),
	}
	if reference_no:
		body["merchantReferenceNo"] = re.sub(r"[^A-Za-z0-9]", "", reference_no)[:32]
	if payment_mode and payment_mode != "ALL":
		body["merchantExtendedInfo"] = {"paymentMode": payment_mode}
	timeout_val = int(config.get("timeout_configuration") or 0)
	if timeout_val:
		body["timeoutConfig"] = timeout_val

	head = _build_head(config, body)
	response = _call(config["sale_endpoint"], head, body)
	_logger.info("Paytm POS Sale %s -> %s", merchant_transaction_id, _result(response)["result_status"])
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


def result_status(result: dict, context: str = "sale") -> str:
	"""Normalized Sale/Status/Void status: 'success', 'failed', 'pending', 'expired'.

	``context`` — "sale" (default, also used for Void) or "status".  Maps the
	Paytm ECR "Response Codes & Messages" tables.  Every rejection there carries
	resultStatus FAIL / FAILED; in a Status Enquiry only 0000 / SUCCESS is a
	settled payment, while 0009 / ACCEPTED_SUCCESS there still means "not final".
	"""
	info = (result.get("body") or {}).get("resultInfo") or {}
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


def refund_result_status(result: dict) -> str:
	"""Normalized Refund / Refund-Status result: 'success', 'failed', 'pending'.

	resultStatus is authoritative (TXN_SUCCESS / PENDING / TXN_FAILURE); a few
	codes are overridden because Paytm labels them TXN_FAILURE while the intent
	is otherwise (629 = already refunded, 628 = pending at bank).
	"""
	info = (result.get("body") or {}).get("resultInfo") or {}
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


def result_message(result: dict) -> str:
	return ((result.get("body") or {}).get("resultInfo") or {}).get("resultMsg") or ""


def refund_request(paytm_order_id: str, paytm_txn_id: str, ref_id: str, refund_amount: str) -> dict:
	"""Initiate a refund for a successful transaction.

	``paytm_order_id`` is the Sale's ``merchantTransactionId`` (Paytm treats it
	as the orderId for ECR), ``paytm_txn_id`` is Paytm's own transaction id
	(``acquirementId`` from the Status response), ``ref_id`` is a unique
	merchant-generated refund reference.
	"""
	config = get_paytm_pos_config()

	body = {
		"mid": config["merchant_id"],
		"txnType": "REFUND",
		"orderId": paytm_order_id,
		"txnId": paytm_txn_id,
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
	_logger.info("Paytm POS Refund %s -> %s", paytm_order_id, _result(response)["result_status"])
	return response


def refund_status(paytm_order_id: str, ref_id: str) -> dict:
	"""Check status of a refund request."""
	config = get_paytm_pos_config()

	body = {
		"mid": config["merchant_id"],
		"orderId": paytm_order_id,
		"refId": ref_id,
	}
	head = {
		"requestTimeStamp": _now_string(),
		"channelId": config["channel_id"],
		"checksum": _generate_checksum(_checksum_body(body), config["merchant_key"]),
		"version": "1.0",
	}
	response = _call(config["refund_status_endpoint"], head, body)
	_logger.info("Paytm POS Refund Status %s -> %s", paytm_order_id, _result(response)["result_status"])
	return response


# ---------------------------------------------------------------------------
# Integration Request helpers
# ---------------------------------------------------------------------------


def _load_pos_ir(order_id: str, *, service: str = IR_SERVICE):
	"""Load an Integration Request and assert it belongs to this gateway."""
	ir = frappe.get_doc("Integration Request", order_id)
	if ir.integration_request_service != service:
		frappe.throw(_("Integration Request {0} is not a {1} request").format(order_id, service))
	return ir


def _ir_age_minutes(ir) -> float:
	return (now_datetime() - get_datetime(ir.creation)).total_seconds() / 60.0


def _check_pos_permission():
	if not frappe.has_permission("PayTM POS Settings", "write"):
		frappe.throw(_("You are not permitted to operate the Paytm POS terminal"), frappe.PermissionError)


def _to_paise(amount) -> int:
	paise = int(round(flt(amount) * 100))
	if paise <= 0:
		frappe.throw(_("Payment amount must be greater than zero"))
	return paise


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
			if not row.enabled:
				continue
			terminal_id = (row.terminal_id or "").strip()
			if not terminal_id:
				frappe.throw(_("Terminal ID is required for enabled POS Devices"))
			if terminal_id in seen_terminal_ids:
				frappe.throw(_("Terminal ID {0} is duplicated in POS Devices").format(terminal_id))
			seen_terminal_ids.add(terminal_id)

	# -- internal -----------------------------------------------------------

	def _require_api(self, api_name: str):
		"""Throw if the given API toggle is off."""
		if not is_api_enabled(api_name):
			frappe.throw(_("Paytm POS {0} API is disabled").format(api_name.replace("_", " ").title()))

	def _run_success_bridge(self, ir):
		"""Call ``on_payment_authorized`` on the reference document."""
		self._run_reference_hook(ir, "on_payment_authorized", "Completed")

	def _run_failure_bridge(self, ir, reason: str):
		"""Call ``on_payment_failed`` on the reference document."""
		self._run_reference_hook(ir, "on_payment_failed", reason)

	@staticmethod
	def _run_reference_hook(ir, method: str, arg: str):
		if not (ir.reference_doctype and ir.reference_docname):
			return
		if not frappe.db.exists(ir.reference_doctype, ir.reference_docname):
			return
		try:
			ref_doc = frappe.get_doc(ir.reference_doctype, ir.reference_docname)
			ref_doc.run_method(method, arg)
		except Exception:
			frappe.log_error(f"PayTM POS {method} bridge failed for {ir.name}")

	def _mark_ir(self, ir_name: str, status: str, *, output=None, error=None, data=None):
		updates = {"status": status}
		if output is not None:
			updates["output"] = frappe.as_json(output)
		if error is not None:
			updates["error"] = error if isinstance(error, str) else frappe.as_json(error)
		if data is not None:
			updates["data"] = frappe.as_json(data)
		frappe.db.set_value("Integration Request", ir_name, updates, update_modified=False)
		frappe.db.commit()

	# -- sale -------------------------------------------------------------

	def start_sale(
		self,
		amount_paise: int,
		reference_doctype: str,
		reference_docname: str,
		payment_key: str | None = None,
		terminal: str | None = None,
		payment_mode: str = "ALL",
	) -> dict:
		"""Create an Integration Request, fire the POS terminal, return status.

		Returns ``{order_id, merchant_txn_id, status}`` where *status* is one of
		``success`` (terminal accepted the request — NOT yet paid), ``pending``,
		``failed``.  Resolve the real outcome via :meth:`poll_sale`.
		"""
		self._require_api("sale_api")

		# Exact terminal + txn id now and persist them, so Status
		# Enquiry later queries with the identical paytmTid / merchantTransactionId.
		terminal_row = get_terminal(terminal)
		paytm_tid = terminal_row["terminal_id"]
		merchant_txn_id = _generate_merchant_txn_id(reference_docname)

		# Raw Logging
		data = {
			"merchantTransactionId": merchant_txn_id,
			"amount": amount_paise,
			"terminal_id": paytm_tid,
			"payment_mode": payment_mode,
			"payment_gateway": IR_SERVICE,
			"payment": payment_key,
			"request": None,
		}

		# Persist the IR before touching the terminal so a crash mid-call still
		# leaves a durable record to reconcile.
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"is_remote_request": 1,
				"integration_request_service": IR_SERVICE,
				"request_description": IR_DESCRIPTION_SALE,
				"status": "Queued",
				"reference_doctype": reference_doctype,
				"reference_docname": reference_docname,
				"data": frappe.as_json(data),
			}
		)
		ir.insert(ignore_permissions=True)
		frappe.db.commit()

		try:
			response = sale_request(
				amount_paise,
				reference_docname,
				merchant_txn_id,
				paytm_tid,
				payment_mode,
				payment_key or reference_docname,
			)
		except Exception:
			self._mark_ir(ir.name, "Failed", output={"error": frappe.get_traceback()})
			self._run_failure_bridge(ir, _("Could not reach the POS terminal"))
			raise

		data["request"] = response.get("_request")
		status = result_status(response)
		ir_status = {"success": "Authorized", "pending": "Queued"}.get(status, "Failed")
		self._mark_ir(ir.name, ir_status, output=response, data=data)

		if ir_status == "Failed":
			self._run_failure_bridge(ir, result_message(response) or _("Sale request rejected"))

		return {"order_id": ir.name, "merchant_txn_id": merchant_txn_id, "status": status}

	def poll_sale(self, order_id: str) -> str:
		"""Query the gateway, advance the IR, run the success/failure bridge.

		Returns the normalised status string (``success``/``failed``/``expired``
		/``pending``).  Idempotent once the IR is in a terminal state.
		"""
		ir = _load_pos_ir(order_id)
		if ir.status in ("Completed", "Cancelled", "Failed"):
			return {"Completed": "success", "Cancelled": "cancelled", "Failed": "failed"}[ir.status]

		data = frappe.parse_json(ir.data) if ir.data else {}
		merchant_txn_id = data.get("merchantTransactionId")
		terminal_id = data.get("terminal_id")
		if not merchant_txn_id:
			frappe.throw(_("Integration Request {0} has no merchantTransactionId").format(order_id))

		response = status_enquiry(merchant_txn_id, terminal_id)
		status = result_status(response, context="status")
		data["request"] = response.get("_request")

		if status == "success":
			data["result"] = _normalize_sale_body(response)
			self._mark_ir(ir.name, "Completed", output=response, data=data)
			self._run_success_bridge(ir)
		elif status == "failed":
			self._mark_ir(ir.name, "Failed", output=response, data=data)
			self._run_failure_bridge(ir, result_message(response) or _("Payment failed at terminal"))
		elif status == "expired":
			# 0404 means terminal doesnot
			# registered the sale yet — keep waiting.
			if _ir_age_minutes(ir) < SALE_GRACE_MINUTES:
				return "pending"
			self._mark_ir(ir.name, "Failed", output=response, data=data)
			self._run_failure_bridge(ir, _("Transaction expired or not found at terminal"))
		else:
			# still pending — record the latest probe, keep the IR open
			self._mark_ir(ir.name, ir.status, output=response, data=data)

		return status

	def void_sale(self, order_id: str) -> str:
		"""Void a same-day successful sale. Returns the void status word."""
		self._require_api("void_api")

		if _load_pos_ir(order_id).status == "Cancelled":
			frappe.throw(_("This sale has already been voided"))

		status = self.poll_sale(order_id)
		if status != "success":
			frappe.throw(_("Cannot void: transaction status is {0}, expected success").format(status))

		ir = _load_pos_ir(order_id)
		data = frappe.parse_json(ir.data) if ir.data else {}
		result = data.get("result") or {}
		txn_datetime = result.get("transaction_datetime") or ""
		if txn_datetime and getdate(txn_datetime[:10]) != getdate(nowdate()):
			frappe.throw(_("Cannot void: transaction is not from today"))

		response = void_transaction(data.get("merchantTransactionId"), data.get("terminal_id"))
		void_status = result_status(response)
		data["request"] = response.get("_request")
		data["void_result"] = _result(response)

		if void_status == "success":
			self._mark_ir(ir.name, "Cancelled", output=response, data=data)
			self._run_failure_bridge(ir, _("Payment voided at terminal"))
		else:
			self._mark_ir(ir.name, ir.status, output=response, data=data)

		return void_status

	# -- refund ---------------------------------------------------------

	def do_refund(self, order_id: str, amount, note: str | None = None) -> dict:
		"""Initiate a refund against a completed sale.

		Creates its own Integration Request (service ``PayTM POS Refund``) so a
		sale can carry several refunds.
		Returns ``{refund_order_id, status, message}``.
		"""
		self._require_api("refund_api")

		sale_ir = _load_pos_ir(order_id)
		if sale_ir.status != "Completed":
			frappe.throw(_("Cannot refund: sale {0} is not completed").format(order_id))

		data = frappe.parse_json(sale_ir.data) if sale_ir.data else {}
		result = data.get("result") or {}
		paytm_order_id = data.get("merchantTransactionId")
		paytm_txn_id = result.get("acquirement_id")
		if not (paytm_order_id and paytm_txn_id):
			frappe.throw(_("Cannot refund: Paytm transaction id unknown — poll the sale first"))

		ref_id = re.sub(r"[^A-Za-z0-9]", "", f"{paytm_order_id}R{frappe.generate_hash(length=6)}")[:32]
		refund_amount = f"{flt(amount):.2f}"

		refund_data = {
			"sale_order_id": order_id,
			"paytm_order_id": paytm_order_id,
			"paytm_txn_id": paytm_txn_id,
			"refId": ref_id,
			"amount": refund_amount,
			"note": note,
			"payment_gateway": IR_SERVICE_REFUND,
			"request": None,
		}
		refund_ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"is_remote_request": 1,
				"integration_request_service": IR_SERVICE_REFUND,
				"request_description": IR_DESCRIPTION_REFUND,
				"status": "Queued",
				"reference_doctype": sale_ir.reference_doctype,
				"reference_docname": sale_ir.reference_docname,
				"data": frappe.as_json(refund_data),
			}
		)
		refund_ir.insert(ignore_permissions=True)
		frappe.db.commit()

		try:
			response = refund_request(paytm_order_id, paytm_txn_id, ref_id, refund_amount)
		except Exception:
			self._mark_ir(refund_ir.name, "Failed", output={"error": frappe.get_traceback()})
			raise

		refund_data["request"] = response.get("_request")
		rstatus = refund_result_status(response)
		ir_status = {"success": "Completed", "pending": "Queued"}.get(rstatus, "Failed")
		self._mark_ir(refund_ir.name, ir_status, output=response, data=refund_data)

		return {
			"refund_order_id": refund_ir.name,
			"status": rstatus,
			"message": result_message(response),
		}

	def refund_status_for(self, refund_order_id: str) -> dict:
		"""Poll a refund request and advance its Integration Request."""
		self._require_api("refund_status_api")

		ir = _load_pos_ir(refund_order_id, service=IR_SERVICE_REFUND)
		data = frappe.parse_json(ir.data) if ir.data else {}

		response = refund_status(data.get("paytm_order_id"), data.get("refId"))
		rstatus = refund_result_status(response)
		data["request"] = response.get("_request")

		if ir.status not in ("Completed", "Failed"):
			ir_status = {"success": "Completed", "pending": "Queued"}.get(rstatus, "Failed")
			self._mark_ir(ir.name, ir_status, output=response, data=data)
		else:
			self._mark_ir(ir.name, ir.status, output=response, data=data)

		return {"status": rstatus, "message": result_message(response)}


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def poll_pending_pos_sales():
	"""Scheduler entry: poll unresolved sales; force-fail the stale ones."""
	if not is_api_enabled("status_enquiry_api"):
		return

	irs = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": IR_SERVICE,
			"status": ["in", ["Queued", "Authorized"]],
		},
		fields=["name", "creation"],
	)
	if not irs:
		return

	settings = frappe.get_single("PayTM POS Settings")
	for ir in irs:
		try:
			settings.poll_sale(ir.name)
		except Exception:
			frappe.log_error(f"PayTM POS poll failed for {ir.name}")

		age_minutes = (now_datetime() - get_datetime(ir.creation)).total_seconds() / 60.0
		if age_minutes < SALE_EXPIRY_MINUTES:
			continue

		doc = frappe.get_doc("Integration Request", ir.name)
		if doc.status in ("Queued", "Authorized"):
			frappe.db.set_value(
				"Integration Request",
				doc.name,
				{"status": "Failed", "error": "Expired: no terminal response"},
				update_modified=False,
			)
			frappe.db.commit()
			settings._run_failure_bridge(doc, _("Payment timed out at the POS terminal"))


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


# ---------------------------------------------------------------------------
# Whitelisted entrypoints ---------------------------------------------------------------------------


@frappe.whitelist()
def start_pos_sale(
	amount,
	reference_doctype: str,
	reference_docname: str,
	payment_key: str | None = None,
	terminal: str | None = None,
	payment_mode: str = "ALL",
) -> dict:
	"""Fire a POS Sale. ``amount`` is in the major unit (e.g. rupees)."""
	_check_pos_permission()
	if not frappe.db.exists(reference_doctype, reference_docname):
		frappe.throw(_("{0} {1} not found").format(reference_doctype, reference_docname))
	settings = frappe.get_single("PayTM POS Settings")
	return settings.start_sale(
		_to_paise(amount), reference_doctype, reference_docname, payment_key, terminal, payment_mode
	)


@frappe.whitelist()
def poll_pos_sale(order_id: str) -> str:
	_check_pos_permission()
	return frappe.get_single("PayTM POS Settings").poll_sale(order_id)


@frappe.whitelist()
def void_pos_sale(order_id: str) -> str:
	_check_pos_permission()
	return frappe.get_single("PayTM POS Settings").void_sale(order_id)


@frappe.whitelist()
def refund_pos_sale(order_id: str, amount, note: str | None = None) -> dict:
	_check_pos_permission()
	return frappe.get_single("PayTM POS Settings").do_refund(order_id, amount, note)


@frappe.whitelist()
def pos_refund_status(refund_order_id: str) -> dict:
	_check_pos_permission()
	return frappe.get_single("PayTM POS Settings").refund_status_for(refund_order_id)


@frappe.whitelist()
def get_pos_terminals() -> list[dict]:
	"""All enabled POS Devices as dicts for use in linked fields."""
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
	"""POS configuration (without the merchant key) for frontend use."""
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
