# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt

"""
# Integrating Cashfree

### Validate Currency

Example:

	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("Cashfree")
	controller().validate_transaction_currency(currency)

### 2. Redirect for payment

Example:

	payment_details = {
		"amount": 600,
		"title": "Payment for bill : 111",
		"description": "payment via cart",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR0001",
		"payer_email": "NuranVerkleij@example.com",
		"payer_name": "Nuran Verkleij",
		"payer_phone": "9898989898",
		"order_id": "111",
		"currency": "INR",
		"payment_gateway": "Cashfree",
	}

	# Redirect the user to this url
	url = controller().get_payment_url(**payment_details)

### 3. On Completion of Payment

Write a method for `on_payment_authorized` in the reference doctype

Example:

	def on_payment_authorized(payment_status):
		# this method will be called when payment is complete

##### Notes:

payment_status - payment gateway will put payment status on callback.
For Cashfree payment status is Completed when order_status is PAID.
"""

import hashlib
import hmac
import base64
import json
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import (
	create_request_log,
	make_get_request,
	make_post_request,
)
from frappe.integrations.utils import get_json
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payments.utils import create_payment_gateway


class CashfreeSettings(Document):
	supported_currencies = ["INR"]
	api_version = "2023-08-01"

	def init_client(self):
		if self.api_key:
			self.secret = self.get_password(fieldname="api_secret", raise_exception=False)

	def validate(self):
		create_payment_gateway("Cashfree")
		call_hook_method("payment_gateway_enabled", gateway="Cashfree")
		if not self.flags.ignore_mandatory:
			self.validate_cashfree_credentials()

	def get_base_url(self):
		"""Get Cashfree API base URL based on environment"""
		if hasattr(self, "use_sandbox") and self.use_sandbox:
			return "https://sandbox.cashfree.com/pg"
		return "https://api.cashfree.com/pg"

	def get_headers(self):
		"""Get common headers for Cashfree API calls"""
		return {
			"x-client-id": self.api_key,
			"x-client-secret": self.get_password(fieldname="api_secret", raise_exception=False),
			"x-api-version": self.api_version or "2023-08-01",
			"Content-Type": "application/json",
		}

	def validate_cashfree_credentials(self):
		if self.api_key and self.api_secret:
			try:
				make_post_request(
					url=f"{self.get_base_url()}/eligibility/payment_methods",
					headers=self.get_headers(),
					data=json.dumps({"queries": {"amount": 100}}),
				)
			except Exception:
				frappe.throw(_("Seems API Key or API Secret is wrong!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Cashfree does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_settings(self, data):
		"""Get API settings, with optional sandbox override (mirrors Razorpay pattern)"""
		settings = frappe._dict(
			{
				"api_key": self.api_key,
				"api_secret": self.get_password(fieldname="api_secret", raise_exception=False),
			}
		)

		if data.get("use_sandbox") or (hasattr(self, "use_sandbox") and self.use_sandbox):
			settings.update(
				{
					"api_key": frappe.conf.get("cashfree_sandbox_api_key") or self.api_key,
					"api_secret": frappe.conf.get("cashfree_sandbox_api_secret")
					or self.get_password(fieldname="api_secret", raise_exception=False),
				}
			)

		return settings

	def get_payment_url(self, **kwargs):
		order = {}
		if not kwargs.get("order_id"):
			order = self.create_order(**kwargs)
			kwargs.update({"order_id": order.get("order_id")})
		integration_request_name = order.get("integration_request") or kwargs.get("integration_request")
		return get_url(f"./cashfree_checkout?token={integration_request_name}")

	def create_order(self, **kwargs):
		"""Create an order on Cashfree PG
		API: POST /pg/orders
		Docs: https://docs.cashfree.com/reference/pgcreateorder
		"""
		# Create integration log
		integration_request = create_request_log(kwargs, service_name="Cashfree")

		# Setup payment options
		payment_options = {
			"order_amount": kwargs.get("amount"),
			"order_currency": kwargs.get("currency", "INR"),
			"customer_details": {
				"customer_id": kwargs.get("payer_email", "guest"),
				"customer_name": kwargs.get("payer_name", ""),
				"customer_email": kwargs.get("payer_email", ""),
				"customer_phone": kwargs.get("payer_phone", "9999999999"),
			},
			"order_meta": {
				"return_url": (
					f"{get_url()}/api/method/payments.payment_gateways.doctype"
					f".cashfree_settings.cashfree_settings.verify_payment"
					f"?token={integration_request.name}"
					"&order_id={order_id}"
				),
			},
		}

		if kwargs.get("order_note"):
			payment_options["order_note"] = kwargs["order_note"]

		if self.api_key and self.api_secret:
			try:
				order = make_post_request(
					f"{self.get_base_url()}/orders",
					headers=self.get_headers(),
					data=json.dumps(payment_options),
				)
				kwargs.update({"order_id": order.get("order_id")})
				integration_request.update_status(kwargs, "Queued")
				integration_request.db_set("output", get_json({"order": order}))
				order["integration_request"] = integration_request.name
				return order
			except Exception:
				frappe.log_error(frappe.get_traceback())
				frappe.throw(_("Could not create Cashfree order"))

	def create_request(self, data):
		self.data = frappe._dict(data)

		try:
			self.integration_request = frappe.get_doc("Integration Request", self.data.token)
			self.integration_request.update_status(self.data, "Queued")
			return self.authorize_payment()

		except Exception:
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"Seems issue with server's Cashfree config. Don't worry, in case of failure amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def authorize_payment(self):
		"""
		Verify payment status from Cashfree.
		Cashfree order_status values: ACTIVE, PAID, EXPIRED, TERMINATED, TERMINATION_REQUESTED
		"""
		data = json.loads(self.integration_request.data)

		try:
			order_id = self.data.get("order_id") or data.get("order_id")
			resp = make_get_request(
				f"{self.get_base_url()}/orders/{order_id}",
				headers=self.get_headers(),
			)

			if resp.get("order_status") == "PAID":
				self.integration_request.update_status(data, "Completed")
				self.flags.status_changed_to = "Completed"

			elif resp.get("order_status") == "ACTIVE":
				self.integration_request.update_status(data, "Authorized")
				self.flags.status_changed_to = "Authorized"

			else:
				frappe.log_error(message=str(resp), title="Cashfree Payment not authorized")

		except Exception:
			frappe.log_error()

		status = frappe.flags.integration_request.status_code

		redirect_to = data.get("redirect_to") or None
		redirect_message = data.get("redirect_message") or None

		if self.flags.status_changed_to in ("Completed", "Authorized"):
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					frappe.flags.data = data
					custom_redirect_to = frappe.get_doc(
						self.data.reference_doctype, self.data.reference_docname
					).run_method("on_payment_authorized", self.flags.status_changed_to)

				except Exception:
					frappe.log_error(frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

			redirect_url = (
				f"payment-success?doctype={self.data.reference_doctype}&docname={self.data.reference_docname}"
			)
		else:
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += "&" + urlencode({"redirect_to": redirect_to})
		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}

	def verify_signature(self, body, signature, timestamp):
		"""Verify Cashfree webhook signature.

		Cashfree signature verification:
			signedPayload = timestamp + body
			expectedSignature = Base64Encode(HMAC-SHA256(signedPayload, secretKey))

		Args:
			body (str): Raw webhook payload body
			signature (str): Value of x-webhook-signature header
			timestamp (str): Value of x-webhook-timestamp header

		Returns:
			bool: True if signature is valid

		Raises:
			frappe.PermissionError: If signature verification fails
		"""
		secret = self.get_password(fieldname="api_secret", raise_exception=False)
		secret_bytes = bytes(secret, "utf-8")
		signed_payload = bytes(f"{timestamp}{body}", "utf-8")

		generated_signature = base64.b64encode(
			hmac.new(key=secret_bytes, msg=signed_payload, digestmod=hashlib.sha256).digest()
		).decode("utf-8")

		result = hmac.compare_digest(generated_signature, signature)

		if not result:
			frappe.throw(_("Cashfree Signature Verification Failed"), exc=frappe.PermissionError)

		return result

	def fetch_order_payments(self, order_id):
		"""Fetch all payments for a given order from Cashfree.
		API: GET /pg/orders/{order_id}/payments
		"""
		try:
			resp = make_get_request(
				f"{self.get_base_url()}/orders/{order_id}/payments",
				headers=self.get_headers(),
			)
			return resp or []
		except Exception as e:
			frappe.log_error(f"Error fetching payments from Cashfree: {str(e)}", "Cashfree API Error")
			return []

	def create_refund(self, order_id, refund_id, refund_amount, refund_note=None, refund_speed="STANDARD"):
		"""Create a refund for an order on Cashfree.
		API: POST /pg/orders/{order_id}/refunds

		Args:
			order_id (str): The Cashfree order ID
			refund_id (str): Unique refund identifier from your system
			refund_amount (float): Amount to refund (should be <= order amount)
			refund_note (str, optional): A note for your reference
			refund_speed (str, optional): STANDARD or INSTANT. Defaults to STANDARD.

		Returns:
			dict: Refund response from Cashfree API
		"""
		refund_data = {
			"refund_amount": refund_amount,
			"refund_id": refund_id,
			"refund_speed": refund_speed,
		}
		if refund_note:
			refund_data["refund_note"] = refund_note

		try:
			resp = make_post_request(
				f"{self.get_base_url()}/orders/{order_id}/refunds",
				headers=self.get_headers(),
				data=json.dumps(refund_data),
			)
			return resp
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Cashfree Refund Error")
			frappe.throw(_("Could not create Cashfree refund"))

	def fetch_settlement_transactions(self, year, month, day):
		"""Fetch settlement transactions from Cashfree API by date.

		Cashfree settlements API: GET /pg/settlements?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

		Args:
			year (int): Year of settlement
			month (int): Month of settlement (1-12)
			day (int): Day of settlement (1-31)

		Returns:
			list: Settlement transaction items
		"""
		try:
			api_key = self.api_key
			api_secret = self.get_password(fieldname="api_secret", raise_exception=False)

			if not api_key or not api_secret:
				frappe.logger().warning("Cashfree API credentials not configured")
				return []

			date_str = f"{year}-{month:02d}-{day:02d}"
			resp = make_get_request(
				f"{self.get_base_url()}/settlements?start_date={date_str}&end_date={date_str}",
				headers=self.get_headers(),
			)

			if not resp or not resp.get("items"):
				frappe.logger().info(
					f"No settlement transactions found for date {date_str} in Cashfree API"
				)
				return []

			items = resp.get("items", [])
			frappe.logger().info(
				f"Fetched {len(items)} settlement items from Cashfree API for date {date_str}"
			)
			return items

		except Exception as e:
			frappe.log_error(f"Error fetching from Cashfree API: {str(e)}", "Cashfree API Error")
			return []

	def build_embedded_checkout_url(
		self,
		payment_session_id,
		callback_url,
		cancel_url,
		return_form_data=False,
		**additional_params,
	):
		"""Build Cashfree embedded/drop checkout URL or form data.

		Cashfree uses a payment_session_id (from create order) to initialize its JS SDK.
		This method builds the necessary data for checkout integration.

		Args:
			payment_session_id (str): Payment session ID from create order response
			callback_url (str): Success callback URL
			cancel_url (str): Cancel/failure redirect URL
			return_form_data (bool, optional): If True, return structured form data
			**additional_params: Any additional parameters

		Returns:
			str or dict: Checkout URL or form data structure
		"""
		try:
			# Cashfree uses JS SDK with payment_session_id, not a simple POST form.
			# Build the data needed to initialize Cashfree JS checkout.
			checkout_data = {
				"payment_session_id": payment_session_id,
				"callback_url": callback_url,
				"cancel_url": cancel_url,
				"environment": "sandbox" if (hasattr(self, "use_sandbox") and self.use_sandbox) else "production",
			}
			checkout_data.update(additional_params)

			if return_form_data:
				return {
					"sdk": "cashfree-js",
					"environment": checkout_data["environment"],
					"fields": checkout_data,
				}
			else:
				# Return a redirect URL with params for a simple GET-based flow
				base = callback_url.split("?")[0] if "?" in callback_url else callback_url
				checkout_url = f"{base}?{urlencode(checkout_data)}"
				frappe.logger().debug(f"Built Cashfree checkout URL: {checkout_url}")
				return checkout_url

		except Exception as e:
			frappe.log_error(frappe.get_traceback(), "Error building Cashfree checkout URL")
			frappe.throw(_("Failed to build payment checkout URL: {0}").format(str(e)))

	@frappe.whitelist()
	def clear(self):
		self.api_key = self.api_secret = None
		self.redirect_url = None
		self.flags.ignore_mandatory = True
		self.save()


def capture_payment():
	"""
	Capture / confirm authorized payments on Cashfree.

	In Cashfree's model, payments move to PAID automatically on successful auth
	(unlike Razorpay where explicit capture is needed). This function checks
	Integration Requests with status 'Authorized' and verifies them with Cashfree.
	If the order is PAID, it marks the integration request as Completed.
	"""
	controller = frappe.get_doc("Cashfree Settings")

	for doc in frappe.get_all(
		"Integration Request",
		filters={"status": "Authorized", "integration_request_service": "Cashfree"},
		fields=["name", "data"],
	):
		try:
			data = json.loads(doc.data)
			order_id = data.get("order_id")

			if not order_id:
				continue

			resp = make_get_request(
				f"{controller.get_base_url()}/orders/{order_id}",
				headers=controller.get_headers(),
			)

			if resp.get("order_status") == "PAID":
				frappe.db.set_value("Integration Request", doc.name, "status", "Completed")

		except Exception:
			integration_doc = frappe.get_doc("Integration Request", doc.name)
			integration_doc.status = "Failed"
			integration_doc.error = frappe.get_traceback()
			integration_doc.save()
			frappe.log_error(integration_doc.error, f"{doc.name} Failed")


@frappe.whitelist(allow_guest=True)
def get_api_key():
	controller = frappe.get_doc("Cashfree Settings")
	return controller.api_key


@frappe.whitelist(allow_guest=True)
def get_order(doctype, docname):
	"""Get Cashfree order — consumed by cashfree checkout JS."""
	doc = frappe.get_doc(doctype, docname)
	try:
		return doc.get_cashfree_order()
	except AttributeError:
		frappe.log_error(frappe.get_traceback(), _("Controller method get_cashfree_order missing"))
		frappe.throw(_("Could not create Cashfree order. Please contact Administrator"))


@frappe.whitelist(allow_guest=True)
def order_payment_success(integration_request, params):
	"""Called by cashfree checkout JS on payment success.

	Args:
		integration_request (str): Integration Request name
		params (str): JSON string with order_id and payment details
	"""
	params = json.loads(params)
	integration = frappe.get_doc("Integration Request", integration_request)

	# Update integration request with payment params
	integration.update_status(params, integration.status)
	integration.reload()

	data = json.loads(integration.data)
	controller = frappe.get_doc("Cashfree Settings")

	controller.integration_request = integration
	controller.data = frappe._dict(data)

	# Authorize / verify payment
	controller.authorize_payment()


@frappe.whitelist(allow_guest=True)
def order_payment_failure(integration_request, params):
	"""Called by cashfree checkout JS on failure.

	Args:
		integration_request (str): Integration Request name
		params (str): JSON error data
	"""
	frappe.log_error(params, "Cashfree Payment Failure")
	params = json.loads(params)
	integration = frappe.get_doc("Integration Request", integration_request)
	integration.update_status(params, integration.status)


@frappe.whitelist(allow_guest=True)
def verify_payment(token, order_id=None):
	"""Verify payment callback from Cashfree (return_url redirect).

	Args:
		token (str): Integration Request name
		order_id (str, optional): Cashfree order ID (passed via return_url template)
	"""
	integration = frappe.get_doc("Integration Request", token)

	data = json.loads(integration.data)
	if order_id:
		data["order_id"] = order_id

	controller = frappe.get_doc("Cashfree Settings")
	controller.integration_request = integration
	controller.data = frappe._dict(data)

	response = controller.authorize_payment()
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = f"{get_url()}/{response.get('redirect_to')}"
	frappe.local.response["status"] = 200


@frappe.whitelist(allow_guest=True)
def cashfree_webhook_handler():
	"""Handle incoming webhooks from Cashfree.

	Verifies the webhook signature and processes payment/refund events.
	Webhook headers:
		x-webhook-signature: HMAC-SHA256 signature (base64)
		x-webhook-timestamp: Timestamp used in signature generation
	"""
	try:
		data = frappe.local.form_dict
		raw_body = frappe.request.get_data(as_text=True)

		signature = frappe.get_request_header("x-webhook-signature")
		timestamp = frappe.get_request_header("x-webhook-timestamp")

		if not signature or not timestamp:
			frappe.throw(_("Missing webhook signature or timestamp"), exc=frappe.PermissionError)

		controller = frappe.get_doc("Cashfree Settings")
		controller.verify_signature(raw_body, signature, timestamp)

		# Parse event type and process
		event_type = data.get("type")
		event_data = data.get("data", {})

		doc = frappe.get_doc(
			{
				"data": json.dumps(data),
				"doctype": "Integration Request",
				"request_description": f"Cashfree Webhook: {event_type}",
				"is_remote_request": 1,
				"status": "Queued",
				"integration_request_service": "Cashfree",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		if event_type in ("PAYMENT_SUCCESS_WEBHOOK", "PAYMENT_SUCCESS"):
			order_id = event_data.get("order", {}).get("order_id")
			if order_id:
				# Find the matching integration request and complete it
				existing = frappe.get_all(
					"Integration Request",
					filters={
						"integration_request_service": "Cashfree",
						"status": ["in", ["Queued", "Authorized"]],
					},
					fields=["name", "data"],
				)
				for req in existing:
					req_data = json.loads(req.data)
					if req_data.get("order_id") == order_id:
						frappe.db.set_value("Integration Request", req.name, "status", "Completed")

						# Trigger on_payment_authorized hook
						if req_data.get("reference_doctype") and req_data.get("reference_docname"):
							try:
								frappe.get_doc(
									req_data["reference_doctype"], req_data["reference_docname"]
								).run_method("on_payment_authorized", "Completed")
							except Exception:
								frappe.log_error(frappe.get_traceback())
						break

		elif event_type in ("REFUND_STATUS_WEBHOOK", "REFUND_SUCCESS"):
			frappe.logger().info(f"Cashfree refund webhook received: {event_data}")

		frappe.db.set_value("Integration Request", doc.name, "status", "Completed")

	except frappe.PermissionError:
		raise
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Cashfree Webhook Error")
