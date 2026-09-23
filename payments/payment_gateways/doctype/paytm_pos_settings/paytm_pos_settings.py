"""Paytm Wireless POS (ECR) — controller and entry-points.

See Utils -> `paytm_pos_utils.py`

Flow -->
-----------------
On a terminal-successful sale this module calls, on the reference document::

    reference_doc.run_method("on_payment_authorized", "Completed")

and stashes a gateway-neutral summary at ``Integration Request.data["result"]``
(keys: ``payment_method``, ``acquirement_id``, ``acquiring_bank``, ``amount``,
``transaction_datetime``).  On a terminal failure / expiry / void it calls::

    reference_doc.run_method("on_payment_failed", "<reason>")

"""

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime, nowdate

from payments.payment_gateways.doctype.paytm_pos_settings.paytm_pos_utils import (
	IR_DESCRIPTION_REFUND,
	IR_DESCRIPTION_REFUND_STATUS,
	IR_DESCRIPTION_SALE,
	IR_DESCRIPTION_STATUS,
	IR_DESCRIPTION_VOID,
	IR_SERVICE,
	IR_SERVICE_REFUND,
	IR_SERVICE_REFUND_STATUS,
	IR_SERVICE_STATUS,
	SALE_EXPIRY_MINUTES,
	SALE_GRACE_MINUTES,
	_logger,
	build_head,
	call,
	check_pos_permission,
	checksum_body,
	generate_checksum,
	generate_merchant_txn_id,
	get_enabled_terminals,
	get_merchant_key,
	get_paytm_pos_config,
	get_terminal,
	ir_age_minutes,
	is_api_enabled,
	load_pos_ir,
	normalize_sale_body,
	now_string,
	refund_result_status,
	result,
	result_message,
	result_status,
	to_paise,
)

# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class PayTMPOSSettings(Document):
	def validate(self):
		if not (self.merchant_id or "").strip():
			frappe.throw(_("Merchant ID is required"))

		if not (self.channel_id or "").strip():
			frappe.throw(_("Channel ID is required"))

		if not self.multi_branch_setup:
			if not (self.get_password("merchant_key", raise_exception=False) or "").strip():
				frappe.throw(_("Merchant Key is required"))
		else:
			for row in self.pos_devices:
				if (
					row.enabled
					and not (row.get_password("merchant_key", raise_exception=False) or "").strip()
				):
					frappe.throw(
						_("Merchant Key is required for enabled POS Device '{0}'").format(
							row.terminal_name or row.terminal_id
						)
					)

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
		frappe.db.commit()  # nosemgrep — POS: persist IR status before bridge call

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

		terminal_row = get_terminal(terminal)
		paytm_tid = terminal_row["terminal_id"]
		merchant_txn_id = generate_merchant_txn_id(reference_docname)

		data = {
			"merchantTransactionId": merchant_txn_id,
			"amount": amount_paise,
			"terminal_id": paytm_tid,
			"payment_mode": payment_mode,
			"payment_gateway": IR_SERVICE,
			"payment": payment_key,
			"request": None,
		}

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
		frappe.db.commit()  # nosemgrep — POS: persist IR before terminal call

		try:
			response = _sale_request(
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
		status = result_status(response, context="status")
		ir_status = {"success": "Authorized", "pending": "Queued"}.get(status, "Failed")
		self._mark_ir(ir.name, ir_status, output=response, data=data)

		if ir_status == "Failed":
			self._run_failure_bridge(ir, result_message(response) or _("Sale request rejected"))

		return {"order_id": ir.name, "merchant_txn_id": merchant_txn_id, "status": status}

	def poll_sale(self, order_id: str) -> str:
		"""Query the gateway via Status Enquiry API, advance the IR, run success/failure bridge.

		Maintains a single dedicated Status Enquiry Integration Request per sale,
		updating it in-place across subsequent poll attempts with incremented poll numbers
		(e.g., 'POS Status Enquiry #1', 'POS Status Enquiry #2').
		"""
		ir = load_pos_ir(order_id)
		if ir.status in ("Completed", "Cancelled", "Failed"):
			return {"Completed": "success", "Cancelled": "cancelled", "Failed": "failed"}[ir.status]

		data = frappe.parse_json(ir.data) if ir.data else {}
		merchant_txn_id = data.get("merchantTransactionId")
		terminal_id = data.get("terminal_id")
		if not merchant_txn_id:
			frappe.throw(_("Integration Request {0} has no merchantTransactionId").format(order_id))

		# Find existing Status Enquiry IR or create the first one for this sale attempt
		existing_status_ir_name = frappe.db.get_value(
			"Integration Request",
			{
				"reference_doctype": ir.reference_doctype,
				"reference_docname": ir.reference_docname,
				"integration_request_service": IR_SERVICE_STATUS,
			},
			"name",
			order_by="creation desc",
		)

		if existing_status_ir_name:
			status_ir = frappe.get_doc("Integration Request", existing_status_ir_name)
			status_data = frappe.parse_json(status_ir.data) if status_ir.data else {}
			poll_count = cint(status_data.get("poll_count") or 1) + 1
		else:
			poll_count = 1
			status_data = {
				"sale_order_id": order_id,
				"merchantTransactionId": merchant_txn_id,
				"terminal_id": terminal_id,
				"payment_gateway": IR_SERVICE_STATUS,
			}
			status_ir = frappe.get_doc(
				{
					"doctype": "Integration Request",
					"is_remote_request": 1,
					"integration_request_service": IR_SERVICE_STATUS,
					"status": "Queued",
					"reference_doctype": ir.reference_doctype,
					"reference_docname": ir.reference_docname,
				}
			)

		status_data["poll_count"] = poll_count
		status_data["request"] = None
		description = f"{IR_DESCRIPTION_STATUS} #{poll_count}"

		status_ir.request_description = description
		status_ir.data = frappe.as_json(status_data)
		if status_ir.is_new():
			status_ir.insert(ignore_permissions=True)
		else:
			status_ir.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep — POS: persist status enquiry IR before API call

		try:
			response = _status_enquiry(merchant_txn_id, terminal_id)
		except Exception:
			self._mark_ir(status_ir.name, "Failed", output={"error": frappe.get_traceback()})
			raise

		status = result_status(response, context="status")
		status_data["request"] = response.get("_request")
		data["request"] = response.get("_request")

		status_ir_status = {"success": "Completed", "pending": "Queued"}.get(status, "Failed")
		self._mark_ir(status_ir.name, status_ir_status, output=response, data=status_data)

		if status == "success":
			data["result"] = normalize_sale_body(response)
			self._mark_ir(ir.name, "Completed", output=response, data=data)
			self._run_success_bridge(ir)
		elif status == "failed":
			self._mark_ir(ir.name, "Failed", output=response, data=data)
			self._run_failure_bridge(ir, result_message(response) or _("Payment failed at terminal"))
		elif status == "expired":
			if ir_age_minutes(ir) < SALE_GRACE_MINUTES:
				return "pending"
			self._mark_ir(ir.name, "Failed", output=response, data=data)
			self._run_failure_bridge(ir, _("Transaction expired or not found at terminal"))
		else:
			self._mark_ir(ir.name, ir.status, output=response, data=data)

		return status

	def void_sale(self, order_id: str) -> str:
		"""Void a same-day successful sale. Returns the void status word.

		Creates a dedicated Integration Request (service ``PayTM POS``,
		description ``POS Payment VOID``) so the void call is logged
		separately from the original sale."""
		self._require_api("void_api")

		if load_pos_ir(order_id).status == "Cancelled":
			frappe.throw(_("This sale has already been voided"))

		status = self.poll_sale(order_id)
		if status not in ("success"):
			frappe.throw(_("Cannot void: transaction status is {0}, expected success").format(status))

		sale_ir = load_pos_ir(order_id)
		sale_data = frappe.parse_json(sale_ir.data) if sale_ir.data else {}
		txn_data = sale_data.get("result") or {}
		txn_datetime = txn_data.get("transaction_datetime") or ""
		if txn_datetime and getdate(txn_datetime[:10]) != getdate(nowdate()):
			frappe.throw(_("Cannot void: transaction is not from today"))

		merchant_txn_id = sale_data.get("merchantTransactionId")
		terminal_id = sale_data.get("terminal_id")

		# Create a dedicated Integration Request for the void call
		void_data = {
			"sale_order_id": order_id,
			"merchantTransactionId": merchant_txn_id,
			"terminal_id": terminal_id,
			"payment_gateway": IR_SERVICE,
			"request": None,
		}
		void_ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"is_remote_request": 1,
				"integration_request_service": IR_SERVICE,
				"request_description": IR_DESCRIPTION_VOID,
				"status": "Queued",
				"reference_doctype": sale_ir.reference_doctype,
				"reference_docname": sale_ir.reference_docname,
				"data": frappe.as_json(void_data),
			}
		)
		void_ir.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep — POS void: persist void IR before API call

		try:
			response = _void_transaction(merchant_txn_id, terminal_id)
		except Exception:
			self._mark_ir(void_ir.name, "Failed", output={"error": frappe.get_traceback()})
			raise

		void_status = result_status(response)
		void_data["request"] = response.get("_request")
		void_data["void_result"] = result(response)

		ir_status = {"success": "Completed"}.get(void_status, "Failed")
		self._mark_ir(void_ir.name, ir_status, output=response, data=void_data)

		return void_status

	# -- refund ---------------------------------------------------------

	def do_refund(self, order_id: str, amount, note: str | None = None) -> dict:
		"""Initiate a refund against a completed sale.

		Creates its own Integration Request (service ``PayTM POS Refund``) so a
		sale can carry several refunds.
		Returns ``{refund_order_id, status, message}``.
		"""
		self._require_api("refund_api")

		sale_ir = load_pos_ir(order_id)
		if sale_ir.status != "Completed":
			frappe.throw(_("Cannot refund: sale {0} is not completed").format(order_id))

		data = frappe.parse_json(sale_ir.data) if sale_ir.data else {}
		txn_data = data.get("result") or {}
		paytm_order_id = data.get("merchantTransactionId")
		paytm_txn_id = txn_data.get("acquirement_id")
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
		frappe.db.commit()  # nosemgrep — POS: persist refund IR before API call

		terminal_id = data.get("terminal_id")

		try:
			response = _refund_request(
				paytm_order_id, paytm_txn_id, ref_id, refund_amount, terminal_name=terminal_id
			)
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
		"""Poll a refund request via Refund Status API and advance its Integration Request."""
		self._require_api("refund_status_api")

		ir = load_pos_ir(refund_order_id, service=IR_SERVICE_REFUND)
		data = frappe.parse_json(ir.data) if ir.data else {}

		paytm_order_id = data.get("paytm_order_id")
		ref_id = data.get("refId")

		# Find existing Refund Status Enquiry IR or create the first one for this refund attempt
		existing_status_ir_name = frappe.db.get_value(
			"Integration Request",
			{
				"reference_doctype": ir.reference_doctype,
				"reference_docname": ir.reference_docname,
				"integration_request_service": IR_SERVICE_REFUND_STATUS,
			},
			"name",
			order_by="creation desc",
		)

		if existing_status_ir_name:
			status_ir = frappe.get_doc("Integration Request", existing_status_ir_name)
			status_data = frappe.parse_json(status_ir.data) if status_ir.data else {}
			poll_count = cint(status_data.get("poll_count") or 1) + 1
		else:
			poll_count = 1
			status_data = {
				"refund_order_id": refund_order_id,
				"paytm_order_id": paytm_order_id,
				"refId": ref_id,
				"payment_gateway": IR_SERVICE_REFUND_STATUS,
			}
			status_ir = frappe.get_doc(
				{
					"doctype": "Integration Request",
					"is_remote_request": 1,
					"integration_request_service": IR_SERVICE_REFUND_STATUS,
					"status": "Queued",
					"reference_doctype": ir.reference_doctype,
					"reference_docname": ir.reference_docname,
				}
			)

		status_data["poll_count"] = poll_count
		status_data["request"] = None
		description = f"{IR_DESCRIPTION_REFUND_STATUS} #{poll_count}"

		status_ir.request_description = description
		status_ir.data = frappe.as_json(status_data)
		if status_ir.is_new():
			status_ir.insert(ignore_permissions=True)
		else:
			status_ir.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep — POS: persist refund status enquiry IR before API call

		terminal_id = data.get("terminal_id")

		try:
			response = _refund_status(paytm_order_id, ref_id, terminal_name=terminal_id)
		except Exception:
			self._mark_ir(status_ir.name, "Failed", output={"error": frappe.get_traceback()})
			raise

		rstatus = refund_result_status(response)
		status_data["request"] = response.get("_request")
		data["request"] = response.get("_request")

		status_ir_status = {"success": "Completed", "pending": "Queued"}.get(rstatus, "Failed")
		self._mark_ir(status_ir.name, status_ir_status, output=response, data=status_data)

		if ir.status not in ("Completed", "Failed"):
			self._mark_ir(ir.name, status_ir_status, output=response, data=data)
		else:
			self._mark_ir(ir.name, ir.status, output=response, data=data)

		return {"status": rstatus, "message": result_message(response)}


