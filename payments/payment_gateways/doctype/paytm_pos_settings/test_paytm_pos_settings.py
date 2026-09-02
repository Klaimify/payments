# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

import re
import unittest

from payments.payment_gateways.doctype.paytm_pos_settings.paytm_pos_utils import (
	checksum_body as _checksum_body,
)
from payments.payment_gateways.doctype.paytm_pos_settings.paytm_pos_utils import (
	generate_merchant_txn_id as _generate_merchant_txn_id,
)
from payments.payment_gateways.doctype.paytm_pos_settings.paytm_pos_utils import (
	normalize_sale_body as _normalize_sale_body,
)
from payments.payment_gateways.doctype.paytm_pos_settings.paytm_pos_utils import (
	refund_result_status,
	result_status,
)


def _sale(result_status_value=None, result_code=None, **body):
	info = {}
	if result_status_value is not None:
		info["resultStatus"] = result_status_value
	if result_code is not None:
		info["resultCode"] = result_code
	body["resultInfo"] = info
	return {"body": body}


class TestPaytmPosHelpers(unittest.TestCase):
	def test_checksum_body_stringifies_and_drops_nested(self):
		out = _checksum_body(
			{
				"paytmMid": "M123",
				"transactionAmount": 100,
				"merchantExtendedInfo": {"paymentMode": "ALL"},
				"tags": ["a", "b"],
			}
		)
		self.assertEqual(out, {"paytmMid": "M123", "transactionAmount": "100"})
		self.assertNotIn("merchantExtendedInfo", out)
		self.assertNotIn("tags", out)

	def test_merchant_txn_id_shape(self):
		txn = _generate_merchant_txn_id("EVENT-BOOKING/2026/0001")
		self.assertTrue(8 <= len(txn) <= 32)
		self.assertTrue(re.fullmatch(r"[A-Za-z0-9]+", txn))

	def test_merchant_txn_id_unique_within_same_second(self):
		a = _generate_merchant_txn_id("BK-1")
		b = _generate_merchant_txn_id("BK-1")
		self.assertNotEqual(a, b)

	def test_merchant_txn_id_min_length_padded(self):
		self.assertGreaterEqual(len(_generate_merchant_txn_id("")), 8)

	def test_result_status_success(self):
		self.assertEqual(result_status(_sale("ACCEPTED_SUCCESS", "0009")), "success")
		self.assertEqual(result_status(_sale("S", "0000")), "success")

	def test_result_status_failed(self):
		self.assertEqual(result_status(_sale("F", "0011")), "failed")
		self.assertEqual(result_status(_sale(None, "0330")), "failed")

	def test_result_status_fail_word_is_failure(self):
		# Sale / Status / Void rejections carry resultStatus "FAIL" (4 letters).
		self.assertEqual(result_status(_sale("FAIL", "0333")), "failed")
		self.assertEqual(result_status(_sale("FAIL", "0007")), "failed")
		self.assertEqual(result_status(_sale("FAIL", "0182")), "failed")

	def test_result_status_transient_server_error_is_pending(self):
		self.assertEqual(result_status(_sale("FAIL", "0012")), "pending")

	def test_result_status_expired_and_pending(self):
		self.assertEqual(result_status(_sale("FAIL", "0404")), "expired")
		self.assertEqual(result_status(_sale("PENDING", "0030")), "pending")
		self.assertEqual(result_status(_sale(None, None)), "pending")

	def test_status_context_accepted_success_is_not_final(self):
		# In a Status Enquiry, ACCEPTED_SUCCESS / 0009 is not a settled payment.
		self.assertEqual(result_status(_sale("ACCEPTED_SUCCESS", "0009"), context="status"), "pending")
		self.assertEqual(result_status(_sale("SUCCESS", "0000"), context="status"), "success")

	def test_refund_result_status(self):
		self.assertEqual(refund_result_status(_sale("PENDING", "501")), "pending")
		self.assertEqual(refund_result_status(_sale("PENDING", "601")), "pending")
		self.assertEqual(refund_result_status(_sale("PENDING", "677")), "pending")
		self.assertEqual(refund_result_status(_sale("TXN_FAILURE", "628")), "pending")
		self.assertEqual(refund_result_status(_sale("TXN_FAILURE", "629")), "success")
		self.assertEqual(refund_result_status(_sale("TXN_SUCCESS", "10")), "success")
		self.assertEqual(refund_result_status(_sale("TXN_FAILURE", "330")), "failed")
		self.assertEqual(refund_result_status(_sale("TXN_FAILURE", "602")), "failed")

	def test_normalize_sale_body(self):
		norm = _normalize_sale_body(
			_sale(
				"SUCCESS",
				"0000",
				payMethod="UPI",
				acquirementId="ACQ123",
				acquiringBank="RBL Bank",
				transactionAmount="100",
				transactionDateTime="2026-08-23 19:34:49",
			)
		)
		self.assertEqual(norm["payment_method"], "UPI")
		self.assertEqual(norm["acquirement_id"], "ACQ123")
		self.assertEqual(norm["amount"], "100")
		self.assertEqual(norm["transaction_datetime"], "2026-08-23 19:34:49")
