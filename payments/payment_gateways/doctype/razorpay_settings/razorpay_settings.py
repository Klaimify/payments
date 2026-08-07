# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""
# Integrating RazorPay

### Validate Currency

Example:

	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("Razorpay")
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
		"order_id": "111",
		"currency": "INR",
		"payment_gateway": "Razorpay",
		"subscription_details": {
			"plan_id": "plan_12313", # if Required
			"start_date": "2018-08-30",
			"billing_period": "Month" #(Day, Week, Month, Year),
			"billing_frequency": 1,
			"customer_notify": 1,
			"upfront_amount": 1000
		}
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
For razorpay payment status is Authorized

"""

import hashlib
import hmac
import json
from urllib.parse import urlencode

import frappe
import razorpay
from frappe import _
from frappe.integrations.utils import (
	create_request_log,
	make_get_request,
	make_post_request,
)
from frappe.model.document import Document
from frappe.utils import add_to_date, call_hook_method, cint, get_timestamp, get_url, now_datetime
from frappe.integrations.utils import get_json
from payments.utils import create_payment_gateway


PENDING_ONDEMAND_IR_CACHE_KEY = "razorpay_ondemand_pending_integration_requests"
PENDING_ONDEMAND_IR_CACHE_TTL = 7 * 24 * 60 * 60
SETTLEMENT_ONDEMAND_URL = "https://api.razorpay.com/v1/settlements/ondemand"

# Freshly captured funds aren't always in Razorpay's available balance yet, so
# a settlement request can fail right after capture. retry_pending_instant_settlements
# (cron, every 10 min) re-attempts until this cap is hit.
MAX_ONDEMAND_SETTLEMENT_ATTEMPTS = 3

# Razorpay's documented bounds for the on-demand settlement "amount" param, in paise
# (₹100 to ₹500000000, despite the API error text saying "100").
MIN_ONDEMAND_SETTLEMENT_AMOUNT = 10000
MAX_ONDEMAND_SETTLEMENT_AMOUNT = 50000000000


class RazorpaySettings(Document):
	supported_currencies = (
		"AED",
		"ALL",
		"AMD",
		"ARS",
		"AUD",
		"AWG",
		"AZN",
		"BAM",
		"BBD",
		"BDT",
		"BGN",
		"BHD",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BTN",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CUP",
		"CVE",
		"CZK",
		"DJF",
		"DKK",
		"DOP",
		"DZD",
		"EGP",
		"ETB",
		"EUR",
		"FJD",
		"GBP",
		"GHS",
		"GIP",
		"GMD",
		"GNF",
		"GTQ",
		"GYD",
		"HKD",
		"HNL",
		"HRK",
		"HTG",
		"HUF",
		"IDR",
		"ILS",
		"INR",
		"IQD",
		"ISK",
		"JMD",
		"JOD",
		"JPY",
		"KES",
		"KGS",
		"KHR",
		"KMF",
		"KRW",
		"KWD",
		"KYD",
		"KZT",
		"LAK",
		"LKR",
		"LRD",
		"LSL",
		"MAD",
		"MDL",
		"MGA",
		"MKD",
		"MMK",
		"MNT",
		"MOP",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"MZN",
		"NAD",
		"NGN",
		"NIO",
		"NOK",
		"NPR",
		"NZD",
		"OMR",
		"PEN",
		"PGK",
		"PHP",
		"PKR",
		"PLN",
		"PYG",
		"QAR",
		"RON",
		"RSD",
		"RUB",
		"RWF",
		"SAR",
		"SCR",
		"SEK",
		"SGD",
		"SLL",
		"SOS",
		"SSP",
		"SVC",
		"SZL",
		"THB",
		"TND",
		"TRY",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VND",
		"VUV",
		"XAF",
		"XCD",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
		"ZMW",
	)

	def init_client(self):
		if self.api_key:
			secret = self.get_password(fieldname="api_secret", raise_exception=False)
			self.client = razorpay.Client(auth=(self.api_key, secret))

	def validate(self):
		create_payment_gateway("Razorpay")
		call_hook_method("payment_gateway_enabled", gateway="Razorpay")
		if not self.flags.ignore_mandatory:
			self.validate_razorpay_credentails()

	def validate_razorpay_credentails(self):
		if self.api_key and self.api_secret:
			try:
				make_get_request(
					url="https://api.razorpay.com/v1/payments",
					auth=(
						self.api_key,
						self.get_password(fieldname="api_secret", raise_exception=False),
					),
				)
			except Exception:
				frappe.throw(_("Seems API Key or API Secret is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Razorpay does not support transactions in currency '{0}'"
				).format(currency)
			)

	def setup_addon(self, settings, **kwargs):
		"""
		Addon template:
		{
		        "item": {
		                "name": row.upgrade_type,
		                "amount": row.amount,
		                "currency": currency,
		                "description": "add-on description"
		        },
		        "quantity": 1 (The total amount is calculated as item.amount * quantity)
		}
		"""
		url = "https://api.razorpay.com/v1/subscriptions/{}/addons".format(kwargs.get("subscription_id"))

		try:
			if not frappe.conf.converted_rupee_to_paisa:
				convert_rupee_to_paisa(**kwargs)

			for addon in kwargs.get("addons"):
				resp = make_post_request(
					url,
					auth=(settings.api_key, settings.api_secret),
					data=json.dumps(addon),
					headers={"content-type": "application/json"},
				)
				if not resp.get("id"):
					frappe.log_error(message=str(resp), title="Razorpay Failed while creating subscription")
		except Exception:
			frappe.log_error()
			# failed
			pass

	def setup_subscription(self, settings, **kwargs):
		start_date = (
			get_timestamp(kwargs.get("subscription_details").get("start_date"))
			if kwargs.get("subscription_details").get("start_date")
			else None
		)

		subscription_details = {
			"plan_id": kwargs.get("subscription_details").get("plan_id"),
			"total_count": kwargs.get("subscription_details").get("billing_frequency"),
			"customer_notify": kwargs.get("subscription_details").get("customer_notify"),
		}

		if start_date:
			subscription_details["start_at"] = cint(start_date)

		if kwargs.get("addons"):
			convert_rupee_to_paisa(**kwargs)
			subscription_details.update({"addons": kwargs.get("addons")})

		try:
			resp = make_post_request(
				"https://api.razorpay.com/v1/subscriptions",
				auth=(settings.api_key, settings.api_secret),
				data=json.dumps(subscription_details),
				headers={"content-type": "application/json"},
			)

			if resp.get("status") == "created":
				kwargs["subscription_id"] = resp.get("id")
				frappe.flags.status = "created"
				return kwargs
			else:
				frappe.log_error(message=str(resp), title="Razorpay Failed while creating subscription")

		except Exception:
			frappe.log_error()

	def prepare_subscription_details(self, settings, **kwargs):
		if not kwargs.get("subscription_id"):
			kwargs = self.setup_subscription(settings, **kwargs)

		if frappe.flags.status != "created":
			kwargs["subscription_id"] = None

		return kwargs

	def get_payment_url(self, **kwargs):
		order = {}
		if not kwargs.get("order_id"):
			order = self.create_order(**kwargs)
			kwargs.update({"order_id": order.get("id")})
		integration_request_name = order.get("integration_request") or kwargs.get("integration_request")		
		return get_url(f"./razorpay_checkout?token={integration_request_name}")

	def create_order(self, **kwargs):
		# Creating Orders https://razorpay.com/docs/api/orders/

		# convert rupees to paisa
		amount = int(kwargs["amount"] * 100)
		
		# Create integration log
		integration_request = create_request_log(kwargs, service_name="Razorpay")

		# Setup payment options
		payment_options = {
			"amount": amount,
			"currency": kwargs.get("currency", "INR"),
			"receipt": kwargs.get("receipt"),
			"payment_capture": kwargs.get("payment_capture"),
		}
		if self.api_key and self.api_secret:
			try:
				order = make_post_request(
					"https://api.razorpay.com/v1/orders",
					auth=(
						self.api_key,
						self.get_password(fieldname="api_secret", raise_exception=False),
					),
					data=payment_options,
				)
				kwargs.update({"order_id": order.get("id")})
				integration_request.update_status(kwargs, "Queued")
				integration_request.db_set("output", get_json({"order": order}))
				order["integration_request"] = integration_request.name
				return order  # Order returned to be consumed by razorpay.js
			except Exception:
				frappe.log(frappe.get_traceback())
				frappe.throw(_("Could not create razorpay order"))

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
						"Seems issue with server's razorpay config. Don't worry, in case of failure amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def authorize_payment(self):
		"""
		An authorization is performed when user's payment details are successfully authenticated by the bank.
		The money is deducted from the customer's account, but will not be transferred to the merchant's account
		until it is explicitly captured by merchant.
		"""
		data = json.loads(self.integration_request.data)
		settings = self.get_settings(data)

		try:
			resp = make_get_request(
				f"https://api.razorpay.com/v1/payments/{self.data.razorpay_payment_id}",
				auth=(settings.api_key, settings.api_secret),
			)

			if resp.get('order_id') != data.get("order_id"):
				frappe.throw(_("Order ID mismatch"))

			if resp.get("status") == "authorized":
				self.integration_request.update_status(data, "Authorized")
				self.flags.status_changed_to = "Authorized"

			elif resp.get("status") == "captured":
				self.integration_request.update_status(data, "Completed")
				self.flags.status_changed_to = "Completed"
				self.process_instant_settlement_on_payment(resp, data)

			elif data.get("subscription_id"):
				if resp.get("status") == "refunded":
					# if subscription start date is in future then
					# razorpay refunds the amount after authorizing the card details
					# thus changing status to Verified

					self.integration_request.update_status(data, "Completed")
					self.flags.status_changed_to = "Verified"

			else:
				frappe.log_error(message=str(resp), title="Razorpay Payment not authorized")

		except Exception:
			frappe.log_error()

		status = frappe.flags.integration_request.status_code

		redirect_to = data.get("redirect_to") or None
		redirect_message = data.get("redirect_message") or None
		if self.flags.status_changed_to in ("Authorized", "Verified", "Completed"):
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

	def process_instant_settlement_on_payment(self, payment_response, request_data):
		"""Trigger Razorpay on-demand settlement for captured payments when enabled.

		Safe to call more than once for the same payment (redirect flow, webhook,
		and the capture cron can all reach this) and safe to retry after failure —
		see _ondemand_settlement_already_triggered and retry_pending_instant_settlements.
		"""
		if not cint(getattr(self, "enable_instant_settlement", 0)):
			return

		if self._ondemand_settlement_already_triggered():
			return

		payment_ir = self.integration_request
		attempts = cint((self._payment_ir_output() or {}).get("ondemand_settlement_attempts"))
		if attempts >= MAX_ONDEMAND_SETTLEMENT_ATTEMPTS:
			return

		settle_full_balance = bool(cint(getattr(self, "settle_full_balance", 0)))
		amount = cint(payment_response.get("amount"))
		if amount <= 0 and not settle_full_balance:
			frappe.logger().warning("Skipping instant settlement: invalid captured amount")
			return

		# Razorpay rejects on-demand settlement requests outside this range; retrying
		# won't help since the amount never changes, so give up permanently instead
		# of letting the retry cron hammer the API every 10 min.
		if not settle_full_balance and not (
			MIN_ONDEMAND_SETTLEMENT_AMOUNT <= amount <= MAX_ONDEMAND_SETTLEMENT_AMOUNT
		):
			frappe.logger().warning(
				f"Skipping instant settlement: amount {amount} outside Razorpay's allowed range "
				f"({MIN_ONDEMAND_SETTLEMENT_AMOUNT}-{MAX_ONDEMAND_SETTLEMENT_AMOUNT} paise)"
			)
			if payment_ir:
				self._update_payment_ir_output({"ondemand_settlement_skipped": "amount_out_of_range"})
			return

		settings = self.get_settings(request_data)
		description = getattr(self, "instant_settlement_description", None) or _(
			"Instant settlement for captured Razorpay payment"
		)

		ref_doctype = request_data.get("reference_doctype") or (
			payment_ir.reference_doctype if payment_ir else ""
		)
		ref_docname = request_data.get("reference_docname") or (
			payment_ir.reference_docname if payment_ir else ""
		)

		payload = {
			"settle_full_balance": settle_full_balance,
			"description": description,
			"notes": {
				"integration_request": payment_ir.name if payment_ir else "",
				"reference_doctype": ref_doctype,
				"reference_docname": ref_docname,
				"razorpay_payment_id": payment_response.get("id") or request_data.get("razorpay_payment_id") or "",
			},
		}
		if not settle_full_balance:
			payload["amount"] = amount

		if payment_ir:
			self._update_payment_ir_output({"ondemand_settlement_attempts": attempts + 1})

		# Create a dedicated Integration Request for this settlement call so it can
		# be tracked separately from the payment IR.
		settlement_ir = frappe.get_doc({
			"doctype": "Integration Request",
			"integration_request_service": "Razorpay",
			"request_description": "Razorpay On-Demand Settlement",
			"url": SETTLEMENT_ONDEMAND_URL,
			"is_remote_request": 1,
			"status": "Queued",
			"data": json.dumps(payload),
			"reference_doctype": ref_doctype,
			"reference_docname": ref_docname,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		try:
			response = make_post_request(
				SETTLEMENT_ONDEMAND_URL,
				auth=(settings.api_key, settings.api_secret),
				data=json.dumps(payload),
				headers={"content-type": "application/json"},
			)

			if response and response.get("id"):
				trigger_data = {
					"id": response.get("id"),
					"status": response.get("status"),
					"amount_requested": response.get("amount_requested") or response.get("amount") or amount,
					"amount_settled": response.get("amount_settled"),
					"amount_pending": response.get("amount_pending"),
					"fees": response.get("fees"),
					"tax": response.get("tax"),
					"currency": response.get("currency"),
					"settle_full_balance": settle_full_balance,
					"created_at": response.get("created_at"),
					"last_polled_at": None,
				}
				settlement_ir.db_set("status", "Completed")
				settlement_ir.db_set("output", get_json({"ondemand_settlement_trigger": trigger_data}))
				_add_pending_ondemand_ir(settlement_ir.name)

				# Back-reference on the payment IR: this is also what
				# _ondemand_settlement_already_triggered checks for.
				if payment_ir:
					self._update_payment_ir_output({"ondemand_settlement_ir": settlement_ir.name})

				frappe.logger().info(
					f"Instant settlement triggered: {response.get('id')} (settlement IR: {settlement_ir.name})"
				)
			else:
				settlement_ir.db_set("status", "Failed")
				settlement_ir.db_set("output", get_json({"error": str(response)}))
				frappe.log_error(message=str(response), title="Razorpay instant settlement failed")

		except Exception as e:
			error_body = ""
			if hasattr(e, "response") and e.response is not None:
				error_body = e.response.text
			settlement_ir.db_set("status", "Failed")
			settlement_ir.db_set(
				"output", get_json({"error": frappe.get_traceback(), "api_response": error_body})
			)
			# Not marked as a hard failure here on purpose: this is usually the
			# just-captured amount not yet reflected in Razorpay's available
			# balance. retry_pending_instant_settlements re-attempts every 10 min
			# up to MAX_ONDEMAND_SETTLEMENT_ATTEMPTS.
			frappe.log_error(
				f"{frappe.get_traceback()}\n\nAPI Response: {error_body}",
				"Razorpay instant settlement error",
			)

	def _payment_ir_output(self):
		if not self.integration_request or not self.integration_request.output:
			return {}
		try:
			parsed = json.loads(self.integration_request.output)
			return parsed if isinstance(parsed, dict) else {}
		except Exception:
			return {}

	def _update_payment_ir_output(self, updates):
		output = self._payment_ir_output()
		output.update(updates)
		self.integration_request.db_set("output", get_json(output))

	def _ondemand_settlement_already_triggered(self):
		output = self._payment_ir_output()
		return bool(output.get("ondemand_settlement_ir") or output.get("ondemand_settlement_skipped"))

	def get_settings(self, data):
		settings = frappe._dict(
			{
				"api_key": self.api_key,
				"api_secret": self.get_password(fieldname="api_secret", raise_exception=False),
				"webhook_secret": self.get_password(fieldname="webhook_secret", raise_exception=False),
			}
		)

		if cint(data.get("notes", {}).get("use_sandbox")) or data.get("use_sandbox"):
			settings.update(
				{
					"api_key": frappe.conf.sandbox_api_key,
					"api_secret": frappe.conf.sandbox_api_secret,
				}
			)

		return settings

	def fetch_ondemand_settlement(self, settlement_id, request_data=None):
		"""Fetch on-demand settlement status by settlement id."""
		request_data = request_data or {}
		settings = self.get_settings(request_data)
		return make_get_request(
			f"https://api.razorpay.com/v1/settlements/ondemand/{settlement_id}",
			auth=(settings.api_key, settings.api_secret),
		)

	def cancel_subscription(self, subscription_id):
		settings = self.get_settings({})

		try:
			make_post_request(
				f"https://api.razorpay.com/v1/subscriptions/{subscription_id}/cancel",
				auth=(settings.api_key, settings.api_secret),
			)
		except Exception:
			frappe.log_error(frappe.get_traceback())

	def verify_signature(self, body, signature, key):
		key = bytes(key, "utf-8")
		body = bytes(body, "utf-8")

		dig = hmac.new(key=key, msg=body, digestmod=hashlib.sha256)

		generated_signature = dig.hexdigest()
		result = hmac.compare_digest(generated_signature, signature)

		if not result:
			frappe.throw(_("Razorpay Signature Verification Failed"), exc=frappe.PermissionError)

		return result

	def fetch_settlement_transactions(self, year, month, day):
		"""Fetch settlement transactions from Razorpay API by date
		
		Args:
			year (int): Year of settlement
			month (int): Month of settlement (1-12)
			day (int): Day of settlement (1-31)
		"""
		try:
			api_key = self.api_key
			api_secret = self.get_password(fieldname="api_secret", raise_exception=False)
			
			if not api_key or not api_secret:
				frappe.logger().warning("Razorpay API credentials not configured")
				return []
			
			# Fetch settlements for the specified date
			# Razorpay API endpoint: GET /v1/settlements/recon/combined?year=YYYY&month=MM&day=DD
			settlement_transactions_resp = make_get_request(
				f"https://api.razorpay.com/v1/settlements/recon/combined?year={year}&month={month:02d}&day={day:02d}",
				auth=(api_key, api_secret)
			)
			
			if not settlement_transactions_resp or not settlement_transactions_resp.get("items"):
				frappe.logger().info(f"No transactions found for date {year}-{month:02d}-{day:02d} in Razorpay API")
				return []
			
			# Return raw settlement transaction items from Razorpay API
			items = settlement_transactions_resp.get("items", [])
			frappe.logger().info(f"Fetched {len(items)} settlement items from Razorpay API for date {year}-{month:02d}-{day:02d}")
			return items
			
		except Exception as e:
			frappe.log_error(f"Error fetching from Razorpay API: {str(e)}", "Razorpay API Error")
			return []

	@frappe.whitelist()
	def clear(self):
		self.api_key = self.api_secret = None
		self.redirect_url = None
		self.flags.ignore_mandatory = True
		self.save()

	def build_embedded_checkout_url(self, order_id, callback_url, cancel_url, 
									description=None, name=None, image=None,
									prefill_name=None, prefill_email=None, prefill_contact=None,
									readonly_contact=False, readonly_email=False,
									return_form_data=False, **additional_params):
		"""
		Build Razorpay embedded checkout URL or form data with customizable parameters
		
		Args:
			order_id (str): Razorpay order ID
			callback_url (str): Success callback URL
			cancel_url (str): Cancel/failure callback URL
			description (str, optional): Custom description to show on checkout page
			name (str, optional): Merchant/business name to display
			image (str, optional): URL to merchant logo/image
			prefill_name (str, optional): Prefill customer name
			prefill_email (str, optional): Prefill customer email
			prefill_contact (str, optional): Prefill customer contact
			readonly_contact (bool, optional): If True, prefilled contact is read-only
			readonly_email (bool, optional): If True, prefilled email is read-only
			return_form_data (bool, optional): If True, return form data for POST submission
			**additional_params: Any additional form parameters
			
		Returns:
			str or dict: Complete Razorpay embedded checkout URL or form data structure
		"""
		try:
			from urllib.parse import urlencode
			
			# Base URL for Razorpay embedded checkout
			base_url = "https://api.razorpay.com/v1/checkout/embedded"
			
			# Build form data
			form_data = {
				'key_id': self.api_key,
				'order_id': order_id,
				'callback_url': callback_url,
				'cancel_url': cancel_url,
				'redirect': 'true'
			}
			
			# Add custom branding if provided
			if description:
				form_data['description'] = description
			if name:
				form_data['name'] = name
			if image:
				form_data['image'] = image
			
			# Add prefill data if provided
			if prefill_name:
				form_data['prefill[name]'] = prefill_name
			if prefill_email:
				form_data['prefill[email]'] = prefill_email
			if prefill_contact:
				form_data['prefill[contact]'] = prefill_contact
			
			form_data['readonly[email]'] = readonly_email
			form_data['readonly[contact]'] = readonly_contact
			
			# Add any additional parameters
			form_data.update(additional_params)
			
			if return_form_data:
				# Return form data structure for POST submission
				form_structure = {
					'action': base_url,
					'method': 'POST',
					'fields': form_data
				}
				frappe.logger().debug(f"Built Razorpay form data: {form_structure}")
				return form_structure
			else:
				# Create final URL for GET method (backward compatibility)
				checkout_url = f"{base_url}?{urlencode(form_data)}"
				frappe.logger().debug(f"Built Razorpay checkout URL: {checkout_url}")
				return checkout_url
			
		except Exception as e:
			frappe.log_error(frappe.get_traceback(), "Error building Razorpay checkout URL")
			frappe.throw(_("Failed to build payment checkout URL: {0}").format(str(e)))



def capture_payment(is_sandbox=False, sanbox_response=None):
	"""
	Verifies the purchase as complete by the merchant.
	After capture, the amount is transferred to the merchant within T+3 days
	where T is the day on which payment is captured.

	Note: Attempting to capture a payment whose status is not authorized will produce an error.
	"""
	controller = frappe.get_doc("Razorpay Settings")

	for doc in frappe.get_all(
		"Integration Request",
		filters={"status": "Authorized", "integration_request_service": "Razorpay"},
		fields=["name", "data"],
	):
		try:
			if is_sandbox:
				resp = sanbox_response
			else:
				data = json.loads(doc.data)
				settings = controller.get_settings(data)

				resp = make_get_request(
					"https://api.razorpay.com/v1/payments/{}".format(data.get("razorpay_payment_id")),
					auth=(settings.api_key, settings.api_secret),
					data={"amount": data.get("amount")},
				)

				if resp.get("status") == "authorized":
					resp = make_post_request(
						"https://api.razorpay.com/v1/payments/{}/capture".format(
							data.get("razorpay_payment_id")
						),
						auth=(settings.api_key, settings.api_secret),
						data={"amount": data.get("amount")},
					)

			if resp.get("status") == "captured":
				frappe.db.set_value("Integration Request", doc.name, "status", "Completed")
				controller.integration_request = frappe.get_doc("Integration Request", doc.name)
				controller.process_instant_settlement_on_payment(resp, data)

		except Exception:
			doc = frappe.get_doc("Integration Request", doc.name)
			doc.status = "Failed"
			doc.error = frappe.get_traceback()
			doc.save()
			frappe.log_error(doc.error, f"{doc.name} Failed")


@frappe.whitelist(allow_guest=True)
def get_api_key():
	controller = frappe.get_doc("Razorpay Settings")
	return controller.api_key


@frappe.whitelist(allow_guest=True)
def get_order(doctype, docname):
	# Order returned to be consumed by razorpay.js
	doc = frappe.get_doc(doctype, docname)
	try:
		# Do not use run_method here as it fails silently
		return doc.get_razorpay_order()
	except AttributeError:
		frappe.log_error(frappe.get_traceback(), _("Controller method get_razorpay_order missing"))
		frappe.throw(_("Could not create Razorpay order. Please contact Administrator"))


@frappe.whitelist(allow_guest=True)
def order_payment_success(integration_request, params):
	"""Called by razorpay.js on order payment success, the params
	contains razorpay_payment_id, razorpay_order_id, razorpay_signature
	that is updated in the data field of integration request

	Args:
	        integration_request (string): Name for integration request doc
	        params (string): Params to be updated for integration request.
	"""
	params = json.loads(params)
	integration = frappe.get_doc("Integration Request", integration_request)

	# Update integration request
	integration.update_status(params, integration.status)
	integration.reload()

	data = json.loads(integration.data)
	controller = frappe.get_doc("Razorpay Settings")

	# Update payment and integration data for payment controller object
	controller.integration_request = integration
	controller.data = frappe._dict(data)

	# Authorize payment
	controller.authorize_payment()


@frappe.whitelist(allow_guest=True)
def order_payment_failure(integration_request, params):
	"""Called by razorpay.js on failure

	Args:
	        integration_request (TYPE): Description
	        params (TYPE): error data to be updated
	"""
	frappe.log_error(params, "Razorpay Payment Failure")
	params = json.loads(params)
	integration = frappe.get_doc("Integration Request", integration_request)
	integration.update_status(params, integration.status)


def convert_rupee_to_paisa(**kwargs):
	for addon in kwargs.get("addons"):
		addon["item"]["amount"] *= 100

	frappe.conf.converted_rupee_to_paisa = True


@frappe.whitelist(allow_guest=True)
def razorpay_subscription_callback():
	try:
		data = frappe.local.form_dict

		validate_payment_callback(data)

		data.update({"payment_gateway": "Razorpay"})

		doc = frappe.get_doc(
			{
				"data": json.dumps(frappe.local.form_dict),
				"doctype": "Integration Request",
				"request_description": "Subscription Notification",
				"is_remote_request": 1,
				"status": "Queued",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		frappe.enqueue(
			method="payments.payment_gateways.doctype.razorpay_settings.razorpay_settings.handle_subscription_notification",
			queue="long",
			timeout=600,
			is_async=True,
			**{"doctype": "Integration Request", "docname": doc.name},
		)

	except frappe.InvalidStatusError:
		pass
	except Exception as e:
		frappe.log(frappe.log_error(title=e))


def validate_payment_callback(data):
	def _throw():
		frappe.throw(_("Invalid Subscription"), exc=frappe.InvalidStatusError)

	subscription_id = data.get("payload").get("subscription").get("entity").get("id")

	if not (subscription_id):
		_throw()

	controller = frappe.get_doc("Razorpay Settings")

	settings = controller.get_settings(data)

	resp = make_get_request(
		f"https://api.razorpay.com/v1/subscriptions/{subscription_id}",
		auth=(settings.api_key, settings.api_secret),
	)

	if resp.get("status") != "active":
		_throw()


def handle_subscription_notification(doctype, docname):
	call_hook_method("handle_subscription_notification", doctype=doctype, docname=docname)


def check_missed_settlement_webhooks():
	"""
	Cron job to check for missed settlement webhooks.
	Runs every 6 hours to fetch recent settlements and process any that weren't 
	captured by webhooks.
	"""
	import datetime
	
	frappe.logger().info("Starting missed settlement webhook check")
	
	try:
		# Get Razorpay settings
		razorpay_settings = frappe.get_doc("Razorpay Settings")
		
		# Check settlements for the last 7 days to catch any missed webhooks
		today = datetime.datetime.now()
		processed_settlements = 0
		skipped_settlements = 0
		
		for days_back in range(7):
			check_date = today - datetime.timedelta(days=days_back)
			year = check_date.year
			month = check_date.month
			day = check_date.day
			
			frappe.logger().info(
				f"Checking settlements for {year}-{month:02d}-{day:02d}"
			)
			
			# Fetch settlement transactions for this date
			settlement_items = razorpay_settings.fetch_settlement_transactions(year, month, day)
			
			if not settlement_items:
				continue
			
			# Get all unique settlement IDs from the items
			settlement_ids = set()
			for item in settlement_items:
				if item.get("settlement_id"):
					settlement_ids.add(item.get("settlement_id"))
			
			# For each settlement, check if it has been processed
			for settlement_id in settlement_ids:
				if is_settlement_already_processed(settlement_id):
					frappe.logger().debug(
						f"Settlement {settlement_id} already processed, skipping"
					)
					skipped_settlements += 1
					continue
				
				# Settlement not processed, process it now
				frappe.logger().info(
					f"Processing missed settlement {settlement_id} from {year}-{month:02d}-{day:02d}"
				)
				
				# Create a webhook data structure similar to what Razorpay sends
				webhook_data = {
					"event": "settlement.processed",
					"payload": {
						"settlement": {
							"entity": {
								"id": settlement_id,
								"status": "processed",
								"created_at": int(check_date.timestamp())
							}
						}
					}
				}
				
				# Enqueue settlement processing
				frappe.enqueue(
					method="payments.webhook.razorpay.handle_settlement_webhook",
					queue="short",
					timeout=300,
					data=webhook_data,
				)
				
				processed_settlements += 1
				
				# Mark settlement as processed to avoid reprocessing
				mark_settlement_as_processed(settlement_id)
		
		frappe.logger().info(
			f"Missed settlement check complete: "
			f"{processed_settlements} settlements queued for processing, "
			f"{skipped_settlements} already processed"
		)
		
	except Exception as e:
		frappe.log_error(
			frappe.get_traceback(), 
			"Missed Settlement Webhook Check Error"
		)


def is_settlement_already_processed(settlement_id):
	"""
	Check if a settlement has already been processed by looking for the 
	settlement_id in Integration Request output events.
	"""
	# Check if any Integration Request has this settlement_id in its events
	integration_requests = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": "Razorpay",
			"output": ("like", f'%"settlement_id": "{settlement_id}"%'),
		},
		limit=1
	)
	
	if integration_requests:
		return True
	
	# Also check in a custom tracking doctype if you have one
	# For now, we'll use a simple cache-based approach
	cache_key = f"razorpay_settlement_processed_{settlement_id}"
	if frappe.cache().get(cache_key):
		return True
	
	return False


def mark_settlement_as_processed(settlement_id):
	"""
	Mark a settlement as processed to prevent duplicate processing.
	Uses cache with 30-day expiration.
	"""
	cache_key = f"razorpay_settlement_processed_{settlement_id}"
	# Cache for 30 days (in seconds)
	frappe.cache().setex(cache_key, 30 * 24 * 60 * 60, "1")


@frappe.whitelist()
def process_settlements_for_date(date):
	"""
	Manually trigger settlement processing for a specific date.
	Useful for testing or reprocessing missed settlements.
	
	Args:
		date (str): Date in YYYY-MM-DD format
		
	Returns:
		str: Status message
	"""
	import datetime
	
	try:
		# Parse the date
		check_date = datetime.datetime.strptime(date, "%Y-%m-%d")
		year = check_date.year
		month = check_date.month
		day = check_date.day
		
		frappe.logger().info(
			f"Manual settlement check triggered for {year}-{month:02d}-{day:02d}"
		)
		
		# Get Razorpay settings
		razorpay_settings = frappe.get_doc("Razorpay Settings")
		
		# Fetch settlement transactions for this date
		settlement_items = razorpay_settings.fetch_settlement_transactions(year, month, day)
		
		if not settlement_items:
			return f"No settlement transactions found for {date}"
		
		# Get all unique settlement IDs from the items
		settlement_ids = set()
		for item in settlement_items:
			if item.get("settlement_id"):
				settlement_ids.add(item.get("settlement_id"))
		
		if not settlement_ids:
			return f"No settlement IDs found in transactions for {date}"
		
		# Process each settlement
		processed_count = 0
		skipped_count = 0
		
		for settlement_id in settlement_ids:
			if is_settlement_already_processed(settlement_id):
				frappe.logger().info(
					f"Settlement {settlement_id} already processed, skipping"
				)
				skipped_count += 1
				continue
			
			# Create webhook data structure
			webhook_data = {
				"event": "settlement.processed",
				"payload": {
					"settlement": {
						"entity": {
							"id": settlement_id,
							"status": "processed",
							"created_at": int(check_date.timestamp())
						}
					}
				}
			}
			
			# Enqueue settlement processing
			frappe.enqueue(
				method="payments.webhook.razorpay.handle_settlement_webhook",
				queue="short",
				timeout=300,
				data=webhook_data,
			)
			
			processed_count += 1
			mark_settlement_as_processed(settlement_id)
		
		return (
			f"Found {len(settlement_items)} transaction(s) in {len(settlement_ids)} settlement(s) for {date}. "
			f"Queued {processed_count} settlement(s) for processing, {skipped_count} already processed."
		)
		
	except Exception as e:
		frappe.log_error(
			frappe.get_traceback(), 
			f"Manual Settlement Check Error for {date}"
		)
		return f"Error processing settlements: {str(e)}"


@frappe.whitelist()
def poll_ondemand_settlement_statuses():
	"""Poll initiated on-demand settlements and finalize processed records."""
	try:
		from payments.webhook.razorpay import finalize_integration_request_settlement

		razorpay_settings = frappe.get_doc("Razorpay Settings")
		pending_ir_names = _get_pending_ondemand_ir_names()

		_base_filters = {
			"integration_request_service": "Razorpay",
			"status": "Completed",
			"output": ["like", '%"ondemand_settlement_trigger"%'],
		}

		if pending_ir_names:
			integration_requests = frappe.get_all(
				"Integration Request",
				filters={**_base_filters, "name": ["in", pending_ir_names[:200]]},
				or_filters=None,
				fields=["name", "data", "output", "reference_doctype", "reference_docname"],
			)
			# Post-filter in Python since Frappe filters don't support NOT LIKE on the same field twice.
			integration_requests = [
				ir for ir in integration_requests
				if '"settlement_processed"' not in (ir.output or "")
			]
		else:
			# Recovery mode for cache misses: bounded scan of recently modified rows only.
			integration_requests = frappe.get_all(
				"Integration Request",
				filters={
					**_base_filters,
					"modified": [">=", add_to_date(now_datetime(), days=-2)],
				},
				fields=["name", "data", "output", "reference_doctype", "reference_docname"],
				limit=100,
			)
			integration_requests = [
				ir for ir in integration_requests
				if '"settlement_processed"' not in (ir.output or "")
			]

		for ir in integration_requests:
			try:
				output = json.loads(ir.output or "{}")
				trigger = output.get("ondemand_settlement_trigger") or {}
				settlement_id = trigger.get("id")
				if not settlement_id:
					_remove_pending_ondemand_ir(ir.name)
					continue

				resp = razorpay_settings.fetch_ondemand_settlement(settlement_id, json.loads(ir.data or "{}"))
				trigger["status"] = resp.get("status")
				trigger["amount_requested"] = resp.get("amount_requested")
				trigger["amount_settled"] = resp.get("amount_settled")
				trigger["amount_pending"] = resp.get("amount_pending")
				trigger["fees"] = resp.get("fees")
				trigger["tax"] = resp.get("tax")
				trigger["currency"] = resp.get("currency")
				trigger["created_at"] = resp.get("created_at")
				trigger["last_polled_at"] = frappe.utils.now()
				output["ondemand_settlement_trigger"] = trigger

				ir_doc = frappe.get_doc("Integration Request", ir.name)
				ir_doc.db_set("output", get_json(output))

				if resp.get("status") == "processed":
					payout_item = (resp.get("ondemand_payouts") or {}).get("items") or []
					first_payout = payout_item[0] if payout_item else {}
					settlement_data = {
						"settlement_id": settlement_id,
						"payment_id": output.get("payment_id"),
						"utr": first_payout.get("utr") or resp.get("utr"),
						"settled": True,
						"amount": resp.get("amount_settled") or resp.get("amount_requested"),
						"currency": resp.get("currency"),
						"settled_at": first_payout.get("processed_at") or resp.get("created_at"),
						"settlement_data": resp,
					}
					finalize_integration_request_settlement(ir_doc, settlement_data, source="poller")
					_remove_pending_ondemand_ir(ir.name)
				elif resp.get("status") in ("failed", "cancelled", "reversed"):
					_remove_pending_ondemand_ir(ir.name)

			except Exception:
				frappe.log_error(frappe.get_traceback(), f"Failed polling on-demand settlement for Integration Request {ir.name}")

	except Exception:
		frappe.log_error(frappe.get_traceback(), "On-demand settlement polling job failed")


def _get_pending_ondemand_ir_names():
	raw = frappe.cache().get(PENDING_ONDEMAND_IR_CACHE_KEY)
	if not raw:
		return []

	if isinstance(raw, bytes):
		raw = raw.decode()

	try:
		names = json.loads(raw)
	except Exception:
		return []

	if not isinstance(names, list):
		return []

	return [name for name in names if isinstance(name, str) and name]


def _set_pending_ondemand_ir_names(names):
	unique_names = sorted(set(name for name in names if name))
	frappe.cache().setex(
		PENDING_ONDEMAND_IR_CACHE_KEY,
		PENDING_ONDEMAND_IR_CACHE_TTL,
		json.dumps(unique_names),
	)


def _add_pending_ondemand_ir(integration_request_name):
	names = _get_pending_ondemand_ir_names()
	if integration_request_name not in names:
		names.append(integration_request_name)
		_set_pending_ondemand_ir_names(names)


def _remove_pending_ondemand_ir(integration_request_name):
	names = _get_pending_ondemand_ir_names()
	if integration_request_name in names:
		names.remove(integration_request_name)
		_set_pending_ondemand_ir_names(names)


def retry_pending_instant_settlements():
	"""Cron (every 10 min): re-attempt instant settlement triggers that failed on
	a previous try — most commonly because the captured amount wasn't yet in
	Razorpay's available balance. process_instant_settlement_on_payment itself
	is idempotent and caps retries at MAX_ONDEMAND_SETTLEMENT_ATTEMPTS, so this
	just needs to find candidate payment Integration Requests and call it again.
	"""
	try:
		razorpay_settings = frappe.get_doc("Razorpay Settings")
		if not cint(getattr(razorpay_settings, "enable_instant_settlement", 0)):
			return

		candidates = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "Razorpay",
				"status": "Completed",
				"url": ["!=", SETTLEMENT_ONDEMAND_URL],
				"data": ["like", "%razorpay_payment_id%"],
				"modified": [">=", add_to_date(now_datetime(), hours=-24)],
			},
			fields=["name", "data", "output"],
			limit=200,
		)

		for ir in candidates:
			try:
				output = json.loads(ir.output or "{}")
				if output.get("ondemand_settlement_ir") or output.get("ondemand_settlement_skipped"):
					continue
				if cint(output.get("ondemand_settlement_attempts")) >= MAX_ONDEMAND_SETTLEMENT_ATTEMPTS:
					continue

				data = json.loads(ir.data or "{}")
				razorpay_settings.integration_request = frappe.get_doc("Integration Request", ir.name)
				payment_response = {"id": data.get("razorpay_payment_id"), "amount": data.get("amount")}
				razorpay_settings.process_instant_settlement_on_payment(payment_response, data)
			except Exception:
				frappe.log_error(frappe.get_traceback(), f"Failed retrying instant settlement for {ir.name}")

	except Exception:
		frappe.log_error(frappe.get_traceback(), "Instant settlement retry job failed")