# ---------------------------------------------------------------------------
# Gateway API calls (module-level — only used by controller methods above)
# ---------------------------------------------------------------------------


def _sale_request(
	amount_paise: int,
	reference_docname: str,
	merchant_transaction_id: str,
	terminal_name: str | None = None,
	payment_mode: str = "ALL",
	reference_no: str | None = None,
) -> dict:
	"""Send a payment request to the POS terminal."""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"transactionDateTime": now_string(),
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

	head = build_head(config, body, terminal_id=terminal["terminal_id"])
	response = call(config["sale_endpoint"], head, body)
	_logger.info("Paytm POS Sale %s -> %s", merchant_transaction_id, result(response)["result_status"])
	return response


def _status_enquiry(merchant_transaction_id: str, terminal_name: str | None = None) -> dict:
	"""Fetch status of a Sale or Void transaction."""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"transactionDateTime": now_string(),
		"merchantTransactionId": merchant_transaction_id,
	}
	head = build_head(config, body, terminal_id=terminal["terminal_id"])
	response = call(config["status_endpoint"], head, body)
	_logger.info("Paytm POS Status %s -> %s", merchant_transaction_id, result(response)["result_status"])
	return response


def _void_transaction(merchant_transaction_id: str, terminal_name: str | None = None) -> dict:
	"""Cancel a same-day successful Sale transaction."""
	config = get_paytm_pos_config()
	terminal = get_terminal(terminal_name)

	body = {
		"paytmMid": config["merchant_id"],
		"paytmTid": terminal["terminal_id"],
		"merchantTransactionId": merchant_transaction_id,
		"transactionDateTime": now_string(),
	}
	head = build_head(config, body, terminal_id=terminal["terminal_id"])
	response = call(config["void_endpoint"], head, body)
	_logger.info("Paytm POS Void %s -> %s", merchant_transaction_id, result(response)["result_status"])
	return response


def _refund_request(
	paytm_order_id: str, paytm_txn_id: str, ref_id: str, refund_amount: str, terminal_name: str | None = None
) -> dict:
	"""Initiate a refund for a successful transaction."""
	config = get_paytm_pos_config()
	merchant_key = get_merchant_key(config, terminal_name)

	body = {
		"mid": config["merchant_id"],
		"txnType": "REFUND",
		"orderId": paytm_order_id,
		"txnId": paytm_txn_id,
		"refId": ref_id,
		"refundAmount": refund_amount,
	}
	head = {
		"requestTimeStamp": now_string(),
		"channelId": config["channel_id"],
		"checksum": generate_checksum(checksum_body(body), merchant_key),
		"version": "1.0",
	}
	response = call(config["refund_endpoint"], head, body)
	_logger.info("Paytm POS Refund %s -> %s", paytm_order_id, result(response)["result_status"])
	return response


def _refund_status(paytm_order_id: str, ref_id: str, terminal_name: str | None = None) -> dict:
	"""Check status of a refund request."""
	config = get_paytm_pos_config()
	merchant_key = get_merchant_key(config, terminal_name)

	body = {
		"mid": config["merchant_id"],
		"orderId": paytm_order_id,
		"refId": ref_id,
	}
	head = {
		"requestTimeStamp": now_string(),
		"channelId": config["channel_id"],
		"checksum": generate_checksum(checksum_body(body), merchant_key),
		"version": "1.0",
	}
	response = call(config["refund_status_endpoint"], head, body)
	_logger.info("Paytm POS Refund Status %s -> %s", paytm_order_id, result(response)["result_status"])
	return response


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
			frappe.db.commit()  # nosemgrep — POS: persist expired IR before bridge
			settings._run_failure_bridge(doc, _("Payment timed out at the POS terminal"))


# ---------------------------------------------------------------------------
# Whitelisted entry-points
# ---------------------------------------------------------------------------


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
	check_pos_permission()
	if not frappe.db.exists(reference_doctype, reference_docname):
		frappe.throw(_("{0} {1} not found").format(reference_doctype, reference_docname))
	settings = frappe.get_single("PayTM POS Settings")
	return settings.start_sale(
		to_paise(amount), reference_doctype, reference_docname, payment_key, terminal, payment_mode
	)


@frappe.whitelist()
def poll_pos_sale(order_id: str) -> str:
	check_pos_permission()
	return frappe.get_single("PayTM POS Settings").poll_sale(order_id)


@frappe.whitelist()
def void_pos_sale(order_id: str) -> str:
	check_pos_permission()
	return frappe.get_single("PayTM POS Settings").void_sale(order_id)


@frappe.whitelist()
def refund_pos_sale(order_id: str, amount, note: str | None = None) -> dict:
	check_pos_permission()
	return frappe.get_single("PayTM POS Settings").do_refund(order_id, amount, note)


@frappe.whitelist()
def pos_refund_status(refund_order_id: str) -> dict:
	check_pos_permission()
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
